"""專用空間的專案登記（ADR-0332）.

- 一資料夾一專案：比對結構層解析出的資料夾名稱（不比字串）；所有身分都套用（含管理員）；
  更新同一個專案不算重複。
- 順序寫死：權限 → 根目錄檢查 → 結構層 → 寫法 → 資料夾存在 → 重複 → 清 HOME → 寫入。被拒的登記
  不會清到任何 HOME；清不掉就不寫入（暫時原因）。整段一把鎖：同時兩個登記同一個資料夾只成一個。
- 資料夾第一次被這個專案綁定（新列、或改指到另一個資料夾）才清；清除期間其他存檔不被資料庫鎖住。
- 刪除（專案頁、刪使用者、自己清資料）之後盡力清，失敗只記 log、不擋刪除。
- 專用空間外的專案完全照舊（不清、不查重複）。
全部在測試沙盒：工作區、錨點在 tmp_path，清除換成記錄用的替身。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.routers import ui_sessions
from src.routers.ui_sessions import ProjectModel
from src.services.isolation import project_binding, project_space
from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.home_wipe import HomeWipeError
from src.services.resource_manager import resource_manager
from src.utils.async_db import get_async_db, get_project_root_path, init_db_async

USER = AgentIdentity.for_role(agent_id="bindUser", role="Base", is_human=True, capabilities=[])
OTHER = AgentIdentity.for_role(agent_id="bindOther", role="Base", is_human=True, capabilities=[])
ADMIN = AgentIdentity.for_role(agent_id="bindAdmin", role="admin", is_human=True, capabilities=[])
_LOCALES = Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "locales"


class _Wipes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.fail: str | None = None
        self.during: object = None

    async def __call__(self, name: str, project: Path) -> None:
        self.calls.append((name, project))
        await asyncio.sleep(0)
        if callable(self.during):
            await self.during()
        if self.fail:
            raise HomeWipeError(self.fail)


class _Space:
    def __init__(self, workspace: Path, anchor: Path, wipes: _Wipes) -> None:
        self.workspace, self.anchor, self.wipes = workspace, anchor, wipes

    def folder(self, name: str) -> str:
        (self.anchor / name).mkdir(exist_ok=True)
        return str(self.anchor / name)


@pytest.fixture
async def space(tmp_path, monkeypatch) -> _Space:
    await init_db_async()
    workspace = (tmp_path / "workspace").resolve()
    anchor = workspace / PROJECT_ROOT_NAME
    anchor.mkdir(parents=True)

    async def _workspace_root():
        return workspace

    monkeypatch.setattr(resource_manager, "_get_workspace_root", _workspace_root)
    monkeypatch.setattr(resource_manager, "_is_docker", False)
    monkeypatch.setattr(project_binding, "configured_projects_root", lambda: anchor)
    monkeypatch.setattr(project_binding, "binding_lock", asyncio.Lock())
    wipes = _Wipes()
    monkeypatch.setattr(project_binding, "wipe_project_home", wipes)

    async def _no_agents(_project_id):
        return []

    monkeypatch.setattr(ui_sessions.session_service, "get_project_agent_ids", _no_agents)
    yield _Space(workspace, anchor, wipes)
    async with get_async_db() as db:
        await db.execute("DELETE FROM projects WHERE id LIKE 'bind-%'")
        await db.commit()


async def _save(project_id: str, root: str, identity: AgentIdentity = USER) -> dict:
    model = ProjectModel(id=project_id, name=project_id, root_path=root, description="")
    return await ui_sessions.save_project(model, identity=identity)


async def _refused(project_id: str, root: str, identity: AgentIdentity = USER) -> ToukaException:
    with pytest.raises(ToukaException) as raised:
        await _save(project_id, root, identity)
    return raised.value


def _code(exc: ToukaException) -> str:
    return exc.detail["code"]


# --- 綁定與清除 ---------------------------------------------------------------------------


async def test_a_new_binding_wipes_the_folder_home_before_saving(space):
    root = space.folder("repo")
    await _save("bind-a", root)
    assert space.wipes.calls == [("repo", space.anchor / "repo")]
    assert await get_project_root_path("bind-a") == root


async def test_saving_the_same_project_again_does_not_wipe(space):
    root = space.folder("repo")
    await _save("bind-a", root)
    await _save("bind-a", root + "/")
    assert len(space.wipes.calls) == 1


async def test_pointing_a_project_at_another_folder_wipes_that_folder(space):
    await _save("bind-a", space.folder("repo"))
    await _save("bind-a", space.folder("other"))
    assert [name for name, _path in space.wipes.calls] == ["repo", "other"]


async def test_projects_outside_the_space_are_unchanged(space):
    outside = space.workspace / "code"
    outside.mkdir()
    await _save("bind-out", str(outside))
    await _save("bind-out2", str(outside))  # 空間外不查重複（照舊）
    assert space.wipes.calls == []


# --- 一資料夾一專案 -------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["{root}", "{root}/", "{anchor}/./repo"])
@pytest.mark.parametrize("second", [USER, OTHER, ADMIN])
async def test_one_folder_is_one_project_for_everyone(space, spelling, second):
    root = space.folder("repo")
    await _save("bind-a", root)
    again = spelling.format(root=root, anchor=space.anchor)
    exc = await _refused("bind-b", again, second)
    assert _code(exc) == ErrorCode.PROJECT_FOLDER_TAKEN.value
    assert len(space.wipes.calls) == 1  # 被拒的那次沒有清到別人的 HOME
    assert await get_project_root_path("bind-b") is None


async def test_two_saves_of_one_folder_at_once_bind_it_once(space):
    root = space.folder("repo")
    results = await asyncio.gather(
        _save("bind-a", root), _save("bind-b", root), return_exceptions=True
    )
    errors = [r for r in results if isinstance(r, ToukaException)]
    assert len(errors) == 1 and _code(errors[0]) == ErrorCode.PROJECT_FOLDER_TAKEN.value
    assert len(space.wipes.calls) == 1


# --- 被拒的登記 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "reason"),
    [(".", "project_space_root"), ("repo/src", "project_not_top_level"),
     ("repo/../repo", "project_linked")],
)
async def test_refused_shapes_are_not_saved_or_wiped(space, relative, reason):
    (space.anchor / "repo" / "src").mkdir(parents=True)
    exc = await _refused("bind-x", f"{space.anchor}/{relative}", ADMIN)
    assert _code(exc) == ErrorCode.VALIDATION_ERROR.value and exc.detail["reason"] == reason
    assert space.wipes.calls == []


async def test_a_missing_folder_is_refused_without_wiping(space):
    exc = await _refused("bind-x", str(space.anchor / "not-yet"))
    assert _code(exc) == ErrorCode.PROJECT_FOLDER_MISSING.value
    assert space.wipes.calls == []


async def test_a_space_path_outside_the_workspace_form_is_refused_with_the_rule(
    space, monkeypatch, tmp_path
):
    """管理員不經根目錄檢查：在錨點底下但工作區檢查不過（寫法不對）也要拒絕並說明."""
    anchor = (tmp_path / "mounted" / PROJECT_ROOT_NAME).resolve()
    (anchor / "repo").mkdir(parents=True)
    monkeypatch.setattr(project_binding, "configured_projects_root", lambda: anchor)
    exc = await _refused("bind-x", str(anchor / "repo"), ADMIN)
    assert exc.detail["reason"] == project_binding.PROJECT_PATH_FORM
    assert space.wipes.calls == []


async def test_a_failed_wipe_refuses_the_save_with_a_temporary_reason(space):
    space.wipes.fail = "executor_unreachable"
    exc = await _refused("bind-a", space.folder("repo"))
    assert _code(exc) == ErrorCode.PROJECT_HOME_NOT_CLEARED.value
    assert exc.detail["reason"] == "executor_unreachable"
    assert await get_project_root_path("bind-a") is None


async def test_other_saves_are_not_locked_out_of_the_database_while_wiping(space):
    other = space.workspace / "code"
    other.mkdir()

    async def _write_elsewhere() -> None:
        async with get_async_db() as db:
            await db.execute(
                "INSERT INTO projects (id, name, root_path, updatedAt) VALUES (?, ?, ?, ?)",
                ("bind-side", "side", str(other), 1),
            )
            await db.commit()

    space.wipes.during = _write_elsewhere
    await _save("bind-a", space.folder("repo"))
    assert await get_project_root_path("bind-side") == str(other)


# --- 刪除之後盡力清 -------------------------------------------------------------------------


@pytest.mark.parametrize("fail", [None, "executor_unreachable"])
async def test_deleting_a_project_wipes_its_home_best_effort(space, fail, caplog):
    await _save("bind-a", space.folder("repo"))
    space.wipes.fail = fail
    with caplog.at_level(logging.WARNING):
        result = await ui_sessions.delete_project("bind-a", identity=USER)
    assert result["status"] == "success" and await get_project_root_path("bind-a") is None
    assert [name for name, _path in space.wipes.calls] == ["repo", "repo"]
    assert bool(fail and fail in caplog.text) is bool(fail)


async def test_clearing_owned_projects_wipes_the_space_ones(space):
    await _save("bind-a", space.folder("repo"))
    outside = space.workspace / "code"
    outside.mkdir()
    await _save("bind-out", str(outside))
    space.wipes.fail = "timeout"
    assert await project_binding.clear_owned_projects(USER.agent_id) == 2
    assert [name for name, _path in space.wipes.calls] == ["repo", "repo"]
    assert await get_project_root_path("bind-a") is None


# --- 畫面說得出原因 -------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_every_refusal_reason_has_wording_on_the_projects_page(language):
    """後端的每個被拒原因在專案頁都有說明（沒有就只剩一般的「存不了」）."""
    reasons = {
        project_space.PROJECT_SPACE_ROOT,
        project_space.PROJECT_NOT_TOP_LEVEL,
        project_space.PROJECT_NAME_UNSAFE,
        project_space.PROJECT_LINKED,
        project_space.PROJECT_SPACE_UNAVAILABLE,
        project_binding.PROJECT_PATH_FORM,
    }
    catalog = json.loads((_LOCALES / language / "translation.json").read_text(encoding="utf-8"))
    assert reasons <= set(catalog["projects"]["projects_view"]["space_reasons"])

"""專用空間的專案登記（ADR-0332）.

- 一資料夾一專案：比對結構層解析出的資料夾名稱（不比字串，不分大小寫）；所有身分都套用
  （含管理員）；更新同一個專案不算重複。只有名稱完全相同才算自己的（HOME 依實際名稱分）。
- 順序寫死：權限 → 根目錄檢查 → 結構層 → 寫法 → 資料夾存在 → 重複 → 清 HOME → 寫入。被拒的登記
  不會清到任何 HOME；清不掉就不寫入。整段一把鎖：同時兩個登記同一個資料夾只成一個；拿到鎖後
  再查一次權限（等鎖時同一個編號可能已被別人建好）。
- 資料夾第一次被這個專案綁定（新列、或改指到另一個資料夾）才清；清除期間其他存檔不被資料庫鎖住。
- 刪除（專案頁、刪使用者、自己清資料）之後盡力清，失敗只記 log、不擋刪除；拿到鎖時別人已經
  登記了同一個資料夾就不清。
- 別處的專案根解析不了（連結迴圈）不會讓登記出錯。
- 專用空間外的專案完全照舊（不清、不查重複）。
全部在測試沙盒：工作區、錨點在 tmp_path，清除換成記錄用的替身。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.routers import ui_sessions
from src.routers.ui_sessions import ProjectModel
from src.services.isolation import project_binding, project_space
from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.home_wipe import HomeWipeError
from src.services.isolation.status import (
    EXECUTOR_NO_PROJECT_ROOT,
    EXECUTOR_PROJECT_ROOT_MISMATCH,
)
from src.services.resource_manager import resource_manager
from src.utils.async_db import get_async_db, get_project_root_path, init_db_async
from src.utils.touka_dirs import PROTECTED_LOCATION, SCAN_LIMIT

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


async def _insert(project_id: str, root: str, owner: str) -> None:
    """別人已經寫好的一列（不經存檔端點）."""
    async with get_async_db() as db:
        await db.execute(
            "INSERT INTO projects (id, name, root_path, owner_user_id, updatedAt) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, project_id, root, owner, 1),
        )
        await db.commit()


class _WatchedLock(asyncio.Lock):
    """記下有沒有人在等鎖：測試要在對方等鎖的時候動手."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


def _loop(path: Path) -> None:
    """指向自己的連結（解析會遇到迴圈）；這台機器不能建連結就跳過（Linux CI 一定跑）."""
    try:
        os.symlink(path.name, path)
    except OSError as exc:
        pytest.skip(f"symlinks are not available here: {exc}")


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


async def test_another_spelling_of_the_name_is_another_folder_and_is_wiped(space):
    """只差大小寫的是另一個資料夾（HOME 依實際名稱分）：改指過去要清，只比大小寫會漏清."""
    await _save("bind-a", space.folder("repo"))
    await _save("bind-a", space.folder("Repo"))  # Windows 上是同一個資料夾，名稱照樣不同
    assert [name for name, _path in space.wipes.calls] == ["repo", "Repo"]
    exc = await _refused("bind-b", space.folder("REPO"), OTHER)
    assert _code(exc) == ErrorCode.PROJECT_FOLDER_TAKEN.value  # 判重複仍不分大小寫


async def test_a_project_created_while_waiting_for_the_lock_is_not_taken_over(
    space, monkeypatch
):
    """等鎖時（別人清 HOME 最長 60 秒）同一個編號被別人建好：拿到鎖再查權限，不改別人的專案."""
    lock = _WatchedLock()
    monkeypatch.setattr(project_binding, "binding_lock", lock)
    mine, bait = space.folder("repo"), space.folder("bait")
    async with lock:
        taking = asyncio.create_task(_save("bind-a", bait, OTHER))
        await asyncio.wait_for(lock.waiting.wait(), 5)
        await _insert("bind-a", mine, USER.agent_id)
    with pytest.raises(ToukaException) as raised:
        await taking
    assert _code(raised.value) == ErrorCode.AUTH_PERMISSION_DENIED.value
    assert await get_project_root_path("bind-a") == mine
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


async def test_a_folder_bound_again_while_the_delete_waits_is_not_wiped(space, monkeypatch):
    """刪除後要清 HOME，但等鎖時別人已經登記了同一個資料夾：那是新擁有者的 HOME，不清."""
    root = space.folder("repo")
    await _save("bind-a", root)
    lock = _WatchedLock()
    monkeypatch.setattr(project_binding, "binding_lock", lock)
    async with lock:
        deleting = asyncio.create_task(ui_sessions.delete_project("bind-a", identity=USER))
        await asyncio.wait_for(lock.waiting.wait(), 5)
        await _insert("bind-b", root, OTHER.agent_id)
    assert (await deleting)["status"] == "success"
    assert [name for name, _path in space.wipes.calls] == ["repo"]  # 只有第一次登記清過


async def test_a_failing_check_after_the_delete_is_only_logged(space, monkeypatch, caplog):
    """刪除已經完成：清除前讀登記出錯也只記 log，不回 500（自己清資料接著還要清別的）."""
    await _save("bind-a", space.folder("repo"))
    monkeypatch.setattr(project_binding, "_claims", AsyncMock(side_effect=OSError))
    with caplog.at_level(logging.WARNING):
        result = await ui_sessions.delete_project("bind-a", identity=USER)
    assert result["status"] == "success" and await get_project_root_path("bind-a") is None
    assert "not cleared" in caplog.text
    assert len(space.wipes.calls) == 1  # 只有登記時那一次


async def test_clearing_owned_projects_wipes_the_space_ones(space):
    await _save("bind-a", space.folder("repo"))
    outside = space.workspace / "code"
    outside.mkdir()
    await _save("bind-out", str(outside))
    space.wipes.fail = "timeout"
    assert await project_binding.clear_owned_projects(USER.agent_id) == 2
    assert [name for name, _path in space.wipes.calls] == ["repo", "repo"]
    assert await get_project_root_path("bind-a") is None


# --- 解析不了的根 ---------------------------------------------------------------------------


def test_a_root_that_cannot_be_resolved_is_linked_inside_and_outside_elsewhere(space):
    """連結迴圈（3.12 的 resolve 會丟錯）：在錨點底下算路上有連結，別處照舊不算."""
    inside, elsewhere = space.anchor / "loop", space.workspace / "loop"
    _loop(inside)
    _loop(elsewhere)
    scope = project_space.structural_scope(inside, space.anchor)
    assert (scope.kind, scope.reason) == ("refused", project_space.PROJECT_LINKED)
    assert project_space.structural_scope(elsewhere, space.anchor).kind == "outside"
    assert project_space.structural_scope(inside, None).kind == "refused"  # 名稱照樣擋


@pytest.mark.parametrize("error", [RuntimeError, OSError])
def test_a_root_whose_resolve_fails_is_linked_inside_and_outside_elsewhere(
    space, monkeypatch, error
):
    """解析時丟錯（3.12 遇到連結迴圈丟 RuntimeError；這台不能建連結也驗得到）：同上，不往外丟."""
    real = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        if self.name == "loop":
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    scope = project_space.structural_scope(space.anchor / "loop", space.anchor)
    assert (scope.kind, scope.reason) == ("refused", project_space.PROJECT_LINKED)
    assert project_space.structural_scope(space.workspace / "loop", space.anchor).kind == "outside"


async def test_a_looping_project_root_elsewhere_does_not_break_registration(space):
    """別人的一般專案根是連結迴圈：查重複時逐列判定不出錯，其他人照樣登記得了."""
    loop = space.workspace / "x" / "l"
    loop.parent.mkdir()
    _loop(loop)
    await _insert("bind-loop", str(loop), OTHER.agent_id)
    root = space.folder("repo")
    await _save("bind-a", root)
    assert await get_project_root_path("bind-a") == root


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


@pytest.mark.parametrize("language", ["zh-TW", "en"])
def test_reasons_a_wipe_will_not_outgrow_have_wording_on_the_projects_page(language):
    """清 HOME 前被擋、等再久也不會好的原因（掃描、執行器設定）有自己的說明，不說「稍後再試」."""
    reasons = {
        PROTECTED_LOCATION, SCAN_LIMIT, EXECUTOR_NO_PROJECT_ROOT, EXECUTOR_PROJECT_ROOT_MISMATCH
    }
    catalog = json.loads((_LOCALES / language / "translation.json").read_text(encoding="utf-8"))
    assert reasons <= set(catalog["projects"]["projects_view"]["home_reasons"])

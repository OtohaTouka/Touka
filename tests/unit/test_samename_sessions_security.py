"""刪掉的代理人的舊對話不給同名的新代理人（ADR-0326 PR 3a；禁令 20 對應安全測試）.

修補前：刪除自訂模型不碰對話，對話一直綁著名字；刪除靈魂前的重置雖然會刪掉它的對話，舊版本、
重置失敗留下的殘留也一直綁著名字。之後有人建同名的靈魂或自訂模型：

- 新代理人的擁有者經 ``is_owner`` 在清單、讀取、刪除、GDPR 匯出看到別人的舊對話，也能讀、改、刪
  沒有擁有者的舊專案；
- 同名新靈魂接手舊的家、舊的心跳與 SNS 頻道（頻道編號是算出來的，新靈魂算出同一個），
  以代理人身分也讀得到舊對話；
- 舊頻道失效的回報位置永遠不能改回來，新靈魂的心跳頻道從此不回報給家。

修補後（``session_service.release_agent_sessions``）：靈魂自己的頻道刪掉；其他對話解除綁定
（``agent_id`` 清空、``former_agent_id`` 記原名字、家的標記清掉），只剩 ``owner_user_id`` 那位
人類與管理員看得到；回報位置刪掉、換家紀錄與前情設失效。刪除靈魂（鎖內、取消註冊之後）、
刪除自訂模型、建立靈魂（兩條路）、建立與改名自訂模型時都做；名字還屬於某個代理人時不做。

conftest 沙盒、真的靈魂與自訂模型路由、真的對話資料庫；殘留直接寫進資料庫模擬舊版本留下的。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import mission_board_path
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult, SessionChanges, SessionDocument
from src.routers import souls as souls_router
from src.routers import ui_sessions
from src.services import agent_leftovers, destructive_ops, gdpr_export_service
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils import async_db, home_transitions
from src.utils.session_scope import channel_session_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

MODEL = "RelModel"  # 自訂模型（現行版本最主要的來源）
SOUL = "RelSoul"  # 靈魂（殘留）
TARGET = "RelTarget"  # 人類接著聊時自己選的靈魂
RENAMED = "RelRenamed"
NAMES = [MODEL, SOUL, TARGET, RENAMED, MODEL.lower()]
HEARTBEAT = channel_session_id("heartbeat", SOUL)
SNS = channel_session_id("sns", SOUL, "telegram", "rel-chat")
PROJECT = "rel-project"
TREE = {
    "messages": {
        "u": {"id": "u", "role": "user", "content": "private old words", "parentId": None},
        "a": {"id": "a", "role": "assistant", "content": "old answer", "parentId": "u"},
    },
    "currentId": "a",
}
EMPTY = {"messages": {}, "currentId": None}
_MODEL_BODY = {"name": "x", "description": "", "systemPrompt": "PROMPT", "config": {}}
_CREATED: list[str] = []  # 本檔寫進資料庫的對話編號（結束時刪）


# --------------------------------------------------------------------------- #
# 共用
# --------------------------------------------------------------------------- #


@pytest.fixture
async def sandbox() -> AsyncIterator[Path]:
    """靈魂與自訂模型路由要 conftest 沙盒的對話資料庫；本檔用到的名字與列在結束時清掉."""
    data = Path(resource_manager.get_base_path())
    await async_db.init_db_async()
    yield data
    ids = [*_CREATED, HEARTBEAT, SNS]
    _CREATED.clear()
    async with async_db.get_async_db() as db:
        for sid in ids:
            await db.execute("DELETE FROM user_sessions WHERE id = ?", (sid,))
        await db.execute("DELETE FROM projects WHERE id = ?", (PROJECT,))
        for name in NAMES:
            await db.execute("DELETE FROM soul_child_report_marks WHERE agent_id = ?", (name,))
            await db.execute("DELETE FROM soul_home_transitions WHERE agent_id = ?", (name,))
            await db.execute("DELETE FROM soul_prelude_segments WHERE agent_id = ?", (name,))
        await db.commit()
    for agent_id in NAMES:
        mission_board_path(agent_id).unlink(missing_ok=True)
        for base in ("soul", "personal_drive", "legacy", "stats", "custom"):
            shutil.rmtree(data / base / agent_id, ignore_errors=True)


@pytest.fixture(autouse=True)
def registry(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """models.json、agents_registry.json 從空的開始；沒有心跳位址（建立時的排程那步不連網路）."""
    _ = sandbox
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_EXTERNAL_URL", None)
    config = Path(get_config_dir())
    files = [config / "models.json", config / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()
    yield config / "agents_registry.json"
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")
    model_manager.load_initial_config()


@pytest.fixture
def people() -> Iterator[SimpleNamespace]:
    """真的帳號：管理員（刪除靈魂要重新輸入密碼）、舊對話的人類、同名新代理人的擁有者."""
    pw = "RelTakeoverPass123!"
    auth = AuthService()
    users = auth.users_file_path
    before = users.read_bytes() if users.exists() else None
    admin = auth.create_user("rel-admin@test.com", pw, role="admin")
    first = auth.create_user("rel-first@test.com", pw)
    second = auth.create_user("rel-second@test.com", pw)
    yield SimpleNamespace(
        pw=pw,
        admin=AgentIdentity.for_role(admin.id, "admin", is_human=True),
        first=AgentIdentity.for_role(first.id, "normal", is_human=True),
        second=AgentIdentity.for_role(second.id, "normal", is_human=True),
    )
    if before is None:
        users.unlink(missing_ok=True)
    else:
        users.write_bytes(before)


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _create_soul(name: str, people: SimpleNamespace) -> Any:
    return _send("POST", "/v1/souls", {"agent_id": name, "config": {}}, people.admin)


def _edit_create(name: str, people: SimpleNamespace) -> Any:
    body = {"config": {"is_toukasoul": True}, "admin_password": people.pw}
    return _send("POST", f"/v1/souls/{name}", body, people.admin)


def _delete_soul(name: str, people: SimpleNamespace) -> Any:
    return _send("DELETE", f"/v1/souls/{name}", {"admin_password": people.pw}, people.admin)


def _create_model(name: str, people: SimpleNamespace) -> Any:
    return _send("POST", f"/v1/ui/custom_models/{name}", _MODEL_BODY, people.admin)


def _rename_model(old: str, new: str, people: SimpleNamespace) -> Any:
    return _send("POST", f"/v1/ui/custom_models/{old}/rename", {"new_id": new}, people.admin)


def _delete_model(name: str, people: SimpleNamespace) -> Any:
    return _send("DELETE", f"/v1/ui/custom_models/{name}", None, people.admin)


async def _insert(
    sid: str,
    agent_id: str | None,
    *,
    owner: str | None = None,
    is_home: int = 0,
    home_for: str | None = None,
    project: str | None = None,
    tree: dict[str, Any] | None = None,
    scope: int = 1,
) -> None:
    """殘留的列（舊版本留下的、重置沒刪成的、刪自訂模型留下的）直接寫進資料庫."""
    _CREATED.append(sid)
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO user_sessions (id, title, messages, updatedAt, is_home, home_for_agent, "
            "agent_id, project_id, auth_level, active_mcp_servers, active_skills, "
            "last_activity_at, context_summary, owner_user_id, session_scope_version) "
            "VALUES (?, ?, ?, 1000, ?, ?, ?, ?, 'Manual', '[]', '[]', 1000, NULL, ?, ?)",
            (
                sid,
                "old chat",
                json.dumps(TREE if tree is None else tree),
                is_home,
                home_for,
                agent_id,
                project,
                owner,
                scope,
            ),
        )
        await db.commit()


async def _row(sid: str) -> dict[str, Any] | None:
    async with async_db.get_async_db() as db:
        cursor = await db.execute("SELECT * FROM user_sessions WHERE id = ?", (sid,))
        raw = await cursor.fetchone()
    return dict(raw) if raw is not None else None


async def _mark(sid: str, agent_id: str) -> None:
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO soul_child_report_marks (session_id, agent_id, scope_version, last_node, "
            "updated_at) VALUES (?, ?, 1, 'a', 1000)",
            (sid, agent_id),
        )
        await db.commit()


def _give(name: str, who: AgentIdentity) -> None:
    UserService().add_owned_agent(who.agent_id, AgentIdentity(agent_id=name))


def _listed(who: AgentIdentity) -> set[str]:
    response = _send("GET", "/v1/ui/sessions", None, who)
    assert response.status_code == 200, response.text
    return {s["id"] for s in response.json()}


async def _no_facts(_identity: AgentIdentity) -> None:
    return None


async def _exported(who: AgentIdentity) -> dict[str, dict[str, Any]]:
    user = AuthService().get_user_by_id(who.agent_id)
    assert user is not None
    export = await gdpr_export_service._collect(user, facts_fn=_no_facts)  # noqa: SLF001
    return {c["id"]: c for c in json.loads(export.text_entries["conversations.json"])}


async def _model_with_old_chat(people: SimpleNamespace, *, delete: bool = True) -> None:
    """主情境：自訂模型 MODEL 有一段第一位人類的對話；刪掉自訂模型."""
    assert _create_model(MODEL, people).status_code == 200
    await _insert("rel-chat", MODEL, owner=people.first.agent_id)
    if delete:
        response = _delete_model(MODEL, people)
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# 解除綁定之後誰看得到
# --------------------------------------------------------------------------- #


async def test_custom_model_delete_releases_conversations(
    sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _model_with_old_chat(people, delete=False)
    before = await _row("rel-chat")
    assert before is not None
    response = _delete_model(MODEL, people)
    assert response.status_code == 200, response.text
    after = await _row("rel-chat")
    assert after is not None
    assert after["agent_id"] is None
    assert after["former_agent_id"] == MODEL
    kept = {"agent_id", "former_agent_id", "session_scope_version"}
    assert {k: v for k, v in after.items() if k not in kept} == {
        k: v for k, v in before.items() if k not in kept
    }
    assert "rel-chat" in _listed(people.first)
    single = _send("GET", "/v1/ui/sessions/rel-chat", None, people.first)
    assert single.status_code == 200, single.text
    assert single.json()["former_agent_id"] == MODEL


@pytest.mark.parametrize("kind", ["soul", "model"])
async def test_new_owner_does_not_see_old_conversations(
    kind: str, sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _model_with_old_chat(people, delete=False)
    await _insert("rel-agentless", MODEL)  # 沒有人類歸屬的列：只剩管理員
    assert _delete_model(MODEL, people).status_code == 200
    created = _create_soul(MODEL, people) if kind == "soul" else _create_model(MODEL, people)
    assert created.status_code == 200, created.text
    _give(MODEL, people.second)
    old = {"rel-chat", "rel-agentless"}
    assert not old & _listed(people.second)
    for sid in old:
        assert _send("GET", f"/v1/ui/sessions/{sid}", None, people.second).status_code == 403
        assert _send("DELETE", f"/v1/ui/sessions/{sid}", None, people.second).status_code == 403
    assert not old & set(await _exported(people.second))
    assert "rel-agentless" not in _listed(people.first)
    assert old <= _listed(people.admin)


async def test_new_owner_cannot_reach_old_projects(sandbox: Path, people: SimpleNamespace) -> None:
    """沒有擁有者的舊專案：權限從底下對話的 agent_id 推，解除綁定後新擁有者推不到."""
    _ = sandbox
    assert _create_model(MODEL, people).status_code == 200
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO projects (id, name, root_path, description, owner_user_id, updatedAt) "
            "VALUES (?, 'old project', 'unused', '', NULL, 1000)",
            (PROJECT,),
        )
        await db.commit()
    await _insert("rel-chat", MODEL, owner=people.first.agent_id, project=PROJECT)
    assert _delete_model(MODEL, people).status_code == 200
    assert _create_model(MODEL, people).status_code == 200
    _give(MODEL, people.second)
    assert not await ui_sessions._can_access_project(people.second, PROJECT)  # noqa: SLF001
    assert not await ui_sessions._can_modify_project(people.second, PROJECT)  # noqa: SLF001


async def test_original_human_export_keeps_released_rows(
    sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _model_with_old_chat(people)
    exported = await _exported(people.first)
    assert exported["rel-chat"]["former_agent_id"] == MODEL


async def test_new_soul_cannot_reach_old_conversations(
    sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _model_with_old_chat(people)
    assert _create_soul(MODEL, people).status_code == 200
    with pytest.raises(ToukaException) as caught:
        await async_db.session_service.read_execution_snapshot(
            "rel-chat", AgentIdentity(agent_id=MODEL)
        )
    assert caught.value.code == ErrorCode.SESSION_SCOPE_MISMATCH


# --------------------------------------------------------------------------- #
# 家與頻道（靈魂的殘留）
# --------------------------------------------------------------------------- #


async def test_new_soul_does_not_inherit_the_home(sandbox: Path, people: SimpleNamespace) -> None:
    _ = sandbox
    await _insert("rel-home", SOUL, is_home=1, home_for=SOUL)
    assert _create_soul(SOUL, people).status_code == 200
    assert await async_db.session_service.read_home_snapshot(AgentIdentity(agent_id=SOUL)) is None
    row = await _row("rel-home")
    assert row is not None
    assert (row["is_home"], row["home_for_agent"], row["agent_id"]) == (0, None, None)
    assert row["former_agent_id"] == SOUL


async def test_home_marker_alone_is_cleared(sandbox: Path, people: SimpleNamespace) -> None:
    """只有家的標記指著這個名字（agent_id 是空的）：也清掉，原名字取家的主人."""
    _ = sandbox
    await _insert("rel-marker", None, is_home=1, home_for=SOUL)
    assert _create_soul(SOUL, people).status_code == 200
    assert await async_db.session_service.read_home_snapshot(AgentIdentity(agent_id=SOUL)) is None
    row = await _row("rel-marker")
    assert row is not None
    assert (row["is_home"], row["home_for_agent"]) == (0, None)
    assert row["former_agent_id"] == SOUL


async def test_leftover_channels_are_deleted(sandbox: Path, people: SimpleNamespace) -> None:
    _ = sandbox
    await _insert(HEARTBEAT, SOUL)
    await _insert(SNS, SOUL)
    await _mark(HEARTBEAT, SOUL)
    assert _create_soul(SOUL, people).status_code == 200
    assert await _row(HEARTBEAT) is None
    assert await _row(SNS) is None
    opened = await async_db.session_service.open_channel_session(
        "heartbeat", AgentIdentity(agent_id=SOUL), title="Pulse"
    )
    assert opened == HEARTBEAT
    fresh = await _row(HEARTBEAT)
    assert fresh is not None
    assert json.loads(fresh["messages"])["messages"] == {}


async def test_old_channel_revision_cannot_write_new_channel(
    sandbox: Path, people: SimpleNamespace
) -> None:
    """刪舊頻道時世代沒有加一：新頻道建立時會加一、內容也不同，舊版本號照樣被擋."""
    _ = sandbox
    soul = AgentIdentity(agent_id=SOUL)
    await _insert(HEARTBEAT, SOUL)
    old = await async_db.session_service.read_persistence_snapshot(HEARTBEAT, soul)
    assert _create_soul(SOUL, people).status_code == 200
    opened = await async_db.session_service.open_channel_session("heartbeat", soul, title="Pulse")
    assert opened == HEARTBEAT
    stale = await async_db.session_service.conditional_save_session(
        HEARTBEAT, old.revision, SessionChanges(title="stale round"), soul
    )
    assert not isinstance(stale, SavedResult)
    fresh = await async_db.session_service.read_persistence_snapshot(HEARTBEAT, soul)
    saved = await async_db.session_service.conditional_save_session(
        HEARTBEAT, fresh.revision, SessionChanges(title="new round"), soul
    )
    assert isinstance(saved, SavedResult)  # 被擋的原因是舊版本號，不是頻道不准存


async def test_new_heartbeat_channel_reports_again(sandbox: Path, people: SimpleNamespace) -> None:
    """舊頻道的回報位置刪掉了：同編號的新頻道寫得進新位置（失效的位置永遠改不回來）."""
    _ = sandbox
    await _insert(HEARTBEAT, SOUL)
    await _mark(HEARTBEAT, SOUL)
    assert _create_soul(SOUL, people).status_code == 200
    opened = await async_db.session_service.open_channel_session(
        "heartbeat", AgentIdentity(agent_id=SOUL), title="Pulse"
    )
    async with async_db.get_async_db() as db:
        assert await home_transitions.set_report_mark(db, opened, SOUL, "n1", 2000)
        await db.commit()


async def test_released_old_home_is_no_longer_read_only(
    sandbox: Path, people: SimpleNamespace
) -> None:
    """解除綁定時換家紀錄一起失效：自訂模型的路不會呼叫 invalidate_agent_sessions."""
    _ = sandbox
    assert _create_model(MODEL, people).status_code == 200
    await _insert("rel-old-home", MODEL, owner=people.first.agent_id)
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO soul_home_transitions (agent_id, old_session, created_at, updated_at) "
            "VALUES (?, 'rel-old-home', 1000, 1000)",
            (MODEL,),
        )
        await db.commit()
    assert await async_db.session_service.is_old_home("rel-old-home")
    assert _delete_model(MODEL, people).status_code == 200
    assert not await async_db.session_service.is_old_home("rel-old-home")


async def test_release_forgets_reports_and_preludes(
    sandbox: Path, people: SimpleNamespace
) -> None:
    """自訂模型的路不會呼叫 invalidate_agent_sessions：回報位置要刪、前情要設失效."""
    _ = sandbox
    await _model_with_old_chat(people, delete=False)
    await _mark("rel-chat", MODEL)
    async with async_db.get_async_db() as db:
        await db.execute(
            "INSERT INTO soul_prelude_segments (agent_id, kind, source_session, dedupe_key, "
            "content, created_at) VALUES (?, 'child_report', 'rel-chat', 'k', 'c', 1000)",
            (MODEL,),
        )
        await db.commit()
    assert _delete_model(MODEL, people).status_code == 200
    async with async_db.get_async_db() as db:
        marks = await db.execute_fetchall(
            "SELECT 1 FROM soul_child_report_marks WHERE agent_id = ?", (MODEL,)
        )
        scopes = await db.execute_fetchall(
            "SELECT scope_version FROM soul_prelude_segments WHERE agent_id = ?", (MODEL,)
        )
    assert not list(marks)
    assert [row[0] for row in scopes] == [0]


async def test_release_matches_both_spellings_only(sandbox: Path, people: SimpleNamespace) -> None:
    _ = sandbox
    assert _create_model(MODEL, people).status_code == 200
    owner = people.first.agent_id
    await _insert("rel-plain", MODEL, owner=owner)
    await _insert("rel-prefixed", f"ToukaSoul{MODEL}", owner=owner)
    await _insert("rel-case", MODEL.lower(), owner=owner)
    await _insert("rel-other", TARGET, owner=owner)
    await _insert("sns_rel", MODEL)  # 舊的共享頻道：只解除綁定、不刪
    assert _delete_model(MODEL, people).status_code == 200
    rows = {sid: await _row(sid) for sid in _CREATED}
    assert rows["rel-plain"]["agent_id"] is None
    assert rows["rel-plain"]["former_agent_id"] == MODEL
    assert rows["rel-prefixed"]["agent_id"] is None
    assert rows["rel-prefixed"]["former_agent_id"] == f"ToukaSoul{MODEL}"
    assert rows["rel-case"]["agent_id"] == MODEL.lower()
    assert rows["rel-other"]["agent_id"] == TARGET
    assert rows["sns_rel"] is not None
    assert rows["sns_rel"]["agent_id"] is None


# --------------------------------------------------------------------------- #
# 解除綁定本身
# --------------------------------------------------------------------------- #


async def test_release_is_all_or_nothing(
    sandbox: Path, people: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """頻道刪了之後才失敗：整個交易不寫."""
    _ = sandbox
    await _insert(HEARTBEAT, SOUL)
    await _insert("rel-chat", SOUL, owner=people.first.agent_id)
    monkeypatch.setattr(async_db, "_RELEASE_SESSIONS_SQL", "UPDATE no_such_table SET x = 1")
    with pytest.raises(ToukaException) as caught:
        await async_db.session_service.release_agent_sessions(AgentIdentity(agent_id=SOUL))
    assert caught.value.code == ErrorCode.SESSION_SAVE_FAILED
    assert await _row(HEARTBEAT) is not None
    chat = await _row("rel-chat")
    assert chat is not None
    assert chat["agent_id"] == SOUL


async def test_release_without_database_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sessions.db"
    monkeypatch.setattr(async_db, "get_db_path", lambda: path)
    released = await async_db.session_service.release_agent_sessions(AgentIdentity(agent_id=MODEL))
    assert (released.channels, released.sessions) == (0, 0)
    assert not path.exists()
    async with aiosqlite.connect(path):
        pass  # 有檔案、沒有表
    released = await async_db.session_service.release_agent_sessions(AgentIdentity(agent_id=MODEL))
    assert (released.channels, released.sessions) == (0, 0)


async def test_stale_tab_save_is_rejected_after_release(
    sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _model_with_old_chat(people, delete=False)
    snapshot = await async_db.session_service.read_persistence_snapshot("rel-chat", people.first)
    assert _delete_model(MODEL, people).status_code == 200
    result = await async_db.session_service.conditional_save_session(
        "rel-chat", snapshot.revision, SessionChanges(title="stale tab"), people.first
    )
    assert not isinstance(result, SavedResult)
    row = await _row("rel-chat")
    assert row is not None
    assert row["title"] == "old chat"


async def test_second_save_of_a_new_session_succeeds(
    sandbox: Path, people: SimpleNamespace
) -> None:
    """新列的預設要有新欄位：第一次存檔回的版本號，和之後讀出來的整列對得上."""
    _ = sandbox
    _CREATED.append("rel-new")
    absent = await async_db.session_service.read_persistence_snapshot("rel-new", people.first)
    first = await async_db.session_service.conditional_save_session(
        "rel-new", absent.revision, SessionChanges(title="a", messages=EMPTY), people.first
    )
    assert isinstance(first, SavedResult)
    second = await async_db.session_service.conditional_save_session(
        "rel-new", first.revision, SessionChanges(title="b"), people.first
    )
    assert isinstance(second, SavedResult)


async def test_former_agent_is_public_and_read_only(sandbox: Path, people: SimpleNamespace) -> None:
    _ = sandbox
    await _model_with_old_chat(people)
    listed = _send("GET", "/v1/ui/sessions", None, people.first).json()
    assert next(s for s in listed if s["id"] == "rel-chat")["former_agent_id"] == MODEL
    snap = _send("GET", "/v1/ui/sessions/rel-chat?snapshot=true", None, people.first)
    assert snap.status_code == 200, snap.text
    assert snap.json()["session"]["former_agent_id"] == MODEL
    assert "former_agent_id" not in SessionChanges.model_fields
    body = {"id": "rel-chat", "title": "t", "messages": EMPTY, "updatedAt": 1, "former_agent_id": "X"}
    assert "former_agent_id" not in ui_sessions.SessionModel.model_validate(body).to_changes().model_fields_set


async def test_migration_adds_former_agent_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE user_sessions (id TEXT PRIMARY KEY, title TEXT, messages TEXT, "
            "updatedAt INTEGER, is_home INTEGER DEFAULT 0, home_for_agent TEXT, agent_id TEXT)"
        )
        await conn.execute(
            "INSERT INTO user_sessions (id, title, messages, updatedAt, agent_id) "
            "VALUES ('rel-old', 't', '{}', 1, 'Someone')"
        )
        await conn.commit()
    monkeypatch.setattr(async_db, "get_db_path", lambda: path)
    await async_db.init_db_async()
    async with aiosqlite.connect(path) as conn:
        columns = {row[1] for row in await conn.execute_fetchall("PRAGMA table_info(user_sessions)")}
        rows = await conn.execute_fetchall("SELECT former_agent_id FROM user_sessions")
    former = next(iter(rows))[0]
    assert "former_agent_id" in columns
    assert former is None
    # 還原失敗、程序沒重啟時可能讀到沒有這欄的舊庫：公開模型要給預設值
    document = SessionDocument(
        id="rel-old",
        title="t",
        messages=EMPTY,
        updatedAt=1,
        is_home=False,
        home_for_agent=None,
        agent_id=None,
        project_id=None,
        auth_level="Manual",
        active_mcp_servers=[],
        active_skills=[],
        last_activity_at=1,
        context_summary=None,
    )
    assert document.former_agent_id is None


# --------------------------------------------------------------------------- #
# 呼叫點
# --------------------------------------------------------------------------- #


async def test_soul_delete_releases_inside_the_lock(
    sandbox: Path, people: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """刪除靈魂：破壞性操作鎖內、取消註冊之後解除（正常流程下重置已刪光，這裡是安全網）."""
    _ = sandbox
    assert _create_soul(SOUL, people).status_code == 200
    await _insert("rel-draft", SOUL, owner=people.first.agent_id, tree=EMPTY)
    order: list[Any] = []
    real_unregister = souls_router._unregister_soul  # noqa: SLF001
    real_release = async_db.session_service.release_agent_sessions

    def _unregister(identity: AgentIdentity) -> list[str]:
        order.append("unregister")
        return real_unregister(identity)

    async def _release(identity: AgentIdentity) -> Any:
        order.append(("release", destructive_ops.is_held(identity)))
        return await real_release(identity)

    monkeypatch.setattr(souls_router, "_unregister_soul", _unregister)
    monkeypatch.setattr(async_db.session_service, "release_agent_sessions", _release)
    response = _delete_soul(SOUL, people)
    assert response.status_code == 200, response.text
    assert "sessions" not in response.json()["skipped"]
    assert order == ["unregister", ("release", True)]
    row = await _row("rel-draft")
    assert row is not None
    assert row["agent_id"] is None


async def test_incomplete_delete_keeps_conversations(
    sandbox: Path, people: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登記表沒寫成（回應照樣 200）：名字看起來還有人用，對話不動、看得出來."""
    _ = sandbox
    assert _create_soul(SOUL, people).status_code == 200
    await _insert("rel-draft", SOUL, owner=people.first.agent_id, tree=EMPTY)
    monkeypatch.setattr(souls_router, "_update_agents_registry", lambda *_a, **_k: None)
    response = _delete_soul(SOUL, people)
    assert response.status_code == 200, response.text
    assert "sessions" in response.json()["skipped"]
    row = await _row("rel-draft")
    assert row is not None
    assert row["agent_id"] == SOUL


@pytest.mark.parametrize("breakage", ["release", "judge"])
async def test_delete_finishes_when_release_cannot(
    breakage: str, sandbox: Path, people: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解除綁定或「名字有沒有人用」的判斷丟例外：刪除照樣 200，鎖外的機器人、排程照做."""
    _ = sandbox
    assert _create_soul(SOUL, people).status_code == 200
    assert _create_model(MODEL, people).status_code == 200

    async def _broken_release(_identity: AgentIdentity) -> Any:
        raise RuntimeError

    def _broken_judge(_identity: AgentIdentity, **_kwargs: Any) -> bool:
        raise RuntimeError

    if breakage == "release":
        monkeypatch.setattr(async_db.session_service, "release_agent_sessions", _broken_release)
    else:
        monkeypatch.setattr(agent_leftovers.mission_boards, "is_unclaimed", _broken_judge)
    response = _delete_soul(SOUL, people)
    assert response.status_code == 200, response.text
    skipped = response.json()["skipped"]
    assert "sessions" in skipped
    if breakage == "judge":
        assert "schedules" in skipped  # 鎖外照做（判斷同樣失敗，列出來）
    assert not (sandbox / "soul" / SOUL).exists()
    # 刪除自訂模型也一樣：回應照舊成功、資料夾刪掉
    assert _delete_model(MODEL, people).status_code == 200
    assert not (sandbox / "custom" / MODEL).exists()


@pytest.mark.parametrize("path", ["create", "edit_create", "model", "rename"])
async def test_create_paths_release_leftovers(
    path: str, sandbox: Path, people: SimpleNamespace
) -> None:
    _ = sandbox
    await _insert("rel-chat", MODEL, owner=people.first.agent_id)
    if path == "rename":
        assert _create_model(RENAMED, people).status_code == 200
        response = _rename_model(RENAMED, MODEL, people)
    elif path == "model":
        response = _create_model(MODEL, people)
    elif path == "create":
        response = _create_soul(MODEL, people)
    else:
        response = _edit_create(MODEL, people)
    assert response.status_code == 200, response.text
    row = await _row("rel-chat")
    assert row is not None
    assert row["agent_id"] is None
    assert row["former_agent_id"] == MODEL


@pytest.mark.parametrize("path", ["create", "model"])
async def test_claimed_names_are_not_released(
    path: str, sandbox: Path, people: SimpleNamespace, registry: Path
) -> None:
    """名字還在登記表（有人用）：不解除，那是它的."""
    _ = sandbox
    registry.write_text(json.dumps({MODEL: [MODEL]}), encoding="utf-8")
    await _insert("rel-chat", MODEL, owner=people.first.agent_id)
    response = _create_soul(MODEL, people) if path == "create" else _create_model(MODEL, people)
    assert response.status_code == 200, response.text
    row = await _row("rel-chat")
    assert row is not None
    assert row["agent_id"] == MODEL


@pytest.mark.parametrize("path", ["create", "edit_create", "model", "rename"])
async def test_release_failure_refuses_create(
    path: str, sandbox: Path, people: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = sandbox
    if path == "rename":
        assert _create_model(RENAMED, people).status_code == 200

    async def _broken(_identity: AgentIdentity) -> Any:
        raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

    monkeypatch.setattr(async_db.session_service, "release_agent_sessions", _broken)
    if path == "rename":
        response = _rename_model(RENAMED, MODEL, people)
    elif path == "model":
        response = _create_model(MODEL, people)
    elif path == "create":
        response = _create_soul(MODEL, people)
    else:
        response = _edit_create(MODEL, people)
    assert response.status_code != 200
    assert response.json()["detail"]["code"] == "SESSION_SAVE_FAILED"
    assert not (sandbox / "soul" / MODEL).exists()
    assert not (sandbox / "custom" / MODEL).exists()
    if path == "rename":
        assert (sandbox / "custom" / RENAMED).exists()


async def test_custom_delete_spares_a_soul_name(sandbox: Path, people: SimpleNamespace) -> None:
    """和靈魂同名的舊自訂模型資料夾：刪掉資料夾後名字仍屬於靈魂，對話不動."""
    _ = sandbox
    assert _create_soul(SOUL, people).status_code == 200
    legacy = sandbox / "custom" / SOUL
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text(json.dumps({"id": SOUL, "name": SOUL}), encoding="utf-8")
    (legacy / "system_prompt.md").write_text("PROMPT", encoding="utf-8")
    await _insert("rel-chat", SOUL, owner=people.first.agent_id)
    response = _delete_model(SOUL, people)
    assert response.status_code == 200, response.text
    row = await _row("rel-chat")
    assert row is not None
    assert row["agent_id"] == SOUL


async def test_custom_model_named_like_a_human_releases(
    sandbox: Path, people: SimpleNamespace
) -> None:
    """自訂模型的名字剛好是人類帳號：對話這邊不把人類帳號算成「有人用」."""
    _ = sandbox
    name = people.second.agent_id
    NAMES.append(name)
    try:
        assert _create_model(name, people).status_code == 200
        await _insert("rel-chat", name, owner=people.first.agent_id)
        assert _delete_model(name, people).status_code == 200
        row = await _row("rel-chat")
        assert row is not None
        assert row["agent_id"] is None
        assert row["former_agent_id"] == name
    finally:
        mission_board_path(name).unlink(missing_ok=True)
        shutil.rmtree(sandbox / "custom" / name, ignore_errors=True)
        NAMES.remove(name)


async def test_human_continues_with_own_choice(sandbox: Path, people: SimpleNamespace) -> None:
    """原本的人類接著聊：改綁到自己能驅動的靈魂可以；別人不行."""
    _ = sandbox
    await _model_with_old_chat(people)
    assert _create_soul(TARGET, people).status_code == 200
    _give(TARGET, people.first)
    target = AgentIdentity(agent_id=TARGET)
    # 驗證過的身分每個請求從 users.json 帶出擁有清單
    first = AgentIdentity.for_role(
        people.first.agent_id, "normal", is_human=True, owned_agents=[TARGET]
    )

    other = await async_db.session_service.read_persistence_snapshot("rel-chat", people.second)
    try:
        refused = await async_db.session_service.save_ui_session(
            "rel-chat", other.revision, SessionChanges(), people.second, target
        )
    except ToukaException as exc:
        assert exc.code == ErrorCode.AUTH_PERMISSION_DENIED
    else:
        assert not isinstance(refused, SavedResult)

    mine = await async_db.session_service.read_persistence_snapshot("rel-chat", first)
    result = await async_db.session_service.save_ui_session(
        "rel-chat", mine.revision, SessionChanges(), first, target
    )
    assert isinstance(result, SavedResult)
    row = await _row("rel-chat")
    assert row is not None
    assert row["agent_id"] == TARGET
    assert row["former_agent_id"] == MODEL

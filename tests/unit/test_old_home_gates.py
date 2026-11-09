"""舊家的唯讀與刪除（3d PR 2a，ADR-0306）：每個入口都擋，擋在 hook 與組主線之前.

- 共同入口（``proxy_service._run_request``）在 ``pipeline.execute`` 之前擋；IDE 舊路徑、SNS 家模式
  在這裡記新家開始用，網頁在准入交易裡記與重驗。
- 插話、換靈魂也擋；交接開始後（唯讀第二段）存檔閘擋所有內容寫入。
- 舊家交接完成前不能刪：路由和刪除閘都擋。
- 心跳建家走同一套交接紀錄。

全部離線：SQLite 只在測試沙盒；模型、hook 都不會被呼叫。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.middleware.layers.monitor import turn_session_id
from src.models.identity import AgentIdentity
from src.models.interjection import InterjectionCreateRequest
from src.models.session_persistence import (
    PersistedStreamAdmission,
    SessionChanges,
)
from src.routers import ui_interjections, ui_sessions
from src.services import proxy_service
from src.services.resource_manager import resource_manager
from src.utils import async_db
from src.utils.history_manager import TreeMessageManager

from tests.support.session_scope import ADMIN, DEBUG, HUMAN, TREE, A, B, attempt, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

service = async_db.session_service
OUTSIDER = AgentIdentity.for_role("other-human", "normal", is_human=True, owned_agents=["ScopeC"])
READ_ONLY = ToukaException(ErrorCode.SESSION_OLD_HOME_READ_ONLY).error_id
NOT_DELETABLE = ToukaException(ErrorCode.SESSION_OLD_HOME_NOT_DELETABLE).error_id
WAITING_TREE = copy.deepcopy(TREE)
WAITING_TREE["messages"]["u2"] = {"id": "u2", "role": "user", "content": "next", "parentId": "a"}
WAITING_TREE["currentId"] = "u2"


async def started(sid):
    async with async_db.get_async_db() as db:
        cursor = await db.execute(
            "SELECT new_started FROM soul_home_transitions WHERE new_session = ?", (sid,)
        )
        return [row[0] for row in await cursor.fetchall()]


async def set_status(status):
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE soul_home_transitions SET status = ?", (status,))
        await db.commit()


@pytest.fixture
async def moved(scope_db):
    """A 是舊家、B 是還沒開始用的新家."""
    await seed("A", tree=WAITING_TREE)
    await seed("B", tree=WAITING_TREE)
    assert await service.set_home("A", A, True)
    await service.check_old_home("A", mark_started=True, requester=None)  # A 當家時用過
    assert await service.set_home("B", A, True)


def is_code(result, code):
    return isinstance(result, ToukaException) and result.code is code


# --- 共同入口 ---


def proxy_request(sid, *, web=False):
    return SimpleNamespace(session_id=sid, ui_persistence=object() if web else None, model="m")


@pytest.mark.usefixtures("moved")
async def test_old_home_is_refused_before_hooks_and_the_mainline(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(proxy_service.pipeline, "execute", execute)
    for web in (False, True):
        result = await attempt(proxy_service._run_request(proxy_request("A", web=web), {}, True))
        assert is_code(result, ErrorCode.SESSION_OLD_HOME_READ_ONLY)
    execute.assert_not_awaited()


class Stop(Exception):
    """pipeline 被呼叫了（這一輪沒被舊家擋下）."""


@pytest.fixture
def pipeline_stops(monkeypatch):
    execute = AsyncMock(side_effect=Stop)
    monkeypatch.setattr(proxy_service.pipeline, "execute", execute)
    return execute


@pytest.mark.usefixtures("moved")
async def test_ide_turn_marks_the_new_home_started_web_waits_for_admission(pipeline_stops):
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request("B", web=True), {}, True)
    assert await started("B") == [0]
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request("B"), {}, True)
    assert await started("B") == [1]


@pytest.mark.usefixtures("moved")
@pytest.mark.parametrize("header", ["x-touka-session-id", "x-toukachat-session-id"])
async def test_a_session_id_sent_in_a_header_is_checked_the_same_way(pipeline_stops, header):
    """對話編號放在標頭（MonitorLayer 也認）時一樣擋舊家、記新家開始用（審查 J1）."""
    result = await attempt(
        proxy_service._run_request(proxy_request(None), {"headers": {header: "A"}}, True)
    )
    assert is_code(result, ErrorCode.SESSION_OLD_HOME_READ_ONLY)
    pipeline_stops.assert_not_awaited()
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request(None), {"headers": {header: "B"}}, True)
    assert await started("B") == [1]


async def test_the_body_session_id_wins_over_the_headers():
    headers = {"x-touka-session-id": "header", "x-toukachat-session-id": "other"}
    assert turn_session_id(proxy_request("body"), headers) == "body"
    assert turn_session_id(proxy_request(None), headers) == "header"
    assert turn_session_id(proxy_request(None), {"x-toukachat-session-id": "other"}) == "other"
    assert turn_session_id(proxy_request(None), {}) is None


@pytest.mark.usefixtures("moved")
@pytest.mark.parametrize("caller", [OUTSIDER, B], ids=["other-human", "other-soul"])
async def test_a_caller_who_cannot_use_the_conversation_learns_and_marks_nothing(
    pipeline_stops, caller
):
    """不能用這段對話的人：不回是不是舊家、不記開始用，交給後面的層照原樣拒絕（審查 J2）."""
    for sid in ("A", "B"):
        with pytest.raises(Stop):
            await proxy_service._run_request(proxy_request(sid), {"identity": caller}, True)
    assert await started("B") == [0]


@pytest.mark.usefixtures("moved")
@pytest.mark.parametrize("caller", [HUMAN, A], ids=["owner", "soul"])
async def test_a_caller_who_can_use_the_conversation_is_checked_as_before(pipeline_stops, caller):
    result = await attempt(
        proxy_service._run_request(proxy_request("A"), {"identity": caller}, True)
    )
    assert is_code(result, ErrorCode.SESSION_OLD_HOME_READ_ONLY)
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request("B"), {"identity": caller}, True)
    assert await started("B") == [1]


@pytest.mark.parametrize("caller", [HUMAN, ADMIN, DEBUG], ids=["owner", "admin", "debug"])
async def test_a_legacy_prefixed_old_home_is_refused_to_whoever_can_drive_the_soul(
    monkeypatch, caller
):
    """舊前綴（``ToukaSoulX``）的列：擁有者比對逐字失敗，但能驅動這個靈魂的人後面各層照樣
    寫得進去，所以照樣檢查舊家（第 2 輪審查 K5）；不能驅動的人照舊什麼都不知道."""
    monkeypatch.setattr(  # 正式程式的人類分支逐字比對 owned_agents，不去掉前綴
        resource_manager,
        "is_owner",
        lambda target, who: who.is_admin_authority()
        or (who.is_human and target.agent_id in who.owned_agents),
    )
    await seed("legacy")
    await seed("new")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET agent_id = 'ToukaSoulScopeA', owner_user_id = NULL "
            "WHERE id = 'legacy'"
        )
        await db.commit()
    assert await service.set_home("legacy", A, True)
    await service.check_old_home("legacy", mark_started=True, requester=None)
    assert await service.set_home("new", A, True)
    assert await service.check_old_home("legacy", mark_started=False, requester=caller)
    assert not await service.check_old_home("legacy", mark_started=False, requester=OUTSIDER)


async def test_the_owner_marks_a_legacy_prefixed_new_home_started(monkeypatch):
    """舊前綴的新家：擁有者比對逐字失敗、但人類擁有者後面各層存得進去，照樣記開始用
    （第 3 輪審查 L6「或是人類」那半，第 4 輪審查補測試）."""
    monkeypatch.setattr(
        resource_manager,
        "is_owner",
        lambda target, who: who.is_admin_authority()
        or (who.is_human and target.agent_id in who.owned_agents),
    )
    await seed("first")
    await seed("legacy")
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET agent_id = 'ToukaSoulScopeA', owner_user_id = NULL "
            "WHERE id = 'legacy'"
        )
        await db.commit()
    assert await service.set_home("first", A, True)
    await service.check_old_home("first", mark_started=True, requester=None)
    assert await service.set_home("legacy", A, True)
    assert await started("legacy") == [0]
    assert not await service.check_old_home("legacy", mark_started=True, requester=HUMAN)
    assert await started("legacy") == [1]


@pytest.mark.usefixtures("moved")
async def test_only_a_caller_whose_turn_is_saved_marks_the_new_home_started():
    """DebugAgent、代打別的靈魂的非人類身分存不進新家：不記開始用，人工測試也不改到別的靈魂的
    紀錄；舊家照樣擋（第 3 輪審查 L6）."""
    agent_admin = AgentIdentity(agent_id="ScopeB", human_role="admin", is_human=False)
    for caller in (DEBUG, agent_admin):
        assert not await service.check_old_home("B", mark_started=True, requester=caller)
        assert await started("B") == [0]
        assert await service.check_old_home("A", mark_started=True, requester=caller)
    assert not await service.check_old_home("B", mark_started=True, requester=HUMAN)
    assert await started("B") == [1]


@pytest.mark.usefixtures("moved")
async def test_unreadable_records_fail_the_turn_with_a_save_error(pipeline_stops, monkeypatch):
    """讀不到交接紀錄：這一輪失敗、有錯誤碼，不放寬（審查 J12）."""
    monkeypatch.setattr(
        async_db.home_transitions, "is_old_home", AsyncMock(side_effect=RuntimeError("disk"))
    )
    result = await attempt(proxy_service._run_request(proxy_request("A"), {}, True))
    assert is_code(result, ErrorCode.SESSION_SAVE_FAILED)
    pipeline_stops.assert_not_awaited()


async def test_an_internal_channel_cannot_become_home():
    """頻道編號固定：當過家、換走、刪掉重建會一出生就是舊家；設家一律拒絕（審查 J7）."""
    channel = await service.open_channel_session("heartbeat", A, title="Pulse")
    result = await attempt(service.set_home(channel, A, True))
    assert is_code(result, ErrorCode.SESSION_SCOPE_MISMATCH)
    assert (await raw_row(channel))["is_home"] == 0


# --- 網頁准入 ---


async def admit(sid):
    snapshot = await service.read_persistence_snapshot(sid, HUMAN)
    admission = PersistedStreamAdmission(
        mode="persistent", revision=snapshot.revision, user_parent_id="u2", assistant_node_id="n"
    )
    return await service.admit_ui_stream(sid, admission, HUMAN, None)


@pytest.mark.usefixtures("moved")
async def test_web_admission_refuses_the_old_home_and_marks_the_new_home_started():
    refused = await admit("A")
    assert refused.status == "failed"
    assert refused.error_code == READ_ONLY
    assert await started("B") == [0]
    admitted = await admit("B")
    assert admitted.session["id"] == "B"
    assert await started("B") == [1]


@pytest.mark.usefixtures("moved")
async def test_web_admission_does_not_mark_a_conversation_that_is_no_longer_home():
    """紀錄還指向 B、B 已經不是家（舊資料）：在 B 開一輪不算新家開始用（審查 J13）."""
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE user_sessions SET is_home = 0, home_for_agent = NULL")
        await db.commit()
    admitted = await admit("B")
    assert admitted.session["id"] == "B"
    assert await started("B") == [0]


# --- 插話、換靈魂 ---


@pytest.mark.usefixtures("moved")
async def test_new_interjections_into_the_old_home_are_refused_listing_still_works(monkeypatch):
    """只擋新增；換家前就在跑的回覆，插話照樣列得出、收得回（審查 J11）."""
    state = {"started": 1, "stream_id": "s", "status": "active"}
    monkeypatch.setattr(ui_interjections, "_authorized_stream_state", AsyncMock(return_value=state))
    monkeypatch.setattr(ui_interjections, "check_interjection", AsyncMock(return_value=False))
    enqueue = Mock(return_value="queued")
    monkeypatch.setattr(ui_interjections.interjection_queue, "enqueue", enqueue)
    request = InterjectionCreateRequest(text="more")
    result = await attempt(ui_interjections.add_interjection("A", "s", request, HUMAN))
    assert is_code(result, ErrorCode.SESSION_OLD_HOME_READ_ONLY)
    enqueue.assert_not_called()
    listed = await ui_interjections.list_interjections("A", "s", HUMAN)
    assert listed.interjections == []
    assert await ui_interjections.add_interjection("B", "s", request, HUMAN) == "queued"


async def test_a_stream_that_ends_during_the_old_home_check_takes_no_interjection(monkeypatch):
    """舊家檢查會讓出執行權：回覆剛好在這時結束，插話不能回 201 卻沒送達（第 2 輪審查 K8）."""
    state = {"started": 1, "stream_id": "s", "status": "active"}
    monkeypatch.setattr(ui_interjections, "_authorized_stream_state", AsyncMock(return_value=state))
    monkeypatch.setattr(ui_interjections, "check_interjection", AsyncMock(return_value=False))

    async def reply_ends(_sid):
        state["status"] = "done"
        return False

    monkeypatch.setattr(ui_interjections.session_service, "is_old_home", reply_ends)
    enqueue = Mock(return_value="queued")
    monkeypatch.setattr(ui_interjections.interjection_queue, "enqueue", enqueue)
    request = InterjectionCreateRequest(text="more")
    result = await attempt(ui_interjections.add_interjection("B", "s", request, HUMAN))
    assert is_code(result, ErrorCode.STREAM_NOT_FOUND)
    enqueue.assert_not_called()


@pytest.mark.usefixtures("moved")
async def test_the_old_home_cannot_switch_souls():
    before = await raw_row("A")
    current = await service.read_persistence_snapshot("A", HUMAN)
    result = await service.save_ui_session("A", current.revision, SessionChanges(), HUMAN, B)
    assert result.status == "failed"
    assert result.error_code == READ_ONLY
    assert await raw_row("A") == before


@pytest.mark.usefixtures("moved")
async def test_before_handover_the_old_home_still_takes_ordinary_saves():
    """退任中（交接開始前）：改名、收尾保存照舊."""
    current = await service.read_persistence_snapshot("A", HUMAN)
    result = await service.conditional_save_session(
        "A", current.revision, SessionChanges(title="renamed"), HUMAN
    )
    assert result.status == "saved"


@pytest.mark.usefixtures("moved")
async def test_once_handover_starts_the_save_gate_refuses_every_write():
    await set_status("running")
    before = await raw_row("A")
    current = await service.read_persistence_snapshot("A", HUMAN)
    result = await service.conditional_save_session(
        "A", current.revision, SessionChanges(title="renamed"), HUMAN
    )
    assert result.status == "failed"
    assert result.error_code == READ_ONLY
    assert not await service.append_message_to_session("A", "assistant", "late", identity=A)
    assert await raw_row("A") == before
    assert await service.set_home("A", A, True)  # 設回家就能寫
    current = await service.read_persistence_snapshot("A", HUMAN)
    result = await service.conditional_save_session(
        "A", current.revision, SessionChanges(title="renamed"), HUMAN
    )
    assert result.status == "saved"


# --- 刪除 ---


@pytest.mark.usefixtures("moved")
async def test_the_delete_gate_refuses_an_old_home_until_handover_finishes():
    current = await service.read_persistence_snapshot("A", HUMAN)
    refused = await service.conditional_delete_session("A", current.revision, HUMAN)
    assert refused.status == "failed"
    assert refused.error_code == NOT_DELETABLE
    await set_status("done")
    assert (await service.conditional_delete_session("A", current.revision, HUMAN)).status == (
        "saved"
    )


@pytest.mark.usefixtures("moved")
async def test_the_delete_route_refuses_an_old_home():
    current = await service.read_persistence_snapshot("A", HUMAN)
    result = await attempt(ui_sessions.delete_session("A", current.revision, HUMAN))
    assert is_code(result, ErrorCode.SESSION_OLD_HOME_NOT_DELETABLE)
    assert await raw_row("A") is not None


# --- 心跳建家 ---


async def create_home(sid):
    snapshot = await service.read_execution_snapshot(sid, A)
    return await service.conditional_save_session(
        sid,
        snapshot.revision,
        SessionChanges(
            title="home",
            messages=TreeMessageManager.create_empty_tree(),
            agent_id=A,
            is_home=True,
            home_for_agent=A,
            project_id="default",
        ),
        A,
        after_write=async_db.new_home_after_write(A, sid),
    )


async def test_heartbeat_home_creation_follows_the_records():
    await seed("A")
    assert await service.set_home("A", A, True)
    await service.check_old_home("A", mark_started=True, requester=None)
    assert await service.set_home("A", A, False)
    assert (await create_home("D")).status == "saved"
    assert await started("D") == [0]
    assert await service.is_old_home("A")


async def test_heartbeat_home_creation_never_makes_a_second_home():
    await seed("A")
    assert await service.set_home("A", A, True)
    assert (await create_home("D")).status == "failed"
    assert await raw_row("D") is None

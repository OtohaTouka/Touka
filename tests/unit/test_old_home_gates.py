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
from unittest.mock import AsyncMock

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.session_persistence import (
    PersistedStreamAdmission,
    SessionChanges,
)
from src.routers import ui_interjections, ui_sessions
from src.services import proxy_service
from src.utils import async_db
from src.utils.history_manager import TreeMessageManager

from tests.support.session_scope import HUMAN, TREE, A, B, attempt, raw_row, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

service = async_db.session_service
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


@pytest.mark.usefixtures("moved")
async def test_ide_turn_marks_the_new_home_started_web_waits_for_admission(monkeypatch):
    class Stop(Exception):
        pass

    monkeypatch.setattr(proxy_service.pipeline, "execute", AsyncMock(side_effect=Stop))
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request("B", web=True), {}, True)
    assert await started("B") == [0]
    with pytest.raises(Stop):
        await proxy_service._run_request(proxy_request("B"), {}, True)
    assert await started("B") == [1]


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


# --- 插話、換靈魂 ---


@pytest.mark.usefixtures("moved")
async def test_interjection_into_the_old_home_is_refused(monkeypatch):
    state = {"started": 1, "stream_id": "s", "status": "active"}
    monkeypatch.setattr(ui_interjections, "_authorized_stream_state", AsyncMock(return_value=state))
    result = await attempt(ui_interjections._ensure_current_stream("A", "s", HUMAN, active=True))
    assert is_code(result, ErrorCode.SESSION_OLD_HOME_READ_ONLY)
    assert await ui_interjections._ensure_current_stream("B", "s", HUMAN, active=True) is None


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
    assert await service.set_home("A", A, False)
    assert (await create_home("D")).status == "saved"
    assert await started("D") == [0]
    assert await service.is_old_home("A")


async def test_heartbeat_home_creation_never_makes_a_second_home():
    await seed("A")
    assert await service.set_home("A", A, True)
    assert (await create_home("D")).status == "failed"
    assert await raw_row("D") is None

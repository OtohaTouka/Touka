"""主線怎麼組（3d PR 1，ADR-0305）：網頁、IDE、群聊、心跳都呼叫同一個函式。

| 誰送出 | 前情 | 家原文 |
|---|---|---|
| 網頁版家本身（送完整紀錄） | 帶 | 不帶 |
| 家，但 IDE、SNS 家模式（只送最新一句） | 帶 | 帶 |
| 子對話（網頁、IDE） | 帶 | 帶，每則內容前標時間 |
| 群聊 | 帶 | 照舊帶 |
| 心跳（在家或子對話） | 帶 | 不帶 |
| 無痕 | 不帶 | 不帶 |

另外：家不再插自己的 ``context_summary``（已搬進前情）；子對話自己的舊摘要照舊插，
已經搬進前情的（例如舊家）不再插第二次。上下文預覽不搬舊摘要（不寫資料）；每輪只讀一次家；
沒有對話編號的請求不當成子對話（不標時間）。
全部離線：SQLite 只在測試沙盒。
"""

from __future__ import annotations

import logging

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers import soul as soul_layer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sliding_window import SlidingWindowConfig
from src.routers.ui_sessions import SetHomeRequest, set_home_session
from src.services import context_snapshot_service, mainline_service, soul_prelude_service
from src.services.proxy_service import pipeline
from src.services.soul_service import soul_service
from src.utils import async_db
from src.utils import soul_prelude_db as db_mod
from src.utils.soul_prelude_db import PreludeSegment

from tests.support.session_scope import HUMAN, A, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

HOME_TREE = {
    "messages": {
        "u": {
            "id": "u",
            "role": "user",
            "content": "家裡的問題",
            "parentId": None,
            "timestamp": 1_700_000_000_000,
        },
        "a": {
            "id": "a",
            "role": "assistant",
            "content": "家裡的回答",
            "parentId": "u",
            "timestamp": 1_700_000_060_000,
        },
    },
    "currentId": "a",
}


@pytest.fixture(autouse=True)
async def _home_and_prelude(scope_db, monkeypatch):  # noqa: ARG001 — 先建好資料庫
    _ = scope_db
    monkeypatch.setattr(mainline_service, "load_sliding_window_config", SlidingWindowConfig)
    await seed("home", tree=HOME_TREE)
    await seed("child", context_summary="子對話自己的舊摘要")
    assert await async_db.session_service.set_home("home", A, True)
    await db_mod.insert_segment(
        PreludeSegment(
            agent=A.agent_id,
            kind="home_trim",
            source_session="home",
            dedupe_key="m0",
            content="前情的一段",
            created_at=1,
        )
    )


def _request() -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="m",
        messages=[
            OpenAIMessage(role="system", content="人格"),
            OpenAIMessage(role="user", content="這一輪"),
        ],
    )


def _field(message: object, name: str) -> object:
    """家原文照舊以 dict 插入（只有角色與內容，ADR-0260）；其他是 OpenAIMessage."""
    return message[name] if isinstance(message, dict) else getattr(message, name)


def _system(request: OpenAIChatRequest) -> str:
    return "".join(
        str(_field(m, "content")) for m in request.messages if _field(m, "role") == "system"
    )


def _dialogue(request: OpenAIChatRequest) -> list[str]:
    return [str(_field(m, "content")) for m in request.messages if _field(m, "role") != "system"]


@pytest.mark.parametrize(
    ("mode", "session_id", "sends_own", "prelude", "home", "stamped"),
    [
        ("chat", "home", True, True, False, False),  # 網頁版家本身
        ("chat", "home", False, True, True, False),  # 家，但 IDE／SNS 家模式
        ("chat", "child", True, True, True, True),  # 子對話（網頁）
        ("chat", "child", False, True, True, True),  # 子對話（IDE）
        ("group", None, False, True, True, False),
        ("heartbeat", "home", True, True, False, False),
        ("heartbeat", "child", True, True, False, False),
        ("incognito", "child", True, False, False, False),
        ("chat", None, False, True, True, False),  # 沒有對話編號：不當成子對話（審查 F14）
    ],
    ids=[
        "web-home", "ide-home", "web-child", "ide-child",
        "group", "hb-home", "hb-child", "incognito", "no-session",
    ],
)
async def test_mainline_table(mode, session_id, sends_own, prelude, home, stamped):
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode=mode, session_id=session_id, sends_own_history=sends_own
    )
    system = _system(request)
    assert ("<soul_prelude>" in system) is prelude
    assert ("前情的一段" in system) is prelude
    dialogue = _dialogue(request)
    joined = "\n".join(dialogue)
    assert ("家裡的問題" in joined) is home
    assert dialogue[-1] == "這一輪"
    if home:
        stamped_lines = [line for line in dialogue if "家裡的回答" in line]
        assert stamped_lines
        assert ("2023" in stamped_lines[0]) is stamped  # 1_700_000_060_000 是 2023 年


async def test_prelude_sits_after_the_persona_in_the_system_prompt():
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="home", sends_own_history=True
    )
    system = _system(request)
    assert system.index("人格") < system.index("<soul_prelude>")


async def test_home_summary_moves_into_the_prelude_instead_of_its_own_block():
    await seed("home2", context_summary="家的舊摘要")
    assert await async_db.session_service.set_home("home2", A, True)
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="home2", sends_own_history=True
    )
    await soul_service.inject_context_summary(request, "home2")
    system = _system(request)
    assert "家的舊摘要" in system
    assert "<conversation_summary>" not in system


async def test_child_keeps_its_own_old_summary_block():
    request = _request()
    await mainline_service.inject_conversation_summary(request, A, "child")
    assert "<conversation_summary>" in _system(request)
    assert "子對話自己的舊摘要" in _system(request)


async def test_prelude_failure_does_not_block_the_turn(monkeypatch, caplog):
    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mainline_service, "build_prelude_block", broken)
    request = _request()
    with caplog.at_level(logging.WARNING, logger=mainline_service.__name__):
        await mainline_service.inject_mainline(
            request, A, mode="chat", session_id="child", sends_own_history=True
        )
    assert "<soul_prelude>" not in _system(request)
    assert "家裡的問題" in "\n".join(_dialogue(request))
    assert any(r.exc_info for r in caplog.records)  # 記下原因，不只一行 warning（審查 F20）


async def test_soul_without_home_still_gets_its_prelude():
    assert await async_db.session_service.set_home("home", A, False)
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="child", sends_own_history=True
    )
    assert "前情的一段" in _system(request)


async def test_set_home_carries_both_old_summaries_into_the_prelude():
    """升級後還沒聊過就換家：舊家與新家升級前的舊摘要都搬進前情（ADR-0305）。"""
    await seed("h1", context_summary="舊家的舊摘要")
    await seed("h2", context_summary="新家的舊摘要")
    assert await async_db.session_service.set_home("h1", A, True)
    request = SetHomeRequest(session_id="h2", agent_id=A, is_home=True)
    await set_home_session(request, identity=HUMAN)
    contents = {s.content for s in await db_mod.list_live_segments(A.agent_id)}
    assert {"舊家的舊摘要", "新家的舊摘要"} <= contents


async def test_set_home_still_succeeds_when_the_prelude_is_unavailable(monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(soul_prelude_service, "migrate_legacy_summary", broken)
    await seed("h3", context_summary="新家的舊摘要")
    result = await set_home_session(
        SetHomeRequest(session_id="h3", agent_id=A, is_home=True), identity=HUMAN
    )
    assert result["session_id"] == "h3"


# ── 審查第 1 輪的修正 ───────────────────────────────────────────────────────


async def test_preview_does_not_move_the_old_summary():
    """上下文預覽不寫資料：不搬家的舊摘要，前情照樣顯示（審查 F12）."""
    await seed("home2", context_summary="家的舊摘要")
    assert await async_db.session_service.set_home("home2", A, True)
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id=None, preview=True
    )
    assert "前情的一段" in _system(request)
    kinds = [s.kind for s in await db_mod.list_live_segments(A.agent_id)]
    assert "legacy_summary" not in kinds


async def test_former_home_summary_is_not_injected_twice():
    """舊家的舊摘要已經在前情裡：回到舊家（現在是子對話）時不再插一份（審查 F13）."""
    await seed("old", context_summary="舊家的舊摘要")
    assert await async_db.session_service.set_home("old", A, True)
    await mainline_service.inject_mainline(_request(), A, mode="chat", session_id="old")
    assert await async_db.session_service.set_home("home", A, True)
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="old", sends_own_history=True
    )
    await mainline_service.inject_conversation_summary(request, A, "old")
    system = _system(request)
    assert system.count("舊家的舊摘要") == 1
    assert "<conversation_summary>" not in system


async def test_home_is_read_once_per_turn(monkeypatch):
    """前情與家原文用同一次讀到的家，不會一個新一個舊（審查 F16）."""
    real = async_db.session_service.read_home_snapshot
    reads: list[str] = []

    async def counting(identity):
        reads.append(identity.agent_id)
        return await real(identity)

    monkeypatch.setattr(async_db.session_service, "read_home_snapshot", counting)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="home")
    assert reads == [A.agent_id]
    assert "家裡的問題" in "\n".join(_dialogue(request))


async def test_unreadable_home_still_sends_the_turn(monkeypatch):
    """讀家失敗：當作沒有家，這一輪照樣送出（不會第二次讀家又失敗）."""

    async def broken(_identity):
        raise async_db.SessionPersistenceError

    monkeypatch.setattr(async_db.session_service, "read_home_snapshot", broken)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="group")
    assert "家裡的問題" not in "\n".join(_dialogue(request))
    assert _dialogue(request)[-1] == "這一輪"


async def test_soul_layer_passes_the_preview_flag(monkeypatch):
    """SoulLayer 把預覽旗標交給主線（審查 F12）."""
    seen: dict[str, object] = {}

    async def fake_mainline(_request, _identity, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(soul_layer, "inject_mainline", fake_mainline)
    monkeypatch.setattr(soul_layer.soul_service, "get_soul_data", lambda _aid: ("SOUL", {}))
    monkeypatch.setattr(soul_layer.soul_service, "apply_soul_to_request", lambda *_a: None)
    monkeypatch.setattr(soul_layer, "get_active_brain", lambda _identity: "gemini-2.5-flash")
    request = _request()
    ctx = PipelineContext(is_toukasoul=True, agent_id=A.agent_id, preview=True)
    await soul_layer.SoulLayer().process(request, ctx)
    assert seen["preview"] is True


async def test_context_preview_marks_itself_as_preview(monkeypatch):
    """上下文預覽跑真的組裝時帶預覽旗標，整個流程不寫資料（審查 F12）."""
    seen: list[PipelineContext] = []

    async def capture(_request, ctx):
        seen.append(ctx)

    monkeypatch.setattr(pipeline, "execute", capture)
    await context_snapshot_service.get_context_snapshot(
        AgentIdentity(agent_id="DebugAgent"), {}, seed_mock=False
    )
    assert [ctx.preview for ctx in seen] == [True]

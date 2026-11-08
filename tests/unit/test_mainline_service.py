"""主線怎麼組（3d PR 1，ADR-0305）：網頁、IDE、群聊、心跳都呼叫同一個函式。

| 誰送出 | 前情 | 家原文 |
|---|---|---|
| 網頁版家本身（送完整紀錄） | 帶 | 不帶 |
| 家，但 IDE、SNS 家模式（只送最新一句） | 帶 | 帶 |
| 子對話（網頁、IDE） | 帶 | 帶，每則內容前標時間 |
| 群聊 | 帶 | 照舊帶 |
| 心跳（在家或子對話） | 帶 | 不帶 |
| 無痕 | 不帶 | 不帶 |

另外：對話自己的舊摘要（家與子對話一樣）只在這一輪前情原文照登同一段時不插，截短過、被折掉、
被合併時照插（審查第 2 輪 G1）。上下文預覽不搬舊摘要（不寫資料）；每輪只讀一次家，讀家碰到儲存錯誤
這一輪照舊失敗（心跳照帶前情）；沒有對話編號的請求不當成子對話（不標時間）。
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
from src.services import soul_service as soul_service_module
from src.services.proxy_service import pipeline
from src.utils import async_db
from src.utils import soul_prelude_db as db_mod
from src.utils.soul_prelude_db import PreludeSegment
from src.utils.token_estimator import estimate_messages_tokens

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
        "web-home",
        "ide-home",
        "web-child",
        "ide-child",
        "group",
        "hb-home",
        "hb-child",
        "incognito",
        "no-session",
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
        request, A, mode="chat", session_id="home2", sends_own_history=True, own_summary=True
    )
    system = _system(request)
    assert "家的舊摘要" in system
    assert "<conversation_summary>" not in system


async def test_child_keeps_its_own_old_summary_block():
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="child", own_summary=True
    )
    assert "<conversation_summary>" in _system(request)
    assert "子對話自己的舊摘要" in _system(request)


async def test_prelude_failure_does_not_block_the_turn(monkeypatch, caplog):
    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mainline_service, "prelude_view", broken)
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
    await mainline_service.inject_mainline(request, A, mode="chat", session_id=None, preview=True)
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
        request, A, mode="chat", session_id="old", sends_own_history=True, own_summary=True
    )
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


async def _broken_home(_identity):
    raise async_db.SessionPersistenceError


@pytest.mark.parametrize("mode", ["chat", "group"])
async def test_storage_error_reading_home_fails_the_turn(monkeypatch, mode):
    """讀家碰到儲存錯誤：和以前一樣讓這一輪失敗，不靜默少掉家原文（ADR-0078；審查第 2 輪 G2）."""
    monkeypatch.setattr(async_db.session_service, "read_home_snapshot", _broken_home)
    with pytest.raises(async_db.SessionPersistenceError):
        await mainline_service.inject_mainline(_request(), A, mode=mode, session_id="child")


async def test_storage_error_is_not_retried_with_a_second_read(monkeypatch):
    """讀家碰到儲存錯誤就讓這一輪失敗、不讀第二次（第二次讀到時前情與時間標記會用錯的家，H3）."""
    real = async_db.session_service.read_home_snapshot
    calls: list[str] = []

    async def flaky(identity):
        calls.append(identity.agent_id)
        if len(calls) == 1:
            raise async_db.SessionPersistenceError
        return await real(identity)

    monkeypatch.setattr(async_db.session_service, "read_home_snapshot", flaky)
    with pytest.raises(async_db.SessionPersistenceError):
        await mainline_service.inject_mainline(_request(), A, mode="chat", session_id="home")
    assert calls == [A.agent_id]


async def test_two_homes_keep_the_prelude_and_send_the_turn():
    """既有資料有兩個家（範圍矛盾）：當作沒有家，前情照帶、沒有家原文，照樣送出（第 3 輪 H5）."""
    async with async_db.get_async_db() as db:  # set_home 不會造出兩個家，直接改資料庫
        await db.execute(
            "UPDATE user_sessions SET is_home = 1, home_for_agent = ? WHERE id = 'child'",
            (A.agent_id,),
        )
        await db.commit()
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="child")
    assert "前情的一段" in _system(request)
    assert "家裡的問題" not in "\n".join(_dialogue(request))


async def test_heartbeat_keeps_its_prelude_when_home_is_unreadable(monkeypatch):
    """心跳以前不讀家：讀家失敗時當作沒有家，照樣帶前情（審查第 2 輪 G2）."""
    monkeypatch.setattr(async_db.session_service, "read_home_snapshot", _broken_home)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="heartbeat", session_id="child")
    assert "前情的一段" in _system(request)


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


# ── 審查第 2 輪的修正 ───────────────────────────────────────────────────────


async def _scope_zero(session_id: str) -> None:
    await async_db.session_service.set_home(session_id, A, True)
    async with async_db.get_async_db() as db:
        await db.execute(
            "UPDATE user_sessions SET session_scope_version = 0 WHERE id = ?", (session_id,)
        )
        await db.commit()


async def test_old_home_summary_reaches_the_model():
    """2026-10-17 以前的家（範圍版本 0）：舊摘要搬進前情、模型看得到（審查第 2 輪 G1）."""
    await seed("old-home", context_summary="老家的舊摘要")
    await _scope_zero("old-home")
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="old-home", sends_own_history=True, own_summary=True
    )
    assert _system(request).count("老家的舊摘要") == 1


async def test_home_summary_folded_out_is_still_given_to_the_home(monkeypatch):
    """前情超過上限、舊摘要被折掉：家自己的舊摘要照插，不會兩邊都看不到（審查第 2 輪 G1、G7）."""
    monkeypatch.setattr(
        mainline_service,
        "load_sliding_window_config",
        lambda: SlidingWindowConfig(prelude_max_tokens=500),
    )
    await seed("home3", context_summary="被折掉的舊摘要")
    assert await async_db.session_service.set_home("home3", A, True)
    for i in range(12):
        await db_mod.insert_segment(
            PreludeSegment(
                agent=A.agent_id,
                kind="home_trim",
                source_session="home3",
                dedupe_key=f"t{i}",
                content="剪裁" + "字" * 200,
                created_at=10 + i,
            )
        )
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="home3", sends_own_history=True, own_summary=True
    )
    system = _system(request)
    assert "<conversation_summary>" in system and "被折掉的舊摘要" in system


async def test_cut_summary_is_given_in_full_in_its_own_conversation():
    """搬家時截短的舊摘要：回到那段對話時照插全文（審查第 2 輪 G5）."""
    long_summary = "字" * 3_000 + "結尾標記"
    await seed("long", context_summary=long_summary)
    assert await async_db.session_service.set_home("long", A, True)
    await mainline_service.inject_mainline(_request(), A, mode="chat", session_id="long")
    assert await async_db.session_service.set_home("home", A, True)
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode="chat", session_id="long", sends_own_history=True, own_summary=True
    )
    system = _system(request)
    assert "<conversation_summary>" in system and "結尾標記" in system


# --- 換家後、交接完成前：還沒交接完的舊家原文（3d PR 2a，ADR-0306） ---


OLD_SOURCE = 'source="old_home_pending_handover"'


async def _move_home_to_new():
    await seed("new", tree={"messages": {}, "currentId": None})
    assert await async_db.session_service.set_home("new", A, True)


@pytest.mark.parametrize(
    ("mode", "session_id", "sends_own", "old"),
    [
        ("chat", "new", True, True),  # 網頁版新家本身：第一輪就接得上
        ("chat", "child", True, True),
        ("chat", "child", False, True),
        ("group", None, False, True),
        ("heartbeat", "new", True, True),
        ("heartbeat", "child", True, False),
        ("incognito", "child", True, False),
    ],
    ids=["web-new-home", "web-child", "ide-child", "group", "hb-home", "hb-child", "incognito"],
)
async def test_unfinished_old_home_originals_follow_the_mainline(mode, session_id, sends_own, old):
    await _move_home_to_new()
    request = _request()
    await mainline_service.inject_mainline(
        request, A, mode=mode, session_id=session_id, sends_own_history=sends_own
    )
    joined = "\n".join(_dialogue(request))
    assert ("家裡的問題" in joined) is old
    assert (OLD_SOURCE in joined) is old
    if old:
        assert "2023" in joined  # 舊家原文每則標時間
    assert _dialogue(request)[-1] == "這一輪"


async def test_old_home_originals_come_before_the_current_home():
    await seed(
        "new",
        tree=HOME_TREE
        | {
            "messages": {
                "n": {"id": "n", "role": "user", "content": "新家的話", "parentId": None},
            },
            "currentId": "n",
        },
    )
    assert await async_db.session_service.set_home("new", A, True)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="child")
    joined = "\n".join(_dialogue(request))
    assert joined.index("家裡的問題") < joined.index("新家的話")


def _long_tree(count: int) -> dict:
    nodes = {}
    for i in range(count):
        nodes[f"m{i}"] = {
            "id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"長舊家第{i}則" + "字" * 40,
            "parentId": f"m{i - 1}" if i else None,
            "timestamp": 1_700_000_000_000 + i,
        }
    return {"messages": nodes, "currentId": f"m{count - 1}"}


async def test_long_old_homes_only_send_their_tails_within_the_budget(monkeypatch):
    """舊家原文有總量上限：新的舊家優先、只帶尾端，前面寫一行說明（審查 J5）."""
    monkeypatch.setattr(soul_service_module, "OLD_HOME_TOKEN_BUDGET", 400)
    await seed("long", tree=_long_tree(60))
    assert await async_db.session_service.set_home("long", A, True)
    await async_db.session_service.check_old_home("long", mark_started=True, requester=None)
    await _move_home_to_new()  # 舊家依序：home、long（較新）
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="child")
    old = [m for m in request.messages[1:-1] if "長舊家" in str(_field(m, "content"))]
    joined = "\n".join(str(_field(m, "content")) for m in old)
    assert "長舊家第59則" in joined
    assert "長舊家第0則" not in joined
    assert "前面還有" in joined
    assert "家裡的問題" not in "\n".join(_dialogue(request))  # 額度用完：較舊的整段不帶
    assert 0 < len(old) < 60
    assert estimate_messages_tokens(
        [{"content": f"長舊家第{i}則" + "字" * 40} for i in range(60 - len(old), 60)]
    ) <= 400
    assert joined.index(OLD_SOURCE) < joined.index("前面還有")  # 說明在歷史資料界線裡面


async def _long_old_home_then_new(count: int = 60) -> None:
    await seed("long", tree=_long_tree(count))
    assert await async_db.session_service.set_home("long", A, True)
    await async_db.session_service.check_old_home("long", mark_started=True, requester=None)
    await _move_home_to_new()


@pytest.mark.parametrize("budget", [300, 350, 400, 450, 500, 550])
async def test_the_first_message_sent_is_always_the_users(monkeypatch, budget):
    """截短後第一則不能是 AI 的回覆：Claude 規定第一則要是使用者（第 2 輪審查 K3）."""
    monkeypatch.setattr(soul_service_module, "OLD_HOME_TOKEN_BUDGET", budget)
    await _long_old_home_then_new()
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="new")
    sent = [m for m in request.messages if _field(m, "role") != "system"]
    assert _field(sent[0], "role") == "user"
    assert "長舊家第59則" in "\n".join(_dialogue(request))
    # 較舊那段放不下時前面多一行說明（使用者角色）；截下來的這段自己也要從使用者開始
    tail = next(m for m in sent if "長舊家" in str(_field(m, "content")))
    assert _field(tail, "role") == "user"


async def test_an_old_home_too_long_for_the_budget_still_leaves_a_note(monkeypatch):
    """最後一則就放不下：那段改送一行說明（在界線裡），不讓新家完全不知道有舊家（K4）."""
    monkeypatch.setattr(soul_service_module, "OLD_HOME_TOKEN_BUDGET", 20)
    await _long_old_home_then_new(4)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="new")
    sent = [m for m in request.messages if _field(m, "role") != "system"]
    joined = "\n".join(_dialogue(request))
    assert "長舊家" not in joined
    assert "4 則" in joined and "原文留在舊家" in joined
    assert joined.index(OLD_SOURCE) < joined.index("4 則")
    assert _field(sent[0], "role") == "user"
    assert _dialogue(request)[-1] == "這一輪"


async def test_a_finished_handover_no_longer_sends_the_old_home():
    await _move_home_to_new()
    async with async_db.get_async_db() as db:
        await db.execute("UPDATE soul_home_transitions SET status = 'done'")
        await db.commit()
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="child")
    assert "家裡的問題" not in "\n".join(_dialogue(request))


async def test_an_old_home_that_is_home_again_is_sent_as_the_home():
    await _move_home_to_new()
    await async_db.session_service.check_old_home("new", mark_started=True, requester=None)
    assert await async_db.session_service.set_home("home", A, True)
    request = _request()
    await mainline_service.inject_mainline(request, A, mode="chat", session_id="child")
    joined = "\n".join(_dialogue(request))
    assert joined.count("家裡的問題") == 1
    assert 'source="home_conversation"' in joined


@pytest.mark.parametrize("mode", ["chat", "heartbeat"])
async def test_unreadable_old_homes_fail_the_turn_except_heartbeat(monkeypatch, mode):
    async def broken(_identity):
        raise async_db.SessionPersistenceError

    monkeypatch.setattr(async_db.session_service, "read_unfinished_old_homes", broken)
    request = _request()
    call = mainline_service.inject_mainline(request, A, mode=mode, session_id="home")
    if mode == "chat":
        with pytest.raises(async_db.SessionPersistenceError):
            await call
    else:
        await call
        assert "<soul_prelude>" in _system(request)

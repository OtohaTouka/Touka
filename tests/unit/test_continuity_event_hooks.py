"""可信活動事件的掛點：只在事情真的做成之後由系統記一筆。

- SoulLayer：人類送來、已保存、屬於這個靈魂的一輪記 user_turn。
- 心跳迴圈：in-flight 標記寫入後記開始；finally 記結束（脈衝數、輪數、結束原因）。
- mission_tools：建板、狀態轉換、全部完成、驗收成功後才記；非法轉換與結果不明不記；
  跨看板記在動手的靈魂名下並標看板擁有者；無痕、子代理不記。
- 記憶：存、忘、一步取代在事實已提交後才記；被擋或失敗不記；事件迴圈與工作執行緒都寫得進去；
  記憶閘道在無痕裡不記。
- recorder 壞掉時工具回傳逐字不變（禁令 59）。

全部在 tmp 資料庫、tmp 事實庫與自動規格的向量替身；任務看板寫在測試沙盒；不碰 ``data/``。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, create_autospec, patch

import pytest
from memory_server import factual as fm
from memory_server import tools as memory_gateway
from memory_server.services.manager import MemoryManager
from memory_server.services.mission_manager import MissionManager, MissionPersistenceError
from memory_server.vector import VectorStore
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers import soul as soul_mod
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import heartbeat as hb
from src.services import continuity_events as ce
from src.tools.builtin import memory as proxy_memory_gateway
from src.tools.mission_tools import mission_tools
from src.utils import task_utils
from src.utils.async_db import init_db_async, session_service
from src.utils.continuity_events_db import list_agent_events
from src.utils.history_manager import TreeMessageManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

HUMAN = AgentIdentity.for_role(agent_id="t23-hook-human", role="admin", is_human=True)
FACT_ID = re.compile(r"ID:\s*([^)]+)")
_real_sleep = asyncio.sleep


def _soul(prefix: str = "T23Hook", *capabilities: str) -> AgentIdentity:
    return AgentIdentity.for_role(
        agent_id=f"{prefix}{uuid.uuid4().hex[:8]}", role="Owner", capabilities=list(capabilities)
    )


@pytest.fixture(autouse=True)
async def _tmp_db_and_recorder(tmp_path: Path) -> AsyncIterator[None]:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        ce.start_recording()
        try:
            yield
        finally:
            ce.stop_recording()


async def drain() -> None:
    loop = asyncio.get_running_loop()
    for _ in range(20):
        await _real_sleep(0.01)
        pending = [
            task for task in list(task_utils._background_tasks)
            if not task.done() and task.get_loop() is loop
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _kinds(identity: AgentIdentity) -> list[str]:
    await drain()
    return [event["kind"] for event in await list_agent_events(identity)]


async def _events(identity: AgentIdentity) -> list[dict[str, Any]]:
    await drain()
    return await list_agent_events(identity)


# --------------------------------------------------------------------------- #
# SoulLayer：user_turn
# --------------------------------------------------------------------------- #


def _stub_soul_layer(monkeypatch: pytest.MonkeyPatch, soul: AgentIdentity) -> None:
    monkeypatch.setattr(soul_mod.soul_service, "get_soul_data", lambda _a: ("SOUL", {}))
    monkeypatch.setattr(soul_mod.soul_service, "apply_soul_to_request", lambda *_a: None)
    monkeypatch.setattr(soul_mod.soul_service, "inject_home_messages", AsyncMock())
    monkeypatch.setattr(soul_mod.soul_service, "inject_context_summary", AsyncMock())
    monkeypatch.setattr(soul_mod, "get_active_brain", lambda _identity: "claude-sonnet-4-5")
    monkeypatch.setattr(
        soul_mod.agent_service,
        "identify_agent",
        lambda aid: {"agent_id": aid, "role": "Owner", "is_toukasoul": True},
    )
    # SoulLayer 傳沒有角色的身分給 recorder；沙盒裡沒有靈魂目錄，這裡認得這一位
    monkeypatch.setattr(
        ce.soul_service, "is_toukasoul", lambda identity: identity.agent_id == soul.agent_id
    )


async def test_soul_layer_records_the_saved_human_turn_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul()
    _stub_soul_layer(monkeypatch, soul)
    sid = f"t23-{uuid.uuid4().hex[:8]}"
    assert await session_service.upsert_session(
        sid, "t", TreeMessageManager.create_empty_tree(), identity=soul
    )
    request = OpenAIChatRequest(
        model=soul.agent_id,
        messages=[OpenAIMessage(role="user", content="hi")],
        session_id=sid,
    )
    ctx = PipelineContext(
        is_toukasoul=True,
        agent_id=soul.agent_id,
        session_id=sid,
        identity=soul,
        source_message_id="msg-human-1",
        ui_requester=HUMAN,
    )

    await soul_mod.SoulLayer().process(request, ctx)
    await soul_mod.SoulLayer().process(request, ctx)  # 同一則訊息重來一次

    (event,) = await _events(soul)
    assert (event["kind"], event["subject"], event["session_id"]) == (
        "user_turn",
        "msg-human-1",
        sid,
    )


# --------------------------------------------------------------------------- #
# 心跳：開始與結束
# --------------------------------------------------------------------------- #


def _autonomy(*, quota: bool = True, over_budget: bool = False) -> MagicMock:
    service = MagicMock()
    service.check_quota.return_value = quota
    service.is_over_budget.return_value = over_budget
    return service


async def _run_loop(
    monkeypatch: pytest.MonkeyPatch,
    soul: AgentIdentity,
    pulse: Any,
    *,
    autonomy: MagicMock | None = None,
    pulse_count: int = 1,
    interrupted: bool = False,
) -> str:
    sid = f"t23-hb-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(hb, "_execute_single_pulse", pulse)
    monkeypatch.setattr(hb, "autonomy_service", autonomy or _autonomy())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    monkeypatch.setattr(hb, "session_service", MagicMock(append_message_to_session=AsyncMock()))
    rhythm = MagicMock()
    rhythm.is_interrupted.return_value = interrupted
    monkeypatch.setattr(hb, "rhythm_service", rhythm)
    await hb._process_heartbeat_loop(
        final_session_id=sid,
        identity=soul,
        model_override=None,
        ctx=PipelineContext(heartbeat_run=True, source_message_id="trigger-node"),
        pulse_count=pulse_count,
        pulse_slots=1,
        t0=1_760_000_000.5,
        init_prompt="",
        is_home_mode=False,
    )
    return sid


async def _pulse_ok(*_args: Any, **_kwargs: Any) -> int:
    return 2


async def _pulse_no_model(*_args: Any, **_kwargs: Any) -> None:
    return None


class _SyntheticPulseFailure(RuntimeError):
    """A pulse failure raised by the double below."""


async def _pulse_error(*_args: Any, **_kwargs: Any) -> int:
    raise _SyntheticPulseFailure


@pytest.mark.parametrize(
    ("pulse", "autonomy", "interrupted", "end", "done", "turns"),
    [
        (_pulse_ok, None, False, "completed", 1, 2),
        (_pulse_ok, None, True, "interrupted", 0, 0),
        (_pulse_ok, _autonomy(quota=False), False, "quota", 0, 0),
        (_pulse_ok, _autonomy(over_budget=True), False, "budget", 0, 0),
        (_pulse_no_model, None, False, "no_model", 0, 0),
        (_pulse_error, None, False, "error", 0, 0),
    ],
)
async def test_heartbeat_run_records_start_and_end(
    monkeypatch: pytest.MonkeyPatch,
    pulse: Any,
    autonomy: MagicMock | None,
    interrupted: bool,
    end: str,
    done: int,
    turns: int,
) -> None:
    soul = _soul()
    sid = await _run_loop(
        monkeypatch, soul, pulse, autonomy=autonomy, interrupted=interrupted
    )
    events = await _events(soul)  # 整個 run 只有這兩筆（沒有 user_turn）；同一毫秒時順序不定
    assert sorted(e["kind"] for e in events) == ["heartbeat_finished", "heartbeat_started"]
    started, finished = sorted(events, key=lambda e: e["kind"] != "heartbeat_started")
    run_key = f"{sid}:1760000000500"
    assert (started["kind"], started["subject"], started["session_id"]) == (
        "heartbeat_started",
        run_key,
        sid,
    )
    assert started["metadata"] == {"pulses_total": 1, "home": False}
    assert (finished["kind"], finished["subject"]) == ("heartbeat_finished", run_key)
    assert finished["metadata"] == {
        "pulses_total": 1,
        "pulses_done": done,
        "turns": turns,
        "end": end,
    }


async def test_cancelled_heartbeat_still_records_its_end(monkeypatch: pytest.MonkeyPatch) -> None:
    soul = _soul()
    entered = asyncio.Event()

    async def _hang(*_args: Any, **_kwargs: Any) -> int:
        entered.set()
        await asyncio.Event().wait()
        return 0

    task = asyncio.ensure_future(_run_loop(monkeypatch, soul, _hang))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ends = {e["kind"]: e["metadata"].get("end") for e in await _events(soul)}
    assert ends == {"heartbeat_started": None, "heartbeat_finished": "cancelled"}


async def test_heartbeat_that_never_started_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul()
    autonomy = _autonomy()
    autonomy.mark_heartbeat_in_flight.side_effect = OSError("in-flight marker not written")
    await _run_loop(monkeypatch, soul, _pulse_ok, autonomy=autonomy)
    assert await _kinds(soul) == []


# --------------------------------------------------------------------------- #
# mission_tools
# --------------------------------------------------------------------------- #


async def _mission(identity: AgentIdentity, action: str, **arguments: Any) -> Any:
    arguments.setdefault("_ctx", PipelineContext(session_id="t23-mission"))
    return await mission_tools(action=action, identity=identity, **arguments)


async def _board(identity: AgentIdentity, **arguments: Any) -> None:
    reply = await _mission(
        identity,
        "initialize",
        goal="t23 goal text that must never be recorded",
        tasks=[{"task_id": "T1", "description": "secret description"}],
        **arguments,
    )
    assert reply.startswith("✅ Mission Initialized"), reply


async def test_mission_lifecycle_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    soul = _soul()
    await _board(soul)
    for status in ("DOING", "DONE"):
        reply = await _mission(soul, "update_status", task_id="T1", status=status)
        assert reply == f"✅ Task 'T1' updated to {status}."
    for _ in range(2):
        reply = await _mission(soul, "finalize", verdict="looks good")
        assert reply.startswith("✅ Mission finalized")

    events = await _events(soul)
    by_kind = {e["kind"]: e for e in events}  # 同一次呼叫的兩筆可能同一毫秒，順序不定
    assert sorted(by_kind) == sorted(
        ["mission_started", "task_doing", "task_done", "mission_completed", "mission_finalized"]
    )
    assert len(events) == len(by_kind)  # 兩次 finalize 只留一筆
    mission_id = by_kind["mission_started"]["subject"]
    assert by_kind["mission_started"]["metadata"] == {"tasks": 1}
    assert by_kind["task_doing"]["subject"] == f"{mission_id}/T1"
    assert by_kind["task_doing"]["metadata"] == {"mission_id": mission_id, "task_id": "T1"}
    assert by_kind["mission_completed"]["subject"] == mission_id
    assert {e["session_id"] for e in events} == {"t23-mission"}
    dumped = repr(events)
    assert "goal text" not in dumped and "secret description" not in dumped
    assert "looks good" not in dumped


async def test_repeated_transitions_are_all_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(range(1_760_000_000_000, 1_760_000_001_000))
    monkeypatch.setattr(ce, "get_current_epoch_ms", lambda: next(ticks))  # 每次轉換不同毫秒
    soul = _soul()
    await _board(soul)
    for status in ("DOING", "FAILED", "TODO", "DOING", "FAILED"):
        await _mission(soul, "update_status", task_id="T1", status=status)
    assert await _kinds(soul) == [
        "mission_started",
        "task_doing",
        "task_failed",
        "task_todo",
        "task_doing",
        "task_failed",
    ]


async def test_failed_or_unconfirmed_updates_record_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul()
    await _board(soul)
    invalid = await _mission(soul, "update_status", task_id="T1", status="DONE")
    blocked = await _mission(soul, "update_status", task_id="T1", status="BLOCKED")
    assert "Error" in invalid and "Error" in blocked
    monkeypatch.setattr(
        MissionManager, "update_task", AsyncMock(side_effect=MissionPersistenceError())
    )
    unknown = await _mission(soul, "update_status", task_id="T1", status="DOING")
    assert "unknown" in unknown
    assert await _kinds(soul) == ["mission_started"]


@pytest.mark.parametrize("flag", ["incognito", "subagent_run", "dispatch_run"])
async def test_mission_events_skip_incognito_and_subagents(flag: str) -> None:
    soul = _soul()
    ctx = PipelineContext(session_id="t23-skip", **{flag: True})
    await _board(soul, _ctx=ctx)
    await _mission(soul, "update_status", task_id="T1", status="DOING", _ctx=ctx)
    assert await _kinds(soul) == []


async def test_cross_board_update_is_recorded_under_the_acting_soul() -> None:
    supervisor = _soul("T23Super", "can_supervise_dispatch")
    dispatch = AgentIdentity.for_role(agent_id="DispatchAgent", role="Base")
    await _board(supervisor, target_agent_id="DispatchAgent")
    await _mission(
        supervisor, "update_status", task_id="T1", status="DOING", target_agent_id="DispatchAgent"
    )

    events = await _events(supervisor)
    assert [e["kind"] for e in events] == ["mission_started", "task_doing"]
    assert all(e["metadata"]["board_owner"] == "DispatchAgent" for e in events)
    assert all(e["event_id"].split(":")[1] == supervisor.agent_id for e in events)
    assert await list_agent_events(dispatch) == []


async def test_mission_reply_is_unchanged_when_the_recorder_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul()
    monkeypatch.setattr(ce, "insert_event", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(ce, "get_current_epoch_ms", Mock(side_effect=OSError("clock boom")))
    await _board(soul)
    reply = await _mission(soul, "update_status", task_id="T1", status="DOING")
    assert reply == "✅ Task 'T1' updated to DOING."


# --------------------------------------------------------------------------- #
# 記憶
# --------------------------------------------------------------------------- #


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> MemoryManager:
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


@pytest.fixture
def vector(monkeypatch: pytest.MonkeyPatch, manager: MemoryManager) -> Any:
    stub = create_autospec(VectorStore, instance=True)
    stub.query_vectors.return_value = []
    stub.save_vector.return_value = True
    stub.archive_vector_by_fact_id.return_value = True
    stub.agent_point_count.return_value = 0
    stub.indexed_fact_ids.return_value = set()
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", stub)
    return stub


@pytest.fixture
def store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager
) -> fm.FactualStore:
    factual = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


async def _memory(action: str, identity: AgentIdentity, **arguments: Any) -> str:
    return await proxy_memory_gateway.memory_tools(action=action, identity=identity, **arguments)


def _fact(reply: str) -> str:
    match = FACT_ID.search(reply)
    assert match is not None, reply
    return match.group(1).strip()


@pytest.mark.parametrize("vector_ok", [True, False])
async def test_saved_memory_is_recorded_after_commit(
    store: fm.FactualStore, vector: Any, vector_ok: bool
) -> None:
    soul = _soul()
    vector.save_vector.return_value = vector_ok
    fact_id = _fact(await _memory("save", soul, content="I like green tea"))
    events = await _events(soul)
    assert [(e["kind"], e["subject"]) for e in events] == [("memory_saved", fact_id)]
    assert "green tea" not in repr(events)


async def test_blocked_or_failed_saves_record_nothing(
    store: fm.FactualStore, vector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul()
    await _memory("save", soul, content="only once")
    duplicate = await _memory("save", soul, content="only once")
    assert "重複內容" in duplicate
    monkeypatch.setattr(store, "save_fact", Mock(return_value={"status": "error", "message": "x"}))
    failed = await _memory("save", soul, content="never stored")
    assert "存儲失敗" in failed
    assert await _kinds(soul) == ["memory_saved"]


async def test_forget_records_only_a_real_archive(store: fm.FactualStore, vector: Any) -> None:
    soul = _soul()
    fact_id = store.save_fact(soul, "to be forgotten")["id"]
    assert "archived" in await _memory("forget", soul, fact_id_to_archive=fact_id)
    assert "not found" in await _memory("forget", soul, fact_id_to_archive="fact_missing")
    events = await _events(soul)
    assert [(e["kind"], e["subject"]) for e in events] == [("memory_archived", fact_id)]


async def test_supersede_records_one_event_with_the_new_id(
    store: fm.FactualStore, vector: Any
) -> None:
    soul = _soul()
    old = store.save_fact(soul, "I live in Taipei")["id"]
    reply = await _memory(
        "manage_dups", soul, operation="supersede", target_id=old, content="I moved to Tainan"
    )
    new = _fact(reply)
    missing = await _memory(
        "manage_dups", soul, operation="supersede", target_id="fact_missing", content="x y z"
    )
    assert "not one of your active memories" in missing
    events = await _events(soul)
    assert [(e["kind"], e["subject"], e["metadata"]) for e in events] == [
        ("memory_superseded", old, {"new_fact_id": new})
    ]


async def test_resolve_in_a_worker_thread_is_recorded(
    store: fm.FactualStore, vector: Any, manager: MemoryManager
) -> None:
    soul = _soul()
    fact_id = store.save_fact(soul, "archived from a thread")["id"]
    reply = await asyncio.to_thread(manager.resolve_duplicate, soul, fact_id)
    assert "archived" in reply
    assert await _kinds(soul) == ["memory_archived"]


async def test_gateway_records_nothing_in_incognito(store: fm.FactualStore, vector: Any) -> None:
    soul = _soul()
    reply = await _memory("save", soul, content="incognito note", _ctx={"incognito": True})
    assert "successfully saved" in reply  # 記憶照存（工具行為不變），只是不記事件
    assert await _kinds(soul) == []


async def test_memory_replies_are_unchanged_when_the_recorder_breaks(
    store: fm.FactualStore, vector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul()
    monkeypatch.setattr(ce, "get_current_epoch_ms", Mock(side_effect=OSError("clock boom")))
    saved = await _memory("save", soul, content="still saved")
    assert saved.startswith("Memory successfully saved (ID: ")
    fact_id = _fact(saved)
    assert await _memory("forget", soul, fact_id_to_archive=fact_id) == (
        f"✅ Memory {fact_id} has been archived."
    )

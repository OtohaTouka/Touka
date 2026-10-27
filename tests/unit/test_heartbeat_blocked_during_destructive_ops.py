"""刪除、重置、記憶清除期間擋住這個靈魂的心跳（舊審查報告丙-4，ADR-0287）。

破壞性操作把目標登記在 `destructive_ops`；心跳迴圈開跑前與每個脈衝前看這個標記，
看到就停、不寫 `heartbeat_finished`、不剪裁。迴圈也把自己登記在那裡，剪裁做完才拿掉，
刪除靠它判斷「這個靈魂還有沒有心跳在跑」。全部離線：脈衝、健康檢查、事件、剪裁都換掉。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import heartbeat as hb
from src.services import destructive_ops
from src.services.rhythm_service import rhythm_service

AGENT = "Alice"
IDENTITY = AgentIdentity.for_role(agent_id=AGENT, role="Owner")


def _who(name: str) -> AgentIdentity:
    return AgentIdentity(agent_id=name)


@pytest.fixture
def loop_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    env = SimpleNamespace(
        started=MagicMock(),
        finished=MagicMock(),
        in_flight=MagicMock(),
        trim=AsyncMock(),
        pulses=[],
        during_pulse=None,
    )

    async def pulse(final_session_id, *_args, **_kwargs):
        env.pulses.append(destructive_ops.running_heartbeats(_who(AGENT)))
        if env.during_pulse is not None:
            env.during_pulse()
        return 1

    monkeypatch.setattr(hb, "_execute_single_pulse", pulse)
    monkeypatch.setattr(hb, "record_heartbeat_started", env.started)
    monkeypatch.setattr(hb, "record_heartbeat_finished", env.finished)
    monkeypatch.setattr(hb.autonomy_service, "mark_heartbeat_in_flight", env.in_flight)
    monkeypatch.setattr(hb.sliding_window_service, "check_and_trim", env.trim)
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    return env


async def _run(session_id: str, *, pulses: int = 2) -> None:
    try:
        await hb._process_heartbeat_loop(
            final_session_id=session_id,
            identity=IDENTITY,
            model_override=None,
            ctx=PipelineContext(),
            pulse_count=pulses,
            pulse_slots=1,
            t0=0.0,
            init_prompt="",
            is_home_mode=True,
        )
    finally:
        rhythm_service.clear_interrupt(session_id)


# ── 標記本身 ─────────────────────────────────────────────────────────────────


def test_hold_counts_each_operation_and_ignores_case():
    destructive_ops.hold(_who("Alice"))
    destructive_ops.hold(_who("alice"))  # 重置和它後面的清除各算一次
    try:
        assert destructive_ops.is_held(_who("ALICE"))
        destructive_ops.release(_who("Alice"))
        assert destructive_ops.is_held(_who("Alice"))  # 還有一個沒結束
    finally:
        destructive_ops.release(_who("alice"))
    assert not destructive_ops.is_held(_who("Alice"))
    assert not destructive_ops.is_held(_who("Bob"))


def test_a_heartbeat_cannot_start_while_its_soul_is_held():
    destructive_ops.hold(_who("Alice"))
    try:
        assert destructive_ops.start_heartbeat(_who("alice"), "s-held") is False
        assert destructive_ops.running_heartbeats(_who("Alice")) == []
        assert destructive_ops.start_heartbeat(_who("Bob"), "s-other") is True  # 別的靈魂照跑
    finally:
        destructive_ops.release(_who("Alice"))
        destructive_ops.end_heartbeat(_who("Bob"), "s-other")


def test_stop_interrupts_every_running_heartbeat_of_that_soul_only():
    assert destructive_ops.start_heartbeat(_who("Alice"), "s-1")
    assert destructive_ops.start_heartbeat(_who("alice"), "s-2")
    assert destructive_ops.start_heartbeat(_who("Bob"), "s-3")
    try:
        assert destructive_ops.stop_heartbeats(_who("ALICE")) == ["s-1", "s-2"]
        assert rhythm_service.is_interrupted("s-1")
        assert rhythm_service.is_interrupted("s-2")
        assert not rhythm_service.is_interrupted("s-3")
    finally:
        for agent, sid in (("Alice", "s-1"), ("Alice", "s-2"), ("Bob", "s-3")):
            destructive_ops.end_heartbeat(_who(agent), sid)
            rhythm_service.clear_interrupt(sid)
    assert destructive_ops.running_heartbeats(_who("Alice")) == []


def test_two_runs_in_one_session_are_tracked_separately():
    assert destructive_ops.start_heartbeat(_who("Alice"), "home")
    assert destructive_ops.start_heartbeat(_who("Alice"), "home")
    destructive_ops.end_heartbeat(_who("Alice"), "home")
    try:
        assert destructive_ops.running_heartbeats(_who("Alice")) == ["home"]  # 另一次還在跑
    finally:
        destructive_ops.end_heartbeat(_who("Alice"), "home")
    assert destructive_ops.running_heartbeats(_who("Alice")) == []


# ── 心跳迴圈 ─────────────────────────────────────────────────────────────────


async def test_the_loop_does_not_start_while_its_soul_is_held(loop_env):
    destructive_ops.hold(_who(AGENT))
    try:
        await _run("hb-held")
    finally:
        destructive_ops.release(_who(AGENT))
    assert loop_env.pulses == []
    loop_env.in_flight.assert_not_called()  # 什麼都沒寫
    loop_env.started.assert_not_called()
    loop_env.finished.assert_not_called()
    loop_env.trim.assert_not_called()
    assert destructive_ops.running_heartbeats(_who(AGENT)) == []


async def test_the_loop_stops_before_the_next_pulse_once_held(loop_env):
    loop_env.during_pulse = lambda: destructive_ops.hold(_who(AGENT))
    try:
        await _run("hb-mid", pulses=3)
    finally:
        destructive_ops.release(_who(AGENT))
    assert len(loop_env.pulses) == 1
    loop_env.started.assert_called_once()
    loop_env.finished.assert_not_called()  # 重置或刪除中，結束事件不寫
    loop_env.trim.assert_not_called()  # 也不剪裁（剪裁會寫舊對話庫）


async def test_the_loop_stays_registered_until_trimming_ends(loop_env):
    seen_while_trimming: list[list[str]] = []
    loop_env.trim.side_effect = lambda *_a: seen_while_trimming.append(
        destructive_ops.running_heartbeats(_who(AGENT))
    )
    await _run("hb-trim")
    assert loop_env.pulses == [["hb-trim"], ["hb-trim"]]
    assert seen_while_trimming == [["hb-trim"]]  # 剪裁時刪除會看到它在跑
    loop_env.finished.assert_called_once()
    assert destructive_ops.running_heartbeats(_who(AGENT)) == []


async def test_a_stop_during_startup_is_not_cleared(loop_env, monkeypatch):
    async def stop_now() -> None:
        rhythm_service.interrupt_session("hb-startup")

    monkeypatch.setattr(hb.model_manager, "check_models_health", stop_now)
    await _run("hb-startup")
    assert loop_env.pulses == []  # 開跑後才叫停，不會被起點的清旗標吃掉
    loop_env.finished.assert_called_once()

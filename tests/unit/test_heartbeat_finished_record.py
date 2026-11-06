"""心跳的結束紀錄（ADR-0300）：側欄靠它補上您沒在看時跑完的對話。

只要開跑了就要記（第一輪前就停、失敗、被叫停都算），而且要等剪裁做完才記：
前端看到紀錄就會下載那段對話，太早記會下載到剪裁前的內容。
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.routers import heartbeat as hb
from src.services import destructive_ops
from src.services.activity_registry import activity_registry
from src.services.autonomy import autonomy_service

_ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")


def _finished() -> list[str]:
    return [record["session_id"] for record in activity_registry.finished_since(0)]


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    activity_registry.reload_all()
    monkeypatch.setattr(hb.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(hb.model_manager, "check_models_health", AsyncMock())
    yield
    activity_registry.reload_all()


async def _run(sid: str, pulses: int = 1, *, is_home_mode: bool = False) -> None:
    await hb._process_heartbeat_loop(
        final_session_id=sid,
        identity=_ALICE,
        model_override=None,
        ctx=PipelineContext(),
        pulse_count=pulses,
        pulse_slots=1,
        t0=0.0,
        init_prompt="",
        is_home_mode=is_home_mode,
    )


@pytest.mark.asyncio
async def test_completed_run_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_execute_single_pulse", AsyncMock(return_value=1))
    await _run("hb-finished-done", pulses=2)
    records = activity_registry.finished_since(0)
    assert [(r["session_id"], r["kind"], r["agent"]) for r in records] == [
        ("hb-finished-done", "heartbeat", "Alice")
    ]


@pytest.mark.asyncio
async def test_stopped_before_first_pulse_is_still_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配額用完等原因第一輪前就停：登記表裡從來沒有活動，紀錄的靈魂不能靠它推。"""
    monkeypatch.setattr(autonomy_service, "check_quota", lambda _identity: False)
    pulse = AsyncMock(return_value=1)
    monkeypatch.setattr(hb, "_execute_single_pulse", pulse)
    await _run("hb-finished-quota")
    pulse.assert_not_awaited()
    assert _finished() == ["hb-finished-quota"]
    assert activity_registry.finished_since(0)[0]["agent"] == "Alice"


@pytest.mark.asyncio
async def test_failed_run_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_execute_single_pulse", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(
        hb.session_service, "append_message_to_session", AsyncMock(return_value=True)
    )
    await _run("hb-finished-error")
    assert _finished() == ["hb-finished-error"]


@pytest.mark.asyncio
async def test_not_started_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(destructive_ops, "start_heartbeat", lambda *_a, **_k: None)
    await _run("hb-finished-blocked")
    assert _finished() == []


@pytest.mark.asyncio
async def test_failed_before_starting_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """連開始都沒記到就失敗：什麼都沒跑，不算跑完。"""

    def _fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(autonomy_service, "mark_heartbeat_in_flight", _fail)
    pulse = AsyncMock(return_value=1)
    monkeypatch.setattr(hb, "_execute_single_pulse", pulse)
    with contextlib.suppress(OSError):
        await _run("hb-finished-not-started")
    pulse.assert_not_awaited()
    assert _finished() == []


@pytest.mark.asyncio
async def test_recorded_after_trim_and_after_activity_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """紀錄出現時，工作中的狀態已經清掉、剪裁也做完了。"""
    monkeypatch.setattr(hb, "_execute_single_pulse", AsyncMock(return_value=1))
    seen_during_trim: list[list[str]] = []

    async def _trim(_identity, _sid):
        seen_during_trim.append(_finished())

    monkeypatch.setattr(hb.sliding_window_service, "check_and_trim", _trim)
    real_record = activity_registry.record_finished
    activity_at_record: list[dict] = []

    def _record(session_id, kind, agent):
        activity_at_record.append(activity_registry.snapshot())
        real_record(session_id, kind, agent)

    monkeypatch.setattr(activity_registry, "record_finished", _record)
    await _run("hb-finished-order", is_home_mode=True)
    assert seen_during_trim == [[]]
    assert activity_at_record == [{}]
    assert _finished() == ["hb-finished-order"]

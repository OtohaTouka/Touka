"""B1：互審驗證指令要人核准、但無人在場（U2b-2；決策 P6 甲；SECURITY.md B1）.

修補前：無人在場（背景心跳、子代理、未帶 ``_interactive``）時，CONFIRM 級 ``verification_cmd``
被拒後這一輪照樣送 critic（花錢）、扣 ``review_attempts_total``，扣到上限就 ``review_locked``——
人從沒被問過；回給代理人的訊息還叫它改用 ``execute_command`` 自己跑、把輸出貼進 test_log。

修補後：本輪標 ``BLOCKED_FOR_HUMAN``（等人確認），不呼叫 critic、不扣次數、不上鎖、不佔 mission
子 loop 名額；回結構化 permission_denied（``reason=human_approval_required``），請代理人等人回來
後在對話裡重跑。人回來重跑時走逐條跳卡；attempts 達上限的永鎖仍是另一種回傳。

全部 sandbox：看板走 conftest 沙盒的 MissionManager，critic 的 ``complete_once`` 換成假函式，
指令只用 ``sys.executable`` 在 tmp_path 寫標記檔，審批由測試直接解算行程內 broker。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.services.approval_service import approval_service
from src.services.completion import CompletionResult
from src.services.permission_service import permission_service
from src.services.review import evidence
from src.services.review.budget import _reset_review_ledger, get_review_ledger
from src.services.review.reviewer import CmdResult, EvidenceBundle
from src.tools import mission_tools as mt

_PY = sys.executable
_COMPLETE = "src.services.review.review_loop.complete_once"
_APPROVE = (
    '```json\n{"verdict": "APPROVE", "reasoning": "ok", "required_fixes": [],'
    ' "confidence": 0.9}\n```'
)
_CMD = f"\"{_PY}\" -c \"open('ran.txt', 'w').write('x')\""


@pytest.fixture(autouse=True)
def _clean_state():
    approval_service.reset_for_test()
    _reset_review_ledger()
    yield
    approval_service.reset_for_test()
    _reset_review_ledger()


@pytest.fixture
def critic_calls(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def _fake_complete(*args: Any) -> CompletionResult:
        calls.append(args)
        return CompletionResult(
            text=_APPROVE, usage={}, cost_usd=0.0, stop_reason="end", error=None
        )

    monkeypatch.setattr(_COMPLETE, _fake_complete)
    return calls


async def _board(identity: AgentIdentity) -> MissionManager:
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="等人確認",
        initial_tasks=[
            {"id": "t1", "description": "跑驗證", "review_required": True, "verification_cmd": _CMD}
        ],
    )
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DOING)
    return manager


async def _review(identity: AgentIdentity, tmp_path, monkeypatch, **extra: Any) -> str:
    monkeypatch.setattr(mt, "_agent_artifact_root", lambda _identity: tmp_path)
    call = {"_ctx": None, "model": "producer-brain", "critic_model": "critic-brain", **extra}
    return str(
        await mt.mission_tools(action="request_review", task_id="t1", identity=identity, **call)
    )


async def _task(manager: MissionManager, identity: AgentIdentity) -> Any:
    state = await manager.get_current_mission(identity)
    return state.tasks[0]


async def _emit_nothing(_event: dict[str, Any]) -> None:
    return None


def _chat() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """有人在場的互動對話 kwargs（Manual；事件收進回傳的 list）."""
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    chat = {"auth_level": "Manual", "session_id": "sess-b1", "_interactive": True}
    return events, {**chat, "_emit_event": _emit, "_call_id": "call-r1"}


async def _resolve_next(*, approved: bool) -> None:
    for _ in range(600):  # 等審批登記（避免 sleep 競態）
        open_ids = [k for k, v in approval_service._pending.items() if v.decision is None]
        if open_ids:
            await approval_service.resolve(open_ids[0], approved=approved, resolver="tester")
            return
        await asyncio.sleep(0.005)
    raise AssertionError(approval_service._pending)  # 審批從未登記


def _trail(events: list[dict[str, Any]], step_id: str = "call-r1:verify:1") -> list[tuple]:
    return [
        (e["type"], e.get("decision") or e.get("status"))
        for e in events
        if step_id in (e.get("id"), e.get("call_id"))
    ]


async def _assert_round_cost_nothing(manager, identity, critic_calls) -> None:
    task = await _task(manager, identity)
    assert critic_calls == []
    assert (task.review_status, task.review_attempts_total, task.review_locked) == (
        "BLOCKED_FOR_HUMAN",
        0,
        False,
    )


@pytest.mark.parametrize(
    "headless",
    [
        {"auth_level": "Manual"},  # 背景心跳／未帶 _interactive
        {  # 子代理：互動對話裡也沒有 approver
            "auth_level": "SafeAuto",
            "_interactive": True,
            "_emit_event": _emit_nothing,
            "_call_id": "call-s",
            "_ctx": {"subagent_run": True},
        },
    ],
    ids=["heartbeat", "subagent"],
)
async def test_no_human_present_waits_without_critic_attempts_or_lock(
    tmp_path, monkeypatch, critic_calls, headless
):
    identity = AgentIdentity.for_role(agent_id="B1Wait", role="Owner")
    manager = await _board(identity)

    # 跑到超過往返上限（3）：修補前第 3 輪就永鎖
    results = [await _review(identity, tmp_path, monkeypatch, **headless) for _ in range(4)]

    assert not (tmp_path / "ran.txt").exists()
    assert critic_calls == []
    task = await _task(manager, identity)
    assert task.review_status == "BLOCKED_FOR_HUMAN"
    assert task.review_attempts_total == 0
    assert task.review_locked is False
    assert task.reviewer_id == "__reviewer_t1__@gate"
    state = await manager.get_current_mission(identity)
    assert get_review_ledger().spawned(state.mission_id) == 0

    for result in results:
        payload = json.loads(result)
        assert next(iter(payload)) == "error_class"
        assert payload["error_class"] == "permission_denied"
        assert payload["reason"] == "human_approval_required"
        assert payload["review_status"] == "BLOCKED_FOR_HUMAN"
        assert payload["review_locked"] is False
        assert "request_review" in payload["suggestion"]
        assert "REJECTED" in payload["evidence"]
        assert "execute_command" not in result


async def test_human_returns_and_reruns_in_chat_with_a_card(tmp_path, monkeypatch, critic_calls):
    """等人確認之後，人回來在對話裡重跑：那一條跳卡、核准才跑，送審放行，任務能 DONE."""
    identity = AgentIdentity.for_role(agent_id="B1Return", role="Owner")
    manager = await _board(identity)
    await _review(identity, tmp_path, monkeypatch, auth_level="Manual")
    events, chat = _chat()

    rerun = asyncio.create_task(_review(identity, tmp_path, monkeypatch, **chat))
    await _resolve_next(approved=True)
    result = await rerun

    assert (tmp_path / "ran.txt").exists()
    card = next(e for e in events if e["type"] == "tool_approval_request")
    assert (card["call_id"], card["args"]) == ("call-r1:verify:1", {"command": _CMD})
    assert len(critic_calls) == 1
    assert result.startswith("🔎 Review [t1] → APPROVED")
    task = await _task(manager, identity)
    assert (task.review_status, task.review_attempts_total) == ("APPROVED", 0)
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DONE)
    assert (await _task(manager, identity)).status == TaskStatus.DONE


async def test_unanswered_card_waits_without_critic_attempts_or_lock(
    tmp_path, monkeypatch, critic_calls
):
    """有人在場但卡片逾時：和無人在場同一條路（等人確認），訊息說人沒有回應."""
    monkeypatch.setattr("src.services.approval_service.DEFAULT_TIMEOUT_S", 0.05)
    identity = AgentIdentity.for_role(agent_id="B1Timeout", role="Owner")
    manager = await _board(identity)
    events, chat = _chat()

    result = await _review(identity, tmp_path, monkeypatch, **chat)

    assert not (tmp_path / "ran.txt").exists()
    await _assert_round_cost_nothing(manager, identity, critic_calls)
    payload = json.loads(result)
    assert (payload["error_class"], payload["reason"]) == (
        "permission_denied",
        "human_approval_required",
    )
    assert "no human decision" in payload["message"]
    assert "request_review" in payload["suggestion"]
    assert "execute_command" not in result
    assert ("tool_approval_resolved", "timeout") in _trail(events)


async def test_rejected_card_skips_critic_attempts_and_lock(tmp_path, monkeypatch, critic_calls):
    """人類拒絕這條指令：不送審、不計次數、不上鎖；請代理人先問人，不提 execute_command."""
    identity = AgentIdentity.for_role(agent_id="B1Reject", role="Owner")
    manager = await _board(identity)
    _events, chat = _chat()

    pending = asyncio.create_task(_review(identity, tmp_path, monkeypatch, **chat))
    await _resolve_next(approved=False)
    result = await pending

    assert not (tmp_path / "ran.txt").exists()
    await _assert_round_cost_nothing(manager, identity, critic_calls)
    payload = json.loads(result)
    assert (payload["error_class"], payload["reason"]) == (
        "permission_denied",
        "human_rejected_command",
    )
    assert payload["review_locked"] is False
    assert "rejected by the human" in payload["message"]
    assert "Ask the human" in payload["suggestion"]
    assert "execute_command" not in result


async def test_approved_then_rejected_commands_skip_critic(tmp_path, monkeypatch, critic_calls):
    """兩條指令先准後拒：不送審；核准那條的步驟照常收尾、被拒那條以錯誤結束."""
    identity = AgentIdentity.for_role(agent_id="B1Two", role="Owner")
    manager = await _board(identity)
    events, chat = _chat()

    async def _two_commands(*, command_gate: Any, **_kwargs: Any) -> EvidenceBundle:
        results = []
        for cmd in ("pytest -q tests/a", "pytest -q tests/b"):
            reason = await command_gate(cmd)
            exit_code = None if reason else 0
            results.append(CmdResult(cmd=cmd, exit_code=exit_code, rejected_reason=reason))
        return EvidenceBundle(cmd_results=results)

    monkeypatch.setattr("src.services.review.review_loop.collect_evidence", _two_commands)
    pending = asyncio.create_task(_review(identity, tmp_path, monkeypatch, **chat))
    await _resolve_next(approved=True)
    await _resolve_next(approved=False)
    result = await pending

    await _assert_round_cost_nothing(manager, identity, critic_calls)
    assert json.loads(result)["reason"] == "human_rejected_command"
    assert _trail(events, "call-r1:verify:1") == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "approved"),
        ("tool_start", None),
        ("tool_result", "success"),
    ]
    assert _trail(events, "call-r1:verify:2") == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "rejected"),
        ("tool_result", "error"),
    ]


async def test_attempt_cap_lock_reads_differently(tmp_path, monkeypatch, critic_calls):
    """永鎖（往返達上限）不是「等人確認」：回一般審查結果、不帶 human_approval_required."""
    identity = AgentIdentity.for_role(agent_id="B1Locked", role="Owner")
    manager = await _board(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="BLOCKED_FOR_HUMAN",
        reviewer_id="__reviewer_t1__@critic-brain",
        attempts_increment=3,
        review_locked=True,
    )

    result = await _review(identity, tmp_path, monkeypatch, auth_level="Manual")

    assert result.startswith("🔎 Review [t1] → BLOCKED_FOR_HUMAN\nreview_locked")
    assert "human_approval_required" not in result
    assert critic_calls == []


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (None, "requires human approval, but no human is present to approve it"),
        ("timeout", "requires human approval, but no human decision arrived"),
        ("queue_full", "requires human approval, but no human decision arrived"),
        ("rejected", "rejected by the human"),
        ("approved", None),
    ],
)
async def test_gate_reason_follows_the_approval_verdict(monkeypatch, verdict, reason):
    """政策閘依審批結果給固定理由（無人在場／沒等到決定／被拒），不叫代理人改用 execute_command."""
    monkeypatch.setattr(permission_service, "classify_command", lambda *_a, **_k: "CONFIRM")
    identity = AgentIdentity.for_role(agent_id="B1Gate", role="Owner")

    async def _approver(_cmd: str, **_card: object) -> str:
        return verdict

    gate = evidence.policy_command_gate(
        identity, auth_level="Manual", approver=None if verdict is None else _approver
    )

    assert await gate("pytest -q") == reason

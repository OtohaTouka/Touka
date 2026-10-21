"""H4：互審驗證指令的審批卡帶審查理由與「只有你能核准」（iso-c1 U2；ADR-0246）.

修補前：Auto 審查員對 ``verification_cmd`` 只回判決字串，轉人（escalate）時理由丟掉；
``CommandApprover`` 呼叫 ``await_human_approval`` 也不帶 ``human_only``——卡上看不到為什麼要問人，
碰到底線的指令也沒標「只有你能核准」（終端工具的卡早就有，ADR-0246）。

修補後：審查員回判決＋理由；轉人時卡上有 ``review_reason``；政策判 HUMAN_ONLY（底線）時卡上
``human_only=true``。釘住：審查員的核准不能滿足底線（底線指令不送審，只認人類的卡）。

全部 sandbox：審查員用假的 Completer（不打 API，禁令 61）；看板走 conftest 沙盒的
MissionManager；審批由測試直接解算行程內 broker。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.services import auto_reviewer as reviewer_module
from src.services.approval_flow import CommandApprover
from src.services.approval_service import approval_service
from src.services.auto_reviewer import AutoReviewer
from src.services.completion import CompletionResult
from src.services.review.budget import _reset_review_ledger
from src.services.review.evidence import REJECTED_BY_HUMAN, policy_command_gate
from src.tools import mission_tools as mt

_SOUL = AgentIdentity.for_role(agent_id="H4CardProbe", role="Owner", is_human=False)
_CONFIG = {"model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0}
_ESCALATE_REASON = "not sure the user wants this"
_ESCALATE = json.dumps({"verdict": "escalate", "reason": _ESCALATE_REASON})
_ALLOW = json.dumps({"verdict": "allow", "reason": "the user asked for it"})
_FLOOR_CMD = "cat .env"  # F2（讀金鑰）：三種模式都只有人能核准
_WAIT_S = 5.0


class _FakeCompleter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def __call__(self, identity, purpose, model, system, user, max_tokens, timeout_s,
                       response_schema=None) -> CompletionResult:
        self.calls.append(user)
        return CompletionResult(
            text=self.text, usage={}, cost_usd=0.0, stop_reason="end", error=None
        )


class _Approver:
    """記下每次問人時卡上會帶的東西。"""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, str | None, bool]] = []

    async def __call__(
        self, cmd: str, *, review_reason: str | None = None, human_only: bool = False
    ) -> str:
        self.calls.append((cmd, review_reason, human_only))
        return self.verdict


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    approval_service.reset_for_test()
    _reset_review_ledger()
    monkeypatch.setattr(reviewer_module.audit_service, "log_decision", lambda *_a: None)
    yield
    approval_service.reset_for_test()
    _reset_review_ledger()


def _install_reviewer(monkeypatch: pytest.MonkeyPatch, text: str) -> _FakeCompleter:
    completer = _FakeCompleter(text)
    monkeypatch.setattr(
        mt, "auto_reviewer", AutoReviewer(completer, config_loader=lambda: _CONFIG)
    )
    return completer


def _command_reviewer() -> Any:
    return mt.auto_reviewer.command_reviewer(
        _SOUL, user_intent=None, human_present=True, session_id="s-h4", conversation_key="s-h4"
    )


# ── 審查員回判決＋理由 ─────────────────────────────────────────────────────────────


async def test_command_reviewer_returns_the_verdict_with_its_reason(monkeypatch):
    _install_reviewer(monkeypatch, _ESCALATE)
    outcome = await _command_reviewer()("pytest -q")
    assert (outcome.verdict, outcome.reason) == ("escalate", _ESCALATE_REASON)


# ── 政策閘把卡片資訊交給 approver ──────────────────────────────────────────────────


async def test_escalated_command_asks_the_human_with_the_reviewer_reason(monkeypatch):
    _install_reviewer(monkeypatch, _ESCALATE)
    approver = _Approver("approved")
    gate = policy_command_gate(
        _SOUL, auth_level="Auto", approver=approver, reviewer=_command_reviewer()
    )
    assert await gate("pytest -q") is None
    assert approver.calls == [("pytest -q", _ESCALATE_REASON, False)]


async def test_manual_confirm_card_has_no_reason_and_is_not_human_only():
    approver = _Approver("approved")
    gate = policy_command_gate(_SOUL, auth_level="Manual", approver=approver)
    assert await gate("pytest -q") is None
    assert approver.calls == [("pytest -q", None, False)]


@pytest.mark.parametrize("level", ["Manual", "Auto", "YOLO"])
async def test_floor_command_is_human_only_and_reviewer_approval_cannot_satisfy_it(
    monkeypatch, level
):
    completer = _install_reviewer(monkeypatch, _ALLOW)
    approver = _Approver("rejected")
    gate = policy_command_gate(
        _SOUL, auth_level=level, approver=approver, reviewer=_command_reviewer()
    )
    assert await gate(_FLOOR_CMD) == REJECTED_BY_HUMAN
    assert completer.calls == []  # 底線指令不送審：審查員的 allow 根本沒有機會
    assert approver.calls == [(_FLOOR_CMD, None, True)]


# ── CommandApprover 把兩個欄位放上卡片 ─────────────────────────────────────────────


async def _first_open_approval() -> str:
    async with asyncio.timeout(_WAIT_S):
        while True:
            open_ids = [k for k, v in approval_service._pending.items() if v.decision is None]
            if open_ids:
                return open_ids[0]
            await asyncio.sleep(0.005)


async def test_command_approver_puts_reason_and_human_only_on_the_card():
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    approver = CommandApprover(
        emit=_emit, parent_call_id="call-h4", agent_id=_SOUL.agent_id, session_id="s-h4"
    )
    task = asyncio.create_task(approver("pytest -q", review_reason="why", human_only=True))
    await approval_service.resolve(await _first_open_approval(), approved=False, resolver="t")
    plain = asyncio.create_task(approver("pytest -q"))
    await approval_service.resolve(await _first_open_approval(), approved=False, resolver="t")
    assert await task == "rejected"
    assert await plain == "rejected"

    first, second = (e for e in events if e["type"] == "tool_approval_request")
    assert first["call_id"] == "call-h4:verify:1"
    assert (first["review_reason"], first["human_only"]) == ("why", True)
    assert "review_reason" not in second
    assert "human_only" not in second


# ── 端到端：request_review（Auto、有人在場）─────────────────────────────────────


async def _board(cmd: str) -> None:
    manager = MissionManager()
    await manager.initialize_mission(
        identity=_SOUL,
        goal="驗證",
        initial_tasks=[
            {"id": "t1", "description": "跑驗證", "review_required": True, "verification_cmd": cmd}
        ],
    )
    await manager.update_task(identity=_SOUL, task_id="t1", status=TaskStatus.DOING)


async def _review_and_reject(monkeypatch, tmp_path, cmd: str) -> dict[str, Any]:
    monkeypatch.setattr(mt, "_agent_artifact_root", lambda _identity: tmp_path)
    await _board(cmd)
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    task = asyncio.create_task(
        mt.mission_tools(
            action="request_review",
            task_id="t1",
            identity=_SOUL,
            auth_level="Auto",
            session_id="s-h4",
            _interactive=True,
            _emit_event=_emit,
            _call_id="call-h4",
            _ctx={"user_intent": None},
        )
    )
    try:
        await approval_service.resolve(await _first_open_approval(), approved=False, resolver="t")
        async with asyncio.timeout(_WAIT_S):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return next(e for e in events if e["type"] == "tool_approval_request")


async def test_request_review_escalation_card_shows_the_reviewer_reason(monkeypatch, tmp_path):
    _install_reviewer(monkeypatch, _ESCALATE)
    card = await _review_and_reject(monkeypatch, tmp_path, "pytest -q")
    assert card["review_reason"] == _ESCALATE_REASON
    assert "human_only" not in card


async def test_request_review_floor_card_is_human_only(monkeypatch, tmp_path):
    completer = _install_reviewer(monkeypatch, _ALLOW)
    card = await _review_and_reject(monkeypatch, tmp_path, _FLOOR_CMD)
    assert card["human_only"] is True
    assert completer.calls == []

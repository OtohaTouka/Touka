"""工具核准段的出口（外部審查第 3 步；拆 ``execute_to_queue`` 前先釘住現在的行為）.

- 每條「不跑」的出口恰好送一個結束記號（None）而且在最後：``react_core`` 靠它計數，
  少送會卡住、多送會讓同一輪的其他工具提早結束（單一工具時測不出來，所以這裡數個數）。
  丟例外的出口不在這裡（那時 queue 收不到 None，是現有行為）。
- 人按拒絕／取消／逾時／佇列滿：只送錯誤事件與結束記號，不送 ``turn_notice`` 小提示；
  PermissionDenied 在結束記號之後；停止收掉的卡帶停止原因（不算被拒）。
- 審查員拒絕、hook 擋下、沒人在場：錯誤事件 → 小提示 → 結束記號 → PermissionDenied。
- 沒人在場的結果表：分終端與非終端；終端＋CONFIRM、沒有審查員也沒有 hook 時照跑
  （終端自己的遺留通道），``_hitl_preapproved`` 為 False。

全部直接呼叫 ``execute_to_queue``。sandbox：hook 設定檔在 tmp_path、假的 http runner，
審查員用假的 Completer，不打 API（禁令 61），不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from src.models.policy import PolicyDecision
from src.services import approval_flow, tool_errors
from src.services import tool_executor as executor_module
from src.services.approval_service import ApprovalResolution, approval_service
from src.services.auto_reviewer import AutoReviewer
from src.services.hooks.tool_hooks import HOOK_DENIED_REASON, STOPPED_REASON
from src.services.permission_service import permission_service
from src.utils.tool_events import TOOL_RESULT
from src.utils.turn_notices import (
    NOTICE_APPROVAL_BLOCKED,
    NOTICE_HOOK_DENIED,
    TURN_NOTICE_EVENT,
)
from src.utils.user_intent import chat_intent

from tests.support.hook_wiring import (
    REVIEW_ALLOW,
    REVIEW_DENY,
    SOUL,
    WAIT_S,
    FakeCompleter,
    Recorder,
    clean_approval_state,
    executor_with,
    group,
    install_hooks,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from src.services.tool_executor import ToolExecutor

    from tests.support.hook_wiring import HookSetup

REVIEW_ESCALATE = '{"verdict": "escalate", "reason": "not sure the user wants this"}'
TERMINAL = "execute_command"
NON_TERMINAL = "write"
ARGS = {TERMINAL: {"command": "pytest -q"}, NON_TERMINAL: {"scope": "vault"}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from clean_approval_state(monkeypatch)


@pytest.fixture
def hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookSetup:
    return install_hooks(tmp_path, monkeypatch)


@dataclass
class Outcome:
    result: str
    items: list[dict[str, Any] | None]
    # PermissionDenied 每次被叫時：(原因, 當時 queue 裡已有幾項)
    denials: list[tuple[str | None, int]] = field(default_factory=list)

    @property
    def reason(self) -> str | None:
        return tool_errors.extract_field(self.result, "reason")

    def notices(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i is not None and i.get("type") == TURN_NOTICE_EVENT]


@dataclass
class Setup:
    """一種情境：政策效果、審查員回什麼、hook 回什麼、人怎麼決定。"""

    effect: str
    present: bool
    review: str | None = None
    headless_terminal: bool = False
    pre_tool: str | None = None
    permission_request: str | None = None
    approve: bool | None = None  # 用真的審批服務解算
    verdict: str | None = None  # 直接替換執行器的審批等待
    stopped: bool = False  # 替換的審批等待帶停止的解算者


def _pre_tool(decision: str, reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision,
        "permissionDecisionReason": reason}})


def _permission_request_deny(message: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": message}}})


def _prepare(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, setup: Setup, tool: str
) -> tuple[ToolExecutor, Recorder, FakeCompleter]:
    recorder = Recorder()
    executor = executor_with(monkeypatch, {tool: recorder})
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect=setup.effect, reason=f"test-{setup.effect.lower()}"),
    )
    completer = FakeCompleter(setup.review or REVIEW_ALLOW)
    config = {"model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0,
              "headless_terminal": setup.headless_terminal}
    monkeypatch.setattr(
        executor_module, "auto_reviewer", AutoReviewer(completer, config_loader=lambda: config)
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    if setup.pre_tool is not None:
        groups["PreToolUse"] = [group()]
        hooks.http.bodies["PreToolUse"] = setup.pre_tool
    if setup.permission_request is not None:
        groups["PermissionRequest"] = [group()]
        hooks.http.bodies["PermissionRequest"] = setup.permission_request
    hooks.configure(groups)
    if setup.verdict is not None:
        _replace_wait(monkeypatch, executor, setup.verdict, stopped=setup.stopped)
    return executor, recorder, completer


def _replace_wait(
    monkeypatch: pytest.MonkeyPatch, executor: ToolExecutor, verdict: str, *, stopped: bool
) -> None:
    async def _wait(**kwargs: Any) -> str:
        if stopped:
            kwargs["on_resolved"](ApprovalResolution(
                decision="cancelled", resolver_id=approval_flow.STOPPED_RESOLVER))
        return verdict

    monkeypatch.setattr(executor, "_await_human_approval", _wait)


async def _run(
    monkeypatch: pytest.MonkeyPatch, executor: ToolExecutor, tool: str, setup: Setup
) -> Outcome:
    ctx: dict[str, Any] = {
        "identity": SOUL, "auth_level": "Auto", "session_id": "s-exits",
        "user_intent": chat_intent([{"role": "user", "content": "跑一下測試"}]),
    }
    if setup.present:
        ctx["resumable_stream"] = True
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    denials: list[tuple[str | None, int]] = []

    def _denied(_ctx: Any, _call: Any, reason: str | None) -> None:
        denials.append((reason, queue.qsize()))

    monkeypatch.setattr(executor_module, "notify_permission_denied", _denied)
    async with asyncio.timeout(WAIT_S):
        task = asyncio.create_task(
            executor.execute_to_queue(tool, dict(ARGS[tool]), "req-exits", ctx, queue)
        )
        if setup.approve is not None:
            while not approval_service._pending:
                assert not task.done(), "應該跳出審批卡"
                await asyncio.sleep(0.005)
            exec_id = next(iter(approval_service._pending))
            await approval_service.resolve(exec_id, approved=setup.approve, resolver="tester")
        result = await task
    items: list[dict[str, Any] | None] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return Outcome(result=result, items=items, denials=denials)


def _is_error_result(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("type") == TOOL_RESULT and item.get("status") == "error"


# 人看過卡片之後沒核准的出口：(情境, 結果的 reason)
CARD_EXITS: dict[str, tuple[Setup, str]] = {
    "rejected": (Setup("CONFIRM", present=True, approve=False), "approval_rejected"),
    "cancelled": (Setup("CONFIRM", present=True, verdict="cancelled"), "approval_cancelled"),
    "timeout": (Setup("CONFIRM", present=True, verdict="timeout"), "approval_timeout"),
    "queue_full": (Setup("CONFIRM", present=True, verdict="queue_full"), "approval_queue_full"),
    "stopped": (
        Setup("CONFIRM", present=True, verdict="cancelled", stopped=True), "approval_cancelled"
    ),
    "escalated_then_rejected": (
        Setup("REVIEW", present=True, review=REVIEW_ESCALATE, approve=False), "approval_rejected"
    ),
    "hook_ask_then_rejected": (
        Setup("ALLOW", present=True, pre_tool=_pre_tool("ask", "a person decides"),
              approve=False),
        "approval_rejected",
    ),
}

# 沒有卡片可按、直接拒絕的出口：(情境, 工具, 結果的 reason, 小提示的種類)
HELD_EXITS: dict[str, tuple[Setup, str, str, str]] = {
    "reviewer_denied": (
        Setup("REVIEW", present=True, review=REVIEW_DENY), TERMINAL,
        "auto_review_denied", NOTICE_APPROVAL_BLOCKED,
    ),
    "permission_request_hook": (
        Setup("CONFIRM", present=True, permission_request=_permission_request_deny("no")),
        TERMINAL, HOOK_DENIED_REASON, NOTICE_HOOK_DENIED,
    ),
    "human_only_unattended": (
        Setup("HUMAN_ONLY", present=False), TERMINAL, "human_only", NOTICE_APPROVAL_BLOCKED,
    ),
    "hook_ask_unattended": (
        Setup("ALLOW", present=False, pre_tool=_pre_tool("ask", "a person decides")), TERMINAL,
        "hook_requires_human", NOTICE_APPROVAL_BLOCKED,
    ),
    "review_unattended": (
        Setup("REVIEW", present=False), NON_TERMINAL,
        "auto_review_escalated", NOTICE_APPROVAL_BLOCKED,
    ),
    "confirm_unattended": (
        Setup("CONFIRM", present=False), NON_TERMINAL,
        "approval_required", NOTICE_APPROVAL_BLOCKED,
    ),
}

ALL_EXITS: dict[str, tuple[Setup, str, str]] = {
    **{f"card:{key}": (setup, TERMINAL, reason) for key, (setup, reason) in CARD_EXITS.items()},
    **{
        f"held:{key}": (setup, tool, reason)
        for key, (setup, tool, reason, _notice) in HELD_EXITS.items()
    },
}


@pytest.mark.parametrize("key", sorted(ALL_EXITS))
async def test_every_exit_that_does_not_run_ends_with_exactly_one_none(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, key: str
) -> None:
    setup, tool, reason = ALL_EXITS[key]
    executor, recorder, _ = _prepare(monkeypatch, hooks, setup, tool)
    outcome = await _run(monkeypatch, executor, tool, setup)
    assert recorder.calls == []
    assert outcome.reason == reason
    assert outcome.items.count(None) == 1
    assert outcome.items[-1] is None
    assert len(outcome.denials) == 1


@pytest.mark.parametrize("key", sorted(CARD_EXITS))
async def test_a_card_the_person_did_not_approve_sends_no_notice(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, key: str
) -> None:
    setup, _reason = CARD_EXITS[key]
    executor, _recorder, _ = _prepare(monkeypatch, hooks, setup, TERMINAL)
    outcome = await _run(monkeypatch, executor, TERMINAL, setup)
    assert outcome.notices() == []
    assert _is_error_result(outcome.items[-2])
    # PermissionDenied 在結束記號之後；停止收掉的卡帶停止原因（hook 端不算被拒）
    ((denied_reason, queued),) = outcome.denials
    assert queued == len(outcome.items)
    assert denied_reason == (STOPPED_REASON if setup.stopped else outcome.reason)


async def test_an_escalated_card_that_is_rejected_keeps_the_reviewer_reason(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup
) -> None:
    setup, _reason = CARD_EXITS["escalated_then_rejected"]
    executor, _recorder, completer = _prepare(monkeypatch, hooks, setup, TERMINAL)
    outcome = await _run(monkeypatch, executor, TERMINAL, setup)
    assert completer.calls == 1
    assert "not sure the user wants this" in json.loads(outcome.result)["message"]


@pytest.mark.parametrize("key", sorted(HELD_EXITS))
async def test_a_held_call_sends_error_then_notice_then_none_then_permission_denied(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, key: str
) -> None:
    setup, tool, reason, notice = HELD_EXITS[key]
    executor, _recorder, _ = _prepare(monkeypatch, hooks, setup, tool)
    outcome = await _run(monkeypatch, executor, tool, setup)
    assert _is_error_result(outcome.items[-3])
    assert json.loads(outcome.items[-3]["result"])["reason"] == reason
    tail_notice = outcome.items[-2]
    assert tail_notice is not None and tail_notice.get("type") == TURN_NOTICE_EVENT
    assert tail_notice["kind"] == notice
    assert outcome.notices() == [tail_notice]
    ((denied_reason, queued),) = outcome.denials
    assert queued == len(outcome.items)
    assert denied_reason == reason


# 沒人在場的結果表：(情境, 工具) → 拒絕的 reason，或 None＝照跑
def _ran(outcome: Outcome) -> bool:
    return outcome.denials == [] and not _is_error_result(outcome.items[-2])


UNATTENDED: list[tuple[str, Setup, str, str | None]] = [
    ("human_only", Setup("HUMAN_ONLY", present=False), TERMINAL, "human_only"),
    ("human_only", Setup("HUMAN_ONLY", present=False), NON_TERMINAL, "human_only"),
    (
        "human_only_beats_hook_ask",
        Setup("HUMAN_ONLY", present=False, pre_tool=_pre_tool("ask", "x")), TERMINAL,
        "human_only",
    ),
    (
        "hook_ask",
        Setup("ALLOW", present=False, pre_tool=_pre_tool("ask", "x")), TERMINAL,
        "hook_requires_human",
    ),
    (
        "hook_ask",
        Setup("ALLOW", present=False, pre_tool=_pre_tool("ask", "x")), NON_TERMINAL,
        "hook_requires_human",
    ),
    (
        "hook_ask_skips_reviewer",
        Setup("REVIEW", present=False, pre_tool=_pre_tool("ask", "x"), headless_terminal=True),
        TERMINAL, "hook_requires_human",
    ),
    ("review_default", Setup("REVIEW", present=False), TERMINAL, "auto_review_headless_terminal"),
    ("review_default", Setup("REVIEW", present=False), NON_TERMINAL, "auto_review_escalated"),
    (
        "review_escalate",
        Setup("REVIEW", present=False, review=REVIEW_ESCALATE, headless_terminal=True), TERMINAL,
        "auto_review_escalated",
    ),
    (
        "review_deny",
        Setup("REVIEW", present=False, review=REVIEW_DENY, headless_terminal=True), TERMINAL,
        "auto_review_denied",
    ),
    (
        "review_allow",
        Setup("REVIEW", present=False, review=REVIEW_ALLOW, headless_terminal=True), TERMINAL,
        None,
    ),
    ("confirm", Setup("CONFIRM", present=False), NON_TERMINAL, "approval_required"),
    ("confirm_legacy_terminal", Setup("CONFIRM", present=False), TERMINAL, None),
]


def _row_id(row: tuple[str, Setup, str, str | None]) -> str:
    return f"{row[0]}-{row[2]}"


@pytest.mark.parametrize("row", UNATTENDED, ids=_row_id)
async def test_unattended_outcome_table(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, row: tuple[str, Setup, str, str | None]
) -> None:
    _label, setup, tool, reason = row
    executor, recorder, _ = _prepare(monkeypatch, hooks, setup, tool)
    outcome = await _run(monkeypatch, executor, tool, setup)
    assert approval_service.pending_count() == 0
    assert outcome.items.count(None) == 1
    if reason is not None:
        assert outcome.reason == reason
        assert recorder.calls == []
        return
    assert _ran(outcome)
    (call,) = recorder.calls
    assert call["_hitl_preapproved"] is False
    expected_by = "auto_reviewer" if setup.effect == "REVIEW" else None
    assert call["_approved_by"] == expected_by

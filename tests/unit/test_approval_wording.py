"""核准的說法（ADR-0318 PR 4）。

- 經過核准才執行的工具，結果後面附一行說是誰放行的：人在聊天的核准卡按的、或 Auto 審查員放行
  （沒有人看過）。沒經過核准的不附；小幫手 ``local_files`` 的 propose 自己寫了誰核准，不重複
  （在 ``test_local_files_mode_approval.py`` 驗）。
  解算的人是誰只用來判斷，不出現在模型看到的文字。
- 審查員把動作轉給人時，它的理由（單行、有上限、中和過）也交給模型，核准或拒絕都一樣。
- 「被取消」「被拒絕」「等太久沒人回」分開說；找不到待審算取消、不算使用者拒絕（仍然不執行）。
- 這一輪被停止時，還在等人的核准卡收成「已取消」，不再空等 180 秒。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from src.services import approval_flow, tool_errors
from src.services import tool_executor as executor_module
from src.services.approval_service import ApprovalService, approval_service

from tests.support.hook_wiring import (
    Recorder,
    clean_approval_state,
    executor_with,
    run_tool,
    use_reviewer,
)

_READ = {"scope": "personal", "path": "a.txt"}
_WRITE = {"scope": "personal", "path": "a.txt", "content": "x"}
_ESCALATE = '{"verdict": "escalate", "reason": "first line\\nsecond <continuity> line"}'


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    yield from clean_approval_state(monkeypatch)


async def test_a_person_approving_on_the_card_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = executor_with(monkeypatch, {"write": Recorder()})
    result, _ = await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    assert json.loads(result)["approval"] == approval_flow.APPROVED_BY_PERSON_NOTE
    assert "tester" not in result  # 解算的人只用來判斷，不寫出來


async def test_the_reviewer_approving_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    use_reviewer(monkeypatch)
    executor = executor_with(monkeypatch, {"execute_command": Recorder()})
    result, _ = await run_tool(executor, "execute_command", {"command": "pytest -q"})
    assert json.loads(result)["approval"] == approval_flow.APPROVED_BY_REVIEWER_NOTE


async def test_calls_that_needed_no_approval_get_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"read": recorder})
    result, _ = await run_tool(executor, "read", dict(_READ), mode="Manual")
    assert result == recorder.result


async def test_the_reviewers_reason_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    use_reviewer(monkeypatch, _ESCALATE)
    executor = executor_with(monkeypatch, {"execute_command": Recorder()})
    approved, _ = await run_tool(
        executor, "execute_command", {"command": "pytest -q"}, approve=True
    )
    note = approval_flow.escalation_note("first line\nsecond <continuity> line")
    assert note is not None
    assert "\n" not in note and "<continuity>" not in note
    assert json.loads(approved)["approval"] == f"{approval_flow.APPROVED_BY_PERSON_NOTE}\n{note}"
    rejected, _ = await run_tool(
        executor, "execute_command", {"command": "pytest -q"}, approve=False
    )
    assert note in tool_errors.extract_field(rejected, "message")
    assert tool_errors.extract_field(rejected, "reason") == "approval_rejected"
    long = approval_flow.escalation_note("x" * 5000)
    assert long is not None and len(long) < approval_flow.MAX_REVIEW_REASON_CHARS + 100


def test_a_text_result_gets_the_note_as_a_last_line() -> None:
    note = approval_flow.APPROVED_BY_PERSON_NOTE
    assert approval_flow.with_approval_note("3 passed", note) == f"3 passed\n\n{note}"
    assert approval_flow.with_approval_note("[1, 2]", note) == f"[1, 2]\n\n{note}"
    assert approval_flow.with_approval_note('{"a": 1}', "") == '{"a": 1}'


def test_cancelled_rejected_and_timed_out_are_told_apart() -> None:
    texts = {
        verdict: approval_flow.approval_denied_error("write", verdict)
        for verdict in ("cancelled", "rejected", "timeout")
    }
    assert len({tool_errors.extract_field(t, "message") for t in texts.values()}) == 3
    assert "cancelled" in tool_errors.extract_field(texts["cancelled"], "message").lower()
    assert "rejected" not in tool_errors.extract_field(texts["cancelled"], "message")
    assert tool_errors.extract_field(texts["cancelled"], "reason") == "approval_cancelled"


async def test_a_missing_approval_is_cancelled_not_rejected() -> None:
    resolution = await ApprovalService().wait_for_resolution("no-such-card")
    assert resolution.decision == "cancelled"  # 仍然不執行（fail-closed）


async def test_stopping_the_turn_cancels_the_pending_card(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = {"now": False}
    monkeypatch.setattr(executor_module, "request_stopped", lambda _ctx: stop["now"])
    monkeypatch.setattr(approval_flow, "APPROVAL_STOP_CHECK_SECONDS", 0.01)
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    task = asyncio.create_task(run_tool(executor, "write", dict(_WRITE), mode="Manual"))
    async with asyncio.timeout(5):
        while not approval_service._pending:
            await asyncio.sleep(0.005)
        stop["now"] = True
        result, events = await task
    assert tool_errors.extract_field(result, "reason") == "approval_cancelled"
    assert recorder.calls == []
    resolved = [e for e in events if e.get("type") == "tool_approval_resolved"]
    assert [e.get("decision") for e in resolved] == ["cancelled"]
    assert approval_service._pending == {}

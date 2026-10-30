"""B2：互審 verification_cmd 逐條人類審批（U2b-1；ADR-0137 2026-10-12 補記；SECURITY.md B2）.

修補前：Manual 互動會話核准 ``request_review`` 卡（卡上只有 task_id／producer_claims）後，
``_hitl_preapproved`` 一路透傳到 ``policy_command_gate``，任務板上 CONFIRM 級的
``verification_cmd`` 就直接執行——人從沒在卡上看過那條指令。

修補後：``policy_command_gate`` 不再接受預核准旗標；有人在場時每條需要核准的指令各跳一張卡
（步驟 id＝``<request_review 的 id>:verify:<n>``，卡上是完整指令），核准才執行；
無人在場與子代理不跳卡、不執行（BLOCKED_FOR_HUMAN 收尾留給 U2b-2）；核准後審查中途失敗，
該步驟以 internal_error 收尾。

全部 sandbox：data 根換到 ``zone_data_root``，看板與個人空間都在 tmp_path 底下；指令只用
``sys.executable`` 在個人空間（cwd）寫標記檔，審批由測試直接解算行程內 broker。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.services.approval_flow import CommandApprover
from src.services.approval_service import approval_service
from src.services.review.budget import _reset_review_ledger
from src.tools import mission_tools as mt

_PY = sys.executable
_VERIFY_ID = "call-r1:verify:1"


@pytest.fixture(autouse=True)
def _drive_in_tmp(zone_data_root):
    """看板與蒐證的個人空間都在這個測試自己的 data 根（ADR-0293）。"""


def _ran(identity: AgentIdentity) -> bool:
    """驗證指令在個人空間（cwd）寫的標記檔是否存在。"""
    drive = mt.resource_manager.agent_drive(identity)
    return mt.resource_manager.zone_kind(drive.child("ran.txt")) == "file"


@pytest.fixture(autouse=True)
def _clean_state():
    approval_service.reset_for_test()
    _reset_review_ledger()
    yield
    approval_service.reset_for_test()
    _reset_review_ledger()


def _marker_cmd() -> str:
    # 尾端註解把指令撐到 300 字以上：舊審批預覽在 200 字截斷，卡上必須看得到全文。
    return f'"{_PY}" -c "open(\'ran.txt\', \'w\').write(\'x\')  # {"x" * 300}"'


async def _board(identity: AgentIdentity, cmd: str) -> None:
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="驗證指令核准",
        initial_tasks=[
            {
                "id": "t1",
                "description": "跑驗證",
                "review_required": True,
                "verification_cmd": cmd,
            }
        ],
    )
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DOING)


def _collector() -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    return events, _emit


async def _review(identity: AgentIdentity, **extra: Any) -> str:
    call = {"_ctx": None, **extra}
    return str(
        await mt.mission_tools(action="request_review", task_id="t1", identity=identity, **call)
    )


async def _review_and_resolve(
    identity: AgentIdentity, *, approved: bool, **extra: Any
) -> str:
    task = asyncio.create_task(_review(identity, **extra))
    exec_id = None
    for _ in range(600):  # 等審批登記（避免 sleep 競態）
        if task.done():
            break
        pending = list(approval_service._pending)
        if pending:
            exec_id = pending[0]
            break
        await asyncio.sleep(0.005)
    assert exec_id, "verification_cmd 從未跳出審批卡"
    await approval_service.resolve(exec_id, approved=approved, resolver="tester")
    return await task


def _verify_trail(
    events: list[dict[str, Any]], step_id: str = _VERIFY_ID
) -> list[tuple[str, str | None]]:
    """verify 步驟的事件序列（審批卡以 call_id、工具事件以 id 對到同一步驟）."""
    return [
        (e["type"], e.get("decision") or e.get("status"))
        for e in events
        if step_id in (e.get("id"), e.get("call_id"))
    ]


async def _resolve_next(*, approved: bool) -> None:
    for _ in range(600):
        open_ids = [k for k, v in approval_service._pending.items() if v.decision is None]
        if open_ids:
            await approval_service.resolve(open_ids[0], approved=approved, resolver="tester")
            return
        await asyncio.sleep(0.005)
    raise AssertionError(approval_service._pending)  # 審批從未登記


async def test_legacy_preapproval_flag_no_longer_runs_confirm_command():
    """B2 核心：request_review 卡已核准（舊 _hitl_preapproved=True）不得放行卡上沒顯示的指令."""
    identity = AgentIdentity.for_role(agent_id="B2Legacy", role="Owner")
    await _board(identity, _marker_cmd())

    result = await _review(
        identity, auth_level="Manual", _hitl_preapproved=True
    )

    assert not _ran(identity)
    assert "REJECTED" in result
    assert approval_service.pending_count() == 0


@pytest.mark.parametrize("auth_level", ["Manual", "SafeAuto"])
async def test_interactive_review_asks_for_the_full_command_then_runs(
    auth_level
):
    """有人在場：該指令自己一張卡（完整指令、獨立 call_id），核准後才執行。

    SafeAuto 下 request_review 本身不跳卡，修補前驗證指令一定失敗；現在也是逐條問人。
    """
    identity = AgentIdentity.for_role(agent_id=f"B2Card{auth_level}", role="Owner")
    cmd = _marker_cmd()
    await _board(identity, cmd)
    events, emit = _collector()

    result = await _review_and_resolve(
        identity,
        approved=True,
        auth_level=auth_level,
        session_id="sess-b2",
        _interactive=True,
        _emit_event=emit,
        _call_id="call-r1",
    )

    assert _ran(identity)
    assert "REJECTED" not in result
    card = next(e for e in events if e["type"] == "tool_approval_request")
    assert card["call_id"] == _VERIFY_ID
    assert card["args"] == {"command": cmd}
    assert _verify_trail(events) == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "approved"),
        ("tool_start", None),
        ("tool_result", "success"),
    ]


async def test_rejected_verification_card_never_runs():
    identity = AgentIdentity.for_role(agent_id="B2Reject", role="Owner")
    await _board(identity, _marker_cmd())
    events, emit = _collector()

    result = await _review_and_resolve(
        identity,
        approved=False,
        auth_level="Manual",
        session_id="sess-b2",
        _interactive=True,
        _emit_event=emit,
        _call_id="call-r1",
    )

    assert not _ran(identity)
    assert "REJECTED" in result
    assert _verify_trail(events) == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "rejected"),
        ("tool_result", "error"),
    ]


async def test_headless_review_never_asks_and_never_runs():
    """無人在場：不推任何審批事件、不登記 broker、不執行（維持 fail-closed）."""
    identity = AgentIdentity.for_role(agent_id="B2Headless", role="Owner")
    await _board(identity, _marker_cmd())
    events, emit = _collector()

    result = await _review(
        identity,
        auth_level="SafeAuto",
        _interactive=False,
        _emit_event=emit,
        _call_id="call-h",
    )

    assert not _ran(identity)
    assert "REJECTED" in result
    assert events == []
    assert approval_service.pending_count() == 0


async def test_two_commands_get_separate_steps_one_approved_one_rejected():
    """每條指令自己的步驟 id：一准一拒時兩步驟各自正確，finish 只收核准的那條."""
    events, emit = _collector()
    approver = CommandApprover(
        emit=emit, parent_call_id="call-r2", agent_id="B2Two", session_id="sess-b2"
    )

    first = asyncio.create_task(approver("pytest -q tests/a"))
    await _resolve_next(approved=True)
    assert await first == "approved"
    second = asyncio.create_task(approver("pytest -q tests/b"))
    await _resolve_next(approved=False)
    assert await second == "rejected"
    await approver.finish("cmd `pytest -q tests/a` exit 0")

    cards = [e for e in events if e["type"] == "tool_approval_request"]
    assert [(c["call_id"], c["args"]["command"]) for c in cards] == [
        ("call-r2:verify:1", "pytest -q tests/a"),
        ("call-r2:verify:2", "pytest -q tests/b"),
    ]
    assert _verify_trail(events, "call-r2:verify:1") == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "approved"),
        ("tool_start", None),
        ("tool_result", "success"),
    ]
    assert _verify_trail(events, "call-r2:verify:2") == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "rejected"),
        ("tool_result", "error"),
    ]


async def test_subagent_review_never_asks_even_in_interactive_run():
    """子代理的子 loop 只收文字、卡送不到人：不建 approver，立刻 fail-closed（不等 180 秒）."""
    identity = AgentIdentity.for_role(agent_id="B2Subagent", role="Owner")
    await _board(identity, _marker_cmd())
    events, emit = _collector()

    result = await asyncio.wait_for(
        _review(
            identity,
            auth_level="SafeAuto",
            _interactive=True,
            _emit_event=emit,
            _call_id="call-s",
            _ctx={"subagent_run": True},
        ),
        timeout=10,
    )

    assert not _ran(identity)
    assert "REJECTED" in result
    assert events == []
    assert approval_service.pending_count() == 0


async def test_review_failure_after_approval_closes_the_verify_step(monkeypatch):
    """核准後審查拋例外：該步驟以 internal_error 收尾（不停在執行中），例外照常往上."""
    identity = AgentIdentity.for_role(agent_id="B2Crash", role="Owner")
    await _board(identity, _marker_cmd())
    events, emit = _collector()

    async def _approve_then_crash(**kwargs: Any) -> Any:
        assert await kwargs["approver"]("pytest -q") == "approved"
        raise RuntimeError("review_crashed")

    monkeypatch.setattr("src.services.review.review_loop.run_review_round", _approve_then_crash)

    result = await _review_and_resolve(
        identity,
        approved=True,
        auth_level="Manual",
        session_id="sess-b2",
        _interactive=True,
        _emit_event=emit,
        _call_id="call-r1",
    )

    assert result == "Error executing mission operation: review_crashed"
    assert _verify_trail(events) == [
        ("tool_approval_request", None),
        ("tool_approval_resolved", "approved"),
        ("tool_start", None),
        ("tool_result", "error"),
    ]
    closing = [e for e in events if e.get("id") == _VERIFY_ID][-1]
    assert closing["error_class"] == "internal_error"

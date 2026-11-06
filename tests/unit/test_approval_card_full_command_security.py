"""A7：審批卡顯示完整指令（U2b-1；ADR-0137 2026-10-12 補記；SECURITY.md A7）.

修補前：審批卡的參數預覽把每個字串值截到 200 字，人核准的可能是只看得到前半段的指令。
修補後：``command`` 完整上卡，只在超過 8 KB（模組常數）時截斷並附明確標示；其他大載荷
（如 ``write.content``）仍截到 200 字。前端換行顯示見 Vitest ``ToolApprovalCard.test.tsx``。

全部 sandbox：政策以 monkeypatch 固定為 CONFIRM，工具是記錄器，審批由測試直接拒絕
（只看卡片內容，不執行任何東西）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor

_IDENTITY = AgentIdentity.for_role(agent_id="A7Probe", role="Owner", is_human=False)


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


async def _never_called(**kwargs: Any) -> str:
    raise AssertionError(kwargs)  # 卡被拒絕時工具不得執行


async def _approval_card(monkeypatch, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """互動會話 CONFIRM：取審批卡事件後以拒絕解算（工具不執行）."""
    monkeypatch.setattr(
        permission_service, "evaluate_policy", lambda _ctx: PolicyDecision(effect="CONFIRM")
    )
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    if name in bindings:  # 終端等特殊分類：把記錄器綁成同一份可信註冊
        bindings[name] = replace(bindings[name], handler=_never_called)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={name: _never_called},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    ctx = {
        "identity": _IDENTITY,
        "auth_level": "Manual",
        "session_id": "sess-a7",
        "resumable_stream": True,
    }
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(executor.execute_to_queue(name, args, "req-a7", ctx, queue))
    for _ in range(200):
        if approval_service._pending:
            break
        await asyncio.sleep(0.005)
    exec_id = next(iter(approval_service._pending))
    await approval_service.resolve(exec_id, approved=False, resolver="tester")
    await task
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    return next(e for e in events if e and e.get("type") == "tool_approval_request")


async def test_card_shows_the_whole_command(monkeypatch):
    command = "python -m pytest tests/ " + "-k case " * 120  # 約 1,000 字
    card = await _approval_card(monkeypatch, "execute_command", {"command": command})
    assert card["args"]["command"] == command


async def test_card_caps_command_at_8kb_with_a_visible_marker(monkeypatch):
    command = "a" * 8192 + "b" * 808
    card = await _approval_card(monkeypatch, "execute_command", {"command": command})
    shown = card["args"]["command"]
    assert shown.startswith("a" * 8192)
    assert "b" not in shown.split("\n")[0]
    assert shown.endswith("\n…[truncated: 808 more chars not shown]")


async def test_command_at_exactly_8kb_is_not_truncated(monkeypatch):
    command = "c" * 8192
    card = await _approval_card(monkeypatch, "execute_command", {"command": command})
    assert card["args"]["command"] == command


async def test_other_large_values_stay_truncated(monkeypatch):
    card = await _approval_card(
        monkeypatch, "write", {"scope": "vault", "content": "x" * 5000}
    )
    assert card["args"]["scope"] == "vault"
    assert card["args"]["content"] == "x" * 200 + "…(+4800 chars)"

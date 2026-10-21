"""P1-B：tool_executor 事件走封套 SSOT——TDD.

驗證 ``execute_to_queue`` 的 tool_start / tool_result 經 ``tool_events`` 封套產生：
- **回歸**：wire 欄位（type/id/tool/args/status/result）完全不變。
- **additive**：實際執行的事件帶 ``duration_ms``（float、≥0）；失敗（含 DENY）帶
  ``error_class``（沿用 tool_errors 類別）；未執行的早退（DENY）**不帶** duration_ms。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.tool_executor import ToolExecutor


def _make_ctx(role: str = "Owner") -> dict[str, Any]:
    return {
        "identity": AgentIdentity.for_role(agent_id="DebugAgent", role=role),
        "auth_level": "Manual",
    }


async def _run(executor: ToolExecutor, name: str, args: dict) -> list[dict]:
    queue: asyncio.Queue = asyncio.Queue()
    with patch(
        "src.services.tool_executor.session_service.get_session_by_id",
        new=AsyncMock(return_value=None),
    ):
        await executor.execute_to_queue(name, args, "req-p1b", _make_ctx(), queue, call_id="cid-1")
    events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev is not None:
            events.append(ev)
    return events


def _first(events: list[dict], type_: str) -> dict:
    return next(e for e in events if e.get("type") == type_)


async def _ok_tool(**_kwargs: Any) -> str:
    return "done"


@pytest.mark.asyncio
async def test_tool_start_wire_fields_unchanged():
    """回歸：tool_start 的 wire 欄位＝凍結四鍵，一位不差。"""
    executor = ToolExecutor(None, {"mytool": _ok_tool}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        events = await _run(executor, "mytool", {"x": 1})
    assert _first(events, "tool_start") == {
        "type": "tool_start",
        "id": "cid-1",
        "tool": "mytool",
        "args": {"x": 1},
    }


@pytest.mark.asyncio
async def test_success_result_carries_duration_ms():
    """實際執行成功 → tool_result additive 帶 duration_ms（float、≥0），且 status=success。"""
    executor = ToolExecutor(None, {"mytool": _ok_tool}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        events = await _run(executor, "mytool", {"x": 1})
    res = _first(events, "tool_result")
    assert res["status"] == "success"
    assert res["id"] == "cid-1"
    assert isinstance(res["duration_ms"], (int, float))
    assert res["duration_ms"] >= 0
    # 成功事件不帶 error_class
    assert "error_class" not in res


@pytest.mark.asyncio
async def test_deny_result_carries_error_class_no_duration():
    """DENY（未執行）→ tool_result 帶 error_class，但**不帶** duration_ms。"""
    executor = ToolExecutor(None, {}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="DENY", reason="blocked by policy")
        events = await _run(executor, "bash", {"command": "ls"})
    res = _first(events, "tool_result")
    assert res["status"] == "error"
    assert res["error_class"] == tool_errors.PERMISSION_DENIED
    assert "duration_ms" not in res


@pytest.mark.asyncio
async def test_executed_tool_error_carries_error_class_and_duration():
    """工具實際執行但回傳結構化錯誤 → 同時帶 error_class ＋ duration_ms。"""

    async def _fail(**_kwargs: Any) -> str:
        return tool_errors.build_tool_error(tool_errors.UPSTREAM_ERROR, "backend 500")

    executor = ToolExecutor(None, {"fake": _fail}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        events = await _run(executor, "fake", {"q": 1})
    res = _first(events, "tool_result")
    assert res["status"] == "error"
    assert res["error_class"] == tool_errors.UPSTREAM_ERROR
    assert isinstance(res["duration_ms"], (int, float))

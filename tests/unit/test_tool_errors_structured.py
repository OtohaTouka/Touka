"""Tier 1（工具介面重設計提案 §5）：工具錯誤結構化——TDD 紅燈.

tool_result（模型面）改為可解析的結構化 JSON 錯誤：`error_class` 分類
（validation_error / permission_denied / resource_not_found / timeout_exceeded /
upstream_error / internal_error）＋可據以修正的 `suggestion`。
UI 面（type=log / type=error 事件）維持中文，不在此約束。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.tool_executor import ToolExecutor

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #

_BOOM_TYPE = TypeError("missing required arg")
_BOOM_RUNTIME = RuntimeError("kaboom")
_BOOM_FNF = FileNotFoundError("no such file")
_BOOM_PERM = PermissionError("nope")
_BOOM_TIMEOUT = TimeoutError("too slow")


def _make_ctx(role: str = "Owner") -> dict[str, Any]:
    return {
        "identity": AgentIdentity.for_role(agent_id="DebugAgent", role=role),
        "auth_level": "Manual",
    }


async def _run(
    executor: ToolExecutor, name: str, args: dict, ctx: dict
) -> tuple[str, list[dict]]:
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(name, args, "req-eval", ctx, queue)
    events = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev is not None:
            events.append(ev)
    return result, events


def _tool_result_event(events: list[dict]) -> dict:
    return next(e for e in events if e.get("type") == "tool_result")


def _raiser(exc: Exception):
    async def _tool(**_kwargs: Any) -> str:
        raise exc

    return _tool


# --------------------------------------------------------------------------- #
# tool_errors helper（SSOT）
# --------------------------------------------------------------------------- #


def test_build_and_detect_tool_error():
    payload = tool_errors.build_tool_error(
        tool_errors.PERMISSION_DENIED,
        "denied",
        suggestion="try later",
        denied_tool="bash",
    )
    data = json.loads(payload)
    assert data["error_class"] == "permission_denied"
    assert data["message"] == "denied"
    assert data["suggestion"] == "try later"
    assert data["denied_tool"] == "bash"
    # error_class 必須是第一個 key（is_tool_error 以前綴偵測、容忍尾綴附註）
    assert payload.startswith('{"error_class"')
    assert tool_errors.is_tool_error(payload)
    assert tool_errors.is_tool_error(payload + "\n[附註]")
    assert not tool_errors.is_tool_error("Success: done")
    assert not tool_errors.is_tool_error("Error: legacy plain error")
    assert not tool_errors.is_tool_error("")


def test_extract_error_class():
    """P1：把埋在 result_text JSON 的 error_class 抬成可解析值（供事件封套一級屬性）。"""
    payload = tool_errors.build_tool_error(tool_errors.RESOURCE_NOT_FOUND, "nope")
    assert tool_errors.extract_error_class(payload) == "resource_not_found"
    # 容忍尾端附註（raw_decode 只解析第一個 JSON 值）
    assert tool_errors.extract_error_class(payload + "\n[時間附註]") == "resource_not_found"
    # 非結構化錯誤 / 空字串 / 狀態 payload → None
    assert tool_errors.extract_error_class("Success: done") is None
    assert tool_errors.extract_error_class("") is None
    assert tool_errors.extract_error_class('{"status": "approval_pending"}') is None


def test_build_and_detect_tool_success():
    """P2a-2：結構化成功 payload（ok 為第一個 key）——供執行層穩健判定成功。"""
    payload = tool_errors.build_tool_success("Edited x", preview="  9| a\n> 10| b")
    data = json.loads(payload)
    assert data["ok"] is True
    assert data["message"] == "Edited x"
    assert data["preview"] == "  9| a\n> 10| b"
    assert payload.startswith('{"ok"')
    assert tool_errors.is_tool_success(payload)
    # 結構化成功 ≠ 錯誤；且屬結構化 payload（不被時間尾綴破壞 JSON）
    assert not tool_errors.is_tool_error(payload)
    assert tool_errors.is_structured_payload(payload)
    # 非結構化成功字串 / 空 → 否
    assert not tool_errors.is_tool_success("Success: plain text")
    assert not tool_errors.is_tool_success("")


@pytest.mark.asyncio
async def test_structured_success_with_emoji_marked_success():
    """P2a-2 關鍵：結構化成功即使 preview 含 ❌／Error: 仍判 success（根治子字串誤判）。"""

    async def _edit_like(**_kwargs: Any) -> str:
        return tool_errors.build_tool_success(
            "Edited config.py",
            preview="   9| ok\n> 10| raise ValueError('Error: boom')  # ❌ marker",
        )

    executor = ToolExecutor(None, {"edit_like": _edit_like}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        result, events = await _run(executor, "edit_like", {"a": 1}, _make_ctx())
    assert _tool_result_event(events)["status"] == "success"
    assert tool_errors.is_tool_success(result)


# --------------------------------------------------------------------------- #
# ToolExecutor 錯誤路徑 → 結構化
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_policy_deny_returns_structured_permission_denied():
    executor = ToolExecutor(None, {}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="DENY", reason="blocked by policy")
        result, events = await _run(executor, "bash", {"command": "ls"}, _make_ctx())
    data = json.loads(result)
    assert data["error_class"] == "permission_denied"
    assert data["denied_tool"] == "bash"
    assert "blocked by policy" in data["reason"]
    assert data.get("suggestion")
    ev = _tool_result_event(events)
    assert ev["status"] == "error"
    assert ev["result"] == result


@pytest.mark.asyncio
async def test_unknown_tool_returns_resource_not_found():
    executor = ToolExecutor(None, {}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        result, events = await _run(executor, "dream_tool", {"x": 1}, _make_ctx())
    data = json.loads(result)
    assert data["error_class"] == "resource_not_found"
    assert "dream_tool" in data["message"]
    assert _tool_result_event(events)["status"] == "error"


@pytest.mark.asyncio
async def test_identity_missing_returns_internal_error():
    executor = ToolExecutor(None, {}, {}, {})
    result, events = await _run(executor, "research", {"query": "x"}, {"identity": None})
    data = json.loads(result)
    assert data["error_class"] == "internal_error"
    assert _tool_result_event(events)["status"] == "error"


@pytest.mark.asyncio
async def test_critical_tool_empty_args_returns_validation_error():
    executor = ToolExecutor(None, {}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        result, _events = await _run(executor, "update_my_soul", {}, _make_ctx())
    data = json.loads(result)
    assert data["error_class"] == "validation_error"
    assert "update_my_soul" in data["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_class"),
    [
        (_BOOM_TYPE, "validation_error"),
        (_BOOM_FNF, "resource_not_found"),
        (_BOOM_PERM, "permission_denied"),
        (_BOOM_TIMEOUT, "timeout_exceeded"),
        (_BOOM_RUNTIME, "internal_error"),
    ],
)
async def test_exception_mapping_to_error_class(exc: Exception, expected_class: str):
    executor = ToolExecutor(None, {"boom": _raiser(exc)}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        result, events = await _run(executor, "boom", {"a": 1}, _make_ctx())
    data = json.loads(result)
    assert data["error_class"] == expected_class
    # 既有事件契約：例外路徑發 type=error（無 tool_result），不在此擴大
    assert any(e.get("type") == "error" for e in events)


@pytest.mark.asyncio
async def test_tool_returning_structured_error_marks_event_error():
    """工具自身回傳結構化錯誤時：狀態列 error、且不被時間注入尾綴破壞 JSON。"""

    async def _fake_tool(**_kwargs: Any) -> str:
        return tool_errors.build_tool_error(tool_errors.UPSTREAM_ERROR, "backend 500")

    executor = ToolExecutor(None, {"fake": _fake_tool}, {}, {})
    with patch("src.services.tool_executor.permission_service") as perm:
        perm.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
        result, events = await _run(executor, "fake", {"q": 1}, _make_ctx())
    assert _tool_result_event(events)["status"] == "error"
    assert json.loads(result)["error_class"] == "upstream_error"

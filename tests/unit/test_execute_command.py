"""bash → execute_command 非同步審批（ADR-0119 遞延項落地）——TDD 紅燈.

現況：bash 的危險指令走同步 HITL——`request_approval` 阻塞 ReAct 迴圈最多
180 秒（60s×3），模型整個卡死等人。重設計（提案 §7）：

  1. `execute_command`（正名）＋ CONFIRM 時**立即回傳** `approval_pending`
     （含 execution_id）——模型可繼續其他工作；審批等待與核准後執行都在
     背景任務進行（審批端點／SSE／前端完全不變）。
  2. `get_command_result(execution_id, wait_seconds?)` 取回結果：未核准＝
     pending、執行中＝running、拒絕/逾時＝結構化 permission_denied/timeout、
     完成＝原 markdown 包裝輸出。結果依 agent 隔離（不可跨身分讀取）。
  3. ALLOW（安全指令）行為不變：立即執行、直接回結果。BLOCK 與「無 UI 相容
     模式」改回結構化錯誤（Tier 1 一致性）。
  4. 雙軌：`bash` 保留為 shim（可執行、不再廣告）、委派 execute_command
     （零重複邏輯——舊名同享非同步語義）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.tool_executor import CRITICAL_TOOLS
from src.tools import terminal
from src.tools.builtin import tool_registry
from src.tools.builtin.schemas import builtin_tools_schema

_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")


def _ads() -> set[str]:
    return {s["function"]["name"] for s in builtin_tools_schema}


@pytest.fixture(autouse=True)
def _clean_async_state():
    terminal.reset_async_state_for_test()
    yield
    terminal.reset_async_state_for_test()


# --------------------------------------------------------------------------- #
# 1. Schema／註冊／廣告翻轉
# --------------------------------------------------------------------------- #


def test_schemas_and_advertising():
    ads = _ads()
    assert "execute_command" in ads
    assert "get_command_result" in ads
    assert "bash" not in ads  # 雙軌：shim 不再廣告

    exec_schema = next(
        s for s in builtin_tools_schema if s["function"]["name"] == "execute_command"
    )
    params = exec_schema["function"]["parameters"]
    assert params["required"] == ["command"]
    assert set(params["properties"]["env"]["enum"]) == {"powershell", "wsl"}
    # 非同步語義要寫進描述（模型需知道 pending 後怎麼辦）
    assert "get_command_result" in exec_schema["function"]["description"]

    result_schema = next(
        s for s in builtin_tools_schema if s["function"]["name"] == "get_command_result"
    )
    assert result_schema["function"]["parameters"]["required"] == ["execution_id"]


def test_registry_and_critical_tools():
    for name in ("execute_command", "get_command_result", "bash"):
        assert name in tool_registry
    for name in ("execute_command", "get_command_result", "bash"):
        assert name in CRITICAL_TOOLS


# --------------------------------------------------------------------------- #
# 2. ALLOW：行為不變（立即執行、直接回結果）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_allow_executes_inline():
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="ALLOW"),
        patch.object(
            terminal, "_execute_core", new=AsyncMock(return_value=(0, "hello-out", 12))
        ),
    ):
        result = await terminal.execute_command(
            command="echo hi", identity=_IDENTITY, session_id="s1"
        )
    assert "hello-out" in result
    assert "exit 0" in result  # 原 markdown 包裝格式沿用


# --------------------------------------------------------------------------- #
# 3. CONFIRM：立即回 approval_pending、背景核准後可取結果
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirm_returns_pending_immediately_then_result_available():
    approved = AsyncMock(return_value=(True, {}))
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_is_touka_ui_available", return_value=True),
        patch("src.routers.terminal.request_approval", approved),
        patch.object(
            terminal, "_execute_core", new=AsyncMock(return_value=(0, "danger-out", 34))
        ),
    ):
        result = await terminal.execute_command(
            command="rm -rf sandbox", identity=_IDENTITY, session_id="s1"
        )
        payload = json.loads(result)
        assert payload["status"] == "approval_pending"
        exec_id = payload["execution_id"]
        assert exec_id
        assert not tool_errors.is_tool_error(result)  # pending 不是錯誤

        # 背景任務完成後可取回結果（wait_seconds 內輪詢）
        final = await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )
    assert "danger-out" in final
    assert "exit 0" in final
    approved.assert_awaited()  # 審批機制真的被呼叫（背景）


@pytest.mark.asyncio
async def test_confirm_rejected_yields_legible_denial():
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_is_touka_ui_available", return_value=True),
        patch("src.routers.terminal.request_approval", AsyncMock(return_value=(False, {}))),
        patch.object(terminal, "_execute_core", new=AsyncMock()) as core,
    ):
        result = await terminal.execute_command(
            command="rm -rf sandbox", identity=_IDENTITY, session_id="s1"
        )
        exec_id = json.loads(result)["execution_id"]
        final = await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )
    core.assert_not_awaited()  # 被拒絕 → 絕不執行
    assert tool_errors.is_tool_error(final)
    assert json.loads(final)["error_class"] == tool_errors.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_pending_status_while_awaiting_approval():
    gate = asyncio.Event()

    async def _slow_approval(*_args: Any, **_kwargs: Any) -> tuple[bool, dict]:
        await gate.wait()
        return (True, {})

    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_is_touka_ui_available", return_value=True),
        patch("src.routers.terminal.request_approval", _slow_approval),
        patch.object(
            terminal, "_execute_core", new=AsyncMock(return_value=(0, "late", 1))
        ),
    ):
        result = await terminal.execute_command(
            command="rm x", identity=_IDENTITY, session_id="s1"
        )
        exec_id = json.loads(result)["execution_id"]

        probe = await terminal.get_command_result(
            execution_id=exec_id, identity=_IDENTITY
        )
        assert json.loads(probe)["status"] == "approval_pending"  # 還在等審批

        gate.set()  # 使用者核准
        final = await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )
    assert "late" in final


# --------------------------------------------------------------------------- #
# 4. BLOCK／無 UI：結構化錯誤（Tier 1 一致性）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_block_is_structured_denial():
    with patch.object(terminal, "_classify_command_strategy", return_value="BLOCK"):
        result = await terminal.execute_command(
            command="format c:", identity=_IDENTITY
        )
    assert tool_errors.is_tool_error(result)
    assert json.loads(result)["error_class"] == tool_errors.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_no_ui_compat_mode_is_structured():
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_is_touka_ui_available", return_value=False),
    ):
        result = await terminal.execute_command(
            command="rm x", identity=_IDENTITY
        )
    assert tool_errors.is_tool_error(result)
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.PERMISSION_DENIED
    assert "ToukaUI" in (payload.get("suggestion") or "") + payload["message"]


# --------------------------------------------------------------------------- #
# 5. get_command_result：未知 id／跨身分隔離
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_result_unknown_id():
    result = await terminal.get_command_result(
        execution_id="nope1234", identity=_IDENTITY
    )
    assert tool_errors.is_tool_error(result)
    assert json.loads(result)["error_class"] == tool_errors.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_get_result_is_identity_scoped():
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_is_touka_ui_available", return_value=True),
        patch("src.routers.terminal.request_approval", AsyncMock(return_value=(True, {}))),
        patch.object(
            terminal, "_execute_core", new=AsyncMock(return_value=(0, "secret-out", 1))
        ),
    ):
        result = await terminal.execute_command(
            command="rm x", identity=_IDENTITY, session_id="s1"
        )
        exec_id = json.loads(result)["execution_id"]
        await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )  # 等背景完成

        other = AgentIdentity.for_role(agent_id="SomeoneElse", role="Owner")
        stolen = await terminal.get_command_result(
            execution_id=exec_id, identity=other
        )
    # 不可跨 agent 讀取——回 not_found（不洩漏存在性）
    assert tool_errors.is_tool_error(stolen)
    assert json.loads(stolen)["error_class"] == tool_errors.RESOURCE_NOT_FOUND


# --------------------------------------------------------------------------- #
# 6. 雙軌 shim：bash 委派新實作（同享非同步語義）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bash_shim_delegates():
    with (
        patch.object(terminal, "_classify_command_strategy", return_value="ALLOW"),
        patch.object(
            terminal, "_execute_core", new=AsyncMock(return_value=(0, "via-shim", 5))
        ),
    ):
        result = await terminal.bash(
            command="echo hi", identity=_IDENTITY, session_id="s1"
        )
    assert "via-shim" in result

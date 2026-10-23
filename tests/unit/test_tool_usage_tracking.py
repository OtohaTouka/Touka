"""T3：這一輪的工具使用分成「試過」「執行過」「存成功」。

- 試過（集合本身）：模型發起的每個呼叫，被擋下也算——幻覺偵測用（禁令 59(c)），被擋的呼叫不算幻覺。
- 執行過（``executed``）：真正開始執行（送出工具開始事件）才記；查證守衛與 e_index 看它。
- 存成功（``succeeded``）：執行結果判定成功才記；記憶守衛只看它。

全部走 conftest 沙盒與 mock：不連外、不讀 data/。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import approval_flow, tool_errors
from src.services.auto_reviewer import auto_reviewer
from src.services.guard_service import guard_service
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor, record_tool_usage
from src.utils.tool_usage import (
    ToolUsage,
    executed_tools,
    record_tool_executed,
    record_tool_succeeded,
    succeeded_tools,
)

_OWNER = AgentIdentity.for_role(agent_id="UsageOwner", role="Owner")
_PROMISE = "好的，我已經幫你記住了。"
_RECALL = "根據過去的記憶系統內容，你喜歡烏龍茶。"
_SAVE = {"action": "save", "content": "x"}


# --- 集合與記錄函式 ------------------------------------------------------------


def test_attempt_only_fills_the_set_itself():
    used = ToolUsage()
    record_tool_usage(used, "memory_tools", _SAVE)

    assert used == {"memory_tools", "memory_tools.save"}
    assert used.executed == set()
    assert used.succeeded == set()


def test_executed_and_succeeded_record_name_and_qualified_action():
    used = ToolUsage()
    record_tool_executed(used, "memory_tools", _SAVE)
    record_tool_succeeded(used, "read", {"scope": "vault"})

    assert used.executed == {"memory_tools", "memory_tools.save"}
    assert used.succeeded == {"read"}
    assert used == set()  # 沒有經過「試過」就不進集合本身


@pytest.mark.parametrize("used", [None, set()])
def test_plain_sets_and_none_keep_the_old_single_set(used):
    """只給一個集合的呼叫端（舊的測試替身）：沒有執行紀錄可記，守衛照舊看那個集合."""
    record_tool_executed(used, "memory_tools", _SAVE)
    record_tool_succeeded(used, "memory_tools", _SAVE)
    if used is not None:
        assert used == set()
        used.add("memory_tools.save")
        assert executed_tools(used) is used
        assert succeeded_tools(used) is used


# --- 守衛 ------------------------------------------------------------------


def test_blocked_save_still_triggers_the_memory_warning():
    used = ToolUsage()
    record_tool_usage(used, "memory_tools", _SAVE)
    record_tool_executed(used, "memory_tools", _SAVE)  # 開始執行了，但沒成功

    assert guard_service.check_memory_hallucination(_PROMISE, used) is True


def test_successful_save_suppresses_the_memory_warning():
    used = ToolUsage()
    for record in (record_tool_usage, record_tool_executed, record_tool_succeeded):
        record(used, "memory_tools", _SAVE)

    assert guard_service.check_memory_hallucination(_PROMISE, used) is False


def test_memory_warning_says_nothing_was_saved_and_keeps_its_label():
    message = guard_service.get_memory_warning_message()
    assert "[記憶幻覺警告]" in message  # 歷史清理靠這個標籤辨認
    assert "沒有成功" in message
    assert "memory_tools" in message


def test_blocked_verification_tool_does_not_count_as_checked():
    used = ToolUsage()
    record_tool_usage(used, "memory_tools", {"action": "query", "query": "茶"})

    assert guard_service.check_fact_without_tools(_RECALL, used) is True
    record_tool_executed(used, "memory_tools", {"action": "query", "query": "茶"})
    assert guard_service.check_fact_without_tools(_RECALL, used) is False


def test_blocked_call_is_not_reported_as_a_hallucination():
    used = ToolUsage()
    record_tool_usage(used, "memory_tools", _SAVE)
    text = "🔧 memory_tools 執行中…"

    assert guard_service.detect_tool_hallucinations(text, used, {"memory_tools"}) == []


# --- 執行器 ------------------------------------------------------------------


@dataclass
class _Decision:
    effect: str
    reason: str = "test"


@pytest.fixture
def gates(monkeypatch):
    """PaC 一律放行、審查員與審批卡不應被叫到（叫到就記下）."""
    monkeypatch.setattr(permission_service, "evaluate_policy", lambda _ctx: _Decision("ALLOW"))
    review = AsyncMock(return_value=SimpleNamespace(verdict="allow", reason="r", code="c"))
    card = AsyncMock(return_value="approved")
    monkeypatch.setattr(auto_reviewer, "review", review)
    monkeypatch.setattr(approval_flow, "await_human_approval", card)
    return SimpleNamespace(review=review, card=card)


def _ctx(native: set[str], **extra: Any) -> PipelineContext:
    ctx = PipelineContext(
        identity=_OWNER,
        agent_id=_OWNER.agent_id,
        auth_level="Manual",
        advertised_tools=AdvertisedTools(native=frozenset(native), mcp=frozenset()),
    )
    for key, value in extra.items():
        ctx[key] = value
    return ctx


async def _run(tools: dict, name: str, ctx: PipelineContext, args: dict) -> ToolUsage:
    used = ToolUsage()
    queue: asyncio.Queue = asyncio.Queue()
    executor = ToolExecutor(None, tools, {}, {}, enforce_advertised=True)
    await executor.execute_to_queue(
        name, dict(args), "req-t3", ctx, queue, call_id="c1", actually_used_tools=used
    )
    return used


async def test_unlisted_call_is_attempted_but_never_executed(gates):
    backend = AsyncMock(return_value="saved")
    used = await _run({"memory_tools": backend}, "memory_tools", _ctx({"read"}), _SAVE)

    backend.assert_not_awaited()
    assert used == {"memory_tools", "memory_tools.save"}
    assert used.executed == set()
    assert used.succeeded == set()


async def test_successful_call_is_executed_and_succeeded(gates):
    handler = AsyncMock(return_value="files")
    used = await _run({"list": handler}, "list", _ctx({"read", "list"}), {"scope": "vault"})

    handler.assert_awaited_once()
    assert used.executed == {"list"}
    assert used.succeeded == {"list"}


async def test_structured_error_is_executed_but_not_succeeded(gates):
    failure = tool_errors.build_tool_error(tool_errors.INTERNAL_ERROR, "backend down")
    handler = AsyncMock(return_value=failure)
    used = await _run({"list": handler}, "list", _ctx({"read", "list"}), {"scope": "vault"})

    handler.assert_awaited_once()
    assert used.executed == {"list"}
    assert used.succeeded == set()


async def test_raising_handler_is_executed_but_not_succeeded(gates):
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    used = await _run({"list": handler}, "list", _ctx({"read", "list"}), {"scope": "vault"})

    assert used.executed == {"list"}
    assert used.succeeded == set()

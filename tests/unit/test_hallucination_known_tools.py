"""禁令 59(c)：幻覺偵測器的「已知工具集」必須涵蓋 MCP 工具——SSOT 下沉 guard_service.

歷史：Tier 1 修正原只落在 claude_handler 的私有 `collect_known_tool_names`
（openai/gemini 仍用三個靜態 registry ＝ MCP 幻覺假陰性、違反禁令 59(c)）。
漂移修復批 C1（提案 handler-drift-fix-batch）將其下沉為
`guard_service.collect_known_tool_names`（SSOT），三 handler 一律呼叫；
claude 的 inline 偵測副本（re.DOTALL 跨段落誤殺＋未 re.escape）一併退役。
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from src.handlers import react_core
from src.services.guard_service import guard_service

# react-core-extraction E4：三 handler 的迴圈（含 guard 呼叫點）已單源於 react_core
# （薄殼零邏輯由 test_react_core_ssot.py 守衛錨把關）。
_HANDLER_MODULES = {
    "react_core.py": react_core,
}


@pytest.mark.asyncio
async def test_includes_static_and_mcp_tools():
    pm = AsyncMock()
    pm.get_all_tools.return_value = [
        {"function": {"name": "srv__draw"}},
        {"name": "bare_tool"},
    ]
    names = await guard_service.collect_known_tool_names({"a": 1}, {"b": 2}, {"c": 3}, pm)
    assert {"a", "b", "c", "srv__draw", "bare_tool"} <= names


@pytest.mark.asyncio
async def test_without_protocol_manager_returns_statics():
    names = await guard_service.collect_known_tool_names({"a": 1}, {}, {}, None)
    assert names == {"a"}


@pytest.mark.asyncio
async def test_mcp_failure_falls_back_to_statics():
    pm = AsyncMock()
    pm.get_all_tools.side_effect = RuntimeError("mcp down")
    names = await guard_service.collect_known_tool_names({"a": 1}, {}, {}, pm)
    assert names == {"a"}


# ── C1 SSOT 接線（靜態確認，比照禁令 26 的 no-inline-validation 範式）──────────


def _src(name: str) -> str:
    # inspect.getsource（非檔案 I/O）＝靜態源碼確認的正統路徑（I/O 監獄豁免不適用測試）
    return inspect.getsource(_HANDLER_MODULES[name])


@pytest.mark.parametrize("handler_file", ["react_core.py"])
def test_all_handlers_use_guard_ssot_known_tools(handler_file: str):
    """三 handler 共用的迴圈核心以 guard_service SSOT（MCP-aware）取得已知工具集。"""
    assert "guard_service.collect_known_tool_names(" in _src(handler_file)


def test_claude_inline_detector_retired():
    """claude 的私有副本與 inline 偵測（re.DOTALL＋未 escape）已退役（今單源於核心）。"""
    src = _src("react_core.py")
    assert "async def collect_known_tool_names" not in src
    assert "re.DOTALL" not in src


def test_detect_tool_hallucinations_handles_regex_metachar_names():
    """MCP 工具名含 regex 特殊字元不得炸（claude 舊 inline 版未 escape＝re.error）。"""
    warnings = guard_service.detect_tool_hallucinations(
        "> weird[tool].v2 執行完成",
        actually_used_tools=set(),
        all_known_tools={"weird[tool].v2"},
    )
    assert warnings == ["weird[tool].v2"]

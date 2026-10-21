"""P0-D：工具 schema token 成本量測（Claude Code 15k 教訓）——TDD.

逐工具把 schema JSON 序列化後過 Touka token 估算器 SSOT（ADR-0092，CJK 感知、
fail-safe），回傳「由大到小的 (工具名, tokens)」＋總計。純函式、可測。
"""

from __future__ import annotations

from . import token_cost


def test_per_tool_sums_to_total_and_sorted_desc():
    tools = [
        {"type": "function", "function": {"name": "a", "description": "x", "parameters": {}}},
        {
            "type": "function",
            "function": {"name": "b", "description": "y" * 500, "parameters": {}},
        },
    ]
    per, total = token_cost.measure_tool_token_cost(tools)
    assert sum(n for _, n in per) == total
    assert [n for _, n in per] == sorted((n for _, n in per), reverse=True)  # 由大到小
    assert per[0][0] == "b"  # b 較長 → 排最前
    assert {name for name, _ in per} == {"a", "b"}


def test_empty_tools_zero():
    per, total = token_cost.measure_tool_token_cost([])
    assert per == []
    assert total == 0


def test_handles_anthropic_name_only_shape():
    # Anthropic 形狀 {name, description, input_schema}（無 function 包裝）
    tools = [{"name": "c", "description": "z", "input_schema": {"type": "object"}}]
    per, total = token_cost.measure_tool_token_cost(tools)
    assert per[0][0] == "c"
    assert total > 0


def test_unnamed_tool_is_safe():
    per, _total = token_cost.measure_tool_token_cost([{"garbage": True}])
    assert per[0][0] == "<unnamed>"  # 畸形項不炸

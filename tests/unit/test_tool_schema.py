"""TDD — 軌C 階段0/1：工具 schema SSOT（tool_schema）＋ convert_to_openai 不丟工具。"""
from __future__ import annotations

from src.adapters import tool_schema
from src.adapters.schemas import AnthropicRequest
from src.middleware.layers.tool import ToolLayer
from src.services.dispatchers.utils import convert_to_openai

_OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_tools",
        "description": "查詢/儲存記憶",
        "parameters": {"type": "object", "properties": {"action": {"type": "string"}}},
    },
}
_ANTHROPIC_TOOL = {
    "name": "memory_tools",
    "description": "查詢/儲存記憶",
    "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}},
}


# ---- openai_to_anthropic_tools ----

def test_openai_to_anthropic_basic():
    out = tool_schema.openai_to_anthropic_tools([_OPENAI_TOOL])
    assert out == [_ANTHROPIC_TOOL]


# ---- apply_tool_cache_breakpoint（P4：Anthropic prompt 快取前綴優化）----


def test_cache_breakpoint_marks_last_tool_only():
    tools = [dict(_ANTHROPIC_TOOL), {**_ANTHROPIC_TOOL, "name": "research"}]
    out = tool_schema.apply_tool_cache_breakpoint(tools)
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoint_is_idempotent():
    # 既有斷點（舊的）先清掉，只在最後一個重標——避免超過斷點上限
    tools = [
        {**_ANTHROPIC_TOOL, "cache_control": {"type": "ephemeral"}},
        {**_ANTHROPIC_TOOL, "name": "research"},
    ]
    out = tool_schema.apply_tool_cache_breakpoint(tools)
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoint_does_not_mutate_input():
    tools = [dict(_ANTHROPIC_TOOL)]
    tool_schema.apply_tool_cache_breakpoint(tools)
    assert "cache_control" not in tools[0]  # 純函式：原輸入不變


def test_cache_breakpoint_empty_and_none():
    assert tool_schema.apply_tool_cache_breakpoint([]) == []
    assert tool_schema.apply_tool_cache_breakpoint(None) is None


# ---- to_cached_system（P4b：system 前綴快取，str→block list）----


def test_to_cached_system_str_becomes_cached_block():
    out = tool_schema.to_cached_system("You are Touka.")
    assert out == [
        {"type": "text", "text": "You are Touka.", "cache_control": {"type": "ephemeral"}}
    ]


def test_to_cached_system_list_marks_last_text_block_idempotent():
    system = [
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "b"},
    ]
    out = tool_schema.to_cached_system(system)
    assert "cache_control" not in out[0]  # 舊斷點清掉
    assert out[1]["cache_control"] == {"type": "ephemeral"}  # 只標最後一個 text block


def test_to_cached_system_empty_and_none_passthrough():
    assert tool_schema.to_cached_system("") == ""
    assert tool_schema.to_cached_system(None) is None


def test_to_cached_system_does_not_mutate_input():
    system = [{"type": "text", "text": "a"}]
    tool_schema.to_cached_system(system)
    assert "cache_control" not in system[0]  # 純函式：原輸入不變


# ---- to_cached_system 穩定前綴細分（P4c：persona 跨回合快取）----


def test_to_cached_system_splits_stable_prefix_from_volatile():
    system = "<persona>Touka</persona>\n<mission_awareness>progress 2/5</mission_awareness>"
    out = tool_schema.to_cached_system(system, volatile_tags=("mission_awareness",))
    assert len(out) == 2
    # 穩定前綴（persona）獨立成塊、帶斷點→跨回合命中；不含波動 tag
    assert "<persona>" in out[0]["text"]
    assert "mission_awareness" not in out[0]["text"]
    assert out[0]["cache_control"] == {"type": "ephemeral"}
    # 波動後綴帶斷點→迴圈內命中
    assert "<mission_awareness>" in out[1]["text"]
    assert out[1]["cache_control"] == {"type": "ephemeral"}


def test_to_cached_system_no_volatile_tag_single_block():
    system = "<persona>Touka</persona>\n<tool_availability>x</tool_availability>"
    out = tool_schema.to_cached_system(system, volatile_tags=("mission_awareness",))
    assert len(out) == 1
    assert out[0]["cache_control"] == {"type": "ephemeral"}


def test_to_cached_system_volatile_at_start_single_block():
    # 穩定前綴僅空白 → 不切（避免無意義的小快取塊）
    out = tool_schema.to_cached_system(
        "\n<mission_awareness>x</mission_awareness>", volatile_tags=("mission_awareness",)
    )
    assert len(out) == 1


def test_to_cached_system_default_no_split_backcompat():
    # 預設無 volatile_tags → 維持 ADR-0128 整段單一區塊
    out = tool_schema.to_cached_system(
        "<persona>a</persona>\n<mission_awareness>b</mission_awareness>"
    )
    assert len(out) == 1


# ---- reorder_system_stable_first（P4d：中介層順序重排 → 擴大穩定前綴）----

# 生產組裝順序（soul 路徑）：persona → conversation_summary(波動) →
# tool_availability(穩定) → validation_protocol(穩定) → mission_awareness(波動)。
# 波動的 conversation_summary 夾在 persona 與穩定工具區塊之間，使 to_cached_system
# 於此切開、把穩定的 tool_availability/validation_protocol 困在波動後綴（不跨回合快取）。
_VOLATILE = ("mission_awareness", "conversation_summary")
_TRAPPED_SYSTEM = (
    "<persona>Touka soul</persona>\n\n"
    "<conversation_summary>trimmed chat</conversation_summary>\n"
    "<tool_availability>filtered note</tool_availability>\n"
    "<validation_protocol>validate after edits</validation_protocol>\n"
    "<mission_awareness>progress 2/5</mission_awareness>\n"
)


def test_reorder_hoists_trapped_stable_blocks_before_volatile():
    out = tool_schema.reorder_system_stable_first(_TRAPPED_SYSTEM, volatile_tags=_VOLATILE)
    # 穩定的工具區塊被移到所有波動區塊之前
    assert out.index("<tool_availability>") < out.index("<conversation_summary>")
    assert out.index("<validation_protocol>") < out.index("<mission_awareness>")
    # persona 仍在最前（身份錨點不得移位）
    assert out.index("<persona>") == 0


def test_reorder_noop_when_volatile_already_trailing():
    # 短對話（無 conversation_summary）：mission_awareness 已在尾端 → 原樣不動
    system = (
        "<persona>Touka</persona>\n"
        "<tool_availability>x</tool_availability>\n"
        "<mission_awareness>2/5</mission_awareness>\n"
    )
    out = tool_schema.reorder_system_stable_first(system, volatile_tags=_VOLATILE)
    assert out == system


def test_reorder_is_idempotent():
    once = tool_schema.reorder_system_stable_first(_TRAPPED_SYSTEM, volatile_tags=_VOLATILE)
    twice = tool_schema.reorder_system_stable_first(once, volatile_tags=_VOLATILE)
    assert twice == once


def test_reorder_preserves_every_block_verbatim():
    out = tool_schema.reorder_system_stable_first(_TRAPPED_SYSTEM, volatile_tags=_VOLATILE)
    for block in (
        "<persona>Touka soul</persona>",
        "<conversation_summary>trimmed chat</conversation_summary>",
        "<tool_availability>filtered note</tool_availability>",
        "<validation_protocol>validate after edits</validation_protocol>",
        "<mission_awareness>progress 2/5</mission_awareness>",
    ):
        assert block in out


def test_reorder_then_cached_system_widens_stable_prefix():
    # 端到端：重排 → 切分後，穩定工具區塊進入跨回合快取前綴（out[0]）
    reordered = tool_schema.reorder_system_stable_first(_TRAPPED_SYSTEM, volatile_tags=_VOLATILE)
    out = tool_schema.to_cached_system(reordered, volatile_tags=_VOLATILE)
    assert len(out) == 2
    stable_prefix = out[0]["text"]
    assert "<tool_availability>" in stable_prefix
    assert "<validation_protocol>" in stable_prefix
    assert "<conversation_summary>" not in stable_prefix
    assert "<mission_awareness>" not in stable_prefix


def test_reorder_passthrough_non_str_empty_and_no_tags():
    # 非 str／空／None／未給 volatile_tags → 原樣傳回（不動）
    assert tool_schema.reorder_system_stable_first("", volatile_tags=("x",)) == ""
    assert tool_schema.reorder_system_stable_first(None, volatile_tags=("x",)) is None
    lst = [{"type": "text", "text": "a"}]
    assert tool_schema.reorder_system_stable_first(lst, volatile_tags=("x",)) is lst
    no_tags = "<persona>a</persona>\n<mission_awareness>b</mission_awareness>"
    assert tool_schema.reorder_system_stable_first(no_tags) == no_tags


def test_openai_to_anthropic_skips_non_function():
    out = tool_schema.openai_to_anthropic_tools([{"type": "other"}, _OPENAI_TOOL])
    assert len(out) == 1 and out[0]["name"] == "memory_tools"


def test_openai_to_anthropic_empty_returns_none():
    assert tool_schema.openai_to_anthropic_tools(None) is None
    assert tool_schema.openai_to_anthropic_tools([]) is None


def test_openai_to_anthropic_skips_invalid_name():
    """name=None／空字串 → 略過（避免送出 name=null 違反 Anthropic API）。"""
    bad = [
        {"type": "function", "function": {"name": None, "parameters": {}}},
        {"type": "function", "function": {"name": "  "}},
    ]
    assert tool_schema.openai_to_anthropic_tools(bad) is None


def test_anthropic_to_openai_handles_malformed_function():
    """function=None 不被當 OpenAI 直通（防下游 None.get 崩潰）；有 name 則視為 Anthropic。"""
    item = {"function": None, "name": "x", "input_schema": {}}
    expected = {"type": "function", "function": {"name": "x", "description": "", "parameters": {}}}
    assert tool_schema.anthropic_to_openai_tools([item]) == [expected]


def test_converters_non_list_returns_none():
    assert tool_schema.openai_to_anthropic_tools("not-a-list") is None
    assert tool_schema.anthropic_to_openai_tools({"not": "a-list"}) is None


def test_openai_to_anthropic_missing_parameters_defaults():
    out = tool_schema.openai_to_anthropic_tools([{"type": "function", "function": {"name": "x"}}])
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}


# ---- anthropic_to_openai_tools ----

def test_anthropic_to_openai_basic():
    out = tool_schema.anthropic_to_openai_tools([_ANTHROPIC_TOOL])
    assert out == [_OPENAI_TOOL]


def test_anthropic_to_openai_passthrough_openai_shape():
    # 已是 OpenAI 形狀（含 function 鍵）→ 直通
    out = tool_schema.anthropic_to_openai_tools([_OPENAI_TOOL])
    assert out == [_OPENAI_TOOL]


def test_anthropic_to_openai_empty_returns_none():
    assert tool_schema.anthropic_to_openai_tools(None) is None
    assert tool_schema.anthropic_to_openai_tools([]) is None


def test_roundtrip_openai_anthropic_openai():
    anth = tool_schema.openai_to_anthropic_tools([_OPENAI_TOOL])
    back = tool_schema.anthropic_to_openai_tools(anth)
    assert back == [_OPENAI_TOOL]


# ---- Phase 1 回歸：convert_to_openai 帶上工具（不再寫死 None）----

def test_convert_to_openai_includes_tools():
    req = AnthropicRequest(
        model="deepseek-v4-flash",
        max_tokens=512,
        messages=[{"role": "user", "content": "hi"}],
        tools=[_ANTHROPIC_TOOL],
    )
    converted = convert_to_openai(req, degraded=False, reason="")
    assert converted.tools == [_OPENAI_TOOL]


def test_convert_to_openai_no_tools_stays_none():
    req = AnthropicRequest(
        model="deepseek-v4-flash",
        max_tokens=512,
        messages=[{"role": "user", "content": "hi"}],
    )
    converted = convert_to_openai(req, degraded=False, reason="")
    assert converted.tools is None


# ---- 階段 2/3：canonical OpenAI 工具、冪等出場轉換、anti-drift ----

def test_openai_to_anthropic_idempotent_on_anthropic():
    """已是 Anthropic 形狀 → 直通（claude_handler 出場可對兩種格式安全呼叫）。"""
    assert tool_schema.openai_to_anthropic_tools([_ANTHROPIC_TOOL]) == [_ANTHROPIC_TOOL]


def test_toollayer_injects_canonical_openai():
    """anti-drift：ToolLayer 注入後 request.tools 維持 OpenAI 形狀（不再轉成 Anthropic）。"""
    class _Req:
        def __init__(self) -> None:
            self.tools: list = []

    req = _Req()
    ToolLayer()._inject_tools(req, [_OPENAI_TOOL])
    assert req.tools == [_OPENAI_TOOL]
    assert req.tools[0]["type"] == "function"

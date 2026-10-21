"""跨廠商工具出場形狀（離線）：實際列給模型的工具經各家 adapter 後，必須符合該家的規定。

mission_tools、memory_tools、edit_soul 的參數正本根層帶 oneOf（條件必填）。Anthropic 不收
根層 oneOf／anyOf／allOf，整包 400；Gemini 出場本來就剝，OpenAI 相容與 Responses 照收。
既有測試只比語意、沒核對送給各家的最後形狀，所以一直沒抓到。

adapter 只跑 ``prepare_request``：不送出、不建連線（Claude 用假 client，Responses 不給金鑰
就不建 client），不連任何廠商（禁令 61）。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory_server.tools import memory_tools_schema
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.adapters.tool_schema import openai_to_anthropic_tools
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.tools.builtin.schemas import EDIT_SOUL_SCHEMA
from src.tools.mission_tools import MISSION_TOOLS_SCHEMA
from src.utils.permission_grants import NameGrant

_ROOT_COMBINATORS = {"oneOf", "anyOf", "allOf"}
# openai_to_gemini._convert_schema_to_gemini 列出的 Gemini 不支援欄位
_GEMINI_UNSUPPORTED = {"additionalProperties", "oneOf", "anyOf", "allOf", "const"}
# Gemini functionDeclarations.parameters（Schema 物件）可用的欄位，依官方 API 參考
# （ai.google.dev/api/generate-content 的 v1beta.Schema，2026-10-15 核對 22 個）；
# 不在表上的欄位（如 $ref、$defs、exclusiveMinimum）Gemini 會以 Unknown name 整包拒收。
_GEMINI_SCHEMA_FIELDS = {
    "type", "format", "title", "description", "nullable", "enum", "maxItems", "minItems",
    "properties", "required", "minProperties", "maxProperties", "minLength", "maxLength",
    "pattern", "example", "anyOf", "propertyOrdering", "default", "items", "minimum", "maximum",
}
_ROOT_ONEOF_NATIVE = {"mission_tools", "memory_tools", "edit_soul"}

# 外部 MCP 工具（protocol_manager.get_all_tools 的形狀；normalize 保證根層 type=object）
_MCP_ROOT_ONEOF = {
    "server": "ext",
    "tool": "lookup",
    "type": "function",
    "function": {
        "name": "ext__lookup",
        "description": "root oneOf",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["id", "name"]},
                "id": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["mode"],
            "oneOf": [
                {"properties": {"id": {"type": "integer"}}, "required": ["id"]},
                {"required": ["name"]},
            ],
        },
    },
}
_MCP_BRANCH_ONLY = {
    "server": "ext",
    "tool": "send",
    "type": "function",
    "function": {
        "name": "ext__send",
        "description": "fields only inside branches",
        "parameters": {
            "type": "object",
            "$defs": {"Target": {"type": "object", "properties": {"addr": {"type": "string"}}}},
            "anyOf": [
                {
                    "properties": {
                        "email": {"type": "string"},
                        "target": {"$ref": "#/$defs/Target"},
                    },
                    "required": ["email"],
                },
                {
                    "properties": {"phone": {"type": "string"}, "email": {"type": "integer"}},
                    "required": ["phone"],
                },
            ],
            "allOf": [{"properties": {"note": {"type": "string"}}}],
        },
    },
}
# 本來就是 Anthropic 形狀（原生 /v1/messages 或 openai_to_claude 第一段的輸出）
_ANTHROPIC_SHAPED = {
    "name": "native_pick",
    "description": "already Anthropic shaped",
    "input_schema": {
        "type": "object",
        "properties": {"kind": {"type": "string"}, "detail": {"type": "object", "anyOf": []}},
        "required": ["kind"],
        "allOf": [{"required": ["kind"]}],
        "oneOf": [{"properties": {"extra": {"type": "boolean"}}}],
    },
}


def _branch_fields(schema: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for key in _ROOT_COMBINATORS:
        for branch in schema.get(key) or []:
            fields |= set(branch.get("properties") or {})
    return fields


def _declared_fields(schema: dict[str, Any]) -> set[str]:
    """正本宣告的全部欄位：根層 properties＋根層組合子各分支的 properties。"""
    return set(schema.get("properties") or {}) | _branch_fields(schema)


def _gemini_violations(node: Any, path: str = "") -> list[str]:
    """遞迴列出 schema 節點上的 Gemini 不支援欄位（只看 schema 關鍵字，不看欄位名）。"""
    if not isinstance(node, dict):
        return []
    bad = (_GEMINI_UNSUPPORTED & set(node)) | (set(node) - _GEMINI_SCHEMA_FIELDS)
    found = [f"{path}/{key}" for key in sorted(bad)]
    for name, sub in (node.get("properties") or {}).items():
        found += _gemini_violations(sub, f"{path}/properties/{name}")
    items = node.get("items")
    for i, sub in enumerate(items if isinstance(items, list) else [items]):
        found += _gemini_violations(sub, f"{path}/items[{i}]")
    return found


async def _listed_tools(mcp_tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """真的 ToolLayer（Owner、權限全開、帶記憶 schema）組出實際列給模型的工具。"""
    request = SimpleNamespace(
        tools=None,
        project_id=None,
        ide_context=None,
        system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
    ctx = PipelineContext(identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Owner"))
    ctx["agent_id"] = "DebugAgent"
    ctx["memory_tools_schema"] = memory_tools_schema
    if mcp_tools is not None:
        ctx["protocol_manager"] = SimpleNamespace(get_all_tools=AsyncMock(return_value=mcp_tools))
    permission = MagicMock()
    permission.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    permission.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    permission.is_mcp_server_allowed.return_value = True
    permission.is_mcp_tool_allowed.return_value = True
    permission.evaluate_policy.return_value = MagicMock(effect="ALLOW", reason=None)
    with patch("src.middleware.layers.tool.permission_service", permission):
        await ToolLayer().process(request, ctx)
    return request.tools


def _chat(model: str, tools: list[dict[str, Any]]) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model=model, messages=[{"role": "user", "content": "hi"}], tools=tools
    )


async def _anthropic_wire(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    state = await ClaudeAdapter().prepare_request(
        _chat("claude-sonnet-4-5", tools), {"anthropic_client": MagicMock()}
    )
    return {tool["name"]: tool["input_schema"] for tool in state.kwargs["tools"]}


def _canonical(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}


@pytest.fixture
async def listed() -> list[dict[str, Any]]:
    tools = await _listed_tools()
    names = {tool["function"]["name"] for tool in tools}
    assert names >= _ROOT_ONEOF_NATIVE  # 這三顆就是出事的工具，必須真的在列表裡
    for name in _ROOT_ONEOF_NATIVE:
        assert "oneOf" in _canonical(tools)[name]  # 正本仍帶條件必填（不改正本）
    return tools


async def test_anthropic_wire_has_no_root_combinators_and_keeps_every_field(listed):
    canonical = _canonical(listed)
    wire = await _anthropic_wire(listed)
    assert wire.keys() == canonical.keys()
    for name, schema in wire.items():
        assert not _ROOT_COMBINATORS & set(schema), name
        assert schema["type"] == "object", name
        declared = _declared_fields(canonical[name])
        assert declared <= set(schema.get("properties") or {}), name  # 剝完欄位不會變少
        assert schema.get("required", []) == canonical[name].get("required", []), name


async def test_gemini_wire_drops_every_unsupported_field(listed):
    state = await GeminiAdapter().prepare_request(_chat("gemini-2.5-flash", listed), {})
    declarations = state.payload["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in declarations} == set(_canonical(listed))
    for declaration in declarations:
        assert _gemini_violations(declaration["parameters"]) == [], declaration["name"]


async def test_openai_compatible_and_responses_wire_roots_are_objects(listed):
    compat = await OpenAIAdapter().prepare_request(
        _chat("deepseek-chat", listed),
        {"api_key": "k", "url": "http://fake.test", "provider": "DeepSeek"},
    )
    responses = await ResponsesAdapter().prepare_request(
        _chat("gpt-5", listed), {"base_url": "https://api.openai.test/v1"}
    )
    assert responses.client is None  # 沒給金鑰：不建 client、不可能送出
    compat_roots = {
        t["function"]["name"]: t["function"]["parameters"] for t in compat.payload["tools"]
    }
    responses_roots = {t["name"]: t["parameters"] for t in responses.kwargs["tools"]}
    for roots in (compat_roots, responses_roots):
        assert roots.keys() == _canonical(listed).keys()
        assert all(schema["type"] == "object" for schema in roots.values())


async def test_egress_never_mutates_module_level_schemas(listed):
    """ToolLayer 以參考放進模組層 schema：任何一家出場改到它，就會帶壞其他家與自己的檢查。"""
    snapshots = [copy.deepcopy(x) for x in (MISSION_TOOLS_SCHEMA, EDIT_SOUL_SCHEMA)]
    memory_snapshot = copy.deepcopy(memory_tools_schema)
    listed_snapshot = copy.deepcopy(listed)
    await _anthropic_wire(listed)
    await GeminiAdapter().prepare_request(_chat("gemini-2.5-flash", listed), {})
    assert snapshots == [MISSION_TOOLS_SCHEMA, EDIT_SOUL_SCHEMA]
    assert memory_snapshot == memory_tools_schema
    assert listed_snapshot == listed


async def test_mcp_root_oneof_tool_is_stripped_with_root_precedence():
    tools = await _listed_tools([copy.deepcopy(_MCP_ROOT_ONEOF)])
    schema = (await _anthropic_wire(tools))["ext__lookup"]
    assert not _ROOT_COMBINATORS & set(schema)
    assert schema["properties"]["id"] == {"type": "string"}  # 同名以根層為準
    assert set(schema["properties"]) == {"mode", "id", "name"}
    assert schema["required"] == ["mode"]  # 必填只留根層原有的


async def test_mcp_branch_only_fields_survive_stripping():
    tools = await _listed_tools([copy.deepcopy(_MCP_BRANCH_ONLY)])
    schema = (await _anthropic_wire(tools))["ext__send"]
    assert not _ROOT_COMBINATORS & set(schema)
    assert schema["type"] == "object"
    assert schema["properties"] == {
        "email": {"type": "string"},  # 分支之間先出現者為準
        "target": {"$ref": "#/$defs/Target"},
        "phone": {"type": "string"},
        "note": {"type": "string"},
    }
    assert schema["$defs"] == _MCP_BRANCH_ONLY["function"]["parameters"]["$defs"]  # $ref 還指得到
    assert "required" not in schema  # 分支的必填不升到根層


async def test_anthropic_shaped_passthrough_is_stripped_on_native_ingress():
    request = AnthropicRequest(
        model="claude-sonnet-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tools=[copy.deepcopy(_ANTHROPIC_SHAPED)],
    )
    state = await ClaudeAdapter().prepare_request(request, {"anthropic_client": MagicMock()})
    assert state.is_native_anthropic
    schema = state.kwargs["tools"][0]["input_schema"]
    assert not _ROOT_COMBINATORS & set(schema)
    assert set(schema["properties"]) == {"kind", "detail", "extra"}
    assert schema["properties"]["detail"]["anyOf"] == []  # 內層照留
    assert schema["required"] == ["kind"]


def test_anthropic_shaped_passthrough_returns_new_dicts():
    """直通分支另建新 dict：之後在輸出寫 cache_control 不會改到呼叫端的工具。"""
    tool = copy.deepcopy(_ANTHROPIC_SHAPED)
    tool["cache_control"] = {"type": "ephemeral"}
    snapshot = copy.deepcopy(tool)
    converted = openai_to_anthropic_tools([tool])
    assert converted is not None and converted[0] is not tool
    assert converted[0]["cache_control"] == {"type": "ephemeral"}  # 其他鍵照留
    assert not _ROOT_COMBINATORS & set(converted[0]["input_schema"])
    converted[0]["input_schema"]["properties"]["new"] = {}
    converted[0]["cache_control"] = {"type": "changed"}
    assert tool == snapshot


def test_passthrough_without_input_schema_gets_no_schema_added():
    """Anthropic 伺服器工具（type＋name、沒有 input_schema）照原樣複製，不補欄位。"""
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    converted = openai_to_anthropic_tools([tool])
    assert converted == [tool] and converted[0] is not tool


_Q_BRANCH = [{"properties": {"q": {"type": "string"}}}]


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "function", "function": {"name": "a", "parameters": {"anyOf": _Q_BRANCH}}},
        {"name": "b", "input_schema": {"oneOf": _Q_BRANCH}},
    ],
    ids=["openai-shape", "anthropic-shape"],
)
def test_missing_root_type_becomes_object(tool):
    snapshot = copy.deepcopy(tool)
    schema = openai_to_anthropic_tools([tool])[0]["input_schema"]
    assert schema == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert tool == snapshot

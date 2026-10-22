"""Provider Adapter 契約測試（react-core-extraction E1，紅測先行）。

本套件＝Protocol 語意的 SSOT：每個 adapter 都必須通過同一組義務。E1 掛上
OpenAIAdapter；E3 補 ClaudeAdapter 專屬義務（雙 egress、斷點輪替、ingress
對映實名）；ADR-0157 補三家 Honesty Guard（K2 推廣）＋gemini thought 分頻
（K3）。keepalive 已升核心（K1）＝tests/proxy/test_core_keepalive.py。核心
行為（迴圈/中斷/保險絲/SSE 時序）由特徵化 golden 套件把關，此處專測 adapter
的 wire 職責（request 整備/回合解析/歷史寫回/終端包裝）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from src.handlers.adapters.base import ToolCall, Turn
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.sampling import SAMPLING_PARAM_NAMES
from src.services.guard_service import guard_service
from src.services.rhythm_service import WRAP_UP_NOTE


def _request(messages: list[dict], **extra) -> MagicMock:
    request = MagicMock()
    request.model = extra.get("model", "gpt-4")
    dump = {"model": request.model, "max_tokens": 100, "messages": messages}
    dump.update(extra.get("dump_extra", {}))
    request.model_dump.return_value = dump
    request.messages = [SimpleNamespace(**m, tool_calls=None) for m in messages]
    return request


def _ctx(**extra) -> dict:
    ctx = {
        "api_key": "k", "url": "http://fake.test", "provider": "OpenAI",
        "request_id": "req-contract", "system_degraded": False, "degraded_reason": "",
    }
    ctx.update(extra)
    return ctx


@pytest.fixture
def adapter() -> OpenAIAdapter:
    return OpenAIAdapter()


# ── prepare_request ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_strips_nonstandard_params(adapter):
    """取樣參數（model-p0 一律不送）與 stream 必須剝除；chunk_model 供核心組 SSE chunk。"""
    request = _request([{"role": "user", "content": "hi"}],
                       dump_extra={"temperature": 0.3, "top_p": 0.9, "top_k": 40,
                                   "stream": True})
    state = await adapter.prepare_request(request, _ctx())
    assert not SAMPLING_PARAM_NAMES & set(state.payload)
    assert "stream" not in state.payload
    assert state.chunk_model == "gpt-4"
    await adapter.close(state)


@pytest.mark.asyncio
async def test_prepare_filters_safety_params_for_non_grok(adapter):
    # 各建一個 request：model_dump 回傳同一 dict、prepare 就地 pop（與真 pydantic
    # 每次回傳新 dict 的行為對齊需分離 fixture）。
    extra = {"safety_mode": "strict", "safety_settings": [1]}
    state = await adapter.prepare_request(
        _request([{"role": "user", "content": "hi"}], dump_extra=dict(extra)),
        _ctx(provider="OpenAI"),
    )
    assert "safety_mode" not in state.payload
    state2 = await adapter.prepare_request(
        _request([{"role": "user", "content": "hi"}], dump_extra=dict(extra)),
        _ctx(provider="Grok"),
    )
    assert state2.payload.get("safety_mode") == "strict"
    await adapter.close(state)
    await adapter.close(state2)


@pytest.mark.asyncio
async def test_prepare_splices_degraded_warning_into_system(adapter):
    request = _request([
        {"role": "system", "content": "You are Sumi."},
        {"role": "user", "content": "hi"},
    ])
    state = await adapter.prepare_request(
        request, _ctx(system_degraded=True, degraded_reason="memory down.")
    )
    sys_msg = next(m for m in state.payload["messages"] if m["role"] == "system")
    assert "DEGRADED MODE" in sys_msg["content"] and "You are Sumi." in sys_msg["content"]
    await adapter.close(state)


@pytest.mark.asyncio
async def test_prepare_keeps_tool_role_content_as_string(adapter):
    """OpenAI 契約：tool 訊息 content 必須維持字串（不得被多模態還原成 list）。"""
    request = _request([
        {"role": "tool", "content": '[{"type": "text", "text": "x"}]'},
        {"role": "user", "content": "hi"},
    ])
    state = await adapter.prepare_request(request, _ctx())
    tool_msg = next(m for m in state.payload["messages"] if m["role"] == "tool")
    assert isinstance(tool_msg["content"], str)
    await adapter.close(state)


# ── parse_round ─────────────────────────────────────────────────────────────


def test_parse_text_and_tools(adapter):
    raw = {"choices": [{"message": {"content": "working", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "vault_tools", "arguments": '{"vault": "personal"}'}},
        {"id": "c2", "type": "function",
         "function": {"name": "weird", "arguments": "not-json"}},
    ]}}]}
    turn = adapter.parse_round(None, raw)
    assert isinstance(turn, Turn) and not turn.empty
    assert turn.visible_text == "working"
    assert turn.tool_calls == [
        ToolCall(id="c1", name="vault_tools", args={"vault": "personal"}),
        ToolCall(id="c2", name="weird", args={}),  # 壞 JSON → 空 dict（現行為）
    ]


def test_parse_reasoning_field_popped_from_history(adapter):
    """reasoning_content 必須 pop（歷史回送 DeepSeek 即 400，ADR-0152）。"""
    message = {"content": "答案", "reasoning_content": "想想……"}
    raw = {"choices": [{"message": message}]}
    turn = adapter.parse_round(None, raw)
    assert turn.reasoning_text == "想想……"
    assert turn.visible_text == "答案"
    assert "reasoning_content" not in message  # pop＝歷史絕不回送


def test_parse_embedded_think_stripped_and_written_back(adapter):
    message = {"content": "<think>推理中</think>正文"}
    raw = {"choices": [{"message": message}]}
    turn = adapter.parse_round(None, raw)
    assert turn.reasoning_text == "推理中"
    assert turn.visible_text == "正文"
    assert message["content"] == "正文"  # 乾淨正文寫回同 dict＝全下游一致


def test_parse_no_choices_is_empty_turn(adapter):
    turn = adapter.parse_round(None, {"error": "upstream"})
    assert turn.empty


# ── 歷史寫回／耗盡輪 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_denied_turn_wire_shape(adapter):
    request = _request([{"role": "user", "content": "hi"}])
    state = await adapter.prepare_request(request, _ctx())
    raw = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "vault_tools", "arguments": "{}"}},
    ]}}]}
    turn = adapter.parse_round(None, raw)
    adapter.build_denied_turn(state, turn, "工具輪額度已盡（1 輪）")
    adapter.set_tool_choice(state, "none")

    # 尾三筆＝assistant（原形透傳）→ denied tool 佔位 → WRAP_UP user
    msgs = state.payload["messages"]
    assert msgs[-3].get("tool_calls")  # openai 方言：assistant 在 denied 之前
    assert msgs[-2]["role"] == "tool" and msgs[-2]["tool_call_id"] == "c1"
    assert "工具輪額度已盡" in msgs[-2]["content"]
    assert msgs[-1] == {"role": "user", "content": WRAP_UP_NOTE}
    assert state.payload["tool_choice"] == "none"
    await adapter.close(state)


@pytest.mark.asyncio
async def test_tool_results_wire_shape(adapter):
    request = _request([{"role": "user", "content": "hi"}])
    state = await adapter.prepare_request(request, _ctx())
    await adapter.append_tool_results(state, [("c1", "vault_tools", "OK")])
    assert state.payload["messages"][-1] == {
        "role": "tool", "tool_call_id": "c1", "name": "vault_tools", "content": "OK",
    }
    await adapter.close(state)


# ── usage／終端包裝 ─────────────────────────────────────────────────────────


def test_extract_usage_normalized(adapter):
    raw = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    usage, pt, ct = adapter.extract_usage(None, raw)
    assert (pt, ct) == (10, 5) and usage["prompt_tokens"] == 10


def test_extract_final_text_null_content_safe(adapter):
    raw = {"choices": [{"message": {"content": None}}, {"message": {"content": "x"}}]}
    assert adapter.extract_final_text(None, raw) == "x"


def test_finalize_reshapes_final_chunk(adapter):
    raw = {"choices": [{"message": {"content": "done"}, "index": 0}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    latency = {"request_id": "req-contract", "total_ms": 1}
    out = adapter.finalize(None, raw, "req-contract", latency)
    assert out["id"] == "chatcmpl-req-contract"
    assert out["object"] == "chat.completion.chunk"
    assert out["telemetry"] is latency
    choice = out["choices"][0]
    assert choice["delta"] == {"content": ""} and choice["finish_reason"] == "stop"
    assert "message" not in choice


# ── ClaudeAdapter（E3）──────────────────────────────────────────────────────


class FakeNativeAnthropicRequest:
    """型別名含 anthropic → 觸發 native ingress/egress 分支。"""

    def __init__(self):
        self.model = "claude-3-5-sonnet"
        self.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]

    def model_dump(self, **_k):
        return {"model": "claude-3-5-sonnet", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}]}


def _claude_request(model: str = "claude-3-5-sonnet") -> MagicMock:
    request = MagicMock()
    request.model = model
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    request.tools = None
    request.temperature = None
    request.top_p = None
    request.top_k = None
    request.max_tokens = None  # OpenAIChatRequest 預設；3a 起轉換器會讀它（ADR-0247）
    return request


def _claude_ctx(**extra) -> dict:
    ctx = {"anthropic_client": MagicMock(),
           "system_degraded": False, "degraded_reason": ""}
    ctx.update(extra)
    return ctx


def _tool_use_block(block_id: str) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = block_id
    block.name = "vault_tools"
    block.input = {"action": "list"}
    return block


def test_claude_emits_context_breakdown_only():
    """context 分解 v1 僅 Claude（餘家 token_usage 事件形狀凍結不變）。"""
    assert ClaudeAdapter().emits_context_breakdown is True
    assert OpenAIAdapter().emits_context_breakdown is False
    assert GeminiAdapter().emits_context_breakdown is False


@pytest.mark.asyncio
async def test_claude_telemetry_model_is_mapped_ingress_name():
    """遙測/計費模型名＝ingress 轉換後實名（讀 kwargs.model 非 GUI 字串；3a 起只小寫＋去
    -thinking，不再改寫別名，ADR-0247）。"""
    adapter = ClaudeAdapter()
    request = _claude_request(model="Claude-Sonnet-4-5-thinking")
    state = await adapter.prepare_request(request, _claude_ctx())
    assert state.kwargs["model"] == "claude-sonnet-4-5"
    assert adapter.telemetry_model(state, request) == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_claude_native_egress_records_usage_without_event_and_keeps_anthropic_shape():
    """雙 egress：native＝Anthropic 終端形狀；照常入帳但不送 token_usage 事件（ADR-0244）。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(FakeNativeAnthropicRequest(), _claude_ctx())
    assert state.is_native_anthropic

    raw = MagicMock()
    raw.usage.model_dump.return_value = {"input_tokens": 10, "output_tokens": 5}
    raw.model_dump.return_value = {
        "id": "msg_native", "model": "claude-3-5-sonnet", "role": "assistant",
        "content": [{"type": "text", "text": "已 yield 過的正文"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    assert adapter.extract_usage(state, raw) == ({
        "prompt_tokens": 10, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "completion_tokens": 5,
    }, 10, 5)
    assert adapter.emits_usage_event(state) is False

    latency = {"total_ms": 1}
    out = adapter.finalize(state, raw, "req-contract", latency)
    assert out["id"] == "msg_native"  # 不重寫為 chatcmpl-*
    assert out["content"][0]["text"] == ""  # 正文清空防重複
    assert out["telemetry"] is latency
    assert "object" not in out  # 不套 OpenAI chunk 形狀


@pytest.mark.asyncio
async def test_claude_usage_maps_input_output_tokens():
    """非 native：input/output_tokens 正規化成 prompt/completion_tokens 四欄（ADR-0244）。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_claude_request(), _claude_ctx())
    raw = MagicMock()
    raw.usage.model_dump.return_value = {"input_tokens": 10, "output_tokens": 5}
    usage, pt, ct = adapter.extract_usage(state, raw)
    assert (pt, ct) == (10, 5)
    assert usage == {
        "prompt_tokens": 10, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "completion_tokens": 5,
    }
    assert adapter.emits_usage_event(state) is True


def test_openai_and_gemini_always_emit_usage_event():
    """只有 claude native egress 不送 token_usage 事件；OpenAI 相容與 Gemini 一律送。"""
    assert OpenAIAdapter().emits_usage_event(SimpleNamespace()) is True
    assert GeminiAdapter().emits_usage_event(SimpleNamespace()) is True


def test_claude_parse_thinking_blocks_split_per_part():
    """native thinking block 逐段進 reasoning_parts（ADR-0152 分頻；正文乾淨）。"""
    adapter = ClaudeAdapter()
    think = MagicMock()
    think.type = "thinking"
    think.thinking = "想想……"
    text = MagicMock()
    text.type = "text"
    text.text = "答案是紅茶。"
    raw = MagicMock()
    raw.content = [think, text]
    turn = adapter.parse_round(None, raw)
    assert turn.reasoning_parts == ["想想……"]
    assert turn.text_parts == ["答案是紅茶。"]
    assert turn.visible_text == "答案是紅茶。"
    assert turn.raw_assistant is raw.content  # SDK 原形透傳不變式


@pytest.mark.asyncio
async def test_claude_denied_turn_wire_shape():
    """耗盡輪：assistant 原形＋denied tool_result＋WRAP_UP text 同一 user 訊息。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_claude_request(), _claude_ctx())
    raw = MagicMock()
    raw.content = [_tool_use_block("toolu_1")]
    turn = adapter.parse_round(state, raw)
    adapter.build_denied_turn(state, turn, "工具輪額度已盡（1 輪）")
    adapter.set_tool_choice(state, "none")

    msgs = state.kwargs["messages"]
    assert msgs[-2] == {"role": "assistant", "content": raw.content}
    denied = msgs[-1]
    assert denied["role"] == "user"
    assert denied["content"][0]["type"] == "tool_result"
    assert denied["content"][0]["tool_use_id"] == "toolu_1"
    assert "工具輪額度已盡" in denied["content"][0]["content"][0]["text"]
    assert denied["content"][-1] == {"type": "text", "text": WRAP_UP_NOTE}
    assert state.kwargs["tool_choice"] == {"type": "none"}


@pytest.mark.asyncio
async def test_claude_tool_result_cache_breakpoint_rotates():
    """迴圈內 ephemeral 斷點只留最新一組（Anthropic 4 斷點上限防護）；新對話的首個
    工具輪也打斷點（3a 起，ADR-0247：以前 messages 長度 ≤2 不打）。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_claude_request(), _claude_ctx())

    # round 1：user＋assistant＝2 → 也打上斷點
    state.kwargs["messages"].append({"role": "assistant", "content": []})
    await adapter.append_tool_results(state, [("c1", "vault_tools", "OK1")])
    first = state.kwargs["messages"][-1]
    assert first["content"][-1]["cache_control"] == {"type": "ephemeral"}

    # round 2：round 1 的斷點被輪替移除、round 2 打上
    state.kwargs["messages"].append({"role": "assistant", "content": []})
    await adapter.append_tool_results(state, [("c2", "vault_tools", "OK2")])
    second = state.kwargs["messages"][-1]
    assert second["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in first["content"][-1]

    # round 3：round 2 的斷點被輪替移除、round 3 打上（恆只留最新一組）
    state.kwargs["messages"].append({"role": "assistant", "content": []})
    await adapter.append_tool_results(state, [("c3", "vault_tools", "OK3")])
    third = state.kwargs["messages"][-1]
    assert third["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in second["content"][-1]


# ── Honesty Guard 三家推廣＋gemini thought 分頻（ADR-0157 K2/K3）─────────────


_BRAG = "根據過去的記憶系統內容，你上次說喜歡紅茶。"


async def _gemini_state(adapter: GeminiAdapter):
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    with patch(
        "src.handlers.adapters.gemini_adapter.convert_openai_to_gemini",
        return_value={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    ):
        return await adapter.prepare_request(
            request, {"gemini_api_key": "k", "agent_id": "Sumi"}
        )


def _claude_brag_raw() -> MagicMock:
    text = MagicMock()
    text.type = "text"
    text.text = _BRAG
    raw = MagicMock()
    raw.content = [text]
    return raw


@pytest.mark.asyncio
async def test_openai_honesty_hook_injects_corrects_and_latches(adapter):
    """openai 方言：矯正輪注入＋`required` 強制＋一次性閂鎖＋工具輪後解除。"""
    state = await adapter.prepare_request(
        _request([{"role": "user", "content": "hi"}]), _ctx()
    )
    turn = adapter.parse_round(
        None, {"choices": [{"message": {"role": "assistant", "content": _BRAG}}]}
    )
    action = adapter.mid_loop_hook(state, turn, set(), True)
    assert action is not None
    assert action.deltas == [guard_service.get_honesty_warning_message()]
    msgs = state.payload["messages"]
    assert msgs[-2] is turn.raw_assistant  # assistant 原形透傳
    assert msgs[-1]["role"] == "user"
    assert "memory_tools" in msgs[-1]["content"]  # 教實存工具（SSOT 文案）
    assert "query_memory" not in msgs[-1]["content"]  # 不教已退役名
    assert state.payload["tool_choice"] == "required"  # 強制下一輪查證

    # 一次性閂鎖：同 run 第二次瞎掰不再矯正（防弱強制方言下的矯正迴圈）
    turn2 = adapter.parse_round(
        None, {"choices": [{"message": {"role": "assistant", "content": _BRAG}}]}
    )
    assert adapter.mid_loop_hook(state, turn2, set(), True) is None

    # 工具輪寫回後強制解除（不解除＝之後每輪被迫調工具直到額度耗盡）
    await adapter.append_tool_results(state, [("c1", "memory_tools", "ok")])
    assert state.payload["tool_choice"] == "auto"
    await adapter.close(state)


@pytest.mark.asyncio
async def test_openai_honesty_hook_respects_verification_and_allow_react(adapter):
    """已查證或非 ReAct run 一律不介入（與 gemini 原語意一致）。"""
    state = await adapter.prepare_request(
        _request([{"role": "user", "content": "hi"}]), _ctx()
    )
    turn = adapter.parse_round(
        None, {"choices": [{"message": {"role": "assistant", "content": _BRAG}}]}
    )
    assert adapter.mid_loop_hook(state, turn, {"memory_tools.query"}, True) is None
    assert adapter.mid_loop_hook(state, turn, set(), False) is None
    assert "tool_choice" not in state.payload
    await adapter.close(state)


@pytest.mark.asyncio
async def test_claude_honesty_hook_forces_any_and_resets():
    """claude 方言：矯正輪注入＋`{"type":"any"}` 強制＋閂鎖＋工具輪後解除。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_claude_request(), _claude_ctx())
    raw = _claude_brag_raw()
    turn = adapter.parse_round(state, raw)
    action = adapter.mid_loop_hook(state, turn, set(), True)
    assert action is not None
    msgs = state.kwargs["messages"]
    assert msgs[-2] == {"role": "assistant", "content": raw.content}
    assert "memory_tools" in msgs[-1]["content"]
    assert state.kwargs["tool_choice"] == {"type": "any"}
    assert adapter.mid_loop_hook(state, turn, set(), True) is None  # 閂鎖
    await adapter.append_tool_results(state, [("c1", "memory_tools", "ok")])
    assert state.kwargs["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_claude_honesty_hook_no_forcing_when_thinking_enabled():
    """Anthropic 規格：extended thinking 僅容 tool_choice auto/none——強制即 400，
    故 thinking 啟用時只注入矯正提示、不強制。"""
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_claude_request(), _claude_ctx())
    state.kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4000}
    action = adapter.mid_loop_hook(
        state, adapter.parse_round(state, _claude_brag_raw()), set(), True
    )
    assert action is not None  # 仍注入矯正提示
    assert "tool_choice" not in state.kwargs  # 但不強制


@pytest.mark.asyncio
async def test_gemini_honesty_hook_uses_ssot_prompt_and_latches():
    """gemini：矯正文案改讀 guard_service SSOT（不再教已退役工具名）＋閂鎖。"""
    adapter = GeminiAdapter()
    state = await _gemini_state(adapter)
    raw = {"candidates": [{"content": {"parts": [{"text": _BRAG}]}}]}
    turn = adapter.parse_round(state, raw)
    action = adapter.mid_loop_hook(state, turn, set(), True)
    assert action is not None
    injected = state.payload["contents"][-1]["parts"][0]["text"]
    assert injected == guard_service.get_honesty_correction_prompt()
    assert "query_memory" not in injected
    assert "access_personal_drive" not in injected
    assert state.payload["tool_config"] == {"function_calling_config": {"mode": "ANY"}}
    assert adapter.mid_loop_hook(state, turn, set(), True) is None  # 閂鎖
    await adapter.close(state)


# ── ADR-0177：_claude_request fixture 盲點修（E54/E61/E72/E73）───────────────
# _claude_request() 硬編 tools=None＋無 system → claude_adapter.py:75-85/104-120
# 的 tools/sampling/system wiring 在全部既有測試中恆為死碼。本節以「帶真實
# tools＋取樣參數＋assembled system」的變體打穿四個接線點，對 wire payload 釘死。


class FakeNativeAnthropicRequestFull:
    """native ingress＋Anthropic 形狀 tools（無既有 cache_control）＋str system。

    E72/E73 必須走 native ingress：OpenAI ingress 的 convert_openai_to_anthropic
    會預先在 tools 末項放斷點（legacy 斷點 1）＝「刪 apply_tool_cache_breakpoint」
    變異會被轉換器遮蔽仍綠；native 直通才能證明斷點是 adapter :110 掛的。
    """

    def model_dump(self, **_k):
        return {
            "model": "claude-3-5-sonnet", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": "vault_tools", "description": "d",
                 "input_schema": {"type": "object", "properties": {}}},
                {"name": "web_search", "description": "d2",
                 "input_schema": {"type": "object", "properties": {}}},
            ],
            "system": (
                "<persona>穩定人設</persona>\n"
                "<conversation_summary>每回合變動的摘要</conversation_summary>\n"
                "<tool_availability>穩定工具區塊</tool_availability>"
            ),
        }


@pytest.mark.asyncio
async def test_claude_openai_ingress_tools_reach_wire_in_anthropic_shape():
    """E54：OpenAI 工具清單經出場轉換以 Anthropic 形狀（name/input_schema、無
    function 包裝）進 kwargs["tools"]——方向反轉（anthropic_to_openai_tools）即紅。"""
    request = _claude_request()
    request.tools = [
        {"type": "function", "function": {
            "name": "vault_tools", "description": "d",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "web_search", "parameters": {"type": "object"}}},
    ]
    state = await ClaudeAdapter().prepare_request(request, _claude_ctx())
    tools = state.kwargs["tools"]
    assert [t["name"] for t in tools] == ["vault_tools", "web_search"]
    assert all("input_schema" in t and "function" not in t for t in tools)


@pytest.mark.parametrize("temperature", [0.3, 1.7])
@pytest.mark.asyncio
async def test_claude_sampling_params_never_sent(temperature):
    """E61 改（model-p0，ADR-0241 決定 A）：temperature／top_p／top_k 一律不送——
    請求帶值（含 >1.0）也不進 kwargs；取代舊的夾值與條件轉發。"""
    req = _claude_request()
    req.temperature = temperature
    req.top_p = 0.9
    req.top_k = 40
    state = await ClaudeAdapter().prepare_request(req, _claude_ctx())
    assert not SAMPLING_PARAM_NAMES & set(state.kwargs)


@pytest.mark.asyncio
async def test_claude_native_tools_get_adapter_cache_breakpoint():
    """E72：native ingress 工具（無既有 cache_control）經 adapter 掛 P4 斷點——
    僅末項 ephemeral、前項乾淨；刪 apply_tool_cache_breakpoint 即紅（~6346 tok/輪
    重複計費的靜默迴歸）。"""
    state = await ClaudeAdapter().prepare_request(
        FakeNativeAnthropicRequestFull(), _claude_ctx()
    )
    tools = state.kwargs["tools"]
    assert [t["name"] for t in tools] == ["vault_tools", "web_search"]
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_system_reorder_before_split():
    """E73：assembled str system 先 reorder（波動搬尾）再 to_cached_system 切分——
    穩定前綴涵蓋 persona＋tool_availability（跨回合快取）、波動 conversation_summary
    落後綴；刪 reorder（切點落在 persona 後、工具區塊被困在波動後綴）即紅。"""
    state = await ClaudeAdapter().prepare_request(
        FakeNativeAnthropicRequestFull(), _claude_ctx()
    )
    system = state.kwargs["system"]
    assert isinstance(system, list) and len(system) == 2
    stable, volatile = system[0]["text"], system[1]["text"]
    assert "<persona>" in stable and "<tool_availability>" in stable
    assert "<conversation_summary>" not in stable  # 波動已被搬離前綴
    assert "<conversation_summary>" in volatile
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_gemini_parse_splits_thought_parts():
    """K3：`thought: true` → reasoning 頻道；正文/guard 累積/歷史寫回皆排除。"""
    adapter = GeminiAdapter()
    state = SimpleNamespace(cumulative_text="", call_seq=0)
    raw = {"candidates": [{"content": {"parts": [
        {"text": "使用者偏好……讓我想想。", "thought": True},
        {"text": "答案是紅茶。"},
        {"functionCall": {"name": "read", "args": {"path": "a.md"}}},
    ]}}]}
    turn = adapter.parse_round(state, raw)
    assert turn.reasoning_parts == ["使用者偏好……讓我想想。"]
    assert turn.text_parts == ["答案是紅茶。"]
    assert turn.visible_text == "答案是紅茶。"
    assert state.cumulative_text == "答案是紅茶。"  # guard 輸入不含思考
    assert all(not p.get("thought") for p in turn.raw_assistant)  # 思考不回送歷史
    assert turn.tool_calls[0].id == "gcall_1"

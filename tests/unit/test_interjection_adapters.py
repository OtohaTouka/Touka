"""U4a-2：四家 adapter 的 append_interjection 與 hold_tool_choice（ADR-0249 第 4a 段；裁定 5、6）。

插話接在這一輪工具結果之後，文字範本是事實陳述的模組常數：
- Claude：規則表 ``mid_conversation_system`` True → 工具結果後放 system 訊息；False 或認不出 →
  同一則 user 訊息最後一個 tool_result 後加 text block（不產生連續兩則 user，不加 cache_control）。
- OpenAI 相容：tool 訊息之後加一則 user 訊息。
- Gemini：functionResponse 同一個 content 加 text part。
- Responses：function_call_output 之後加 ``{"role": "user"}`` input item。
OpenAI、Gemini 是否接受這種格式沒有實測，這裡只是模擬契約（寫死期望的訊息形狀）。
第 4b 段（U4b-1）：Ctrl+Enter 打斷第一輪時最後一則是原本的提問，四家一樣接在它後面。
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters.base import Turn
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.interjection import Interjection

_RESULTS = [("call_1", "vault_tools", "a.txt")]
_TOOLS = [{"type": "function", "function": {"name": "vault_tools", "description": "列出檔案",
           "parameters": {"type": "object", "properties": {}}}}]
_NOTE_B = "使用者在工具執行期間補充：先看 b"
_NOTE_BC = "使用者在工具執行期間補充：先看 b\n\n使用者在工具執行期間補充：再看 c"


def _items(*texts: str, urgent: bool = False) -> list[Interjection]:
    return [Interjection(id=f"i{n}", text=text, urgent=urgent, status="pending", step=1,
                         created_at=0.0) for n, text in enumerate(texts, 1)]


def _request(model: str, tools: list | None = None) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, tools=tools,
                             messages=[OpenAIMessage(role="user", content="查一下")])


async def _claude(model: str, tools: list | None = None):
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(_request(model, tools),
                                          {"anthropic_client": MagicMock()})
    state.kwargs["messages"].append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_1", "name": "vault_tools", "input": {}}]})
    await adapter.append_tool_results(state, _RESULTS)
    return adapter, state


async def test_claude_with_mid_conversation_system_adds_a_system_message():
    adapter, state = await _claude("claude-sonnet-5-5")  # 規則表 True
    before = len(state.kwargs["messages"])
    adapter.append_interjection(state, _items("先看 b"))
    messages = state.kwargs["messages"]
    assert len(messages) == before + 1
    assert messages[-1] == {"role": "system", "content": _NOTE_B}
    assert messages[-2]["role"] == "user"
    assert messages[-2]["content"][-1]["type"] == "tool_result"


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "claude-sonnet-5", "claude-unknown"])
async def test_claude_without_mid_conversation_system_appends_a_text_block(model):
    adapter, state = await _claude(model)  # 規則表 False 或認不出版本
    before = [dict(m) for m in state.kwargs["messages"]]
    adapter.append_interjection(state, _items("先看 b", "再看 c"))
    messages = state.kwargs["messages"]
    assert len(messages) == len(before)  # 不產生連續兩則 user
    assert [m["role"] for m in messages[-2:]] == ["assistant", "user"]
    blocks = messages[-1]["content"]
    assert blocks[-1] == {"type": "text", "text": _NOTE_BC}  # 沒有 cache_control
    assert blocks[-2]["type"] == "tool_result"


async def test_openai_compatible_adds_a_user_message_after_the_tool_messages():
    adapter = OpenAIAdapter()
    state = await adapter.prepare_request(_request("grok-4"), {
        "provider": "Grok", "url": "https://api.x.test/v1/chat/completions", "api_key": "k"})
    await adapter.append_tool_results(state, _RESULTS)
    adapter.append_interjection(state, _items("先看 b"))
    await adapter.close(state)
    assert state.payload["messages"][-2:] == [
        {"role": "tool", "tool_call_id": "call_1", "name": "vault_tools", "content": "a.txt"},
        {"role": "user", "content": _NOTE_B},
    ]


async def test_gemini_adds_a_text_part_to_the_function_response_content():
    adapter = GeminiAdapter()
    state = await adapter.prepare_request(_request("gemini-2.5-flash"), {"gemini_api_key": "k"})
    adapter.append_assistant_turn(state, Turn(raw_assistant=[
        {"functionCall": {"name": "vault_tools", "args": {}}}]))
    await adapter.append_tool_results(state, _RESULTS)
    before = len(state.payload["contents"])
    adapter.append_interjection(state, _items("先看 b"))
    await adapter.close(state)
    assert len(state.payload["contents"]) == before
    assert state.payload["contents"][-1] == {"role": "user", "parts": [
        {"functionResponse": {"name": "vault_tools", "response": {"result": "a.txt"}}},
        {"text": _NOTE_B},
    ]}


async def test_responses_adds_a_user_input_item_after_the_function_call_output():
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(_request("gpt-6-astra"), {
        "api_key": "k", "base_url": "https://api.openai.test/v1", "provider": "OpenAI"})
    await adapter.append_tool_results(state, _RESULTS)
    adapter.append_interjection(state, _items("先看 b"))
    await adapter.close(state)
    assert state.kwargs["input"][-2:] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
        {"role": "user", "content": _NOTE_B},
    ]


async def _states(tools: list | None = None) -> list[tuple[object, object, dict, str]]:
    """(adapter, state, 放 tool_choice 的 dict, 鍵名)。"""
    claude, claude_state = await _claude("claude-sonnet-4-5", tools)
    openai = OpenAIAdapter()
    openai_state = await openai.prepare_request(_request("grok-4", tools), {
        "provider": "Grok", "url": "https://api.x.test/v1/chat/completions", "api_key": "k"})
    gemini = GeminiAdapter()
    gemini_state = await gemini.prepare_request(_request("gemini-2.5-flash", tools),
                                                {"gemini_api_key": "k"})
    responses = ResponsesAdapter()
    responses_state = await responses.prepare_request(_request("gpt-6-astra", tools), {
        "api_key": "k", "base_url": "https://api.openai.test/v1", "provider": "OpenAI"})
    return [(claude, claude_state, claude_state.kwargs, "tool_choice"),
            (openai, openai_state, openai_state.payload, "tool_choice"),
            (gemini, gemini_state, gemini_state.payload, "tool_config"),
            (responses, responses_state, responses_state.kwargs, "tool_choice")]


async def test_hold_tool_choice_sets_none_and_restores_the_original():
    for adapter, state, holder, key in await _states():  # M9-R1a：沒有工具什麼都不改
        restore = adapter.hold_tool_choice(state, "none")
        assert key not in holder
        restore()
        assert key not in holder
        await adapter.close(state)
    for adapter, state, holder, key in await _states(_TOOLS):
        assert key not in holder
        restore = adapter.hold_tool_choice(state, "none")
        assert key in holder  # 這一輪是 none
        restore()
        assert key not in holder  # 原本沒設就拿掉
        adapter.set_tool_choice(state, "auto")
        original = holder[key]
        restore = adapter.hold_tool_choice(state, "none")
        assert holder[key] != original
        restore()
        assert holder[key] == original  # 原本有值就還原成那個值
        await adapter.close(state)


# ── 第 4b 段（U4b-1）：打斷第一輪時，最後一則是原本的提問 ──────────────────────


async def test_after_the_original_prompt_each_provider_keeps_its_shape():
    """打斷的範本、接在最後一則使用者訊息後面；Claude 規則表 False 併進同一則 user。"""
    cut = "使用者打斷了正在寫的回答並補充：先看 b"
    claude = ClaudeAdapter()
    for model in ("claude-sonnet-5", "claude-sonnet-5-5"):  # 規則表 False／True
        state = await claude.prepare_request(_request(model), {"anthropic_client": MagicMock()})
        claude.append_interjection(state, _items("先看 b"), interrupted=True)
        last = state.kwargs["messages"][-1]
        if model == "claude-sonnet-5":
            assert len(state.kwargs["messages"]) == 1  # 不產生連續兩則 user
            assert last["content"][0]["text"] == "查一下"
            assert last["content"][-1] == {"type": "text", "text": cut}
        else:
            assert last == {"role": "system", "content": cut}
    for adapter, state, holder, _key in (await _states())[1:]:
        key = next(k for k in ("messages", "contents", "input") if k in holder)
        before = copy.deepcopy(holder[key])
        adapter.append_interjection(state, _items("先看 b"), interrupted=True)
        await adapter.close(state)
        if key == "contents":  # Gemini：同一則 user content 加 text part
            assert holder[key] == [{**before[0], "parts": [*before[0]["parts"], {"text": cut}]}]
        else:  # OpenAI 相容、Responses：另起一則 user
            assert holder[key] == [*before, {"role": "user", "content": cut}]

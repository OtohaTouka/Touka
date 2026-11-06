"""送給模型前拿掉空白的助理訊息（ADR-0231 決定 6）。

模型一個字都沒出就失敗時，畫面會把那則空白回覆存進對話；下一輪它會變成前文。
Anthropic 規定文字區塊至少 1 個字，其他家文件沒寫（2026-11-06 查）。所以在分流到
各家之前的共用處（factory）拿掉，接在一起的前後兩則 user 合併成一則。
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from src.adapters.content_normalize import drop_blank_assistant_turns
from src.adapters.openai_to_claude import convert_openai_to_anthropic
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.adapters.schemas import AnthropicMessage, OpenAIChatRequest, OpenAIMessage
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.dispatchers.route_family import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    RESPONSES_ROUTE,
    ModelRoute,
)

_IDENTITY = AgentIdentity.for_role(
    agent_id="DebugAgent", role="admin", is_human=True, capabilities=[]
)
_IMAGE = json.dumps(
    [
        {"type": "text", "text": "看這張"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
)


def _roles(messages):
    return [m["role"] if isinstance(m, dict) else m.role for m in messages]


def _contents(messages):
    return [m["content"] if isinstance(m, dict) else m.content for m in messages]


@pytest.mark.parametrize("blank", ["", "   \n", None, [{"type": "text", "text": ""}]])
def test_a_blank_reply_is_dropped_and_the_users_around_it_are_merged(blank):
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "第一則"},
        {"role": "assistant", "content": blank},
        {"role": "user", "content": "第二則"},
    ]
    result = drop_blank_assistant_turns(messages)
    assert _roles(result) == ["system", "user"]
    assert _contents(result)[1] == "第一則\n\n第二則"


def test_several_blank_replies_in_a_row_merge_into_one_user():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "c"},
    ]
    assert _contents(drop_blank_assistant_turns(messages)) == ["a\n\nb\n\nc"]


def test_only_the_two_users_around_the_blank_reply_are_merged():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert _contents(drop_blank_assistant_turns(messages)) == ["a\n\nb", "c"]


def test_a_merge_with_an_image_keeps_the_image_block():
    messages = [
        {"role": "user", "content": _IMAGE},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "然後呢"},
    ]
    (merged,) = drop_blank_assistant_turns(messages)
    assert merged["content"] == [
        {"type": "text", "text": "看這張"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": "然後呢"},
    ]


@pytest.mark.parametrize(
    "kept",
    [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
        {"role": "assistant", "content": "有字"},
        {"role": "tool", "content": "", "tool_call_id": "t1"},
    ],
)
def test_replies_with_tool_calls_or_other_blocks_are_not_blank(kept):
    messages = [{"role": "user", "content": "a"}, kept, {"role": "user", "content": "b"}]
    assert drop_blank_assistant_turns(messages) == messages


def test_users_that_were_already_adjacent_are_left_alone():
    messages = [
        {"role": "user", "content": "a", "name": "Alice"},
        {"role": "user", "content": "b", "name": "Bob"},
    ]
    assert drop_blank_assistant_turns(messages) == messages


def test_a_blank_reply_at_the_end_or_start_is_just_dropped():
    messages = [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": " "},
    ]
    assert drop_blank_assistant_turns(messages) == [{"role": "user", "content": "a"}]


def test_the_input_list_and_messages_are_not_changed():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "b"},
    ]
    before = copy.deepcopy(messages)
    drop_blank_assistant_turns(messages)
    assert messages == before


def test_pydantic_messages_keep_their_type():
    openai = drop_blank_assistant_turns(
        [
            OpenAIMessage(role="user", content="a"),
            OpenAIMessage(role="assistant", content=""),
            OpenAIMessage(role="user", content="b"),
        ]
    )
    assert openai == [OpenAIMessage(role="user", content="a\n\nb")]
    anthropic = drop_blank_assistant_turns(
        [
            AnthropicMessage(role="user", content=[{"type": "text", "text": "a"}]),
            AnthropicMessage(role="assistant", content=[{"type": "text", "text": ""}]),
            AnthropicMessage(role="user", content="b"),
        ]
    )
    assert anthropic == [
        AnthropicMessage(
            role="user", content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        )
    ]


def _request_with_failed_turn():
    return OpenAIChatRequest(
        model="m",
        messages=[
            OpenAIMessage(role="system", content="persona"),
            OpenAIMessage(role="user", content="第一則"),
            OpenAIMessage(role="assistant", content=""),
            OpenAIMessage(role="user", content="第二則"),
        ],
    )


@pytest.mark.parametrize(
    ("kind", "handler"),
    [
        (ANTHROPIC_FAMILY, "handle_claude_request"),
        (GEMINI_FAMILY, "handle_gemini_request"),
        (RESPONSES_ROUTE, "handle_responses_request"),
        ("openai", "handle_openai_request"),
    ],
)
@pytest.mark.asyncio
async def test_every_vendor_route_receives_the_history_without_the_blank_reply(
    monkeypatch, kind, handler
):
    seen = SimpleNamespace(request=None)

    def capture(request, _context):
        seen.request = request

    monkeypatch.setattr(
        factory, "resolve_model_route", lambda *_a, **_k: ModelRoute(kind=kind, api_key="")
    )
    monkeypatch.setattr(factory, handler, capture)
    await factory.get_stream_generator("m", _request_with_failed_turn(), _IDENTITY, "rid", 0.0, {})
    assert _roles(seen.request.messages) == ["system", "user"]
    assert _contents(seen.request.messages)[1] == "第一則\n\n第二則"


def test_the_converted_vendor_payloads_have_no_empty_reply():
    cleaned = _request_with_failed_turn()
    cleaned.messages = drop_blank_assistant_turns(cleaned.messages)
    claude = convert_openai_to_anthropic(cleaned)
    assert [m.role for m in claude.messages] == ["user"]
    assert all(block.get("text") for m in claude.messages for block in m.content)
    gemini = convert_openai_to_gemini(cleaned)
    assert [c["role"] for c in gemini["contents"]] == ["user"]

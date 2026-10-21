"""Home conversation outbound: only role and content leave Touka; persona comes first (ADR-0260).

Home conversation nodes are stored exactly as the UI tree saved them, with node ids,
tool steps (calls and results), thinking from other models, attachments, interjections,
interruptions, progress, and the ``name`` the server writes (``System`` or a soul id).
Injection keeps only ``role`` and ``content``; every send boundary (OpenAI-compatible
Chat, Responses, Gemini, Claude from both entries) only lets spec keys through per
message, and ``name`` is never sent. On OpenAI-compatible Chat the persona system message
sits before every home message.

The session store is a mock and soul data is a stub: nothing reads ``data/``, nothing is
sent to a provider.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import (
    AnthropicMessage,
    AnthropicRequest,
    OpenAIChatRequest,
    OpenAIMessage,
)
from src.handlers.adapters import claude_adapter, openai_adapter
from src.handlers.adapters.base import only_keys
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.identity import AgentIdentity
from src.services.dispatchers.utils import convert_to_openai
from src.services.soul_service import SoulService
from src.utils import async_db
from src.utils.interjection_notes import format_interjection_note

PERSONA = "PERSONA-TEXT of the soul"
HOME_OPEN = '<historical_data source="home_conversation"'
STEP_CANARY = "CANARY-STEP-7f3a"
THINKING_CANARY = "CANARY-THINKING-91bd"
ATTACHMENT_CANARY = "CANARY-ATTACHMENT-c40e"
NODE_ID_CANARY = "CANARY-NODE-55aa"
MODEL_CANARY = "CANARY-MODEL-2e8d"
CANARIES = (STEP_CANARY, THINKING_CANARY, ATTACHMENT_CANARY, NODE_ID_CANARY, MODEL_CANARY)
NOTE = format_interjection_note(1, "only 2026")
SERVER_NAME = "System"  # what the heartbeat writes on its nudge nodes
SOUL_NAME = "SoulNameCanary-9c4d"  # the heartbeat and SNS write the soul id on replies
CLIENT_NAME = "ClientNameCanary-61f0"

# Keys each wire format allows on one message (spec keys only; no name on OpenAI Chat).
OPENAI_CHAT_KEYS = {"role", "content", "tool_calls", "tool_call_id"}
RESPONSES_KEYS = {"role", "content", "type", "call_id", "name", "arguments", "output"}
GEMINI_KEYS = {"role", "parts"}
CLAUDE_KEYS = {"role", "content"}

U1, A1 = f"{NODE_ID_CANARY}-u1", f"{NODE_ID_CANARY}-a1"


def _node(node_id: str, parent: str | None, children: list[str], **fields: Any) -> dict:
    return {
        "id": node_id,
        "parentId": parent,
        "childrenIds": children,
        "createdAt": 1700000000000,
        "timestamp": 1700000000000,
        **fields,
    }


def _home_tree() -> dict:
    """A home session tree with every UI key a stored node can carry."""
    user = _node(
        U1, None, [A1], role="user", content="old question", name=SERVER_NAME,
        attachment={"type": "file", "url": f"/v1/outputs/Alice/{ATTACHMENT_CANARY}.pdf",
                    "name": ATTACHMENT_CANARY, "text": ATTACHMENT_CANARY},
    )
    assistant = _node(
        A1, U1, [], role="assistant", content="old answer", name=SOUL_NAME,
        model=MODEL_CANARY,
        thinking=THINKING_CANARY,
        steps=[{"id": "s1", "tool": "vault_tools", "args": {"query": STEP_CANARY},
                "status": "success", "result": STEP_CANARY}],
        interjections=[{"id": "i1", "step": 1, "text": "only 2026", "status": "delivered"}],
        interruptions=[{"step": 2}],
        progress={"react_slots": {"kind": "react_slots", "current": 1, "total": 5,
                                  "tool": STEP_CANARY}},
    )
    return {"messages": {U1: user, A1: assistant}, "currentId": A1}


@pytest.fixture(autouse=True)
def _home_store(monkeypatch: pytest.MonkeyPatch) -> None:
    service = MagicMock(
        read_home_snapshot=AsyncMock(
            return_value=async_db.SessionSnapshot({"id": "home", "messages": _home_tree()}, "rev")
        ),
    )
    monkeypatch.setattr(async_db, "session_service", service)


def _soul() -> SoulService:
    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: (PERSONA, {})
    return soul


async def _assemble(request: Any) -> Any:
    """Same order as SoulLayer: persona first, then the home conversation."""
    soul = _soul()
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    soul.apply_soul_to_request(request, identity)
    await soul.inject_home_messages(request, identity)
    return request


def _openai_request(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="now")])


def _anthropic_request(model: str) -> AnthropicRequest:
    return AnthropicRequest(model=model, messages=[AnthropicMessage(role="user", content="now")])


async def _openai_chat(request: Any) -> tuple[Any, list[dict]]:
    adapter = OpenAIAdapter()
    state = await adapter.prepare_request(
        request, {"provider": "DeepSeek", "url": "http://fake.test", "api_key": "k"}
    )
    await adapter.close(state)
    return state.payload, state.payload["messages"]


async def _responses(request: Any) -> tuple[Any, list[dict]]:
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(request, {"provider": "OpenAI"})  # no key: no client
    await adapter.close(state)
    return state.kwargs, state.kwargs["input"]


async def _gemini(request: Any) -> tuple[Any, list[dict]]:
    adapter = GeminiAdapter()
    state = await adapter.prepare_request(request, {"gemini_api_key": "k", "agent_id": "Alice"})
    await adapter.close(state)
    return state.payload, state.payload["contents"]


async def _claude(request: Any) -> tuple[Any, list[dict]]:
    state = await ClaudeAdapter().prepare_request(request, {"anthropic_client": MagicMock()})
    return state.kwargs, state.kwargs["messages"]


async def _anthropic_entry_to_openai_chat(request: Any) -> tuple[Any, list[dict]]:
    return await _openai_chat(convert_to_openai(request, False, ""))


CASES = [
    pytest.param(_openai_request, "deepseek-chat", _openai_chat, OPENAI_CHAT_KEYS,
                 id="openai-chat"),
    pytest.param(_anthropic_request, "deepseek-chat", _anthropic_entry_to_openai_chat,
                 OPENAI_CHAT_KEYS, id="anthropic-entry-to-openai-chat"),
    pytest.param(_openai_request, "gpt-5", _responses, RESPONSES_KEYS, id="responses"),
    pytest.param(_openai_request, "gemini-2.5-flash", _gemini, GEMINI_KEYS, id="gemini"),
    pytest.param(_openai_request, "claude-sonnet-4-6", _claude, CLAUDE_KEYS,
                 id="claude-openai-entry"),
    pytest.param(_anthropic_request, "claude-sonnet-4-6", _claude, CLAUDE_KEYS,
                 id="claude-native"),
]


@pytest.mark.parametrize(("build", "model", "send", "allowed"), CASES)
async def test_outbound_messages_only_carry_spec_keys(build, model, send, allowed) -> None:
    payload, wire = await send(await _assemble(build(model)))

    for message in wire:
        assert set(message) <= allowed, message
    dumped = json.dumps(payload, ensure_ascii=False, default=str)
    for canary in CANARIES:
        assert canary not in dumped
    # the conversation itself still goes out, interjection note included
    for text in ("old question", "old answer", NOTE, PERSONA):
        assert text in dumped


@pytest.mark.parametrize(
    ("build", "send"),
    [
        pytest.param(_openai_request, _openai_chat, id="openai-chat"),
        pytest.param(_anthropic_request, _anthropic_entry_to_openai_chat,
                     id="anthropic-entry-to-openai-chat"),
    ],
)
async def test_openai_chat_persona_comes_before_home(build, send) -> None:
    _, wire = await send(await _assemble(build("deepseek-chat")))

    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert PERSONA in wire[0]["content"]
    assert wire[1]["content"].startswith(HOME_OPEN)
    assert wire[2]["content"].startswith(NOTE)
    assert wire[3]["content"] == "now"


@pytest.mark.parametrize(("build", "model", "send", "allowed"), CASES)
async def test_home_node_name_never_sent(build, model, send, allowed) -> None:
    """``name="System"`` (heartbeat nudges) and a soul id (replies) stay in the tree only."""
    payload, wire = await send(await _assemble(build(model)))

    assert all("name" not in message for message in wire)
    dumped = json.dumps(payload, ensure_ascii=False, default=str)
    assert json.dumps(SERVER_NAME) not in dumped  # as a value: "System"
    assert SOUL_NAME not in dumped


CLIENT_CASES = [
    pytest.param("deepseek-chat", _openai_chat, id="openai-chat"),
    pytest.param("gpt-5", _responses, id="responses"),
    pytest.param("gemini-2.5-flash", _gemini, id="gemini"),
    pytest.param("claude-sonnet-4-6", _claude, id="claude-openai-entry"),
]


@pytest.mark.parametrize(("model", "send"), CLIENT_CASES)
async def test_client_message_name_not_forwarded(model, send) -> None:
    """A caller's own ``name`` on an OpenAI-shaped message is not forwarded to the vendor."""
    request = OpenAIChatRequest(model=model, messages=[
        OpenAIMessage(role="system", content="P", name=CLIENT_NAME),
        OpenAIMessage(role="user", content="hi", name=CLIENT_NAME),
    ])

    payload, wire = await send(request)

    assert all("name" not in message for message in wire)
    assert CLIENT_NAME not in json.dumps(payload, ensure_ascii=False, default=str)
    assert "hi" in json.dumps(wire, ensure_ascii=False)


# --- the injection itself ----------------------------------------------------


async def _inject(request: Any) -> Any:
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    await _soul().inject_home_messages(request, identity)
    return request


class _AnthropicReq:
    """Class name contains 'Anthropic', so the home injection takes the Anthropic path."""

    def __init__(self, messages: list[Any]) -> None:
        self.system = ""
        self.messages = messages


@pytest.mark.parametrize(
    ("before", "expected_roles"),
    [
        pytest.param([], ["user", "assistant"], id="empty"),
        pytest.param([{"role": "user", "content": "now"}],
                     ["user", "assistant", "user"], id="no-system"),
        pytest.param([{"role": "system", "content": "P"}, {"role": "user", "content": "now"}],
                     ["system", "user", "assistant", "user"], id="one-system"),
        pytest.param([OpenAIMessage(role="system", content="P"),
                      {"role": "system", "content": "Q"},
                      OpenAIMessage(role="user", content="now")],
                     ["system", "system", "user", "assistant", "user"], id="two-systems-mixed"),
        pytest.param([{"role": "user", "content": "now"}, {"role": "system", "content": "late"}],
                     ["user", "assistant", "user", "system"], id="system-not-leading"),
    ],
)
async def test_openai_shape_home_goes_after_leading_system(before, expected_roles) -> None:
    request = await _inject(SimpleNamespace(messages=list(before)))

    roles = [m["role"] if isinstance(m, dict) else m.role for m in request.messages]
    assert roles == expected_roles
    home = [m for m in request.messages if isinstance(m, dict) and m.get("content") is not None
            and (HOME_OPEN in str(m["content"]) or NOTE in str(m["content"]))]
    assert len(home) == 2
    for message in home:
        assert set(message) == {"role", "content"}


async def test_anthropic_shape_home_unchanged() -> None:
    request = await _inject(_AnthropicReq([{"role": "user", "content": "now"}]))

    assert [set(m) for m in request.messages] == [{"role", "content"}] * 3
    assert [m["role"] for m in request.messages] == ["user", "assistant", "user"]
    assert request.messages[0]["content"].startswith(HOME_OPEN)
    assert request.messages[2] == {"role": "user", "content": "now"}


# --- the per-message whitelists at the adapters ------------------------------


def test_only_keys_returns_new_dicts_and_passes_non_dicts() -> None:
    model = OpenAIMessage(role="user", content="hi")
    source = [{"role": "user", "content": "hi", "id": "n1"}, model]

    result = only_keys(source, frozenset({"role", "content"}))

    assert result == [{"role": "user", "content": "hi"}, model]
    assert source[0] == {"role": "user", "content": "hi", "id": "n1"}  # input untouched


def test_openai_whitelist_is_message_schema_without_name() -> None:
    """The whitelist is ``OpenAIMessage``'s fields minus ``name``.

    No Touka path needs to send ``name``: the heartbeat and SNS write it into the tree
    only, requests are built from role and content, group chat sets none, and the only
    path that sent it was the whole home node. A new schema field must be a deliberate
    decision, so the comparison is exact.
    """
    schema_fields = set(OpenAIMessage.model_fields)

    assert schema_fields - {"name"} == openai_adapter._MESSAGE_KEYS
    assert openai_adapter._MESSAGE_KEYS == OPENAI_CHAT_KEYS


def _dumped_request(messages: list[dict]) -> MagicMock:
    request = MagicMock()
    request.model = "deepseek-chat"
    request.model_dump.return_value = {"model": "deepseek-chat", "messages": messages}
    return request


async def test_openai_whitelist_drops_unknown_keys_keeps_spec_keys() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    request = _dumped_request([
        {"role": "user", "content": "hi", "name": CLIENT_NAME, "id": "n1",
         "attachment": {"text": ATTACHMENT_CANARY}},
        {"role": "assistant", "content": None, "tool_calls": [call],
         "thinking": THINKING_CANARY, "model": MODEL_CANARY},
        {"role": "tool", "tool_call_id": "c1", "name": SOUL_NAME,
         "content": '[{"type": "text", "text": "x"}]', "steps": [STEP_CANARY]},
    ])

    payload, wire = await _openai_chat(request)

    assert [set(m) for m in wire] == [
        {"role", "content"},
        {"role", "content", "tool_calls"},
        {"role", "tool_call_id", "content"},
    ]
    assert wire[1]["tool_calls"] == [call]
    assert wire[2]["tool_call_id"] == "c1"
    assert isinstance(wire[2]["content"], str)
    dumped = json.dumps(payload, ensure_ascii=False)
    for canary in (ATTACHMENT_CANARY, THINKING_CANARY, MODEL_CANARY, STEP_CANARY,
                   CLIENT_NAME, SOUL_NAME):
        assert canary not in dumped


class _NativeAnthropicRequest:
    """Type name contains 'anthropic': the Claude adapter takes the native entry."""

    def __init__(self, messages: list[dict]) -> None:
        self.model = "claude-sonnet-4-6"
        self.messages = messages

    def model_dump(self, **_kwargs: Any) -> dict:
        return {"model": self.model, "max_tokens": 100, "messages": self.messages}


async def test_claude_native_whitelist_drops_unknown_keys() -> None:
    blocks = [{"type": "text", "text": "hi"}]
    request = _NativeAnthropicRequest([
        {"role": "user", "content": blocks, "id": NODE_ID_CANARY, "steps": [STEP_CANARY]},
        {"role": "assistant", "content": "ok", "thinking": THINKING_CANARY},
    ])

    payload, wire = await _claude(request)

    assert claude_adapter._MESSAGE_KEYS == CLAUDE_KEYS
    assert wire == [{"role": "user", "content": blocks}, {"role": "assistant", "content": "ok"}]
    dumped = json.dumps(payload, ensure_ascii=False, default=str)
    for canary in (NODE_ID_CANARY, STEP_CANARY, THINKING_CANARY):
        assert canary not in dumped

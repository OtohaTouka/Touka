"""model-p3a U2：anthropic SDK 1.x（httpx2）的傳輸行為（ADR-0247）。

- 主迴圈 client（main.py）注入 follow_redirects=False 的 httpx2 client，且仍用 SDK 預設逾時
  （非串流上限檢查照樣觸發，規則表常數仍有意義）。
- 注入的 client 在 SDK 1.x 真的不跟隨 307，``with_options`` 換金鑰後沿用同一個 client：
  金鑰不會被帶到轉址目標（本機 loopback 伺服器，不連外）。
- 拿掉 2024 年的 prompt-caching beta 標頭後，SDK 仍把 ``cache_control`` 原樣送出（MockTransport）。
- R-3a：SDK 1.11 的 ``Message`` 多了 ``diagnostics``／``stop_details``／
  ``usage.output_tokens_details``；原生出口照樣輸出（沒回時為 null），四欄用量不變。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import httpx2
import pytest
from anthropic import DEFAULT_TIMEOUT, APIStatusError, AsyncAnthropic, DefaultAsyncHttpxClient
from anthropic.types import Message
from src import main as proxy_main
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest, OpenAIMessage
from src.config import ANTHROPIC_API_BASE_URL
from src.handlers.adapters.claude_adapter import ClaudeAdapter

_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_MODELS_PAGE = b'{"data":[],"has_more":false,"first_id":null,"last_id":null}'
_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "好"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


def test_main_loop_client_refuses_redirects():
    http_client = proxy_main.anthropic_client._client
    assert isinstance(http_client, httpx2.AsyncClient)
    assert not isinstance(http_client, httpx.AsyncClient)
    assert http_client.follow_redirects is False
    assert proxy_main.anthropic_client.timeout == DEFAULT_TIMEOUT


async def test_injected_client_stops_at_307_after_with_options(monkeypatch):
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    hits: list[tuple[str, str | None]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in head[1:] if line)}
        path = head[0].split(" ")[1]
        hits.append((path, headers.get("x-api-key")))
        if path == "/v1/models":
            reply = f"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:{port}/elsewhere"
            writer.write(reply.encode() + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        else:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(_MODELS_PAGE)}\r\n\r\n".encode()
                + _MODELS_PAGE
            )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    http_client = DefaultAsyncHttpxClient(follow_redirects=False)  # 同產品建構點
    boot = AsyncAnthropic(
        api_key="fake-key", base_url=f"http://127.0.0.1:{port}", max_retries=0,
        http_client=http_client,
    )
    swapped = boot.with_options(api_key="fake-live-key")
    try:
        assert swapped._client is http_client
        with pytest.raises(APIStatusError) as caught:
            await swapped.models.list()
    finally:
        await boot.close()
        server.close()
        await server.wait_closed()
    assert caught.value.status_code == 307
    assert hits == [("/v1/models", "fake-live-key")]


async def test_sdk_sends_cache_control_without_beta_header():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_MESSAGE)

    client = AsyncAnthropic(
        api_key="fake-key", base_url=ANTHROPIC_API_BASE_URL, max_retries=0,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), follow_redirects=False
        ),
    )
    request = OpenAIChatRequest(
        model="claude-opus-5-5", max_tokens=64,
        messages=[
            OpenAIMessage(role="system", content="妳是 Sumi。"),
            OpenAIMessage(role="user", content="查一下"),
        ],
        tools=[{"type": "function", "function": {
            "name": "vault_tools", "description": "檔案", "parameters": {"type": "object"},
        }}],
    )
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(request, {"anthropic_client": client})
    try:
        rounds = [item async for item in adapter.send_round(state)]
    finally:
        await client.close()

    assert len(seen) == 1
    sent = seen[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert "anthropic-beta" not in sent.headers
    body = json.loads(sent.content)
    assert body["system"] == [
        {"type": "text", "text": "妳是 Sumi。", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert rounds[0].content[0].text == "好"


_WIRE_USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 2000,
               "cache_creation_input_tokens": 400}


@pytest.mark.parametrize(("extra", "usage_extra", "dumped"), [
    ({}, {}, (None, None, None)),  # 廠商沒回這三欄：SDK 補 None
    ({"stop_reason": "refusal", "diagnostics": {"cache_miss_reason": None},
      "stop_details": {"type": "refusal", "category": "cyber", "explanation": None}},
     {"output_tokens_details": {"thinking_tokens": 30}},
     ({"cache_miss_reason": None}, {"type": "refusal", "category": "cyber", "explanation": None},
      {"thinking_tokens": 30})),
])
async def test_real_sdk_message_through_native_finalize_and_usage(extra, usage_extra, dumped):
    wire = {**_MESSAGE, "usage": {**_WIRE_USAGE, **usage_extra}, **extra}
    raw = Message.model_validate(wire)  # SDK 收到回應時用的同一個型別
    adapter = ClaudeAdapter()
    state = await adapter.prepare_request(AnthropicRequest(
        model="claude-opus-5-5", max_tokens=64, messages=[{"role": "user", "content": "好"}],
    ), {"anthropic_client": None})
    native = adapter.finalize(state, raw, "rid", {"latency_ms": 1})
    assert state.is_native_anthropic is True
    assert (native["diagnostics"], native["stop_details"],
            native["usage"]["output_tokens_details"]) == dumped
    assert ([block["text"] for block in native["content"]], native["telemetry"]) == (
        [""], {"latency_ms": 1})
    assert adapter.extract_usage(state, raw) == ({
        "prompt_tokens": 100, "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 400, "completion_tokens": 50,
    }, 2500, 50)
    state.is_native_anthropic = False  # OpenAI 形狀出口也吃得下多出的欄位
    shaped = adapter.finalize(state, raw, "rid", {})
    assert (shaped["id"], shaped["choices"][0]["finish_reason"]) == ("chatcmpl-rid", "stop")

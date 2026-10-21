"""model-p3a U4：環境變數不得改寫 Claude API 的目的地（ADR-0247）。

anthropic SDK 1.11 沒拿到 ``base_url`` 時改連環境變數 ``ANTHROPIC_BASE_URL``，金鑰一起送過去。
每個建構點都顯式傳 ``src.config.ANTHROPIC_API_BASE_URL``。以下把環境變數指到另一台主機，再用
httpx2 MockTransport 看實際送出的請求（不連網）：

- 對照組：不傳 base_url 的 client 真的會改連環境變數（證明 monkeypatch 有效、測試不會空過）。
- 主迴圈 client（main.py）、factory ``with_options`` 換金鑰後的副本、``complete_once`` 的 Claude
  路徑，都仍送到 Touka 設定的目的地。
- ``complete_once`` 的 Claude 路徑走真的 SDK 1.11：``output_config.format`` 照送，停止原因與用量
  照舊解析（1b 契約測試換掉整個 client，這裡補 SDK 本身的序列化與解析）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from src import main as proxy_main
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers.adapters.stream_timeout import liveness_event_hooks
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import complete_once
from src.services.dispatchers import factory
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

_ENV_HOST = "https://env-override.invalid"
_TOUKA_URL = "https://api.anthropic.com/v1/messages"
_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", is_human=True, capabilities=[])
_SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
_USAGE4 = {"prompt_tokens": 1000, "cache_read_input_tokens": 2000,
           "cache_creation_input_tokens": 400, "completion_tokens": 500}


def _message(stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "通過"}], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 400},
    }


class _Wire:
    """取代 ``DefaultAsyncHttpxClient``：記下建構參數，傳輸換成 MockTransport。"""

    def __init__(self, stop: str = "end_turn") -> None:
        self.seen: list[httpx2.Request] = []
        self.client_kwargs: list[dict[str, Any]] = []
        self._body = _message(stop)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        return httpx2.Response(200, json=self._body)

    def http_client(self, **kwargs: Any) -> httpx2.AsyncClient:
        self.client_kwargs.append(kwargs)
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle), **kwargs)


async def _send(client: AsyncAnthropic) -> None:
    await client.messages.create(
        model="claude-opus-5-5", max_tokens=16, messages=[{"role": "user", "content": "好"}]
    )


async def _noop_stream(_req: Any, _ctx: Any):  # pragma: no cover - 佔位用的 async generator
    if False:
        yield ""


@pytest.fixture(autouse=True)
def env_host(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", _ENV_HOST)


async def test_control_sdk_without_base_url_follows_the_env_var():
    wire = _Wire()
    client = AsyncAnthropic(
        api_key="fake-key", max_retries=0, http_client=wire.http_client(follow_redirects=False)
    )
    try:
        await _send(client)
    finally:
        await client.close()
    assert [str(request.url) for request in wire.seen] == [f"{_ENV_HOST}/v1/messages"]


async def test_main_loop_client_ignores_env_base_url(monkeypatch):
    wire = _Wire()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-boot-key")
    monkeypatch.setattr(proxy_main, "DefaultAsyncHttpxClient", wire.http_client)
    client = proxy_main._build_anthropic_client()
    try:
        await _send(client)
    finally:
        await client.close()
    # 3b-U1（ADR-0248 附註 27）：主迴圈 client 另裝 httpx2 位元組計時 hook，其餘參數不變
    assert wire.client_kwargs == [
        {"follow_redirects": False, "event_hooks": liveness_event_hooks()}
    ]
    assert [str(request.url) for request in wire.seen] == [_TOUKA_URL]
    assert wire.seen[0].headers["x-api-key"] == "fake-boot-key"


async def test_factory_live_key_swap_keeps_touka_base_url(monkeypatch):
    wire = _Wire()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-boot-key")
    monkeypatch.setattr(proxy_main, "DefaultAsyncHttpxClient", wire.http_client)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-live-key", raising=False)
    monkeypatch.setattr(factory, "handle_claude_request", _noop_stream)
    boot = proxy_main._build_anthropic_client()
    ctx: dict = {"anthropic_client": boot}
    request = OpenAIChatRequest(
        model="claude-opus-5-5", messages=[OpenAIMessage(role="user", content="好")]
    )
    try:
        await factory.get_stream_generator("claude-opus-5-5", request, _ADMIN, "rid", 0.0, ctx)
        swapped = ctx["anthropic_client"]
        assert swapped is not boot and swapped.api_key == "fake-live-key"
        await _send(swapped)
    finally:
        await boot.close()
    assert [str(request.url) for request in wire.seen] == [_TOUKA_URL]
    assert wire.seen[0].headers["x-api-key"] == "fake-live-key"


@pytest.mark.parametrize(("stop", "normalized", "text"), [
    ("end_turn", "end", "通過"),
    ("refusal", "refusal", ""),
    ("model_context_window_exceeded", "max_tokens", ""),
])
async def test_complete_once_claude_on_sdk_1x(monkeypatch, stop, normalized, text):
    wire = _Wire(stop)
    monkeypatch.setattr(completion, "DefaultAsyncHttpxClient", wire.http_client)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr(completion, "autonomy_service",
                        SimpleNamespace(is_over_budget=lambda _identity: False,
                                        would_exceed=lambda _identity, _worst: False))
    booked: list[tuple] = []
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **_k: booked.append(a))
    monkeypatch.setattr(completion, "log_usage_audit", lambda *_a, **_k: None)

    result = await complete_once(_IDENTITY, "reviewer", "claude-sonnet-4-5", "審查員", "判斷",
                                 256, 5.0, response_schema=_SCHEMA)

    assert wire.client_kwargs == [{"follow_redirects": False}]
    (sent,) = wire.seen
    assert str(sent.url) == _TOUKA_URL
    assert sent.headers["x-api-key"] == "fake-anthropic"
    assert "anthropic-beta" not in sent.headers
    body = json.loads(sent.content)
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": _SCHEMA}}
    assert (body["model"], body["max_tokens"]) == ("claude-sonnet-4-5", 256)
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "判斷"}]}]
    assert (result.text, result.stop_reason, result.ok) == (text, normalized, stop == "end_turn")
    # sonnet 4.5：input 3、output 15、快取讀 0.3、快取寫 3.75（USD／百萬 token）；拒答、截斷照記
    assert (result.usage, result.cost_usd) == (_USAGE4, 0.0126)
    assert booked == [(_IDENTITY, "reviewer", _USAGE4)]

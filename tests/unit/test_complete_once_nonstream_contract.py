"""3b-U1：complete_once 的 Claude 路徑維持非串流、只送 1 次（ADR-0248 D1；附註 2、r2 M8）。

補充 ``test_complete_once_contract.py``（該檔不得修改）：那邊換掉整個 SDK client，看不到送出的
位元組，也測不出 SDK 自己的重試。這裡保留真的 ``completion.AsyncAnthropic``，只把傳輸換成
httpx2.MockTransport：

- 送出內容沒有 ``stream``（主迴圈才打開串流）；
- 用 SDK 真的例外型別（429 RateLimitError、529 OverloadedError）證明只送 1 次、沒有進 SDK 的
  重試等待——回應帶 ``retry-after: 30``，若重試就會等。

3b-U2：OpenAI 相容路徑同樣維持 ``httpx.AsyncClient.post(url, headers=, json=, timeout=)`` 單次
送出，參數一個不多，送出內容沒有 ``stream``／``stream_options``（已知廠商的主迴圈才串流）。

3b-U3：Gemini 路徑同樣是單次 ``post``，網址是 ``:generateContent``、沒有 ``alt=sse``（主迴圈才
串流），輸出上限由轉換器依請求帶入。

3b-U4b-2（裁定 4）：Responses 模型走非串流 ``responses.create``，真的 openai SDK＋httpx2
MockTransport：只送 1 次（429 帶 retry-after 也不重送、不等待）、送出內容沒有 ``stream``、
``store`` 是 false、目的地是常數（OPENAI_BASE_URL 設定與環境變數都不看）；停止原因對應、
用量扣 cached_tokens 並入帳。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic
import httpx
import httpx2
import openai
import pytest
from anthropic._base_client import AsyncAPIClient
from src.handlers.adapters import responses_adapter
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import complete_once
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

_MODEL = "claude-opus-5-5"
_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_OK = {
    "id": "msg_c", "type": "message", "role": "assistant", "model": _MODEL,
    "content": [{"type": "text", "text": "通過"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1},
}


@pytest.fixture
def wire(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "replies": [], "sleeps": [], "clients": []}

    def handle(request: httpx2.Request) -> httpx2.Response:
        state["sent"].append(json.loads(request.content))
        status, body, headers = state["replies"].pop(0)
        return httpx2.Response(status, json=body, headers=headers)

    def http_client(**kwargs: Any) -> Any:
        return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle), **kwargs)

    real = completion.AsyncAnthropic

    def build(**kwargs: Any) -> Any:
        state["clients"].append(kwargs)
        return real(**kwargs)

    async def sleep_for_retry(_self: Any, **kwargs: Any) -> None:
        state["sleeps"].append(kwargs)

    monkeypatch.setattr(completion, "DefaultAsyncHttpxClient", http_client)
    monkeypatch.setattr(completion, "AsyncAnthropic", build)
    monkeypatch.setattr(AsyncAPIClient, "_sleep_for_retry", sleep_for_retry)
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    monkeypatch.setattr(completion.autonomy_service, "is_over_budget", lambda _i: False)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    return state


def test_completion_keeps_the_real_sdk_client_name():
    assert completion.AsyncAnthropic is anthropic.AsyncAnthropic


async def _call(max_tokens: int = 256) -> Any:
    async with asyncio.timeout(5.0):
        return await complete_once(_IDENTITY, "reviewer", _MODEL, "s", "u", max_tokens, 3.0)


async def test_non_streaming_request_has_no_stream_field(wire):
    wire["replies"].append((200, _OK, {}))
    result = await _call()
    assert (result.ok, result.text) == (True, "通過")
    (sent,) = wire["sent"]
    assert "stream" not in sent
    assert sent["model"] == _MODEL
    (built,) = wire["clients"]
    assert built["max_retries"] == 0


@pytest.mark.parametrize("status", [429, 529], ids=["rate-limit", "overloaded"])
async def test_retryable_status_is_sent_once_without_waiting(wire, status, caplog):
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    wire["replies"].extend([(status, body, {"retry-after": "30"}), (200, _OK, {})])
    with caplog.at_level(logging.WARNING, logger=completion.__name__):
        result = await _call()
    assert f"Error code: {status}" in caplog.text  # SDK 真的狀態例外（RateLimitError／OverloadedError）
    assert (result.ok, result.text) == (False, "")
    assert (result.error.error_class, result.error.reason) == ("upstream_error", "upstream")
    assert len(wire["sent"]) == 1
    assert wire["sleeps"] == []


async def test_large_max_tokens_is_sent_as_is(wire):
    """附註 4：complete_once 有傳 timeout，SDK 不做非串流檢查，所以 32768 照送、不壓成 21,333。"""
    wire["replies"].append((200, _OK, {}))
    result = await _call(max_tokens=32768)
    assert (result.ok, result.text) == (True, "通過")
    (sent,) = wire["sent"]
    assert sent["max_tokens"] == 32768


_COMPAT_OK = {"choices": [{"message": {"content": "通過"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
_GEMINI_OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "通過"}]},
                              "finishReason": "STOP"}],
              "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}


@pytest.mark.parametrize("model", ["deepseek-chat", "grok-4", "gemini-2.5-flash"])
async def test_openai_compatible_posts_once_without_stream_fields(monkeypatch, model):
    calls: list[tuple[tuple, dict]] = []

    async def post(_client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> httpx.Response:
        calls.append((args, kwargs))
        body = _GEMINI_OK if model.startswith("gemini") else _COMPAT_OK
        return httpx.Response(200, json=body, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    monkeypatch.setattr(completion.autonomy_service, "is_over_budget", lambda _i: False)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    for name in ("DEEPSEEK_API_KEY", "GROK_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(f"src.config.{name}", "fake-key", raising=False)
    async with asyncio.timeout(5.0):
        result = await complete_once(_IDENTITY, "reviewer", model, "s", "u", 256, 3.0)
    assert (result.ok, result.text) == (True, "通過")
    ((args, kwargs),) = calls
    assert len(args) == 1 and sorted(kwargs) == ["headers", "json", "timeout"]
    assert "stream" not in kwargs["json"] and "stream_options" not in kwargs["json"]
    if model.startswith("gemini"):
        assert args[0] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                           "gemini-2.5-flash:generateContent")  # 沒有 alt=sse
        assert kwargs["headers"] == {"x-goog-api-key": "fake-key"}
        assert kwargs["json"]["generationConfig"] == {"maxOutputTokens": 256}


# ── 3b-U4b-2：Responses（裁定 4） ────────────────────────────────────────────

_RESPONSE_USAGE = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1024},
                   "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 12},
                   "total_tokens": 1240}


def _response(status: str = "completed", **extra: Any) -> dict:
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "通過", "annotations": []}]}
    return {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra",
            "status": status, "output": [message], "usage": _RESPONSE_USAGE, **extra}


@pytest.fixture
def responses_wire(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"requests": [], "replies": [], "booked": []}

    def handle(request: httpx2.Request) -> httpx2.Response:
        state["requests"].append(request)
        return state["replies"].pop(0)

    def http_client(**kwargs: Any) -> Any:
        return openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", http_client)
    monkeypatch.setattr(completion, "accumulate_token_usage",
                        lambda _i, _p, usage, **_k: state["booked"].append(usage))
    monkeypatch.setattr(completion, "log_usage_audit", lambda *a, **k: None)
    monkeypatch.setattr(completion.autonomy_service, "is_over_budget", lambda _i: False)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "fake-openai", raising=False)
    monkeypatch.setattr("src.config.OPENAI_BASE_URL", "https://settings.invalid/v1",
                        raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.invalid/v1")
    return state


async def _responses_call() -> Any:
    async with asyncio.timeout(5.0):
        return await complete_once(_IDENTITY, "reviewer", "gpt-6-astra", "s", "u", 256, 3.0)


async def test_responses_posts_once_without_stream(responses_wire):
    responses_wire["replies"].append(httpx2.Response(200, json=_response()))
    result = await _responses_call()
    assert (result.ok, result.text, result.stop_reason) == (True, "通過", "end")
    usage = {"prompt_tokens": 176, "cache_read_input_tokens": 1024,
             "cache_creation_input_tokens": 0, "completion_tokens": 40}
    assert (result.usage, responses_wire["booked"]) == (usage, [usage])
    (request,) = responses_wire["requests"]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == "Bearer fake-openai"
    body = json.loads(request.content)
    assert "stream" not in body
    assert (body["store"], body["max_output_tokens"], body["instructions"], body["input"]) == (
        False, 256, "s", [{"role": "user", "content": "u"}])


async def test_responses_429_is_sent_once_without_waiting(responses_wire):
    busy = httpx2.Response(429, headers={"retry-after": "30"},
                           json={"error": {"message": "slow down"}})
    responses_wire["replies"].extend([busy, httpx2.Response(200, json=_response())])
    result = await _responses_call()  # 若重試會先等 30 秒，外層 5 秒就會逾時
    assert (result.ok, result.error.reason) == (False, "upstream")
    assert len(responses_wire["requests"]) == 1
    assert responses_wire["booked"] == []


async def test_responses_incomplete_maps_to_max_tokens_and_still_books(responses_wire):
    truncated = _response("incomplete", incomplete_details={"reason": "max_output_tokens"})
    responses_wire["replies"].append(httpx2.Response(200, json=truncated))
    result = await _responses_call()
    assert (result.ok, result.text, result.stop_reason, result.error.reason) == (
        False, "", "max_tokens", "max_tokens")
    assert len(responses_wire["booked"]) == 1  # 已計費：截斷也記帳


async def test_responses_without_key_sends_nothing(responses_wire, monkeypatch):
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "", raising=False)
    result = await _responses_call()
    assert (result.ok, result.error.reason) == (False, "missing_credentials")
    assert responses_wire["requests"] == []

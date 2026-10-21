"""3b-U4a：OpenAI Responses 請求側（ADR-0248 D4／D5；DEV-A 附註 8、9、19、24）。

傳輸層契約：真的 openai SDK＋httpx2.MockTransport，送出內容從 request.content 解析，和
``tests/fixtures/responses_u4a_wire.json`` 逐字比對（U4b 的 send_round 也比這份）。測試先設好
``OPENAI_API_KEY``／``OPENAI_BASE_URL``／``OPENAI_ORG_ID``／``OPENAI_PROJECT_ID``（加上
``OPENAI_CUSTOM_HEADERS``），證明目的地與標頭都只來自路由 context。送出這一步在測試裡直接呼叫
``responses.create(**state.kwargs, stream=True)``——adapter 的 send_round 在 U4b-1。

- 第 1 輪：system 合併成 instructions、扁平工具（strict 預設 False、MCP 鍵不外送）、
  max_output_tokens、store=false、include、prompt_cache_key 雜湊、不送取樣參數與 stop。
- 第 2 輪：加密 reasoning 與 function_call 原樣回送（reasoning 在前），function_call_output
  用 call_id（fixture 的 item id 與 call_id 不同）。耗盡輪：拒絕的 output＋WRAP_UP、tool_choice。
- 外部歷史、多模態、degraded、缺憑證不建 client、OPENAI_LOG 不會讓請求進日誌、GPT 登入接縫。
  環境變數清單與 logger 釘在 WARNING 的單元測試在 test_openai_sdk_env_security.py。
  錄製台的 Responses 快照正規化在 tests/proxy/upstream/test_harness.py。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from src.adapters.schemas import OpenAIChatRequest
from src.handlers.adapters import responses_adapter
from src.handlers.adapters.base import ToolCall, Turn
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.handlers.adapters.stream_timeout import liveness_event_hooks

ROOT = Path(__file__).resolve().parents[2]
WIRE = json.loads((ROOT / "tests/fixtures/responses_u4a_wire.json").read_text(encoding="utf-8"))
_BASE = "https://api.openai.test/v1"
_ENV = {"OPENAI_API_KEY": "sk-env-not-used", "OPENAI_BASE_URL": "https://env.invalid/v1",
        "OPENAI_ORG_ID": "org-env", "OPENAI_PROJECT_ID": "proj-env",
        "OPENAI_CUSTOM_HEADERS": "X-From-Env: 1"}
_CTX = {"provider": "OpenAI", "base_url": _BASE, "api_key": "fake-ctx-key", "agent_id": "Alice",
        "session_id": "sess-1"}
_TOOL = {"type": "function", "server": "touka", "category": "fs", "function": {
    "name": "vault_tools", "description": "檔案",
    "parameters": {"type": "object", "properties": {"action": {"type": "string"}}}}}
_REASONING = {"type": "reasoning", "id": "rs_1", "summary": [],
              "encrypted_content": "gAAAAABfakeEncryptedReasoningBlob=="}
_CALL = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "vault_tools",
         "arguments": '{"action":"list"}', "status": "completed"}
_COMPLETED = (
    b'event: response.completed\ndata: {"type":"response.completed","sequence_number":0,'
    b'"response":{"id":"resp_1","object":"response","created_at":1,"model":"gpt-6-astra",'
    b'"status":"completed","output":[],"parallel_tool_calls":true,"tool_choice":"auto",'
    b'"tools":[]}}\n\n'
)


def _request(messages: list[dict] | None = None, **extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(model="gpt-6-astra", messages=messages or [
        {"role": "system", "content": "妳是助理。"}, {"role": "system", "content": "用繁體中文。"},
        {"role": "user", "content": "讀 a.md"},
    ], **extra)


class _Wire:
    """httpx2.MockTransport：記下每個請求；SDK 的 http client 換成接這個傳輸的版本。"""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.client_kwargs: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_COMPLETED, request=request)

    def http_client(self, **kwargs: Any) -> Any:
        self.client_kwargs.append(kwargs)
        return openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler),
                                              **kwargs)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def wire(monkeypatch) -> _Wire:
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    recorder = _Wire()
    monkeypatch.setattr(responses_adapter, "DefaultAsyncHttpxClient", recorder.http_client)
    return recorder


async def _send(state: Any) -> None:
    stream = await state.client.responses.create(**state.kwargs, stream=True)
    async for _ in stream:
        pass


# ── 傳輸層契約（fixture 逐字比對） ──────────────────────────────────────────


async def test_three_rounds_match_the_wire_fixture(wire):
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(
        _request(tools=[_TOOL], max_tokens=512, temperature=0.3, top_p=0.9, top_k=40,
                 stop=["END"]), dict(_CTX))
    await _send(state)
    adapter.append_assistant_turn(state, Turn(raw_assistant=[_REASONING, _CALL]))
    await adapter.append_tool_results(state, [("call_1", "vault_tools", "a.md")])
    await _send(state)
    second_call = {**_CALL, "id": "fc_2", "call_id": "call_2", "arguments": "{}"}
    adapter.build_denied_turn(state, Turn(raw_assistant=[second_call], tool_calls=[
        ToolCall(id="call_2", name="vault_tools", args={})]), "工具輪額度已用盡")
    adapter.set_tool_choice(state, "none")
    await _send(state)
    await adapter.close(state)
    assert wire.bodies == [WIRE["first_round"], WIRE["second_round"], WIRE["denied_round"]]
    assert [str(r.url) for r in wire.requests] == [WIRE["url"]] * 3
    assert "stream" not in state.kwargs  # 串流旗標只在送出當下合併（D2）
    assert wire.client_kwargs == [{"follow_redirects": False,  # 禁令 22
                                   "event_hooks": liveness_event_hooks()}]  # 附註 27（3b-U4b-1）


async def test_environment_cannot_change_destination_or_headers(wire):
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(_request(), dict(_CTX))
    await _send(state)
    await adapter.close(state)
    (request,) = wire.requests
    assert str(request.url) == f"{_BASE}/responses"
    assert request.headers["authorization"] == "Bearer fake-ctx-key"
    present = {"openai-organization", "openai-project", "x-from-env"} & set(request.headers)
    assert present == set()
    assert "sk-env-not-used" not in str(request.headers)


@pytest.mark.parametrize("missing", ["api_key", "base_url"])
async def test_missing_credential_or_base_url_builds_no_client(wire, missing):
    ctx = {k: v for k, v in _CTX.items() if k != missing}
    state = await ResponsesAdapter().prepare_request(_request(), ctx)
    assert state.client is None  # SDK 沒拿到值會改讀環境變數，所以乾脆不建
    assert wire.client_kwargs == []


async def test_openai_log_debug_cannot_log_the_request(wire, monkeypatch):
    loggers = [logging.getLogger(name) for name in ("openai", "httpx2")]
    original = [lg.level for lg in loggers]
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append  # type: ignore[method-assign]
    monkeypatch.setenv("OPENAI_LOG", "debug")
    try:
        for lg in loggers:
            lg.setLevel(logging.DEBUG)  # 等同 import openai 時依 OPENAI_LOG 調高
            lg.addHandler(handler)
        adapter = ResponsesAdapter()
        state = await adapter.prepare_request(_request([{"role": "user", "content": "SECRET-1"}]),
                                              dict(_CTX))
        levels = [lg.level for lg in loggers]
        await _send(state)
        await adapter.close(state)
    finally:
        for lg, level in zip(loggers, original, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)
    assert levels == [logging.WARNING, logging.WARNING]
    assert not any("SECRET-1" in record.getMessage() for record in records)


# ── 轉換細節 ────────────────────────────────────────────────────────────────


async def test_external_tool_history_uses_call_ids():
    state = await ResponsesAdapter().prepare_request(_request([
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_abc", "type": "function",
            "function": {"name": "vault_tools", "arguments": '{"action": "list"}'}}]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "a.txt"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "繼續"},
    ]), {})
    assert state.kwargs["input"] == [
        {"role": "user", "content": "查一下"},
        {"type": "function_call", "call_id": "call_abc", "name": "vault_tools",
         "arguments": '{"action": "list"}'},
        {"type": "function_call_output", "call_id": "call_abc", "output": "a.txt"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "繼續"},
    ]
    assert "instructions" not in state.kwargs and "tools" not in state.kwargs


async def test_multimodal_user_content_and_unknown_blocks(caplog):
    blocks = [{"type": "text", "text": "看圖"},
              {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    with caplog.at_level(logging.WARNING, logger=responses_adapter.__name__):
        state = await ResponsesAdapter().prepare_request(_request([
            {"role": "user", "content": json.dumps(blocks)},  # ToukaChat 夾帶的 JSON 字串
            {"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]},
        ]), {})
    assert state.kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "看圖"},
                                     {"type": "input_image",
                                      "image_url": "data:image/png;base64,AAAA"}]},
    ]  # 第二則的區塊全不支援：不送空殼
    assert "不支援的內容區塊" in caplog.text


async def test_degraded_warning_goes_into_instructions():
    ctx = {"system_degraded": True, "degraded_reason": "Memory offline."}
    with_system = await ResponsesAdapter().prepare_request(_request(), dict(ctx))
    without = await ResponsesAdapter().prepare_request(
        _request([{"role": "user", "content": "hi"}]), dict(ctx))
    warning = ("⚠️ SYSTEM WARNING: The proxy is running in DEGRADED MODE. Memory offline. "
               "Some tools may be offline. Please inform the user.")
    assert with_system.kwargs["instructions"] == f"妳是助理。\n用繁體中文。\n\n{warning}"
    assert without.kwargs["instructions"] == warning


async def test_prompt_cache_key_is_a_hash_not_the_names():
    state = await ResponsesAdapter().prepare_request(_request(), {"agent_id": "Sumi"})
    assert state.kwargs["prompt_cache_key"] == "touka-7c8c0f3a89f5eb6cc5375f2de76f1b06"
    alice = await ResponsesAdapter().prepare_request(_request(), dict(_CTX))
    sent = json.dumps(alice.kwargs, ensure_ascii=False)
    assert "Alice" not in sent and "sess-1" not in sent


# ── GPT 登入接縫（附註 24；不實作登入） ─────────────────────────────────────


async def test_login_seam_swaps_base_url_headers_and_token_provider(wire):
    tokens = iter(["fake-access-token-1", "fake-access-token-2"])

    async def token_provider() -> str:
        return next(tokens)

    ctx = {"base_url": "https://chatgpt.test/backend-api/codex", "token_provider": token_provider,
           "extra_headers": {"chatgpt-account-id": "acc_fake"}}
    adapter = ResponsesAdapter()
    state = await adapter.prepare_request(_request(), ctx)
    await _send(state)
    await _send(state)
    await adapter.close(state)
    assert [(str(r.url), r.headers["authorization"], r.headers["chatgpt-account-id"])
            for r in wire.requests] == [
        ("https://chatgpt.test/backend-api/codex/responses", "Bearer fake-access-token-1",
         "acc_fake"),
        ("https://chatgpt.test/backend-api/codex/responses", "Bearer fake-access-token-2",
         "acc_fake"),
    ]


async def test_unknown_block_is_logged_by_type_only(caplog):
    """R-3b ① L1：無痕請求的 document 區塊內容不得進日誌，只記區塊型別。"""
    secret = {"type": "document", "text": "INCOGNITO-SECRET-DOC-TEXT"}
    with caplog.at_level(logging.WARNING, logger=responses_adapter.__name__):
        await ResponsesAdapter().prepare_request(_request([
            {"role": "user", "content": [secret, {"type": "text", "text": "看"}]}]),
            {"incognito": True})
    assert "不支援的內容區塊，已略過：document" in caplog.text
    assert "INCOGNITO-SECRET-DOC-TEXT" not in caplog.text

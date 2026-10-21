"""complete_once 契約測試（ADR-0244，1b-U3；master R3／R4）。

3a 換 Claude SDK、3b 換上游傳輸時，本檔必須原封不動通過。假傳輸只換最外層：
Claude 換 ``completion.AsyncAnthropic``，OpenAI 相容／Gemini 換 ``httpx.AsyncClient.post``；
路由、adapter、用量正規化、停止原因都走真的程式。記帳與成本閘換成記錄器，不落盤、不連網。

釘住的合約：
- 回傳 ``CompletionResult(text, usage, cost_usd, stop_reason, error)``；成功＝``error is None``
  且 ``stop_reason == "end"``；
- 先過成本閘，超預算不送出；
- 廠商回了用量就記帳（provider 標籤＝用途），拒答、截斷也照記；只記用量、不記內容；
- 拒答、截斷、安全過濾、逾時、超預算、廠商錯誤一律結構化非成功，``text`` 為空，不會變成 end；
- 不跨廠商、不重試；``timeout_s`` 是整次呼叫的總時間；
- ``response_schema`` 只對 Claude 外送（``output_config.format``）；其他家族不外送、原文照回。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import Completer, CompletionResult, complete_once
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution
from src.utils.time_utils import get_current_epoch

_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_SYSTEM = "SYSTEM-SECRET-7f3a 你是審查員"
_USER = "USER-SECRET-91bc 請判斷這個動作"
_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
_CLAUDE = "claude-sonnet-4-5"
_OPENAI = "deepseek-chat"
_GEMINI = "gemini-2.5-flash"


# ── 假傳輸與記錄器 ────────────────────────────────────────────────────────────


class _Hang:
    """送出後永遠不回（逾時測試用）。"""


def _claude_msg(text: str = "好", stop: str | None = "end_turn",
                usage: dict | None = None) -> Any:
    dumped = usage or {"input_tokens": 1000, "output_tokens": 500,
                       "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 400}
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(model_dump=lambda: dict(dumped)),
    )


class _FakeAnthropic:
    def __init__(self, script: list, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict] = []
        self.closed = False
        self._script = script
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw: Any) -> Any:
        self.calls.append(copy.deepcopy(kw))
        item = self._script.pop(0)
        if isinstance(item, _Hang):
            await asyncio.Event().wait()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
        http_client = self.kwargs.get("http_client")
        if http_client is not None:
            await http_client.aclose()


class _Wire:
    """收集所有假傳輸的呼叫（三家共用一份，證明不跨廠商）。"""

    def __init__(self) -> None:
        self.claude: list[_FakeAnthropic] = []
        self.claude_script: list = []
        self.http_calls: list[dict] = []
        self.http_script: list = []

    def anthropic_factory(self, **kwargs: Any) -> _FakeAnthropic:
        client = _FakeAnthropic(self.claude_script, **kwargs)
        self.claude.append(client)
        return client

    async def post(self, url: str, *, headers: Any = None, json: Any = None,
                   timeout: Any = None) -> httpx.Response:
        self.http_calls.append({"url": url, "json": copy.deepcopy(json)})
        item = self.http_script.pop(0)
        if isinstance(item, _Hang):
            await asyncio.Event().wait()
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    @property
    def claude_calls(self) -> list[dict]:
        return [call for client in self.claude for call in client.calls]


class _Budget:
    def __init__(self) -> None:
        self.over = False
        self.checked: list[AgentIdentity] = []

    def is_over_budget(self, identity: AgentIdentity) -> bool:
        self.checked.append(identity)
        return self.over

    def would_exceed(self, _identity: AgentIdentity, _worst_usd: float) -> bool:
        return False  # r55 送前估算（D5）：本檔只驗既有合約，真假分支在 test_cost_guard／thinking


@pytest.fixture
def wire(monkeypatch) -> _Wire:
    w = _Wire()

    async def _post(_client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return await w.post(url, **kwargs)

    monkeypatch.setattr(completion, "AsyncAnthropic", w.anthropic_factory)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(ModelManager, "get_models",
                        lambda _self: [{"id": "Human", "owned_by": "human"}])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "fake-gemini", raising=False)
    monkeypatch.setattr("src.config.DEEPSEEK_API_KEY", "fake-deepseek", raising=False)
    monkeypatch.setattr("src.config.DEEPSEEK_BASE_URL", "https://deepseek.test/v1",
                        raising=False)
    return w


@pytest.fixture
def budget(monkeypatch) -> _Budget:
    b = _Budget()
    monkeypatch.setattr(completion, "autonomy_service", b)
    return b


@pytest.fixture
def ledger(monkeypatch) -> dict[str, list]:
    book: dict[str, list] = {"usage": [], "audit": []}
    monkeypatch.setattr(completion, "accumulate_token_usage",
                        lambda *a, **k: book["usage"].append((a, k)))
    monkeypatch.setattr(completion, "log_usage_audit",
                        lambda *a, **k: book["audit"].append((a, k)))
    return book


async def _call(model: str, *, timeout_s: float = 5.0, schema: dict | None = None,
                purpose: str = "reviewer") -> CompletionResult:
    return await complete_once(_IDENTITY, purpose, model, _SYSTEM, _USER, 256, timeout_s,
                               response_schema=schema)


def _openai_body(content: str | None = "好", finish: str | None = "stop", **message: Any) -> dict:
    return {
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content, **message}}],
        "usage": {"prompt_tokens": 3000, "completion_tokens": 500,
                  "prompt_tokens_details": {"cached_tokens": 2000}},
    }


def _gemini_body(text: str = "好", finish: str | None = "STOP") -> dict:
    candidate: dict = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish is not None:
        candidate["finishReason"] = finish
    return {"candidates": [candidate],
            "usageMetadata": {"promptTokenCount": 3000, "candidatesTokenCount": 500,
                              "cachedContentTokenCount": 2000}}


_USAGE4 = {"prompt_tokens": 1000, "cache_read_input_tokens": 2000,
           "cache_creation_input_tokens": 400, "completion_tokens": 500}
_USAGE4_INCLUDED = {"prompt_tokens": 1000, "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 0, "completion_tokens": 500}


# ── 成功：三家各一，含送出內容與記帳 ─────────────────────────────────────────────


async def test_claude_success_shape_wire_and_ledger(wire, budget, ledger):
    wire.claude_script.append(_claude_msg("通過"))
    result = await _call(_CLAUDE)
    # sonnet 4：input 3、output 15、快取讀 0.3、快取寫 3.75（USD／百萬 token）
    assert result == CompletionResult(text="通過", usage=_USAGE4, cost_usd=0.0126,
                                      stop_reason="end", error=None)
    assert result.ok is True
    (client,) = wire.claude
    assert client.kwargs["max_retries"] == 0
    assert client.kwargs["timeout"] == 5.0
    assert client.kwargs["api_key"] == "fake-anthropic"
    assert client.kwargs["http_client"].follow_redirects is False
    assert client.closed is True
    (sent,) = client.calls
    assert sent["model"] == _CLAUDE
    assert sent["max_tokens"] == 256
    assert "tools" not in sent and "output_config" not in sent
    assert _SYSTEM in str(sent["system"])
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": _USER}]}]
    assert budget.checked == [_IDENTITY]
    # e_index=None：旁路呼叫不寫 E-Index 歷史（ADR-0244，U2 起 e_index 選填）
    assert ledger["usage"] == [((_IDENTITY, "reviewer", _USAGE4),
                                {"e_index": None, "model": _CLAUDE})]
    ((audit_args, audit_kwargs),) = ledger["audit"]
    assert audit_args[0] == _IDENTITY and audit_args[2] == "reviewer"
    assert audit_args[4] == _USAGE4 and audit_kwargs == {"model": _CLAUDE}
    assert wire.http_calls == []


async def test_openai_compatible_success(wire, budget, ledger):
    wire.http_script.append((200, _openai_body("通過")))
    result = await _call(_OPENAI, purpose="summarizer")
    # deepseek-chat：input 0.27、output 1.10，快取未設費率＝照 input 價
    assert result == CompletionResult(text="通過", usage=_USAGE4_INCLUDED, cost_usd=0.00136,
                                      stop_reason="end", error=None)
    ((call),) = wire.http_calls
    assert call["url"] == "https://deepseek.test/v1/chat/completions"
    assert call["json"]["max_tokens"] == 256
    assert call["json"]["messages"] == [{"role": "system", "content": _SYSTEM},
                                        {"role": "user", "content": _USER}]
    assert "tools" not in call["json"]
    assert ledger["usage"] == [((_IDENTITY, "summarizer", _USAGE4_INCLUDED),
                                {"e_index": None, "model": _OPENAI})]
    assert wire.claude == []


async def test_gemini_success(wire, budget, ledger):
    wire.http_script.append((200, _gemini_body("通過")))
    result = await _call(_GEMINI, purpose="judge")
    # gemini-2.5-flash：input 0.30、output 2.50，快取照 input 價
    assert result == CompletionResult(text="通過", usage=_USAGE4_INCLUDED, cost_usd=0.00215,
                                      stop_reason="end", error=None)
    ((call),) = wire.http_calls
    assert call["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert call["json"]["generationConfig"] == {"maxOutputTokens": 256}
    assert call["json"]["system_instruction"] == {"parts": [{"text": _SYSTEM}]}
    assert call["json"]["contents"] == [{"role": "user", "parts": [{"text": _USER}]}]
    assert "tools" not in call["json"]
    assert [entry[0][1] for entry in ledger["usage"]] == ["judge"]


# ── 拒答、截斷、安全過濾：結構化非成功，仍記帳（已計費） ────────────────────────


@pytest.mark.parametrize(("model", "response", "stop"), [
    (_CLAUDE, _claude_msg("我不能", stop="refusal"), "refusal"),
    (_CLAUDE, _claude_msg("截到一半", stop="max_tokens"), "max_tokens"),
    (_OPENAI, (200, _openai_body("截到一半", finish="length")), "max_tokens"),
    (_OPENAI, (200, _openai_body(None, refusal="I can't")), "refusal"),
    (_OPENAI, (200, _openai_body("", finish="content_filter")), "safety"),
    (_GEMINI, (200, _gemini_body("", finish="SAFETY")), "safety"),
    (_GEMINI, (200, _gemini_body("截到一半", finish="MAX_TOKENS")), "max_tokens"),
])
async def test_refusal_truncation_safety_are_structured_failures(wire, budget, ledger, model,
                                                                 response, stop):
    (wire.claude_script if model == _CLAUDE else wire.http_script).append(response)
    result = await _call(model)
    assert result.ok is False
    assert result.stop_reason == stop
    assert result.text == ""
    assert (result.error.error_class, result.error.reason) == ("upstream_error", stop)
    assert result.usage and result.cost_usd > 0
    assert len(ledger["usage"]) == 1 and len(ledger["audit"]) == 1


async def test_unreadable_billed_response_is_still_booked(wire, budget, ledger):
    body = {**_openai_body(), "choices": [{"finish_reason": "stop", "message": "not-a-dict"}]}
    wire.http_script.append((200, body))
    result = await _call(_OPENAI)
    assert (result.error.error_class, result.error.reason) == ("internal_error", "internal")
    assert (result.text, result.usage) == ("", _USAGE4_INCLUDED)
    assert len(ledger["usage"]) == 1 and len(ledger["audit"]) == 1


@pytest.mark.parametrize(("model", "response"), [
    (_CLAUDE, _claude_msg("?", stop="pause_turn")),
    (_OPENAI, (200, _openai_body("?", finish=None))),
    (_GEMINI, (200, _gemini_body("?", finish="OTHER"))),
])
async def test_unknown_stop_is_error_not_end(wire, budget, ledger, model, response):
    (wire.claude_script if model == _CLAUDE else wire.http_script).append(response)
    result = await _call(model)
    assert (result.stop_reason, result.text) == ("error", "")
    assert (result.error.error_class, result.error.reason) == ("upstream_error", "unknown_stop")


# ── 超預算、路由不到、缺金鑰：不送出、不記帳 ────────────────────────────────────


async def test_over_budget_sends_nothing(wire, budget, ledger):
    budget.over = True
    result = await _call(_CLAUDE)
    assert result == CompletionResult(
        text="", usage={}, cost_usd=0.0, stop_reason="error", error=result.error)
    assert (result.error.error_class, result.error.reason) == ("permission_denied",
                                                              "over_budget")
    assert wire.claude == [] and wire.http_calls == []
    assert ledger == {"usage": [], "audit": []}


@pytest.mark.parametrize("model", ["mystery-model", "Human"])
async def test_unroutable_model_sends_nothing(wire, budget, ledger, model):
    result = await _call(model)
    assert (result.error.error_class, result.error.reason) == ("resource_not_found",
                                                              "model_unavailable")
    assert wire.claude == [] and wire.http_calls == []
    assert ledger == {"usage": [], "audit": []}


@pytest.mark.parametrize(("model", "key_attr"), [
    (_CLAUDE, "ANTHROPIC_API_KEY"),
    (_GEMINI, "GEMINI_API_KEY"),
])
async def test_native_family_without_key_sends_nothing(wire, budget, ledger, monkeypatch,
                                                       model, key_attr):
    monkeypatch.setattr(f"src.config.{key_attr}", "", raising=False)
    result = await _call(model)
    assert (result.error.error_class, result.error.reason) == ("resource_not_found",
                                                              "missing_credentials")
    assert wire.claude == [] and wire.http_calls == []


# ── 廠商錯誤：不重試、不跨廠商 ────────────────────────────────────────────────


async def test_claude_upstream_error_single_attempt(wire, budget, ledger):
    wire.claude_script.append(RuntimeError("overloaded"))
    result = await _call(_CLAUDE)
    assert (result.stop_reason, result.text) == ("error", "")
    assert (result.error.error_class, result.error.reason) == ("upstream_error", "upstream")
    assert len(wire.claude_calls) == 1
    assert wire.claude[0].kwargs["max_retries"] == 0
    assert wire.http_calls == []  # 不跨廠商
    assert ledger == {"usage": [], "audit": []}


@pytest.mark.parametrize(("model", "response"), [
    (_OPENAI, (500, {"error": "boom"})),
    (_GEMINI, (503, {"error": "busy"})),  # adapter 會發重試通知 → 立即中止
    (_GEMINI, (400, {"error": "bad"})),
])
async def test_http_upstream_error_single_attempt(wire, budget, ledger, model, response):
    wire.http_script.append(response)
    started = get_current_epoch()
    result = await _call(model)
    assert (result.error.error_class, result.error.reason) == ("upstream_error", "upstream")
    assert len(wire.http_calls) == 1
    assert wire.claude == []
    assert get_current_epoch() - started < 1.5  # 503 沒有進 adapter 的退避等待


# ── timeout_s＝總時間 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", [_CLAUDE, _OPENAI, _GEMINI])
async def test_timeout_is_structured(wire, budget, ledger, model):
    (wire.claude_script if model == _CLAUDE else wire.http_script).append(_Hang())
    started = get_current_epoch()
    result = await _call(model, timeout_s=0.05)
    assert get_current_epoch() - started < 1.5
    assert (result.stop_reason, result.text, result.usage) == ("error", "", {})
    assert (result.error.error_class, result.error.reason) == ("timeout_exceeded", "timeout")
    assert all(client.closed for client in wire.claude)
    assert ledger == {"usage": [], "audit": []}


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), (503, {"error": "busy"})],
                         ids=["connect-error", "503"])
async def test_gemini_never_resends_even_with_long_timeout(wire, budget, ledger, failure):
    """R3 不重試對 Gemini 也成立：timeout_s 很長、第二次會成功，仍只送出一次並回非成功。"""
    wire.http_script.extend([failure, (200, _gemini_body())])
    result = await _call(_GEMINI, timeout_s=60.0)
    assert (result.ok, result.text) == (False, "")
    assert (result.error.error_class, result.error.reason) == ("upstream_error", "upstream")
    assert len(wire.http_calls) == 1
    assert ledger == {"usage": [], "audit": []}


async def test_timeout_bounds_adapter_internal_backoff(wire, budget, ledger):
    """adapter 失敗後的等待也算在總時間內：總時間 0.3 秒就收尾，只送出一次。"""
    wire.http_script.extend([httpx.ConnectError("down"), (200, _gemini_body())])
    started = get_current_epoch()
    result = await _call(_GEMINI, timeout_s=0.3)
    assert get_current_epoch() - started < 1.5
    assert result.error.reason == "timeout"
    assert len(wire.http_calls) == 1


# ── 只記用量、不記內容 ────────────────────────────────────────────────────────


async def test_ledger_and_logs_never_contain_prompt_content(wire, budget, ledger, caplog):
    wire.claude_script.extend([_claude_msg("通過"), RuntimeError("boom")])
    wire.http_script.append((200, _openai_body("", finish="length")))
    with caplog.at_level(logging.DEBUG):
        await _call(_CLAUDE)
        await _call(_CLAUDE)
        await _call(_OPENAI)
    recorded = repr(ledger) + "\n".join(r.getMessage() for r in caplog.records)
    assert len(ledger["usage"]) == 2
    for secret in ("SYSTEM-SECRET-7f3a", "USER-SECRET-91bc"):
        assert secret not in recorded


# ── response_schema（R4） ────────────────────────────────────────────────────


async def test_claude_sends_schema_via_output_config(wire, budget, ledger):
    wire.claude_script.append(_claude_msg('{"verdict": "allow"}'))
    result = await _call(_CLAUDE, schema=_SCHEMA)
    assert wire.claude_calls[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": _SCHEMA}}
    assert result.text == '{"verdict": "allow"}'


@pytest.mark.parametrize(("model", "response"), [
    (_OPENAI, (200, _openai_body("不是 JSON"))),
    (_GEMINI, (200, _gemini_body("不是 JSON"))),
])
async def test_non_claude_schema_is_not_sent_and_text_is_returned_verbatim(
    wire, budget, ledger, model, response
):
    """非 Claude：schema 不外送、complete_once 不解析；原文照回，由呼叫端嚴格解析、失敗不放行。"""
    wire.http_script.append(response)
    result = await _call(model, schema=_SCHEMA)
    sent = wire.http_calls[0]["json"]
    assert not {"response_format", "output_config", "responseSchema"} & set(sent)
    assert "responseMimeType" not in sent.get("generationConfig", {})
    assert (result.ok, result.stop_reason, result.text) == (True, "end", "不是 JSON")


# ── Completer Protocol（呼叫端與測試注入用） ─────────────────────────────────────


def test_complete_once_signature_matches_completer_protocol():
    proto = list(inspect.signature(Completer.__call__).parameters.values())[1:]
    real = list(inspect.signature(complete_once).parameters.values())
    assert [(p.name, p.default) for p in real] == [(p.name, p.default) for p in proto]
    assert [p.name for p in real] == ["identity", "purpose", "model", "system", "user",
                                      "max_tokens", "timeout_s", "response_schema"]


async def test_fake_completer_can_be_injected():
    seen: list[tuple] = []

    async def fake(identity, purpose, model, system, user, max_tokens, timeout_s,
                   response_schema=None) -> CompletionResult:
        seen.append((purpose, model, max_tokens, response_schema))
        return CompletionResult(text='{"verdict": "deny"}', usage={}, cost_usd=0.0,
                                stop_reason="end", error=None)

    async def caller(completer: Completer) -> str:
        result = await completer(_IDENTITY, "reviewer", "any-model", "s", "u", 64, 1.0,
                                 response_schema=_SCHEMA)
        return result.text if result.ok else "escalate"

    assert await caller(fake) == '{"verdict": "deny"}'
    assert seen == [("reviewer", "any-model", 64, _SCHEMA)]

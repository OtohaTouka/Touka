"""模型名 → 路由＋連線資訊（resolve_model_route）與 factory 各入口（ADR-0244，1b-U3）。

3b-U4b-2：官方 OpenAI（非自訂連線）且規則表標 ``uses_responses_api`` 的模型走 Responses，
base URL 是常數、不看 OPENAI_BASE_URL；自訂連線與其他廠商留在 Chat（ADR-0248 裁定 2、附註 37）。
Anthropic 入口也寫入 ``context["model"]``（附註 35）。

factory 的「模型名→走哪一家＋連線資訊」抽成 ``route_family.resolve_model_route``，factory 與
``complete_once`` 共用。純重構：下方六個入口（Human、有效自訂連線、停用自訂連線、Claude、
Gemini、OpenAI 相容）交給 handler 的 context 逐鍵釘死，抽函式前後都要綠。全程 mock，不連網。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ErrorCode, ToukaException
from src.handlers import openai_handler
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.dispatchers.route_family import ModelRoute, resolve_model_route
from src.services.dispatchers.utils import get_provider_info
from src.services.model_manager import model_manager
from src.services.models.manager import ModelManager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
)

_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_GROK_URL = "https://grok.test/v1/chat/completions"
_RESPONSES_URL = "https://api.openai.com/v1"  # 常數；下方 fixture 把 OPENAI_BASE_URL 設成別的


def _resolve_custom(_self, name: str) -> CustomRouteResolution:
    if name == "local-a":
        route = CustomModelRoute(
            connection_id="conn-1", endpoint="http://127.0.0.1:1", api_key="", provider="X"
        )
        return CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM, route=route
        )
    if name == "gpt-6-astra-mine":  # 名稱像 Responses 模型的自訂連線：留在 Chat
        route = CustomModelRoute(connection_id="conn-2", endpoint="http://127.0.0.1:2",
                                 api_key="k2", provider="OpenAI")
        return CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM, route=route
        )
    if name == "claude-retired":  # 名稱像 claude：停用的自訂連線不得落入原生推測
        return CustomRouteResolution(disposition=CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE)
    return CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(
        ModelManager, "get_models", lambda _self: [{"id": "Human", "owned_by": "human"}]
    )
    monkeypatch.setattr(ModelManager, "resolve_custom_route", _resolve_custom)
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "fake-gemini", raising=False)
    monkeypatch.setattr("src.config.GROK_API_KEY", "fake-grok", raising=False)
    monkeypatch.setattr("src.config.GROK_DEFAULT_URL", _GROK_URL, raising=False)
    monkeypatch.setattr("src.config.OPENAI_API_KEY", "fake-openai", raising=False)
    monkeypatch.setattr("src.config.OPENAI_BASE_URL", "https://settings.test/v1", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.invalid/v1")


@pytest.fixture
def chosen(monkeypatch) -> list[tuple[str, object]]:
    picked: list[tuple[str, object]] = []
    for key in ("claude", "gemini", "openai", "responses"):
        monkeypatch.setattr(
            factory,
            f"handle_{key}_request",
            lambda req, _ctx, _key=key: picked.append((_key, req)) or f"stream-{_key}",
        )
    return picked


def _req(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


async def _dispatch(model: str, ctx: dict):
    return await factory.get_stream_generator(model, _req(model), _IDENTITY, "rid", 1.5, ctx,
                                              session_id="sid")


# ── factory 六個入口：context 逐鍵釘死 ────────────────────────────────────────


async def test_entry_human_uses_human_handler(monkeypatch, chosen):
    class _FakeHuman:
        def generate_response(self, request, context):
            return ("human", request.model, dict(context))

    monkeypatch.setattr(factory, "HumanModelHandler", _FakeHuman)
    ctx: dict = {}
    result = await _dispatch("Human", ctx)
    assert result == ("human", "Human", {"identity": _IDENTITY})
    assert chosen == []


async def test_entry_active_custom_goes_openai_with_route_secrets(chosen):
    ctx: dict = {}
    assert await _dispatch("local-a", ctx) == "stream-openai"
    assert [k for k, _ in chosen] == ["openai"]
    assert ctx == {
        "identity": _IDENTITY, "request_id": "rid", "t0": 1.5, "session_id": "sid",
        "api_key": "", "url": "http://127.0.0.1:1", "provider": "X",
        "custom_connection": True, "model": "local-a",  # 3b 裁定 5：路由層標出自訂連線
    }


async def test_entry_inactive_custom_is_model_not_found(chosen):
    with pytest.raises(ToukaException) as exc:
        await _dispatch("claude-retired", {})
    assert exc.value.code == ErrorCode.MODEL_NOT_FOUND
    assert chosen == []


async def test_entry_claude_swaps_in_live_key(chosen):
    swapped = SimpleNamespace(api_key="fake-anthropic")
    calls: list[dict] = []

    def with_options(**kw):
        calls.append(kw)
        return swapped

    boot = SimpleNamespace(api_key="boot-key", with_options=with_options)
    ctx: dict = {"anthropic_client": boot}
    assert await _dispatch("claude-sonnet-4-5", ctx) == "stream-claude"
    assert calls == [{"api_key": "fake-anthropic"}]
    assert ctx == {"identity": _IDENTITY, "anthropic_client": swapped,
                   "model": "claude-sonnet-4-5"}  # 附註 35


async def test_entry_gemini_gets_live_key(chosen):
    ctx: dict = {}
    assert await _dispatch("gemini-2.5-pro", ctx) == "stream-gemini"
    assert ctx == {
        "identity": _IDENTITY, "request_id": "rid", "t0": 1.5, "session_id": "sid",
        "gemini_api_key": "fake-gemini", "model": "gemini-2.5-pro",
    }


async def test_entry_openai_compatible_uses_registry(chosen):
    ctx: dict = {}
    assert await _dispatch("grok-4", ctx) == "stream-openai"
    assert ctx == {
        "identity": _IDENTITY, "request_id": "rid", "t0": 1.5, "session_id": "sid",
        "api_key": "fake-grok", "url": _GROK_URL, "provider": "Grok",
        "custom_connection": False, "model": "grok-4",
    }


async def test_entry_official_openai_responses_model(chosen):
    ctx: dict = {}
    assert await _dispatch("gpt-6-astra", ctx) == "stream-responses"
    assert [k for k, _ in chosen] == ["responses"]
    assert ctx == {
        "identity": _IDENTITY, "request_id": "rid", "t0": 1.5, "session_id": "sid",
        "api_key": "fake-openai", "base_url": _RESPONSES_URL, "provider": "OpenAI",
        "model": "gpt-6-astra",
    }


async def test_responses_handler_opens_streaming(monkeypatch):
    """主迴圈明確打開串流（D1）；adapter 預設（complete_once）是非串流。"""
    seen: list = []
    monkeypatch.setattr(openai_handler, "run_react_loop",
                        lambda adapter, _req, _ctx: seen.append(adapter) or "loop")
    assert openai_handler.handle_responses_request(_req("gpt-6-astra"), {}) == "loop"
    (adapter,) = seen
    assert isinstance(adapter, ResponsesAdapter)
    assert (await adapter.prepare_request(_req("gpt-6-astra"), {})).stream is True
    assert (await ResponsesAdapter().prepare_request(_req("gpt-6-astra"), {})).stream is False


async def test_entry_unknown_provider_is_model_not_found(chosen):
    with pytest.raises(ToukaException) as exc:
        await _dispatch("mystery-model", {})
    assert exc.value.code == ErrorCode.MODEL_NOT_FOUND
    assert chosen == []


# ── resolve_model_route：factory 與 complete_once 共用的純函式 ──────────────────


def _resolve(name: str) -> ModelRoute:
    return resolve_model_route(name, manager=model_manager, provider_info=get_provider_info)


@pytest.mark.parametrize(("name", "expected"), [
    ("Human", ModelRoute("human")),
    ("local-a", ModelRoute("openai", api_key="", url="http://127.0.0.1:1", provider="X",
                           custom=True)),
    ("claude-sonnet-4-5", ModelRoute("anthropic", api_key="fake-anthropic")),
    ("gemini-2.5-pro", ModelRoute("gemini", api_key="fake-gemini")),
    ("grok-4", ModelRoute("openai", api_key="fake-grok", url=_GROK_URL, provider="Grok")),
    # 3b-U4b-2：官方 OpenAI＋uses_responses_api＝Responses（常數 base URL）；其餘照舊 Chat
    ("gpt-6-astra", ModelRoute("responses", api_key="fake-openai", url=_RESPONSES_URL,
                               provider="OpenAI")),
    ("GPT-6.1-Sol-2026", ModelRoute("responses", api_key="fake-openai", url=_RESPONSES_URL,
                                    provider="OpenAI")),
    ("gpt-6-sol", ModelRoute("openai", api_key="fake-openai",
                             url="https://settings.test/v1/chat/completions", provider="OpenAI")),
    ("gpt-6-astra-mine", ModelRoute("openai", api_key="k2", url="http://127.0.0.1:2",
                                    provider="OpenAI", custom=True)),
])
def test_resolve_model_route_matrix(name, expected):
    assert _resolve(name) == expected


@pytest.mark.parametrize("name", ["claude-retired", "mystery-model"])
def test_resolve_model_route_unroutable_raises(name):
    with pytest.raises(ToukaException) as exc:
        _resolve(name)
    assert exc.value.code == ErrorCode.MODEL_NOT_FOUND


def test_resolve_model_route_uses_injected_dependencies():
    """依賴由呼叫端注入：factory 傳自己模組的符號，既有 patch 點照常生效。"""
    route = resolve_model_route(
        "grok-4", manager=model_manager,
        provider_info=lambda _m: ("http://injected.test", "k", "Injected"),
    )
    assert route == ModelRoute("openai", api_key="k", url="http://injected.test",
                               provider="Injected")


def test_responses_needs_the_official_provider_not_just_the_name():
    """名稱是 Responses 模型、但廠商不是官方 OpenAI：留在 Chat（不能只看模型名）。"""
    route = resolve_model_route(
        "gpt-6-astra", manager=model_manager,
        provider_info=lambda _m: ("http://compat.test/v1/chat/completions", "k", "LiteLLM"),
    )
    assert route == ModelRoute("openai", api_key="k",
                               url="http://compat.test/v1/chat/completions", provider="LiteLLM")

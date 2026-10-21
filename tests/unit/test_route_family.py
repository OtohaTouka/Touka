"""路由家族判定（route_family）矩陣，以及它與 dispatcher factory 的實際路由一致。

route_family 依 factory 同一順序：Human → 有效自訂連線 → 名稱含 claude／gemini →
Provider Registry；判定不出來回 None。factory 的 claude／gemini 判斷也改用同一個
``native_family``，下方一致性測試確認路由沒有改變。全程 mock handler，不打網路。
"""
from __future__ import annotations

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.dispatchers.route_family import (
    native_family,
    route_family,
    same_route_family,
)
from src.services.models.manager import ModelManager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
)

_IDENTITY = AgentIdentity(agent_id="DebugAgent")


def _resolve_custom(_self, name: str) -> CustomRouteResolution:
    if name in ("local-a", "claude-local", "deepseek-local"):
        route = CustomModelRoute(
            connection_id="conn-1", endpoint="http://127.0.0.1:1", api_key="", provider="X"
        )
        return CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM, route=route
        )
    if name == "retired-local":
        return CustomRouteResolution(disposition=CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE)
    return CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN)


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.setattr(
        ModelManager, "get_models", lambda _self: [{"id": "Human", "owned_by": "human"}]
    )
    monkeypatch.setattr(ModelManager, "resolve_custom_route", _resolve_custom)


_MATRIX = [
    ("claude-opus-4-8", "anthropic"),
    ("Claude-Sonnet-4-6", "anthropic"),
    ("gemini-2.5-pro", "gemini"),
    ("claude-gemini-hybrid", "anthropic"),
    ("grok-4", "grok"),
    ("deepseek-chat", "deepseek"),
    ("gpt-4o", "openai"),
    ("ollama-llama3.1", "ollama"),
    ("litellm-proxy-model", "litellm"),
    ("local-a", "custom:conn-1"),
    ("claude-local", "custom:conn-1"),
    ("deepseek-local", "custom:conn-1"),  # 自訂連線先於 registry 關鍵字
    ("retired-local", None),
    ("Human", None),
    ("mystery-model", None),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize(("name", "expected"), _MATRIX)
def test_route_family_matrix(name, expected):
    """自訂連線先於名稱推測；Human／停用自訂／無法判定 → None。"""
    assert route_family(name) == expected


def test_same_route_family_requires_known_family():
    """兩邊都判定不出家族時不算同一家（不會因 None == None 而允許備援）。"""
    assert same_route_family("claude-opus-4-8", "claude-haiku-4-5")
    assert not same_route_family("mystery-a", "mystery-b")
    assert not same_route_family("claude-opus-4-8", "gemini-2.5-pro")


def test_native_family_is_name_only():
    """native_family 只看名稱，不查自訂連線（factory 在自訂路由之後才呼叫它）。"""
    assert native_family("claude-local") == "anthropic"
    assert native_family("grok-4") is None


async def _noop_stream(_req, _ctx):  # pragma: no cover - 僅為可呼叫的 async generator 佔位
    if False:
        yield ""


@pytest.mark.parametrize(
    ("name", "handler", "family"),
    [
        ("claude-3-5-sonnet", "claude", "anthropic"),
        ("gemini-2.5-pro", "gemini", "gemini"),
        ("grok-4", "openai", "grok"),
        ("deepseek-chat", "openai", "deepseek"),
        ("local-a", "openai", "custom:conn-1"),
        ("claude-local", "openai", "custom:conn-1"),
    ],
)
async def test_factory_route_matches_route_family(monkeypatch, name, handler, family):
    """factory 實際選的 handler 與 route_family 的家族一致（路由不因改用 native_family 而變）。"""
    chosen: list[str] = []
    for key in ("claude", "gemini", "openai"):
        monkeypatch.setattr(
            factory,
            f"handle_{key}_request",
            lambda req, ctx, _key=key: chosen.append(_key) or _noop_stream(req, ctx),
        )
    req = OpenAIChatRequest(model=name, messages=[OpenAIMessage(role="user", content="hi")])
    await factory.get_stream_generator(name, req, _IDENTITY, "rid", 0.0, {})
    assert chosen == [handler]
    assert route_family(name) == family

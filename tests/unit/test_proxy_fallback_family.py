"""主要模型失敗時的備援只限同一家（跨廠商備援關閉）。

`process_proxy_request` 在主要模型開始回覆前失敗時，只有「上次能用的模型」與主要模型
屬於同一條路由家族（同一廠商，或同一條自訂連線）才改用它；別家、無法判定家族、Human
都直接把原錯誤丟回。全程 mock pipeline 與 dispatch，不打網路、不寫 payload 日誌。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import proxy_service
from src.services.models.manager import ModelManager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
)

_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_CUSTOM_ROUTES = {
    "local-a": "conn-1",
    "local-b": "conn-1",
    "local-c": "conn-2",
    "claude-local": "conn-1",
    "deepseek-local": "conn-1",
}


class _Req:
    def __init__(self, model: str) -> None:
        self.model = model

    def model_dump(self, **_kwargs) -> dict:
        return {"model": self.model}


def _resolve_custom(_self, name: str) -> CustomRouteResolution:
    connection_id = _CUSTOM_ROUTES.get(name)
    if connection_id is None:
        return CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN)
    route = CustomModelRoute(
        connection_id=connection_id, endpoint="http://127.0.0.1:1", api_key="", provider="X"
    )
    return CustomRouteResolution(disposition=CustomRouteDisposition.ACTIVE_CUSTOM, route=route)


@pytest.fixture
def dispatch(monkeypatch):
    mock = AsyncMock(side_effect=[RuntimeError("primary down"), "fallback-response"])
    monkeypatch.setattr(proxy_service, "dispatch_request", mock)
    monkeypatch.setattr(proxy_service, "pipeline", SimpleNamespace(execute=AsyncMock()))
    monkeypatch.setattr(proxy_service, "log_payload", MagicMock())
    monkeypatch.setattr(proxy_service, "record_working_brain", MagicMock())
    monkeypatch.setattr(
        ModelManager, "get_models", lambda _self: [{"id": "Human", "owned_by": "human"}]
    )
    monkeypatch.setattr(ModelManager, "resolve_custom_route", _resolve_custom)
    return mock


async def _run(monkeypatch, primary: str, last_working: str):
    monkeypatch.setattr(proxy_service, "get_last_working_brain", lambda _identity: last_working)
    ctx = PipelineContext(request_id="rid", t0=0.0, identity=_IDENTITY, is_toukasoul=True)
    return await proxy_service.process_proxy_request(_Req(primary), ctx, is_anthropic=False)


@pytest.mark.parametrize(
    ("primary", "last_working"),
    [
        ("claude-opus-4-8", "claude-sonnet-4-6"),
        ("Gemini-2.5-Pro", "gemini-2.5-flash"),
        ("grok-4", "grok-3-mini"),
        ("local-a", "local-b"),
    ],
)
async def test_same_family_fallback_still_used(monkeypatch, dispatch, primary, last_working):
    """同一家（或同一條自訂連線）→ 照舊改用上次能用的模型。"""
    assert await _run(monkeypatch, primary, last_working) == "fallback-response"
    assert dispatch.await_count == 2
    assert dispatch.await_args_list[1].args[0] == last_working


@pytest.mark.parametrize(
    ("primary", "last_working"),
    [
        ("claude-opus-4-8", "gemini-2.5-pro"),
        ("gemini-2.5-pro", "deepseek-chat"),
        ("grok-4", "deepseek-chat"),
        ("local-a", "local-c"),
        ("claude-local", "claude-opus-4-8"),
        ("deepseek-local", "deepseek-chat"),
        ("mystery-a", "mystery-b"),
        ("mystery-a", "claude-opus-4-8"),
        ("Human", "claude-opus-4-8"),
    ],
)
async def test_cross_family_fallback_is_off(monkeypatch, dispatch, caplog, primary, last_working):
    """別家、無法判定家族或 Human → 不改用，原錯誤直接丟回並寫明備援已關閉。"""
    caplog.set_level(logging.WARNING, logger=proxy_service.logger.name)
    with pytest.raises(RuntimeError, match="primary down"):
        await _run(monkeypatch, primary, last_working)
    assert dispatch.await_count == 1
    assert "跨廠商備援已關閉" in caplog.text

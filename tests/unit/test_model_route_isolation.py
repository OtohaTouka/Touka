"""TDD：控制面／模型上游隔離，以及模型路由與憑證的原子性。"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from src.adapters.schemas import (
    AnthropicMessage,
    AnthropicRequest,
    OpenAIChatRequest,
    OpenAIMessage,
)
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.models import fetcher
from src.services.models.manager import ModelManager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
)

_IDENTITY = AgentIdentity.for_role(
    agent_id="DebugAgent",
    role="admin",
    is_human=True,
    capabilities=[],
)


def _make_manager(cache: list | None = None) -> ModelManager:
    """建立不碰實體設定檔的乾淨 ModelManager。"""
    with (
        patch.object(ModelManager, "_load_blacklist"),
        patch.object(ModelManager, "load_initial_config"),
    ):
        manager = ModelManager()
    manager._models_cache = list(cache or [])
    manager._blacklist = []
    return manager


async def _noop_stream():
    if False:  # pragma: no cover - 僅作 async generator 佔位
        yield ""


def _openai_request(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model=model,
        messages=[OpenAIMessage(role="user", content="hi")],
    )


def _connection(
    *,
    url: str = "https://old.example.test/v1",
    api_key: str = "old-secret",
    enabled: bool = True,
) -> dict:
    return {
        "id": "custom-one",
        "url": url,
        "apiKey": api_key,
        "provider": "custom",
        "enabled": enabled,
    }


@pytest.mark.asyncio
async def test_control_plane_proxy_is_not_scanned_as_model_upstream():
    """Touka Proxy 是 Chat 控制面，不得被目前的 Proxy 當成模型上游掃描。"""
    manager = _make_manager()
    connections = {
        "connections": [
            {
                "id": "local-default",
                "url": "http://localhost:8000",
                "apiKey": "",
                "provider": "proxy",
                "enabled": True,
            },
            {
                "id": "local-ollama",
                "url": "http://localhost:11434",
                "apiKey": "",
                "provider": "ollama",
                "enabled": True,
            },
        ]
    }

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=[
                {"id": "ollama/llama3", "object": "model", "owned_by": "ollama"}
            ],
        ) as fetch_models,
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = connections
        await manager.fetch_custom_connections_models()

    fetch_models.assert_awaited_once()
    assert fetch_models.await_args.kwargs["provider"] == "ollama"


@pytest.mark.asyncio
async def test_custom_route_keeps_endpoint_and_empty_key_atomic(monkeypatch):
    """自訂 URL 配空 key 時，不得偷接正式 provider key。"""
    monkeypatch.setattr(
        ModelManager,
        "get_models",
        lambda self: [
            {
                "id": "grok-4.5",
                "object": "model",
                "owned_by": "custom",
            }
        ],
    )
    monkeypatch.setattr(
        ModelManager,
        "resolve_custom_route",
        lambda self, _model: CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM,
            route=CustomModelRoute(
                connection_id="custom-one",
                endpoint="https://custom.example.test/v1/chat/completions",
                api_key="",
                provider="custom",
            ),
        ),
    )
    monkeypatch.setattr(
        factory,
        "get_provider_info",
        lambda _model: (
            "https://api.x.ai/v1/chat/completions",
            "native-provider-secret",
            "Grok",
        ),
    )
    captured: dict = {}

    def capture_handler(_request, context):
        captured.update(context)
        return _noop_stream()

    monkeypatch.setattr(factory, "handle_openai_request", capture_handler)

    await factory.get_stream_generator(
        "grok-4.5",
        _openai_request("grok-4.5"),
        _IDENTITY,
        "rid",
        0.0,
        {},
    )

    assert captured["url"] == "https://custom.example.test/v1/chat/completions"
    assert captured["api_key"] == ""
    assert captured["custom_connection"] is True  # 名稱含 grok 仍是自訂連線：不串流（3b 裁定 5）


@pytest.mark.asyncio
async def test_explicit_custom_route_precedes_model_name_inference(monkeypatch):
    """自訂連線模型不應因名稱不含官方關鍵字而被錯判 MODEL_NOT_FOUND。"""
    monkeypatch.setattr(
        ModelManager,
        "get_models",
        lambda self: [
            {
                "id": "mistral-small",
                "object": "model",
                "owned_by": "custom",
            }
        ],
    )
    monkeypatch.setattr(
        ModelManager,
        "resolve_custom_route",
        lambda self, _model: CustomRouteResolution(
            disposition=CustomRouteDisposition.ACTIVE_CUSTOM,
            route=CustomModelRoute(
                connection_id="custom-one",
                endpoint="https://custom.example.test/v1/chat/completions",
                api_key="custom-secret",
                provider="custom",
            ),
        ),
    )
    monkeypatch.setattr(
        factory,
        "get_provider_info",
        lambda _model: pytest.fail("明確自訂路由不得再進行名稱推測"),
    )
    captured: dict = {}

    def capture_handler(_request, context):
        captured.update(context)
        return _noop_stream()

    monkeypatch.setattr(factory, "handle_openai_request", capture_handler)

    await factory.get_stream_generator(
        "mistral-small",
        _openai_request("mistral-small"),
        _IDENTITY,
        "rid",
        0.0,
        {},
    )

    assert captured["url"] == "https://custom.example.test/v1/chat/completions"
    assert captured["api_key"] == "custom-secret"


@pytest.mark.asyncio
async def test_custom_route_secret_is_not_stored_in_model_descriptor():
    """URL／key 必須只存在路由 SSOT，不得混進模型 descriptor。"""
    manager = _make_manager()
    connections = {
        "connections": [
            {
                "id": "custom-one",
                "url": "https://custom.example.test/v1",
                "apiKey": "custom-secret",
                "provider": "custom",
                "enabled": True,
            }
        ]
    }
    discovered = [
        {
            "id": "mistral-small",
            "object": "model",
            "owned_by": "custom",
            "_source_url": "https://custom.example.test/v1",
            "_api_key": "custom-secret",
        }
    ]

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=discovered,
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = connections
        await manager.fetch_custom_connections_models()

    model = next(m for m in manager._models_cache if m["id"] == "mistral-small")
    assert "_source_url" not in model
    assert "_api_key" not in model
    route = manager.get_custom_route("mistral-small")
    assert route is not None
    assert route.connection_id == "custom-one"
    assert route.api_key == "custom-secret"


@pytest.mark.asyncio
async def test_custom_route_refresh_atomically_drops_stale_routes():
    """刷新後不存在的來源不得繼續留在執行期路由表。"""
    manager = _make_manager()
    custom = {
        "connections": [
            {
                "id": "custom-one",
                "url": "https://custom.example.test/v1",
                "apiKey": "secret",
                "provider": "custom",
                "enabled": True,
            }
        ]
    }
    control_plane_only = {
        "connections": [
            {
                "id": "local-default",
                "url": "http://localhost:8000",
                "apiKey": "",
                "provider": "proxy",
                "enabled": True,
            }
        ]
    }

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=[
                {"id": "mistral-small", "object": "model", "owned_by": "custom"}
            ],
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = custom
        await manager.fetch_custom_connections_models()
        assert manager.get_custom_route("mistral-small") is not None

        config.read_config_json.return_value = control_plane_only
        await manager.fetch_custom_connections_models()

    assert manager.get_custom_route("mistral-small") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "expected_endpoint", "expected_key"),
    [
        (
            _connection(url="https://new.example.test/v1", api_key="new-secret"),
            "https://new.example.test/v1/chat/completions",
            "new-secret",
        ),
        (
            _connection(api_key="rotated-secret"),
            "https://old.example.test/v1/chat/completions",
            "rotated-secret",
        ),
        (_connection(enabled=False), None, None),
    ],
    ids=("endpoint-change", "key-rotation", "disable"),
)
async def test_stale_refresh_never_republishes_obsolete_route(
    replacement,
    expected_endpoint,
    expected_key,
):
    """舊 discovery 完成時若設定已變，連短暫發布舊 endpoint/key 都不允許。"""
    manager = _make_manager()
    current = {"connections": [_connection()]}
    old_started = asyncio.Event()
    old_release = asyncio.Event()
    latest_started = asyncio.Event()
    latest_release = asyncio.Event()

    async def controlled_fetch(*, base_url, api_key, provider, blacklist_checker):
        del provider, blacklist_checker
        if base_url == "https://old.example.test/v1" and api_key == "old-secret":
            old_started.set()
            await old_release.wait()
        else:
            latest_started.set()
            await latest_release.wait()
        return [{"id": "claude-custom", "object": "model", "owned_by": "custom"}]

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            side_effect=controlled_fetch,
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.side_effect = lambda _filename: deepcopy(current)

        stale_task = asyncio.create_task(manager.fetch_custom_connections_models())
        await old_started.wait()

        current["connections"] = [replacement]
        latest_task = asyncio.create_task(manager.fetch_custom_connections_models())
        old_release.set()

        if replacement["enabled"]:
            await latest_started.wait()
            assert manager.get_custom_route("claude-custom") is None
            latest_release.set()

        await asyncio.gather(stale_task, latest_task)

    route = manager.get_custom_route("claude-custom")
    if expected_endpoint is None:
        assert route is None
    else:
        assert route is not None
        assert route.endpoint == expected_endpoint
        assert route.api_key == expected_key


@pytest.mark.asyncio
async def test_known_custom_route_becomes_fail_closed_after_disable(monkeypatch):
    """曾屬 custom 的 claude 名稱失去 active route 後，不得落入原生 Claude 推測。"""
    manager = _make_manager()
    enabled = {"connections": [_connection()]}
    disabled = {"connections": [_connection(enabled=False)]}

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=[
                {"id": "claude-custom", "object": "model", "owned_by": "custom"}
            ],
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = enabled
        await manager.fetch_custom_connections_models()
        config.read_config_json.return_value = disabled
        await manager.fetch_custom_connections_models()

    monkeypatch.setattr(factory, "model_manager", manager)
    claude_handler = Mock(return_value=_noop_stream())
    monkeypatch.setattr(factory, "handle_claude_request", claude_handler)

    with pytest.raises(ToukaException) as exc_info:
        await factory.get_stream_generator(
            "claude-custom",
            _openai_request("claude-custom"),
            _IDENTITY,
            "rid",
            0.0,
            {},
        )

    assert exc_info.value.code == ErrorCode.MODEL_NOT_FOUND
    claude_handler.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_custom_model_route_does_not_use_last_writer():
    """兩個上游宣告相同 model ID 時保留第一個，並對後者 fail-loud。"""
    manager = _make_manager()
    connections = {
        "connections": [
            {
                "id": "first",
                "url": "https://first.example.test/v1",
                "apiKey": "first-secret",
                "provider": "custom",
                "enabled": True,
            },
            {
                "id": "second",
                "url": "https://second.example.test/v1",
                "apiKey": "second-secret",
                "provider": "custom",
                "enabled": True,
            },
        ]
    }

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=[
                {"id": "same-model", "object": "model", "owned_by": "custom"}
            ],
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = connections
        await manager.fetch_custom_connections_models()

    route = manager.get_custom_route("same-model")
    assert route is not None
    assert route.connection_id == "first"
    assert route.api_key == "first-secret"
    errors = manager.get_connection_errors()
    assert set(errors.values()) == {"route_conflict"}
    assert "second" not in repr(errors)
    assert "second-secret" not in repr(errors)


@pytest.mark.asyncio
async def test_duplicate_route_diagnostic_uses_only_opaque_identifiers():
    """衝突診斷不得保存原始 connection/model ID 或 secret-shaped 片段。"""
    hostile_connection_id = "second?token=sk-connection-leak"
    hostile_model_id = "claude-sk-model-leak"
    manager = _make_manager()
    connections = {
        "connections": [
            _connection(),
            {
                **_connection(
                    url="https://second.example.test/v1",
                    api_key="second-secret",
                ),
                "id": hostile_connection_id,
            },
        ]
    }

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            return_value=[
                {"id": hostile_model_id, "object": "model", "owned_by": "custom"}
            ],
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = connections
        await manager.fetch_custom_connections_models()

    serialized = repr(manager.get_connection_errors())
    assert hostile_connection_id not in serialized
    assert hostile_model_id not in serialized
    assert "sk-connection-leak" not in serialized
    assert "sk-model-leak" not in serialized


@pytest.mark.asyncio
async def test_model_fetch_logs_only_sanitized_origin(monkeypatch, caplog):
    """fetch attempt／failure log 不得含 userinfo、query 或完整 credential-bearing URL。"""

    class FailedResponse:
        status_code = 503

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FailedResponse()

    raw_url = "https://alice:password-leak@models.example.test/root?token=sk-url-leak"
    monkeypatch.setattr(fetcher, "validate_outbound_url", lambda _url: None)
    monkeypatch.setattr(fetcher.httpx, "AsyncClient", FakeAsyncClient)

    with caplog.at_level(logging.DEBUG, logger=fetcher.__name__):
        await fetcher.fetch_openai_compatible_models(
            base_url=raw_url,
            api_key="",
            provider="custom",
            blacklist_checker=lambda _model_id: False,
        )

    assert "password-leak" not in caplog.text
    assert "sk-url-leak" not in caplog.text
    assert "alice:" not in caplog.text
    assert "models.example.test" in caplog.text


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: OpenAIChatRequest(
            model="grok-4.5",
            messages=[OpenAIMessage(role="user", content="hi")],
            base_url="https://attacker.example.test/v1",
        ),
        lambda: AnthropicRequest(
            model="claude-sonnet",
            messages=[AnthropicMessage(role="user", content="hi")],
            base_url="https://attacker.example.test/v1",
        ),
    ],
)
def test_public_chat_schema_rejects_non_null_base_url(request_factory):
    """公開 chat payload 不得以 base_url 注入上游端點。"""
    with pytest.raises(ValidationError):
        request_factory()


# --- 隔離封鎖埠的上游來源（iso-c2 C2-2 U2e2）------------------------------------------


@pytest.mark.asyncio
async def test_route_endpoints_list_every_configured_connection_not_only_active_routes():
    """探測失敗、停用的連線也要在：封鎖埠不能只看探測成功的路由。"""
    manager = _make_manager()
    ok, failing, disabled = (
        "http://localhost:4100/v1",
        "http://localhost:4200/v1",
        "http://192.168.1.5:4300/v1",
    )
    connections = {
        "connections": [
            {**_connection(url=ok), "id": "ok"},
            {**_connection(url=failing), "id": "failing"},
            {**_connection(url=disabled, enabled=False), "id": "off"},
        ]
    }

    async def discover(*, base_url: str, **_kwargs: object) -> list[dict]:
        if base_url == failing:
            raise ConnectionError
        return [{"id": "local-model", "object": "model", "owned_by": "custom"}]

    with (
        patch("src.services.models.manager.config_utils") as config,
        patch(
            "src.services.models.manager.fetch_openai_compatible_models",
            new_callable=AsyncMock,
            side_effect=discover,
        ),
        patch.object(manager, "_save_config"),
    ):
        config.config_exists.return_value = True
        config.read_config_json.return_value = connections
        await manager.fetch_custom_connections_models()

    assert manager.get_custom_route("local-model") is not None  # 只有 ok 探測成功
    assert {ok, failing, disabled} <= manager.route_endpoints()


def test_route_endpoints_include_saved_connections_from_startup():
    """開機後第一次探測還沒跑完：設定裡已有的連線也要在。"""
    saved = {"connections": [_connection(url="http://localhost:4100/v1")]}
    with patch("src.services.models.manager.config_utils") as config:
        config.config_exists.return_value = True
        config.read_config_json.return_value = saved
        manager = _make_manager()
    assert manager.route_endpoints() == {"http://localhost:4100/v1"}


def test_route_endpoints_returns_a_read_only_copy():
    """唯讀：回傳不可變的副本；之後存了別的設定，先前拿到的值不變。"""
    manager = _make_manager()
    manager.invalidate_custom_routes([_connection(url="http://localhost:4100/v1")])
    first = manager.route_endpoints()
    assert isinstance(first, frozenset)
    manager.invalidate_custom_routes([_connection(url="http://localhost:4200/v1")])
    assert first == {"http://localhost:4100/v1"}
    assert manager.route_endpoints() == {"http://localhost:4200/v1"}

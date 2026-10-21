"""P8-A OAuth API contract and callback secret-surface tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.config import OAUTH_CALLBACK_MAX_BODY_BYTES
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.mcp_oauth import (
    McpOAuthCallbackOutcome,
    McpOAuthDisconnectResponse,
    McpOAuthStartResponse,
    McpOAuthStatus,
    McpOAuthStatusResponse,
)
from src.protocols.mcp.oauth.models import (
    OAuthKeyRotationPhase,
    OAuthKeyRotationRecord,
)
from src.protocols.mcp.oauth.protocol import OAuthCallbackPolicy
from src.protocols.mcp.oauth.service import OAuthClockRollbackError, OAuthStateInvalidError
from src.routers.mcp_oauth import (
    _parse_callback,
    get_mcp_oauth_runtime,
    router,
)
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from src.protocols.mcp.oauth.profile import OAuthProfile
from starlette.requests import Request

OWNER = AgentIdentity.for_role(
    "soul-a",
    "Owner",
    owned_agents=[],
)
ADMIN = AgentIdentity(
    agent_id="human-admin",
    human_role="admin",
    is_human=True,
)


@dataclass
class FakeRuntime:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    callback_error: Exception | None = None
    callback_response_mode: str = "form_post"

    def get_callback_policy(self, redirect_route: str) -> OAuthCallbackPolicy:
        return OAuthCallbackPolicy(
            route=redirect_route,
            redirect_uri=f"https://proxy.example{redirect_route}",
            response_mode=self.callback_response_mode,
        )

    async def start_authorization(self, **kwargs: Any) -> McpOAuthStartResponse:
        self.calls.append(("start", kwargs))
        return McpOAuthStartResponse(
            operation_id="op-safe",
            authorization_url="https://issuer.example/authorize?opaque=yes",
            expires_at=1234.0,
        )

    async def get_status(self, **kwargs: Any) -> McpOAuthStatusResponse:
        self.calls.append(("status", kwargs))
        return McpOAuthStatusResponse(
            status=McpOAuthStatus.CONNECTED,
            operation_id=None,
            expires_at=5678.0,
        )

    async def disconnect(self, **kwargs: Any) -> McpOAuthDisconnectResponse:
        self.calls.append(("disconnect", kwargs))
        return McpOAuthDisconnectResponse(status=McpOAuthStatus.AUTH_REQUIRED)

    async def complete_callback(self, **kwargs: Any) -> McpOAuthCallbackOutcome:
        self.calls.append(("callback", kwargs))
        if self.callback_error is not None:
            raise self.callback_error
        return McpOAuthCallbackOutcome(
            operation_id="op-safe",
            status=McpOAuthStatus.CONNECTED,
            client_origin="https://chat.example",
        )

    async def list_profiles(self) -> list[OAuthProfile]:
        self.calls.append(("list_profiles", {}))
        return []

    async def put_profile(self, profile: OAuthProfile) -> OAuthProfile:
        self.calls.append(("put_profile", {"profile": profile}))
        return profile

    async def delete_profile(self, profile_id: str) -> None:
        self.calls.append(("delete_profile", {"profile_id": profile_id}))


@dataclass
class FakeRotation:
    starts: int = 0

    async def status(self) -> OAuthKeyRotationRecord:
        return OAuthKeyRotationRecord(
            phase=OAuthKeyRotationPhase.STABLE,
            current_kid="current-kid",
            epoch=1,
        )

    async def start(self, *, now: float) -> OAuthKeyRotationRecord:
        assert now > 0
        self.starts += 1
        return OAuthKeyRotationRecord(
            phase=OAuthKeyRotationPhase.DUAL_READ_NEW_WRITE,
            current_kid="new-kid",
            retiring_kid="current-kid",
            epoch=2,
            started_at=now,
            state_drain_after=now + 600,
        )


@pytest.fixture
def api() -> tuple[TestClient, FakeRuntime, FastAPI]:
    runtime = FakeRuntime()
    rotation = FakeRotation()
    app = FastAPI()
    app.include_router(router)
    app.include_router(router, prefix="/v1")

    @app.middleware("http")
    async def bind_human_principal(request: Any, call_next: Any) -> Any:
        request.state.human_principal_id = "human-a"
        return await call_next(request)

    app.state.context = {
        "mcp_oauth_runtime": runtime,
        "protocol_manager": _FakeProtocolManager(),
        "mcp_oauth_rotation": rotation,
    }
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    app.dependency_overrides[get_mcp_oauth_runtime] = lambda: runtime
    with TestClient(app) as client:
        yield client, runtime, app


class _FakeRegistry:
    @staticmethod
    def get_server(name: str) -> object | None:
        return object() if name == "remote-a" else None


class _FakeProtocolManager:
    registry = _FakeRegistry()

    @staticmethod
    def get_installation_generation(name: str) -> int:
        assert name == "remote-a"
        return 7


def test_start_status_disconnect_use_verified_slot_facts(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, _app = api
    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", lambda _request: None)
    monkeypatch.setattr(
        "src.routers.mcp_oauth.permission_service.is_mcp_server_allowed",
        lambda _server, _identity: True,
    )

    start = client.post(
        "/mcp/servers/remote-a/oauth/start",
        headers={"X-Agent-ID": "soul-a"},
        json={"authorization_server": "https://issuer.example"},
    )
    status = client.get(
        "/v1/mcp/servers/remote-a/oauth/status",
        headers={"X-Agent-ID": "soul-a"},
    )
    disconnected = client.delete(
        "/mcp/servers/remote-a/oauth",
        headers={"X-Agent-ID": "soul-a"},
    )

    assert start.status_code == 200
    assert status.json()["status"] == "connected"
    assert disconnected.json()["status"] == "auth_required"
    assert [name for name, _kwargs in runtime.calls] == [
        "start",
        "status",
        "disconnect",
    ]
    for _name, kwargs in runtime.calls:
        context = kwargs["request_context"]
        assert context.identity.agent_id == "soul-a"
        assert context.human_principal_id == "human-a"
        assert kwargs["installation_generation"] == 7


def test_status_contract_can_project_safe_authorization_server_selection() -> None:
    response = McpOAuthStatusResponse(
        status=McpOAuthStatus.SELECTION_REQUIRED,
        authorization_servers=[
            "https://issuer-a.example",
            "https://issuer-b.example",
        ],
    )

    assert response.model_dump(mode="json") == {
        "status": "selection_required",
        "operation_id": None,
        "expires_at": None,
        "authorization_servers": [
            "https://issuer-a.example",
            "https://issuer-b.example",
        ],
    }


def test_unknown_or_ungranted_server_is_rejected_before_runtime(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, _app = api
    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", lambda _request: None)
    monkeypatch.setattr(
        "src.routers.mcp_oauth.permission_service.is_mcp_server_allowed",
        lambda _server, _identity: False,
    )
    assert client.get("/mcp/servers/missing/oauth/status").status_code == 422
    assert (
        client.post(
            "/mcp/servers/remote-a/oauth/start",
            json={},
        ).status_code
        == 403
    )
    assert runtime.calls == []


def test_mutations_enforce_origin_but_status_does_not(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _runtime, _app = api
    monkeypatch.setattr(
        "src.routers.mcp_oauth.permission_service.is_mcp_server_allowed",
        lambda _server, _identity: True,
    )

    def reject(_request: object) -> None:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", reject)
    assert client.post("/mcp/servers/remote-a/oauth/start", json={}).status_code == 403
    assert client.delete("/mcp/servers/remote-a/oauth").status_code == 403
    assert client.get("/mcp/servers/remote-a/oauth/status").status_code == 200


def test_callback_is_public_exact_method_and_scrubs_current_history_entry(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    response = client.post(
        "/mcp/oauth/callback",
        content="state=raw-state-secret&code=raw-code-secret",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "history.replaceState" in response.text
    assert "raw-state-secret" not in response.text
    assert "raw-code-secret" not in response.text
    assert "op-safe" in response.text
    assert runtime.calls[-1][0] == "callback"
    assert runtime.calls[-1][1]["redirect_route"] == "/mcp/oauth/callback"


def test_callback_rejects_wrong_content_type_and_oversized_body_before_runtime(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    wrong_type = client.post(
        "/mcp/oauth/callback",
        content="state=x&code=y",
        headers={"content-type": "application/json"},
    )
    oversized = client.post(
        "/mcp/oauth/callback",
        content=b"x" * 20_000,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert wrong_type.status_code == 415
    assert oversized.status_code == 413
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_callback_stream_stops_reading_immediately_after_body_limit() -> None:
    runtime = FakeRuntime()
    calls = 0

    async def receive() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b"x" * OAUTH_CALLBACK_MAX_BODY_BYTES,
                "more_body": True,
            }
        if calls == 2:
            return {
                "type": "http.request",
                "body": b"x",
                "more_body": True,
            }
        message = "超過上限後仍繼續讀取 callback body"
        raise AssertionError(message)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/oauth/callback",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        receive,
    )

    with pytest.raises(ToukaException) as captured:
        await _parse_callback(request, runtime)

    assert captured.value.code == ErrorCode.MCP_OAUTH_CALLBACK_TOO_LARGE
    assert calls == 2


def test_callback_ambiguous_semantics_has_distinct_sanitized_error_code(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    response = client.post(
        "/mcp/oauth/callback",
        content="state=x&code=y&error=denied",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MCP_OAUTH_CALLBACK_AMBIGUOUS"
    assert runtime.calls == []


def test_callback_domain_failure_maps_to_sanitized_error_code(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    runtime.callback_error = OAuthStateInvalidError("raw-state-secret")
    response = client.post(
        "/mcp/oauth/callback",
        content="state=raw-state-secret&code=raw-code-secret",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MCP_OAUTH_STATE_INVALID"
    assert "raw-state-secret" not in response.text
    assert "raw-code-secret" not in response.text


def test_clock_rollback_failure_maps_to_sanitized_unavailable_error(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    runtime.callback_error = OAuthClockRollbackError()
    response = client.post(
        "/mcp/oauth/callback",
        content="state=raw-state-secret&code=raw-code-secret",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MCP_OAUTH_CLOCK_ROLLBACK"
    assert "raw-state-secret" not in response.text


def test_query_callback_failure_scrubs_url_history_and_returns_secret_free_html(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    runtime.callback_response_mode = "query"
    runtime.callback_error = OAuthStateInvalidError("raw-state-secret")

    response = client.get("/mcp/oauth/callback?state=raw-state-secret&code=raw-code-secret")

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "history.replaceState" in response.text
    assert "postMessage" not in response.text
    assert "raw-state-secret" not in response.text
    assert "raw-code-secret" not in response.text


def test_query_callback_parse_failure_also_returns_history_scrubbing_html(
    api: tuple[TestClient, FakeRuntime, FastAPI],
) -> None:
    client, runtime, _app = api
    runtime.callback_response_mode = "query"

    response = client.get(
        "/mcp/oauth/callback?state=raw-state-secret&code=raw-code-secret&error=denied"
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "history.replaceState" in response.text
    assert "raw-state-secret" not in response.text
    assert "raw-code-secret" not in response.text
    assert runtime.calls == []


def test_query_callback_without_runtime_still_returns_history_scrubbing_html() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.context = {}

    with TestClient(app) as client:
        response = client.get("/mcp/oauth/callback?state=raw-state-secret&code=raw-code-secret")

    assert response.status_code >= 400
    assert response.headers["content-type"].startswith("text/html")
    assert "history.replaceState" in response.text
    assert "raw-state-secret" not in response.text
    assert "raw-code-secret" not in response.text


def test_profile_mutation_requires_human_admin_and_origin(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, app = api
    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", lambda _request: None)
    profile = {
        "profile_id": "profile-a",
        "generation": 1,
        "enabled": True,
        "callback_binding": "form_post",
        "trusted_public_origin": "https://chat.example",
        "trusted_issuers": ["https://issuer.example"],
        "client_id": "client-a",
        "scope_rules": [],
        "dcr_enabled": False,
    }
    assert client.put("/admin/mcp/oauth/profiles/profile-a", json=profile).status_code == 403

    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    response = client.put("/admin/mcp/oauth/profiles/profile-a", json=profile)
    assert response.status_code == 200
    assert response.json()["profile_id"] == "profile-a"
    assert runtime.calls[-1][0] == "put_profile"


def test_profile_path_id_must_match_payload(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime, app = api
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", lambda _request: None)
    response = client.put(
        "/admin/mcp/oauth/profiles/profile-a",
        json={
            "profile_id": "profile-b",
            "generation": 1,
            "enabled": False,
            "callback_binding": "form_post",
            "trusted_public_origin": "https://chat.example",
            "trusted_issuers": ["https://issuer.example"],
            "client_id": "client-a",
        },
    )
    assert response.status_code == 422
    assert runtime.calls == []


def test_key_rotation_api_requires_human_admin_and_projects_no_key_material(
    api: tuple[TestClient, FakeRuntime, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _runtime, app = api
    monkeypatch.setattr("src.routers.mcp_oauth.verify_origin", lambda _request: None)

    assert client.get("/admin/mcp/oauth/keys/rotation").status_code == 403
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    status = client.get("/admin/mcp/oauth/keys/rotation")
    started = client.post("/admin/mcp/oauth/keys/rotation")

    assert status.status_code == 200
    assert status.json() == {
        "phase": "stable",
        "current_kid": "current-kid",
        "retiring_kid": None,
        "epoch": 1,
        "started_at": None,
        "state_drain_after": None,
    }
    assert started.status_code == 200
    assert started.json()["phase"] == "dual-read-new-write"
    assert "material" not in started.text

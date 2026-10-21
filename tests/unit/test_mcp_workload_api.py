"""P8-B API gates, prefix parity, and write-only secret schema。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.models.identity import AgentIdentity
from src.models.mcp_oauth_workload import WorkloadCredentialStatus
from src.protocols.mcp.oauth.workload.control import WorkloadControlDenied
from src.routers.mcp_oauth_workload import (
    get_mcp_workload_oauth_control,
    get_mcp_workload_oauth_rotation,
    router,
)
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity(
    agent_id="human-admin",
    human_role="admin",
    is_human=True,
)
OWNER = AgentIdentity(
    agent_id="Alice",
    agent_role="Owner",
    is_human=False,
)


@dataclass
class _Control:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def list_profiles(self) -> list[Any]:
        return []

    async def set_credential(self, **kwargs: Any) -> WorkloadCredentialStatus:
        self.calls.append(("set_credential", kwargs))
        return WorkloadCredentialStatus(
            profile_id=kwargs["profile_id"],
            credential_id="credential-a",
            generation=2,
            status="active",
        )

    async def list_delegations(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("list_delegations", kwargs))
        if kwargs["owner_user_id"] != "owner-1":
            raise WorkloadControlDenied()
        return []

    async def refresh_metadata(self, **kwargs: Any) -> Any:
        self.calls.append(("refresh_metadata", kwargs))
        return {
            "profile_id": kwargs["profile_id"],
            "profile_generation": 3,
            "verified_at": 150.0,
            "status": "active",
        }

    async def put_delegation(self, **kwargs: Any) -> Any:
        self.calls.append(("put_delegation", kwargs))
        return {
            "delegation_id": kwargs["delegation_id"],
            "owner_user_id": kwargs["owner_user_id"],
            "source_slot": {
                "human_principal_id": kwargs["owner_user_id"],
                "execution_agent_id": kwargs["execution_agent_id"],
                "installation_id": kwargs["installation_id"],
                "installation_generation": kwargs["installation_generation"],
                "auth_profile_id": "profile-a",
            },
            "source_profile_generation": 3,
            "source_grant_id": "grant-a",
            "source_grant_version": 2,
            "execution_agent_id": kwargs["execution_agent_id"],
            "installation_id": kwargs["installation_id"],
            "installation_generation": kwargs["installation_generation"],
            "raw_tool_name": kwargs["raw_tool_name"],
            "required_scopes": kwargs["required_scopes"],
            "generation": 1,
            "status": "active",
            "issued_at": 100.0,
            "expires_at": kwargs["expires_at"],
        }

    async def retry_recovery(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("retry_recovery", kwargs))
        return []

    async def reconcile_transition(self, **kwargs: Any) -> Any:
        self.calls.append(("reconcile_transition", kwargs))
        return {"status": "completed", "generation": 1}


@dataclass
class _Rotation:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def maintain(self, **kwargs: Any) -> Any:
        self.calls.append(("maintain", kwargs))
        return None


@pytest.fixture
def api() -> tuple[TestClient, _Control, FastAPI]:
    control = _Control()
    rotation = _Rotation()
    app = FastAPI()
    app.include_router(router)
    app.include_router(router, prefix="/v1")

    @app.middleware("http")
    async def bind_human(request: Any, call_next: Any) -> Any:
        request.state.human_principal_id = request.headers.get("X-Human")
        request.state.request_id = "request-api"
        return await call_next(request)

    app.state.context = {
        "mcp_workload_oauth_control": control,
        "mcp_workload_oauth_rotation": rotation,
    }
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    app.dependency_overrides[get_mcp_workload_oauth_control] = lambda: control
    app.dependency_overrides[get_mcp_workload_oauth_rotation] = lambda: rotation
    with TestClient(app) as client:
        yield client, control, app


def test_credential_secret_is_write_only_and_never_in_response_schema(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, control, _app = api
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.put(
        "/v1/admin/mcp/oauth/workload/profiles/profile-a/credential",
        json={"client_id": "client-canary", "client_secret": "secret-canary"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "profile_id": "profile-a",
        "credential_id": "credential-a",
        "generation": 2,
        "status": "active",
    }
    assert "canary" not in response.text
    supplied = control.calls[-1][1]
    assert supplied["client_secret"].get_secret_value() == "secret-canary"
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    request_fields = schema["WorkloadCredentialSetRequest"]["properties"]
    response_fields = schema["WorkloadCredentialStatus"]["properties"]
    assert request_fields["client_id"]["writeOnly"] is True
    assert request_fields["client_secret"]["writeOnly"] is True
    assert "client_id" not in response_fields
    assert "client_secret" not in response_fields


def test_admin_role_does_not_impersonate_owner_delegation(
    api: tuple[TestClient, _Control, FastAPI],
) -> None:
    client, control, app = api

    denied = client.get(
        "/mcp/agents/Alice/oauth/delegations",
        headers={"X-Human": "human-admin"},
    )
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    allowed = client.get(
        "/v1/mcp/agents/Alice/oauth/delegations",
        headers={"X-Human": "owner-1"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUTH_PERMISSION_DENIED"
    assert allowed.status_code == 200
    assert control.calls[-1] == (
        "list_delegations",
        {"owner_user_id": "owner-1", "execution_agent_id": "Alice"},
    )


def test_workload_routes_have_root_and_v1_parity(
    api: tuple[TestClient, _Control, FastAPI],
) -> None:
    client, _control, _app = api

    assert client.get("/admin/mcp/oauth/workload/profiles").status_code == 200
    assert client.get("/v1/admin/mcp/oauth/workload/profiles").status_code == 200


def test_metadata_refresh_returns_only_minimal_status_projection(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _control, _app = api
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.post(
        "/v1/admin/mcp/oauth/workload/profiles/profile-a/metadata/refresh"
    )

    assert response.status_code == 200
    assert response.json() == {
        "profile_id": "profile-a",
        "profile_generation": 3,
        "verified_at": 150.0,
        "status": "active",
    }
    assert "token" not in response.text
    assert "cidr" not in response.text
    properties = client.get("/openapi.json").json()["components"]["schemas"][
        "WorkloadMetadataStatus"
    ]["properties"]
    assert set(properties) == {
        "profile_id",
        "profile_generation",
        "verified_at",
        "status",
    }


def test_recovery_retry_is_explicit_and_origin_guarded(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, control, app = api
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.post("/v1/admin/mcp/oauth/workload/recovery/retry")

    assert response.status_code == 200
    assert response.json() == []
    rotation = app.state.context["mcp_workload_oauth_rotation"]
    assert rotation.calls[0][0] == "maintain"
    assert control.calls[-1][0] == "retry_recovery"


def test_owner_delegation_source_grant_is_server_derived(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, control, app = api
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.put(
        "/v1/mcp/agents/Alice/oauth/delegations/delegation-a",
        headers={"X-Human": "owner-1"},
        json={
            "installation_id": "calendar",
            "installation_generation": 4,
            "raw_tool_name": "calendar.read",
            "required_scopes": ["calendar.read"],
            "expires_at": 200.0,
        },
    )

    assert response.status_code == 200
    supplied = control.calls[-1][1]
    assert "auth_profile_id" not in supplied
    assert "source_profile_generation" not in supplied
    assert "source_grant_id" not in supplied
    assert "source_grant_version" not in supplied
    fields = client.get("/openapi.json").json()["components"]["schemas"][
        "WorkloadDelegationPutRequest"
    ]["properties"]
    assert "auth_profile_id" not in fields
    assert "source_profile_generation" not in fields
    assert "source_grant_id" not in fields
    assert "source_grant_version" not in fields


def test_owner_delegation_response_hides_internal_source_authority(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _control, app = api
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.put(
        "/v1/mcp/agents/Alice/oauth/delegations/delegation-a",
        headers={"X-Human": "owner-1"},
        json={
            "installation_id": "calendar",
            "installation_generation": 4,
            "raw_tool_name": "calendar.read",
            "required_scopes": ["calendar.read"],
            "expires_at": 200.0,
        },
    )

    assert response.status_code == 200
    assert set(response.json()).isdisjoint(
        {
            "owner_user_id",
            "source_slot",
            "source_profile_generation",
            "source_grant_id",
            "source_grant_version",
        }
    )


def test_transition_reconcile_is_explicit_typed_admin_action(
    api: tuple[TestClient, _Control, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, control, _app = api
    monkeypatch.setattr(
        "src.routers.mcp_oauth_workload.verify_origin",
        lambda _request: None,
    )

    response = client.post(
        "/v1/admin/mcp/oauth/workload/recovery/transitions/operation-1/reconcile",
        json={"resolution": "confirm_absent"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "completed", "generation": 1}
    assert control.calls[-1] == (
        "reconcile_transition",
        {
            "operation_id": "operation-1",
            "resolution": "confirm_absent",
            "actor_id": "human-admin",
            "request_id": "request-api",
        },
    )

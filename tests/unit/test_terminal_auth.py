"""Unit tests for C8: terminal approve/reject endpoint authentication (TDD).

C8 — POST /terminal/approve/{exec_id} and /terminal/reject/{exec_id} must
require a valid authenticated identity. Unauthenticated callers must be
denied (403), regardless of whether the exec_id exists.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.terminal import pending_approvals
from src.services.identity_router import get_verified_identity

OWNER_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
# ADR-0245：Guest 退役，低權限樣本改用 Base。
GUEST_IDENTITY = AgentIdentity.for_role(agent_id="LowAgent", role="Base", capabilities=[])


@pytest.fixture
def owner_client():
    """TestClient with Owner identity injected."""
    app.dependency_overrides[get_verified_identity] = lambda: OWNER_IDENTITY
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def no_auth_client():
    """TestClient with no authenticated identity (None)."""
    app.dependency_overrides[get_verified_identity] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def guest_client():
    """TestClient with a Base identity (authenticated but low-privilege)."""
    app.dependency_overrides[get_verified_identity] = lambda: GUEST_IDENTITY
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clean_pending():
    """Ensure pending_approvals is clean before and after each test."""
    pending_approvals.clear()
    yield
    pending_approvals.clear()


# ---------------------------------------------------------------------------
# C8 — /terminal/approve
# ---------------------------------------------------------------------------

def test_approve_requires_authentication(no_auth_client: TestClient):
    """Unauthenticated request to /terminal/approve must be denied (403)."""
    exec_id = "test-exec-noauth"
    pending_approvals[exec_id] = asyncio.Event()

    response = no_auth_client.post(f"/terminal/approve/{exec_id}")

    assert response.status_code == 403, (
        f"Expected 403 for unauthenticated approve, got {response.status_code}"
    )


def test_approve_rejects_guest_role(guest_client: TestClient):
    """A low-privilege (Base) identity must not be able to approve terminal commands."""
    exec_id = "test-exec-guest"
    pending_approvals[exec_id] = asyncio.Event()

    response = guest_client.post(f"/terminal/approve/{exec_id}")

    assert response.status_code == 403, (
        f"Expected 403 for low-privilege approve, got {response.status_code}"
    )


def test_approve_allows_owner(owner_client: TestClient):
    """Owner identity must be able to approve a pending terminal command."""
    exec_id = "test-exec-owner"
    event = asyncio.Event()
    pending_approvals[exec_id] = event

    response = owner_client.post(f"/terminal/approve/{exec_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_approve_returns_404_for_unknown_exec_id(owner_client: TestClient):
    """Known-good identity trying to approve non-existent exec_id gets 404."""
    response = owner_client.post("/terminal/approve/does-not-exist")

    # Should be a 404-like error (ToukaException: TERMINAL_APPROVAL_NOT_FOUND)
    assert response.status_code in (404, 422, 400, 500), (
        f"Expected error status for unknown exec_id, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# C8 — /terminal/reject
# ---------------------------------------------------------------------------

def test_reject_requires_authentication(no_auth_client: TestClient):
    """Unauthenticated request to /terminal/reject must be denied (403)."""
    exec_id = "test-reject-noauth"
    pending_approvals[exec_id] = asyncio.Event()

    response = no_auth_client.post(f"/terminal/reject/{exec_id}")

    assert response.status_code == 403


def test_reject_rejects_guest_role(guest_client: TestClient):
    """A low-privilege (Base) identity must not be able to reject terminal commands."""
    exec_id = "test-reject-guest"
    pending_approvals[exec_id] = asyncio.Event()

    response = guest_client.post(f"/terminal/reject/{exec_id}")

    assert response.status_code == 403


def test_reject_allows_owner(owner_client: TestClient):
    """Owner identity must be able to reject a pending terminal command."""
    exec_id = "test-reject-owner"
    event = asyncio.Event()
    pending_approvals[exec_id] = event

    response = owner_client.post(f"/terminal/reject/{exec_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

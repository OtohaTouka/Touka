"""ADR-0208 公開模型端點不得接受 caller-controlled upstream route。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.models.identity import AgentIdentity
from src.routers import proxy as proxy_router
from src.services.identity_router import get_verified_identity

_CALLER = AgentIdentity.for_role(agent_id="route-test-human", role="normal", is_human=True)
_ENDPOINT_PAYLOADS = {
    "/v1/chat/completions": {
        "model": "grok-4.5",
        "messages": [{"role": "user", "content": "hi"}],
    },
    "/v1/messages": {
        "model": "claude-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
    },
}


def _client(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> TestClient:
    async def fake_process(request: Any, _context: Any, *, is_anthropic: bool) -> dict[str, Any]:
        calls.append((request, is_anthropic))
        return {"accepted": True}

    monkeypatch.setattr(proxy_router, "process_proxy_request", fake_process)
    monkeypatch.setattr(proxy_router.model_manager, "mark_model_seen", lambda _identity: None)
    test_app = FastAPI()
    test_app.state.context = {}
    test_app.include_router(proxy_router.router, prefix="/v1")
    # ADR-0245：聊天端點要登入；這裡只測 route 欄位，給一個已驗證的人類身分。
    test_app.dependency_overrides[get_verified_identity] = lambda: _CALLER
    return TestClient(test_app)


@pytest.mark.parametrize("endpoint", _ENDPOINT_PAYLOADS)
@pytest.mark.parametrize("route_override", [{}, {"base_url": None}])
def test_public_model_endpoints_accept_only_omitted_or_null_base_url(
    endpoint: str,
    route_override: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    client = _client(monkeypatch, calls)
    response = client.post(endpoint, json=_ENDPOINT_PAYLOADS[endpoint] | route_override)

    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("endpoint", _ENDPOINT_PAYLOADS)
@pytest.mark.parametrize(
    "route_override",
    [
        {"base_url": ""},
        {"base_url": "https://attacker.example.test/v1"},
        {"base_url": 7},
        {"base_url": {"host": "attacker.example.test"}},
        {"baseUrl": None},
    ],
)
def test_public_model_endpoints_reject_every_route_override_shape(
    endpoint: str,
    route_override: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    client = _client(monkeypatch, calls)
    response = client.post(endpoint, json=_ENDPOINT_PAYLOADS[endpoint] | route_override)

    assert response.status_code == 422
    assert calls == []

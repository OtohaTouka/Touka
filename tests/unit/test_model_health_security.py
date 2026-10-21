"""TDD security tests for POST /models/{id}/health (禁令 23 / 禁令 22).

Verifies the model health endpoint:
  - never returns raw exception text (which could embed an API key) — only stable tokens
  - sends the Gemini key via the x-goog-api-key header, never in the request URL
  - constructs httpx clients with follow_redirects=False (SSRF defense)
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
FAKE_GEMINI_KEY = "sk-fake-gemini-SECRET-9f8e7d"


def _client() -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    return TestClient(app, raise_server_exceptions=False)


def _clear() -> None:
    app.dependency_overrides.clear()


class _MockResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_mock_client(capture: dict, *, raise_exc: Exception | None = None, status_code: int = 200):
    class _MockAsyncClient:
        def __init__(self, **kwargs):
            capture["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, **kwargs):
            capture["url"] = url
            capture["headers"] = kwargs.get("headers")
            if raise_exc is not None:
                raise raise_exc
            return _MockResp(status_code)

    return _MockAsyncClient


def _patches(capture, **kwargs):
    return (
        patch("src.config.GEMINI_API_KEY", FAKE_GEMINI_KEY),
        patch("src.routers.models.get_model_metadata", return_value={"owned_by": "google"}),
        patch("httpx.AsyncClient", _make_mock_client(capture, **kwargs)),
    )


def test_gemini_key_not_leaked_in_error_on_connect_error():
    """A connection error must surface a stable token, never the key/exception text (禁令 23)."""
    capture: dict = {}
    exc = httpx.ConnectError(f"failed connecting with key={FAKE_GEMINI_KEY}")
    p1, p2, p3 = _patches(capture, raise_exc=exc)
    with p1, p2, p3:
        c = _client()
        resp = c.post("/models/gemini-2.5-flash/health")
    _clear()
    assert resp.status_code == 200
    body = resp.json()
    assert FAKE_GEMINI_KEY not in str(body)
    assert "SECRET" not in str(body)
    assert body["error"] == "connection_error"


def test_generic_exception_returns_stable_token():
    """Any other exception → 'request_failed', never str(exc) (禁令 23)."""
    capture: dict = {}
    exc = RuntimeError(f"boom Bearer {FAKE_GEMINI_KEY}")
    p1, p2, p3 = _patches(capture, raise_exc=exc)
    with p1, p2, p3:
        c = _client()
        resp = c.post("/models/gemini-2.5-flash/health")
    _clear()
    body = resp.json()
    assert body["error"] == "request_failed"
    assert FAKE_GEMINI_KEY not in str(body)


def test_gemini_key_in_header_not_url():
    """The Gemini key must travel in x-goog-api-key, never in the request URL (禁令 23)."""
    capture: dict = {}
    p1, p2, p3 = _patches(capture, status_code=200)
    with p1, p2, p3:
        c = _client()
        resp = c.post("/models/gemini-2.5-flash/health")
    _clear()
    assert resp.status_code == 200
    assert "key=" not in capture["url"]
    assert FAKE_GEMINI_KEY not in capture["url"]
    assert capture["headers"].get("x-goog-api-key") == FAKE_GEMINI_KEY


def test_health_client_disables_redirects():
    """httpx client must set follow_redirects=False (禁令 22 SSRF defense)."""
    capture: dict = {}
    p1, p2, p3 = _patches(capture, status_code=200)
    with p1, p2, p3:
        c = _client()
        c.post("/models/gemini-2.5-flash/health")
    _clear()
    assert capture["client_kwargs"].get("follow_redirects") is False

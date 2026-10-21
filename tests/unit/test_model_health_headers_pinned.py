"""模型健康檢查送出的請求被凍結：URL、標頭、請求本文、逾時與不跟隨轉址整組精確比對。

健康檢查路由直接用 httpx（不經 adapter），故比照 test_model_health_security.py：
換掉 httpx.AsyncClient，以 TestClient＋dependency override 呼叫既有端點。
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

_OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])


def _post_health(model_id: str, owner: str, key_name: str) -> dict:
    """呼叫健康檢查端點，回傳攔下的 httpx 呼叫（client 參數、URL、post 參數）。"""
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["post_kwargs"] = kwargs
            return type("Resp", (), {"status_code": 200})()

    app.dependency_overrides[get_verified_identity] = lambda: _OWNER
    try:
        with patch(f"src.config.{key_name}", "fake-key"), patch(
            "src.routers.models.get_model_metadata", return_value={"owned_by": owner}
        ), patch("httpx.AsyncClient", _FakeAsyncClient):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(f"/models/{model_id}/health")
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)
    assert resp.json()["status"] == "healthy"
    assert captured["client_kwargs"] == {"follow_redirects": False}
    return captured


def test_claude_health_check_sends_anthropic_version():
    """Claude 健康檢查必須帶 Anthropic API 要求的 anthropic-version 標頭。

    少了這個標頭，上游會直接拒絕，健康檢查永遠回 degraded。
    """
    captured = _post_health("claude-sonnet-4-6", "anthropic", "ANTHROPIC_API_KEY")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["post_kwargs"] == {
        "headers": {"x-api-key": "fake-key", "anthropic-version": "2023-06-01"},
        "json": {"model": "claude-sonnet-4-6", "max_tokens": 10,
                 "messages": [{"role": "user", "content": "test"}]},
        "timeout": 10,
    }


def test_openai_health_check_sends_max_completion_tokens():
    """OpenAI 健康檢查必須送 max_completion_tokens，不送 max_tokens。

    新的 OpenAI 推理模型拒收 max_tokens；只有 owned_by == "openai" 走這條路。
    """
    captured = _post_health("gpt-4.1", "openai", "OPENAI_API_KEY")
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["post_kwargs"] == {
        "headers": {"Authorization": "Bearer fake-key"},
        "json": {"model": "gpt-4.1", "messages": [{"role": "user", "content": "test"}],
                 "max_completion_tokens": 10},
        "timeout": 10,
    }

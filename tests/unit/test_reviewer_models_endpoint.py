"""GET /v1/autonomy/reviewer-models（ADR-0246 U2c-6，決定 5）。

審查員選單只能列「供應商金鑰已設定」的一般模型：沒金鑰的供應商、靈魂、自訂模型、
不用金鑰的端點型供應商、停用的模型都不列。金鑰只用假值，不連任何供應商。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import src.routers.autonomy as autonomy_router
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
BASE = AgentIdentity.for_role(agent_id="DebugAgent", role="Base", capabilities=[])

MODELS: list[dict[str, Any]] = [
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "owned_by": "anthropic"},
    {"id": "deepseek-chat", "owned_by": "deepseek"},
    {"id": "gpt-5", "name": "GPT-5", "owned_by": "openai"},
    {"id": "Alice", "name": "Alice", "owned_by": "anthropic", "is_agent": True},
    {"id": "claude-old", "owned_by": "anthropic", "enabled": False},
    {"id": "llama3", "owned_by": "ollama"},
    {"id": "my-custom", "owned_by": "custom"},
    {"id": "Human", "owned_by": "human"},
]


class _FakeManager:
    def get_models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in MODELS]


@pytest.fixture
def client_as(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """anthropic、deepseek 有假金鑰；openai 沒有。"""
    monkeypatch.setattr(autonomy_router, "model_manager", _FakeManager())
    keys = {"anthropic": "fake-key", "deepseek": "fake-key", "ollama": "fake-key"}
    monkeypatch.setattr(autonomy_router.registry, "get_key", lambda spec: keys.get(spec.id, ""))

    def _factory(identity: AgentIdentity) -> TestClient:
        app.dependency_overrides[get_verified_identity] = lambda: identity
        return TestClient(app, raise_server_exceptions=False)

    yield _factory
    app.dependency_overrides.pop(get_verified_identity, None)


def test_lists_only_models_whose_provider_key_is_set(client_as) -> None:
    resp = client_as(OWNER).get("/v1/autonomy/reviewer-models")
    assert resp.status_code == 200
    assert resp.json() == {
        "models": [
            {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "provider": "anthropic"},
            {"id": "deepseek-chat", "name": "deepseek-chat", "provider": "deepseek"},
        ]
    }


def test_root_path_parity(client_as) -> None:
    resp = client_as(OWNER).get("/autonomy/reviewer-models")
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["models"]] == ["claude-haiku-5-5", "deepseek-chat"]


def test_requires_the_autonomy_role(client_as) -> None:
    resp = client_as(BASE).get("/v1/autonomy/reviewer-models")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_PERMISSION_DENIED"

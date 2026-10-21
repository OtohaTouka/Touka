"""ADR-0069 Phase D — 創建路由整合測試（門檻單元 + TestClient）。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers.creation import _require_creation_role
from src.services.creation.evaluation import eval_runner
from src.services.creation.models import SmokeReport
from src.services.creation.smoke import ToolInfo
from src.services.identity_router import get_verified_identity

_PATCH_SMOKE = "src.services.creation.mcp_kind.smoke_mcp_server"
_PATCH_LIST = "src.services.creation.mcp_kind.list_mcp_tools"


@pytest.fixture
def owner_client():
    """明確以 Owner 身分覆寫 get_verified_identity 的 TestClient（不依賴預設角色）。"""
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="owner-test", role="Owner"
    )
    with TestClient(app) as client:
        yield client


# --- 門檻邏輯（單元、確定性）---


def test_require_creation_role_gate():
    owner = AgentIdentity.for_role(agent_id="o", role="Owner")
    admin = AgentIdentity.for_role(agent_id="a", role="admin")
    assert _require_creation_role(owner) is owner
    assert _require_creation_role(admin) is admin
    for bad in (None, AgentIdentity.for_role(agent_id="m", role="Member")):
        with pytest.raises(ToukaException):
            _require_creation_role(bad)


# --- HTTP ---


def test_skill_403_wrong_role():
    app.dependency_overrides[get_verified_identity] = lambda: AgentIdentity.for_role(
        agent_id="m", role="Member"
    )
    with TestClient(app) as client:
        resp = client.post("/v1/creation/skill", json={"id": "r403", "description": "d"})
    assert resp.status_code == 403


def test_skill_200_happy(owner_client):
    resp = owner_client.post(
        "/v1/creation/skill",
        json={"id": "route-skill-a", "description": "d", "system_prompt": "b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is True
    assert body["status"] == "created"


def test_mcp_200_happy(owner_client):
    with (
        patch(_PATCH_SMOKE, new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=1))),
        patch(_PATCH_LIST, new=AsyncMock(return_value=[ToolInfo(name="ping")])),
    ):
        resp = owner_client.post(
            "/v1/creation/mcp",
            json={"name": "route-mcp-a", "command": "uvx", "args": ["x"]},
        )
    assert resp.status_code == 200
    assert resp.json()["committed"] is True


def test_scaffold_200_happy(owner_client):
    with (
        patch(_PATCH_SMOKE, new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=1))),
        patch(_PATCH_LIST, new=AsyncMock(return_value=[ToolInfo(name="ping")])),
    ):
        resp = owner_client.post(
            "/v1/creation/scaffold",
            json={
                "name": "route-scaffold-a",
                "tools": [{"name": "ping", "description": "p"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is True
    assert body["scaffold"] is not None


def test_skill_validation_fail_returns_200_not_500(owner_client):
    resp = owner_client.post(
        "/v1/creation/skill",
        json={"id": "Bad Id", "description": "d", "system_prompt": "b"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is False
    assert body["errors"]


def test_extra_field_rejected_422(owner_client):
    resp = owner_client.post(
        "/v1/creation/skill",
        json={"id": "r422", "description": "d", "bogus": 1},
    )
    assert resp.status_code == 422


# --- 評分記帳到建立者（ADR-0244）---


class _RecordingJudge:
    def __init__(self) -> None:
        self.identities: list = []

    async def score(self, tasks, artifact_md, identity=None) -> float:
        self.identities.append(identity)
        return 0.5


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/creation/skill",
         {"id": "route-judge-skill", "description": "d", "system_prompt": "b"}),
        ("/v1/creation/mcp", {"name": "route-judge-mcp", "command": "uvx", "args": ["x"]}),
        ("/v1/creation/scaffold",
         {"name": "route-judge-scaffold", "tools": [{"name": "ping", "description": "p"}]}),
    ],
)
def test_eval_scoring_bills_the_creator(owner_client, path, payload):
    """eval_scoring 時，評分拿到的是發起建立的身分（記帳到建立者，不是系統帳）。"""
    judge = _RecordingJudge()
    eval_runner.set_judge(judge)
    try:
        with (
            patch(_PATCH_SMOKE, new=AsyncMock(return_value=SmokeReport(ok=True, tool_count=1))),
            patch(_PATCH_LIST, new=AsyncMock(return_value=[ToolInfo(name="ping")])),
        ):
            resp = owner_client.post(path, json={**payload, "eval_scoring": True})
    finally:
        eval_runner.set_judge(None)
    assert resp.status_code == 200
    assert resp.json()["committed"] is True
    assert judge.identities == [AgentIdentity.for_role(agent_id="owner-test", role="Owner")]

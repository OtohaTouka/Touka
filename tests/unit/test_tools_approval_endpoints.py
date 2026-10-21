"""H2 — 統一審批解算端點授權矩陣（POST /v1/tools/approve|reject/{exec_id}）。

授權立場（嚴於遺留 /terminal/approve 的 is_privileged）：
1. **必須人類**（is_human）——agent token（含 Owner）一律 403＝杜絕 agent 自批。
2. 且其一：admin 權威／待批 agent 擁有者（owned_agents）／session 擁有者
   （owner_user_id）。
3. 未知／已解算 exec_id → TERMINAL_APPROVAL_NOT_FOUND；解算單發、等待端如實收到。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.approval_service import approval_service
from src.services.identity_router import get_verified_identity

ADMIN = AgentIdentity.for_role(agent_id="AdminH", role="admin", is_human=True)
OWNER_OF_SUMI = AgentIdentity.for_role(
    agent_id="user-owns-sumi", role="normal", is_human=True, owned_agents=["Sumi"]
)
STRANGER = AgentIdentity.for_role(
    agent_id="user-stranger", role="normal", is_human=True, owned_agents=[]
)
AGENT_OWNER = AgentIdentity.for_role(agent_id="SomeAgent", role="Owner", is_human=False)


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()
    app.dependency_overrides.clear()


def _client(identity: AgentIdentity) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


async def _pending(exec_id: str = "appr_test1", session_id: str | None = "sess-appr") -> str:
    ok = await approval_service.register(
        exec_id,
        tool_name="write",
        agent_id="Sumi",
        session_id=session_id,
        arguments_preview={"scope": "vault"},
    )
    assert ok
    return exec_id


@pytest.mark.asyncio
async def test_admin_can_approve_and_waiter_proceeds():
    exec_id = await _pending()
    waiter = asyncio.create_task(approval_service.wait(exec_id))
    await asyncio.sleep(0)
    with _client(ADMIN) as c:
        r = c.post(f"/v1/tools/approve/{exec_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert await waiter == "approved"


@pytest.mark.asyncio
async def test_agent_owner_human_can_reject():
    exec_id = await _pending("appr_test2")
    waiter = asyncio.create_task(approval_service.wait(exec_id))
    await asyncio.sleep(0)
    with _client(OWNER_OF_SUMI) as c:
        r = c.post(f"/v1/tools/reject/{exec_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert await waiter == "rejected"


@pytest.mark.asyncio
async def test_session_owner_human_can_approve(monkeypatch):
    """既非 admin 也非 agent 擁有者，但擁有該 session（owner_user_id 相符）→ 放行。"""
    exec_id = await _pending("appr_test3", session_id="sess-owned")

    async def _fake_get(session_id):
        assert session_id == "sess-owned"
        return {"owner_user_id": STRANGER.agent_id}

    monkeypatch.setattr(
        "src.routers.tools_approval.session_service.get_session_by_id", _fake_get
    )
    with _client(STRANGER) as c:
        r = c.post(f"/v1/tools/approve/{exec_id}")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_unrelated_human_denied_and_pending_survives():
    exec_id = await _pending("appr_test4", session_id=None)
    with _client(STRANGER) as c:
        r = c.post(f"/v1/tools/approve/{exec_id}")
    assert r.status_code == 403
    assert approval_service.get(exec_id) is not None  # 拒絕授權≠解算，待批仍在


@pytest.mark.asyncio
async def test_agent_token_cannot_self_approve():
    """agent（即使 Owner 角色）不得解算——HITL 的 H 是人。"""
    exec_id = await _pending("appr_test5")
    with _client(AGENT_OWNER) as c:
        r = c.post(f"/v1/tools/approve/{exec_id}")
    assert r.status_code == 403
    assert approval_service.get(exec_id) is not None


def test_unknown_exec_id_not_found():
    with _client(ADMIN) as c:
        r = c.post("/v1/tools/approve/ghost-exec")
    assert r.json()["detail"]["code"] == "TERMINAL_APPROVAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_interaction_approve_accepts_structured_response_payload():
    exec_id = await _pending("appr_payload")
    waiter = asyncio.create_task(approval_service.wait_for_resolution(exec_id))
    await asyncio.sleep(0)
    with _client(ADMIN) as c:
        response = c.post(
            f"/v1/tools/approve/{exec_id}",
            json={"payload": {"answer": "yes"}},
        )
    assert response.status_code == 200
    resolution = await waiter
    assert resolution.decision == "accepted"
    assert resolution.response_payload == {"answer": "yes"}


@pytest.mark.asyncio
async def test_invalid_interaction_payload_is_rejected_without_consuming_pending():
    exec_id = "appr_invalid_payload"
    assert await approval_service.register(
        exec_id,
        tool_name="server__demo",
        agent_id="Sumi",
        session_id=None,
        arguments_preview={},
        interaction_kind="mcp_elicitation",
        interaction_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )

    with _client(ADMIN) as c:
        response = c.post(
            f"/v1/tools/approve/{exec_id}",
            json={"payload": {"answer": 7}},
        )

    assert response.status_code == 422
    assert approval_service.get(exec_id) is not None


@pytest.mark.asyncio
async def test_human_can_explicitly_cancel_interaction():
    exec_id = await _pending("appr_cancel")
    waiter = asyncio.create_task(approval_service.wait_for_resolution(exec_id))
    await asyncio.sleep(0)
    with _client(ADMIN) as c:
        response = c.post(f"/v1/tools/cancel/{exec_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert (await waiter).decision == "cancelled"


@pytest.mark.asyncio
async def test_double_resolve_not_found():
    exec_id = await _pending("appr_test6")
    waiter = asyncio.create_task(approval_service.wait(exec_id))
    await asyncio.sleep(0)
    with _client(ADMIN) as c:
        r1 = c.post(f"/v1/tools/approve/{exec_id}")
        r2 = c.post(f"/v1/tools/reject/{exec_id}")
    assert r1.status_code == 200
    assert r2.json()["detail"]["code"] == "TERMINAL_APPROVAL_NOT_FOUND"  # 單發、不可翻盤
    assert await waiter == "approved"

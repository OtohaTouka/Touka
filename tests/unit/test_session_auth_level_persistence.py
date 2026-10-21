"""H1 — auth_level 持久化鏈（hitl-unification 提案 H1；實作前＝紅）。

診斷（2026-07-11）：前端 AuthSwitcher 把 ``auth_level`` 放進 session JSON 送
``POST /v1/ui/sessions/{id}``，但 ``ui_sessions.py`` 的 ``SessionModel`` 沒有該欄位
＝**靜默丟棄**；DB ``user_sessions.auth_level`` 永遠停在預設 ``'Manual'``，而工具閘門
（ToolLayer，tool.py:65-74）讀的正是 DB → 切換器近乎裝飾品（``async_db.upsert_session``
本就支援 auth_level 參數，只是路由沒傳）。

本檔鎖：API 寫入 → DB 持久化 → ToolLayer 讀出 的整條鏈，以及非法值 422。全走
conftest 沙箱。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest
from src.main import app
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.utils.async_db import session_service

ADMIN = AgentIdentity.for_role(agent_id="AuthLvlAdmin", role="admin", is_human=True)


@pytest.fixture
def client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _payload(client: TestClient, session_id: str, **extra) -> dict:
    snapshot = client.get(f"/v1/ui/sessions/{session_id}?snapshot=true")
    assert snapshot.status_code == 200, snapshot.text
    return {
        "revision": snapshot.json()["revision"],
        "id": session_id,
        "title": "auth-level probe",
        "messages": [],
        "updatedAt": 1760000000000,
        **extra,
    }


@pytest.mark.asyncio
async def test_save_session_persists_auth_level(client):
    """POST 帶 auth_level=Auto → DB row 實際存入（現況紅：SessionModel 無欄位、
    upsert 未傳 → 永遠 'Manual'）。"""
    sid = "hitl-persist-a"
    r = client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid, auth_level="Auto"))
    assert r.status_code == 200, r.text

    session = await session_service.get_session_by_id(sid)
    assert session is not None
    persisted = session.get("auth_level")
    assert persisted == "Auto"


@pytest.mark.asyncio
async def test_update_flips_level_and_omission_keeps_it(client):
    """再存一次改 YOLO → 覆蓋；之後省略 auth_level → 保留舊值（upsert 語意）。"""
    sid = "hitl-persist-b"
    client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid, auth_level="Auto"))
    client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid, auth_level="YOLO"))
    session = await session_service.get_session_by_id(sid)
    flipped = session.get("auth_level")
    assert flipped == "YOLO"

    client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid))  # 未帶 → 不得退回 Manual
    session = await session_service.get_session_by_id(sid)
    kept = session.get("auth_level")
    assert kept == "YOLO"


def test_invalid_auth_level_rejected(client):
    """非三模式之一的值 → 422（Literal 驗證），不得靜默落 DB。"""
    sid = "hitl-persist-c"
    r = client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid, auth_level="GodMode"))
    assert r.status_code == 422


def test_get_session_returns_auth_level(client):
    """GET 回傳含 auth_level——前端重載後 AuthSwitcher 據此水合。"""
    sid = "hitl-persist-d"
    client.post(f"/v1/ui/sessions/{sid}", json=_payload(client, sid, auth_level="Auto"))
    r = client.get(f"/v1/ui/sessions/{sid}")
    assert r.status_code == 200, r.text
    returned = r.json().get("auth_level")
    assert returned == "Auto"


@pytest.mark.asyncio
async def test_tool_layer_reads_persisted_level(client, monkeypatch):
    """ToolLayer 從 session row 讀 auth_level 進 ctx（既有鏈；持久化修好後端到端成立）。"""
    sid = "hitl-persist-e"
    # session 綁這次執行的 agent，存下的 Auto 才會被採用（ADR-0246 R-2c）
    client.post(
        f"/v1/ui/sessions/{sid}",
        json=_payload(client, sid, agent_id="AuthLvlAgent", auth_level="Auto"),
    )

    ctx = PipelineContext(
        session_id=sid,
        agent_id="AuthLvlAgent",
        identity=AgentIdentity.for_role(agent_id="AuthLvlAgent", role="Owner", is_human=False),
    )
    request = OpenAIChatRequest(
        model="claude-test-brain", messages=[{"role": "user", "content": "hi"}]
    )
    await ToolLayer().process(request, ctx)
    assert ctx.auth_level == "Auto"

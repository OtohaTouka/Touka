"""Unit tests for artifact path traversal and IDOR security fixes (TDD)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from memory_server.models.mission import MissionState, Task, TaskStatus
from pydantic import ValidationError
from src.core.exceptions import ErrorCode
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

# 定義測試身份
OWNER_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])
BASE_IDENTITY = AgentIdentity.for_role(
    agent_id="MockUnauthorizedAgent_Test", role="base", capabilities=[]
)
SUPERVISOR_IDENTITY = AgentIdentity.for_role(
    agent_id="MockSupervisorAgent_Test",
    role="base",
    capabilities=["can_supervise_dispatch"],
)


def _override(identity):
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _clear():
    app.dependency_overrides.clear()


# ===========================================================================
# 1. Pydantic validation tests (Path Traversal 防禦)
# ===========================================================================

def test_task_artifact_ref_safe_paths():
    """驗證安全的相對路徑能通過驗證。"""
    t1 = Task(
        id="t1",
        description="safe file",
        status=TaskStatus.TODO,
        artifact_ref="src/main.py",
    )
    assert t1.artifact_ref == "src/main.py"

    t2 = Task(
        id="t2",
        description="safe files list",
        status=TaskStatus.TODO,
        artifact_refs=["src/main.py", "tests/test.py"],
    )
    assert t2.artifact_refs == ["src/main.py", "tests/test.py"]


def test_task_artifact_ref_traversal_rejected():
    """驗證包含 .. 的路徑穿越被拒絕。"""
    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t1",
            description="traversal file",
            status=TaskStatus.TODO,
            artifact_ref="../etc/passwd",
        )
    assert "Path traversal detected" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t2",
            description="traversal files list",
            status=TaskStatus.TODO,
            artifact_refs=["src/main.py", "../../users.json"],
        )
    assert "Path traversal detected" in str(exc_info.value)


def test_task_artifact_ref_absolute_rejected():
    """驗證絕對路徑被拒絕。"""
    # Unix 絕對路徑
    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t1",
            description="absolute file",
            status=TaskStatus.TODO,
            artifact_ref="/etc/passwd",
        )
    assert "Absolute paths are not allowed" in str(exc_info.value)

    # Windows 絕對路徑
    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t2",
            description="absolute file win",
            status=TaskStatus.TODO,
            artifact_ref="C:" + "\\Windows\\system32\\cmd.exe",
        )
    assert "Absolute paths are not allowed" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t3",
            description="absolute file win drive",
            status=TaskStatus.TODO,
            artifact_ref="d:" + "/Touka/config/users.json",
        )
    assert "Absolute paths are not allowed" in str(exc_info.value)

    # List 包含絕對路徑
    with pytest.raises(ValidationError) as exc_info:
        Task(
            id="t4",
            description="absolute files list",
            status=TaskStatus.TODO,
            artifact_refs=["src/main.py", "/etc/passwd"],
        )
    assert "Absolute paths are not allowed" in str(exc_info.value)


# ===========================================================================
# 2. UI /mission endpoint IDOR tests (越權存取防禦)
# ===========================================================================

def test_ui_mission_requires_auth():
    """驗證無憑證請求會被拒絕。"""
    _override(None)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ui/mission")
    _clear()
    assert resp.status_code in (401, 403)


def test_ui_mission_self_access_allowed():
    """驗證普通 Agent 存取自己的看板被允許（此處預計沒有 mission 回傳 404，但不應是 403 越權）。"""
    _override(BASE_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "MockUnauthorizedAgent_Test"})
    _clear()
    assert resp.status_code == 404  # 由於沒有 active mission，回傳 404
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == ErrorCode.SESSION_NOT_FOUND.value


def test_ui_mission_idor_blocked():
    """驗證無權限的普通 Agent 跨越權限邊界請求他人看板會被 403 阻斷。"""
    _override(BASE_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    # 試圖請求 target_agent_id="Alice"
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "Alice"})
    _clear()
    assert resp.status_code == 403
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == ErrorCode.AUTH_PERMISSION_DENIED.value


def test_ui_mission_owner_cross_access_allowed():
    """驗證具有 Owner 角色的管理者可以跨 Agent 存取。"""
    _override(OWNER_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    # Owner 可以查詢 Alice 看板
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "Alice"})
    _clear()
    assert resp.status_code in (200, 404)  # 200/404 表示已通過權限判定


def test_ui_mission_supervisor_dispatch_allowed():
    """驗證具備 can_supervise_dispatch 能力的 Agent 存取 DispatchAgent 被允許。"""
    _override(SUPERVISOR_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "DispatchAgent"})
    _clear()
    assert resp.status_code in (200, 404)


def test_ui_mission_regular_dispatch_blocked():
    """驗證無 can_supervise_dispatch 能力的普通 Agent 存取 DispatchAgent 被阻斷。"""
    _override(BASE_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "DispatchAgent"})
    _clear()
    assert resp.status_code == 403
    detail = resp.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == ErrorCode.AUTH_PERMISSION_DENIED.value


def test_ui_mission_active_mission_returns_200_dict(monkeypatch):
    """E26（ADR-0045）：有 active mission 時回 200 且 body 為 model_dump() 後的 dict。

    上列測試全停在 404/403、從不 seed 真 mission → :801 的 ``return mission.model_dump()``
    從未走過。變異改 ``return mission``（raw model 撞 ``dict[str, Any]`` 回應驗證）→ 500 → 紅。
    """
    mission = MissionState(
        mission_id="m-e26",
        goal="serialization wiring",
        agent_id="MockUnauthorizedAgent_Test",
        tasks=[Task(id="t1", description="d", status=TaskStatus.TODO)],
    )

    class _StubManager:
        async def get_current_mission(self, target_identity):
            return mission

    monkeypatch.setattr("src.routers.ui_sessions.MissionManager", lambda: _StubManager())
    _override(BASE_IDENTITY)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/ui/mission", headers={"X-Touka-Soul-ID": "MockUnauthorizedAgent_Test"})
    _clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mission_id"] == "m-e26"
    assert body["goal"] == "serialization wiring"
    assert body["tasks"][0]["id"] == "t1"

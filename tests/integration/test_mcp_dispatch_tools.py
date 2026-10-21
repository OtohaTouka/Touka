"""Integration tests for DispatchAgent mission board via ToukaProxy MCP endpoints.

NOTE: access_mission_board_tool has been removed from memory_server.server (C1 修復).
Dispatch operations now go through ToukaDispatch MCP server or ToukaProxy debug/execute.
"""

import shutil
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service
from memory_server import _get_app_data_dir
from memory_server.services.mission_manager import MissionManager
from memory_server.models.mission import TaskStatus

@pytest.fixture(autouse=True)
def setup_identity_map():
    map_data = {
        "tag_rules": {
            "is_toukasoul": {"role": "Owner", "capabilities": ["heartbeat_awakening"]}
        },
        "agent_overrides": {
            "DebugAgent": {
                "role": "Owner",
                "capabilities": ["heartbeat_awakening", "can_supervise_dispatch"]
            },
            "MockUnauthorizedAgent": {
                "role": "base",
                "capabilities": []
            },
            "DispatchAgent": {
                "role": "Base",
                "capabilities": []
            }
        },
        "default_role": "Base"
    }
    permission_service.update_identity(map_data)
    yield

@pytest.fixture
def client():
    # 藉由指定 client 參數為外網 IP，模擬外部請求
    with TestClient(app, client=("8.8.8.8", 1234)) as c:
        yield c

@pytest.fixture
def clean_dispatch_board():
    # 每次測試前清理 DispatchAgent 的看板目錄，確保無殘留
    path = Path(_get_app_data_dir()) / "personal_drive" / "DispatchAgent" / "missions"
    if path.exists():
        shutil.rmtree(path)
    yield
    if path.exists():
        shutil.rmtree(path)

@pytest.mark.asyncio
async def test_mcp_dispatch_execute_authorized(client, clean_dispatch_board):
    """測試 Supervisor (如 DebugAgent) 帶有 can_supervise_dispatch 權限，透過 debug/execute 成功初始化與操作 DispatchAgent 的看板。"""
    identity = AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="Owner",
        capabilities=["can_supervise_dispatch"]
    )
    token = identity_service.issue_token(identity)

    # 1. 初始化看板
    init_payload = {
        "tool_name": "mission_tools",
        "args": {
            "action": "initialize",
            "goal": "Integration test dispatch mission",
            "target_agent_id": "DispatchAgent",
            "tasks": [
                {
                    "id": "t1",
                    "description": "Integration task 1",
                    "verification_cmd": "pytest tests/integration/test_mcp_dispatch_tools.py",
                    "boundary_rules": ["NO_PROMPT_INJECTION"]
                }
            ]
        },
        "agent_id": "DebugAgent",
        # H1：initialize 屬 safe 類——Manual 下 fail-closed，監督者顯式 Auto 放行
        "auth_level": "Auto"
    }
    
    response = client.post(
        "/v1/debug/execute",
        json=init_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "success"
    assert "Mission Initialized" in res_data["result"]

    # 2. 獲取看板
    get_payload = {
        "tool_name": "mission_tools",
        "args": {
            "action": "get_board",
            "target_agent_id": "DispatchAgent"
        },
        "agent_id": "DebugAgent"
    }
    response = client.post(
        "/v1/debug/execute",
        json=get_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    res_data = response.json()
    assert "Integration test dispatch mission" in res_data["result"]
    assert "t1" in res_data["result"]

    # 3. 先更新看板任務狀態為 DOING
    update_payload_doing = {
        "tool_name": "mission_tools",
        "args": {
            "action": "update_status",
            "task_id": "t1",
            "status": "DOING",
            "target_agent_id": "DispatchAgent"
        },
        "agent_id": "DebugAgent",
        "auth_level": "Auto"
    }
    response = client.post(
        "/v1/debug/execute",
        json=update_payload_doing,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200

    # 4. 更新看板任務狀態為 DONE
    update_payload = {
        "tool_name": "mission_tools",
        "args": {
            "action": "update_status",
            "task_id": "t1",
            "status": "DONE",
            "target_agent_id": "DispatchAgent",
            "artifact_refs": ["src/main.py"],
            "test_log": "All tests passed successfully."
        },
        "agent_id": "DebugAgent",
        "auth_level": "Auto"
    }
    response = client.post(
        "/v1/debug/execute",
        json=update_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    res_data = response.json()
    assert "updated to DONE" in res_data["result"]

@pytest.mark.asyncio
async def test_mcp_dispatch_execute_unauthorized(client, clean_dispatch_board):
    """測試無 can_supervise_dispatch 權限的一般 Agent 呼叫 debug/execute 操作 DispatchAgent 看板時，會拋出權限拒絕。"""
    identity = AgentIdentity.for_role(
        agent_id="MockUnauthorizedAgent",
        role="base",
        capabilities=[]
    )
    token = identity_service.issue_token(identity)

    # 嘗試初始化
    init_payload = {
        "tool_name": "mission_tools",
        "args": {
            "action": "initialize",
            "goal": "Hack goal",
            "target_agent_id": "DispatchAgent"
        },
        "agent_id": "MockUnauthorizedAgent"
    }
    response = client.post(
        "/v1/debug/execute",
        json=init_payload,
        headers={"Authorization": f"Bearer {token}"}
    )
    # 權限拒絕應該在執行層面被攔截回傳，或者拋出 PermissionError
    assert response.status_code == 403

@pytest.mark.asyncio
async def test_direct_mission_manager_authorized(clean_dispatch_board):
    """測試 MissionManager 直接呼叫（整合模式）：Supervisor 可操作 DispatchAgent 看板。"""
    from src.models.identity import AgentIdentity as ProxyIdentity
    identity = ProxyIdentity.for_role(
        agent_id="DebugAgent",
        role="Debugger",
        capabilities=["can_supervise_dispatch"],
    )
    manager = MissionManager()
    mission = await manager.initialize_mission(
        identity=identity,
        goal="Direct MissionManager authorized test",
        initial_tasks=[{"id": "t_dm", "description": "direct manager task"}],
        target_agent_id="DispatchAgent",
    )
    assert mission.goal == "Direct MissionManager authorized test"

    board = await manager.get_current_mission(identity, target_agent_id="DispatchAgent")
    assert board is not None
    assert "t_dm" in [t.id for t in board.tasks]


@pytest.mark.asyncio
async def test_direct_mission_manager_unauthorized(clean_dispatch_board):
    """測試 MissionManager 直接呼叫：無 can_supervise_dispatch 應拋出 PermissionError。"""
    from src.models.identity import AgentIdentity as ProxyIdentity
    identity = ProxyIdentity.for_role(
        agent_id="MockUnauthorizedAgent",
        role="Base",
        capabilities=[],
    )
    manager = MissionManager()
    with pytest.raises(PermissionError):
        await manager.initialize_mission(
            identity=identity,
            goal="Unauthorized attempt",
            target_agent_id="DispatchAgent",
        )

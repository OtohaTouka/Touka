import inspect

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity



@pytest.fixture
def mission_manager():
    return MissionManager()

@pytest.fixture
def supervisor_identity():
    # 具有 can_supervise_dispatch 能力的 AgentIdentity
    return AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="base",
        capabilities=["can_supervise_dispatch"]
    )

@pytest.fixture
def regular_identity():
    # 不具備該特權能力的 AgentIdentity
    return AgentIdentity.for_role(
        agent_id="MockUnauthorizedAgent_Test",
        role="base",
        capabilities=[]
    )

@pytest.fixture
def dispatch_identity():
    # DispatchAgent 自己的身分
    return AgentIdentity.for_role(
        agent_id="DispatchAgent",
        role="base",
        capabilities=[]
    )

@pytest.mark.asyncio
async def test_dispatch_board_unauthorized_access(mission_manager, regular_identity):
    """一般未經授權的 Agent 讀寫 DispatchAgent 的看板應拋出 PermissionError。"""
    # 嘗試初始化
    with pytest.raises(PermissionError) as exc_info:
        await mission_manager.initialize_mission(
            identity=regular_identity,
            goal="Hacking dispatch",
            initial_tasks=[],
            target_agent_id="DispatchAgent"
        )
    assert "Permission denied" in str(exc_info.value)

    # 嘗試獲取
    with pytest.raises(PermissionError):
        await mission_manager.get_current_mission(
            identity=regular_identity,
            target_agent_id="DispatchAgent"
        )

    # 嘗試更新
    with pytest.raises(PermissionError):
        await mission_manager.update_task(
            identity=regular_identity,
            task_id="t1",
            status=TaskStatus.DONE,
            target_agent_id="DispatchAgent"
        )

@pytest.mark.asyncio
async def test_dispatch_board_authorized_supervisor_access(mission_manager, supervisor_identity):
    """具備 can_supervise_dispatch 能力的 Agent 可以讀寫 DispatchAgent 的看板。"""
    goal = "Supervisor Test Goal"
    tasks = [
        {
            "id": "t1",
            "description": "Supervised task 1",
            "verification_cmd": "pytest tests/unit/test_dispatch_auth.py",
            "boundary_rules": ["NO_HARDCODED_PATHS"]
        }
    ]

    # 初始化
    mission = await mission_manager.initialize_mission(
        identity=supervisor_identity,
        goal=goal,
        initial_tasks=tasks,
        target_agent_id="DispatchAgent"
    )
    assert mission.goal == goal
    assert len(mission.tasks) == 1
    assert mission.tasks[0].verification_cmd == "pytest tests/unit/test_dispatch_auth.py"
    assert mission.tasks[0].boundary_rules == ["NO_HARDCODED_PATHS"]

    # 獲取看板
    fetched = await mission_manager.get_current_mission(
        identity=supervisor_identity,
        target_agent_id="DispatchAgent"
    )
    assert fetched is not None
    assert fetched.goal == goal
    assert fetched.tasks[0].id == "t1"

    # 更新狀態與測試日誌
    await mission_manager.update_task(
        identity=supervisor_identity,
        task_id="t1",
        status=TaskStatus.DOING,
        target_agent_id="DispatchAgent"
    )
    updated = await mission_manager.update_task(
        identity=supervisor_identity,
        task_id="t1",
        status=TaskStatus.DONE,
        artifact_refs=["src/file.py"],
        test_log="All 3 tests passed.",
        target_agent_id="DispatchAgent"
    )
    assert updated.tasks[0].status == TaskStatus.DONE
    assert updated.tasks[0].artifact_refs == ["src/file.py"]
    assert updated.tasks[0].test_log == "All 3 tests passed."

@pytest.mark.asyncio
async def test_dispatch_board_self_access(mission_manager, dispatch_identity):
    """DispatchAgent 本身可以直接讀寫自己的看板。"""
    goal = "Self Dispatch Goal"
    tasks = [{"id": "t2", "description": "Self task"}]

    # 初始化
    mission = await mission_manager.initialize_mission(
        identity=dispatch_identity,
        goal=goal,
        initial_tasks=tasks,
        target_agent_id="DispatchAgent"
    )
    assert mission.goal == goal

    # 獲取
    fetched = await mission_manager.get_current_mission(
        identity=dispatch_identity,
        target_agent_id="DispatchAgent"
    )
    assert fetched is not None
    assert fetched.goal == goal


def test_dispatch_self_identity_condition_is_unreachable():
    """[Dead Code Regression] 驗證 _get_mission_path 中的死條件已被移除。

    合約規格（MEMORY_INTERFACE.md §C-B）：
    - 當 identity.agent_id == target_agent_id 時，外層條件 `target_agent_id != identity.agent_id`
      直接攔截，resolved_identity = identity 不進入權限分支。
    - 因此，`or identity.agent_id == "DispatchAgent"` 在中層分支內永遠不成立，
      屬於 dead code，應從 source 中移除。

    此測試以靜態方式掃描 source code 確認死條件已刪除。
    """
    source = inspect.getsource(MissionManager._get_mission_path)
    assert 'or identity.agent_id == "DispatchAgent"' not in source, (
        "Dead code detected: `or identity.agent_id == 'DispatchAgent'` "
        "is unreachable inside the `target_agent_id != identity.agent_id` block "
        "and must be removed."
    )

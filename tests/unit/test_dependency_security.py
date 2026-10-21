"""Unit tests for task dependencies and circular dependency validation fixes (TDD)."""

from __future__ import annotations

import pytest

from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity

# 測試用身份
TEST_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="base", capabilities=[])


# ===========================================================================
# 1. initialize_mission 依賴與結構驗證測試
# ===========================================================================

@pytest.mark.asyncio
async def test_initialize_missing_dependency():
    """驗證依賴不存在的任務 ID 會被拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": ["t99"]}  # t99 不存在
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    assert "Dependency 't99' does not exist" in str(exc_info.value)


@pytest.mark.asyncio
async def test_initialize_circular_dependency_direct():
    """驗證直接循環依賴 (A -> B -> A) 會被拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": ["t2"]},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    assert "Circular dependency detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_initialize_circular_dependency_indirect():
    """驗證間接循環依賴 (A -> B -> C -> A) 會被拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": ["t2"]},
        {"id": "t2", "description": "task 2", "dependencies": ["t3"]},
        {"id": "t3", "description": "task 3", "dependencies": ["t1"]},
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    assert "Circular dependency detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_initialize_self_dependency():
    """驗證自依賴 (A -> A) 會被拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": ["t1"]}
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    assert "Circular dependency detected" in str(exc_info.value)


# ===========================================================================
# 2. update_task 狀態流轉依賴驗證測試
# ===========================================================================

@pytest.mark.asyncio
async def test_update_task_unmet_dependency_doing():
    """驗證前置依賴未完成時，更新任務狀態為 DOING 會被拒絕。"""
    manager = MissionManager()
    # 建立看板，t2 依賴 t1
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    state = await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    
    # 嘗試更新 t2 為 DOING，但 t1 還只是 TODO
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_IDENTITY,
            task_id="t2",
            status=TaskStatus.DOING,
        )
    assert "Dependency 't1' is not completed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_update_task_unmet_dependency_done():
    """驗證前置依賴未完成時，更新任務狀態為 DONE 會被 FSM 拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    
    # 嘗試更新 t2 為 DONE，但因為 TODO -> DONE 違規被拒絕
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_IDENTITY,
            task_id="t2",
            status=TaskStatus.DONE,
        )
    assert "Invalid status transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_update_task_met_dependency_allowed():
    """驗證前置依賴完成後，更新任務狀態能被順利放行。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    
    # 1. 先將 t1 更新為 DONE (必須先經過 DOING)
    await manager.update_task(TEST_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_IDENTITY, "t1", TaskStatus.DONE)
    
    # 2. 再次更新 t2 為 DOING 與 DONE，應成功放行
    state = await manager.update_task(TEST_IDENTITY, "t2", TaskStatus.DOING)
    assert state.tasks[1].status == TaskStatus.DOING

    state = await manager.update_task(TEST_IDENTITY, "t2", TaskStatus.DONE)
    assert state.tasks[1].status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_update_task_other_status_without_restriction():
    """驗證更新為 TODO / FAILED / BLOCKED 不需要校驗前置依賴是否完成。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    await manager.initialize_mission(TEST_IDENTITY, "goal", tasks)
    
    # 1. t1 還未完成，但將 t2 更新為 BLOCKED 應放行 (TODO -> BLOCKED)
    # H1 fix: blocker_detail is required for BLOCKED transitions
    state = await manager.update_task(
        TEST_IDENTITY, "t2", TaskStatus.BLOCKED, blocker_detail="waiting on t1"
    )
    assert state.tasks[1].status == TaskStatus.BLOCKED

    # 2. 將沒有依賴的 t1 轉為 DOING 再轉為 FAILED 應放行 (DOING -> FAILED)
    await manager.update_task(TEST_IDENTITY, "t1", TaskStatus.DOING)
    state = await manager.update_task(TEST_IDENTITY, "t1", TaskStatus.FAILED)
    assert state.tasks[0].status == TaskStatus.FAILED

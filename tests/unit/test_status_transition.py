"""Unit tests for task status transition validation (FSM) and privilege restrictions (TDD)."""

from __future__ import annotations

import pytest

from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity

# 測試用身份
TEST_PRIVILEGED_IDENTITY = AgentIdentity.for_role(
    agent_id="DebugAgent", 
    role="Debugger", 
    capabilities=["can_supervise_dispatch"]
)
TEST_NORMAL_IDENTITY = AgentIdentity.for_role(
    agent_id="MockNormalAgent", 
    role="base", 
    capabilities=[]
)


@pytest.mark.asyncio
async def test_valid_transitions():
    """驗證合法狀態轉換：TODO -> DOING -> DONE 與 DOING -> FAILED。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": []},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    # TODO -> DOING
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t1",
        status=TaskStatus.DOING
    )
    assert state.tasks[0].status == TaskStatus.DOING

    # DOING -> DONE
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t1",
        status=TaskStatus.DONE
    )
    assert state.tasks[0].status == TaskStatus.DONE

    # TODO -> DOING
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t2",
        status=TaskStatus.DOING
    )
    assert state.tasks[1].status == TaskStatus.DOING

    # DOING -> FAILED
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t2",
        status=TaskStatus.FAILED
    )
    assert state.tasks[1].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_idempotent_transitions():
    """驗證相同狀態轉換（冪等性）直接被放行。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    # TODO -> TODO
    state = await manager.update_task(
        identity=TEST_NORMAL_IDENTITY,
        task_id="t1",
        status=TaskStatus.TODO
    )
    assert state.tasks[0].status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_invalid_transitions_from_todo():
    """驗證 TODO 狀態無法直接轉為 DONE（必須先經過 DOING）。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_NORMAL_IDENTITY,
            task_id="t1",
            status=TaskStatus.DONE
        )
    assert "Invalid status transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_transitions_from_done():
    """驗證 DONE 狀態無法轉向任何其他狀態。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DONE)

    # 嘗試 DONE -> TODO
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_PRIVILEGED_IDENTITY,
            task_id="t1",
            status=TaskStatus.TODO
        )
    assert "Invalid status transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_transitions_from_failed():
    """驗證 FAILED 狀態無法轉至 DOING。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.FAILED)

    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_PRIVILEGED_IDENTITY,
            task_id="t1",
            status=TaskStatus.DOING
        )
    assert "Invalid status transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failed_reset_privilege_success():
    """驗證特權身份能成功將 FAILED 狀態轉回 TODO。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.FAILED)

    # 特權身份重置 FAILED -> TODO 應成功
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t1",
        status=TaskStatus.TODO
    )
    assert state.tasks[0].status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_failed_reset_privilege_denied():
    """驗證普通身份將自己看板的 FAILED 轉為 TODO 時會被拒絕。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []}
    ]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)
    await manager.update_task(TEST_NORMAL_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_NORMAL_IDENTITY, "t1", TaskStatus.FAILED)

    # 普通身份重置 FAILED -> TODO 應失敗
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_NORMAL_IDENTITY,
            task_id="t1",
            status=TaskStatus.TODO
        )
    assert "Only authorized agents can reset a FAILED task" in str(exc_info.value)


# ===========================================================================
# A6: 突變測試補強 —— VALID_TRANSITIONS / 特權 / BLOCKED / 相依 / 總體狀態
# 額外身份：只有 admin 角色（無 can_supervise_dispatch capability），
# 用來咬住 FAILED->TODO 特權判定的 `or` 兩側。
# ===========================================================================

TEST_ADMIN_ROLE_IDENTITY = AgentIdentity.for_role(
    agent_id="AdminRoleAgent",
    role="admin",
    capabilities=[],
)


@pytest.mark.asyncio
async def test_transition_blocked_to_doing_allowed():
    """BLOCKED -> DOING 是合法轉換，且任務狀態精確變為 DOING。"""
    manager = MissionManager()
    tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    # TODO -> BLOCKED（需 blocker_detail）
    await manager.update_task(
        identity=TEST_NORMAL_IDENTITY,
        task_id="t1",
        status=TaskStatus.BLOCKED,
        blocker_detail="blocked reason",
    )
    # BLOCKED -> DOING
    state = await manager.update_task(
        identity=TEST_NORMAL_IDENTITY,
        task_id="t1",
        status=TaskStatus.DOING,
    )
    assert state.tasks[0].status == TaskStatus.DOING


@pytest.mark.asyncio
async def test_transition_blocked_to_done_forbidden():
    """BLOCKED -> DONE 不在合法集合中，必須拋出 'Invalid status transition'。"""
    manager = MissionManager()
    tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    await manager.update_task(
        identity=TEST_NORMAL_IDENTITY,
        task_id="t1",
        status=TaskStatus.BLOCKED,
        blocker_detail="blocked reason",
    )
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_NORMAL_IDENTITY,
            task_id="t1",
            status=TaskStatus.DONE,
        )
    assert "Invalid status transition" in str(exc_info.value)


@pytest.mark.asyncio
async def test_failed_reset_admin_role_without_capability_succeeds():
    """僅憑 admin 角色（無 can_supervise_dispatch capability）也能重置 FAILED -> TODO。

    咬住特權判定 `or` 的「角色」那一側。
    """
    manager = MissionManager()
    tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    await manager.initialize_mission(TEST_ADMIN_ROLE_IDENTITY, "goal", tasks)
    await manager.update_task(TEST_ADMIN_ROLE_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_ADMIN_ROLE_IDENTITY, "t1", TaskStatus.FAILED)

    state = await manager.update_task(
        identity=TEST_ADMIN_ROLE_IDENTITY,
        task_id="t1",
        status=TaskStatus.TODO,
    )
    assert state.tasks[0].status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_blocked_requires_blocker_detail_raises():
    """轉入 BLOCKED 但未提供 blocker_detail → 'blocker_detail is required'。"""
    manager = MissionManager()
    tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_NORMAL_IDENTITY,
            task_id="t1",
            status=TaskStatus.BLOCKED,
        )
    assert "blocker_detail is required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_blocked_with_detail_sets_status_and_detail():
    """轉入 BLOCKED 並提供 blocker_detail → 成功，狀態與細節精確寫入。"""
    manager = MissionManager()
    tasks = [{"id": "t1", "description": "task 1", "dependencies": []}]
    await manager.initialize_mission(TEST_NORMAL_IDENTITY, "goal", tasks)

    state = await manager.update_task(
        identity=TEST_NORMAL_IDENTITY,
        task_id="t1",
        status=TaskStatus.BLOCKED,
        blocker_detail="awaiting review",
    )
    assert state.tasks[0].status == TaskStatus.BLOCKED
    assert state.tasks[0].blocker_detail == "awaiting review"


@pytest.mark.asyncio
async def test_move_to_doing_with_incomplete_dependency_raises():
    """相依任務未 DONE 時，轉 DOING 必須拋出 'is not completed'。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    # t1 仍是 TODO（未 DONE），t2 轉 DOING 應被擋下
    with pytest.raises(ValueError) as exc_info:
        await manager.update_task(
            identity=TEST_PRIVILEGED_IDENTITY,
            task_id="t2",
            status=TaskStatus.DOING,
        )
    assert "is not completed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_move_to_doing_with_completed_dependency_succeeds():
    """相依任務已 DONE 時，轉 DOING 應成功（咬住相依檢查的放行側）。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": ["t1"]},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    # 先完成 t1
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DONE)

    # 現在 t2 轉 DOING 應成功
    state = await manager.update_task(
        identity=TEST_PRIVILEGED_IDENTITY,
        task_id="t2",
        status=TaskStatus.DOING,
    )
    assert state.tasks[1].status == TaskStatus.DOING


@pytest.mark.asyncio
async def test_overall_status_all_done():
    """所有任務 DONE → 總體 state.status 精確為 DONE。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": []},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DONE)
    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t2", TaskStatus.DOING)
    state = await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t2", TaskStatus.DONE)

    assert state.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_overall_status_any_doing():
    """有任一任務 DOING（非全 DONE）→ 總體 state.status 精確為 DOING。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": []},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    state = await manager.update_task(
        TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING
    )
    # t1=DOING, t2=TODO → 總體 DOING
    assert state.status == TaskStatus.DOING


@pytest.mark.asyncio
async def test_overall_status_any_failed():
    """有 FAILED 但無 DOING、非全 DONE → 總體 state.status 精確為 FAILED。"""
    manager = MissionManager()
    tasks = [
        {"id": "t1", "description": "task 1", "dependencies": []},
        {"id": "t2", "description": "task 2", "dependencies": []},
    ]
    await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)

    await manager.update_task(TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.DOING)
    state = await manager.update_task(
        TEST_PRIVILEGED_IDENTITY, "t1", TaskStatus.FAILED
    )
    # t1=FAILED, t2=TODO → 無 DOING、非全 DONE、有 FAILED → 總體 FAILED
    assert state.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_initialize_mission_circular_dependency_raises():
    """循環相依 → initialize_mission 拋出 'Circular dependency detected'。"""
    manager = MissionManager()
    tasks = [
        {"id": "a", "description": "task a", "dependencies": ["b"]},
        {"id": "b", "description": "task b", "dependencies": ["a"]},
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)
    assert "Circular dependency detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_initialize_mission_missing_dependency_raises():
    """相依到不存在的任務 ID → initialize_mission 拋出 'does not exist'。"""
    manager = MissionManager()
    tasks = [
        {"id": "a", "description": "task a", "dependencies": ["ghost"]},
    ]
    with pytest.raises(ValueError) as exc_info:
        await manager.initialize_mission(TEST_PRIVILEGED_IDENTITY, "goal", tasks)
    assert "does not exist" in str(exc_info.value)

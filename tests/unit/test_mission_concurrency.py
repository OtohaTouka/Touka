import asyncio
import shutil
from pathlib import Path
import pytest
from memory_server import _get_app_data_dir
from memory_server.services.mission_manager import MissionManager
from memory_server.services.mission_lock import _reset_mission_lock_cache
from memory_server.models.mission import TaskStatus
from src.models.identity import AgentIdentity

@pytest.fixture
def clean_dispatch_board():
    # 每次測試前後清理 DispatchAgent 的看板目錄，確保無殘留
    path = Path(_get_app_data_dir()) / "personal_drive" / "DispatchAgent" / "missions"
    _reset_mission_lock_cache()
    if path.exists():
        shutil.rmtree(path)
    yield
    _reset_mission_lock_cache()
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)  # .lock file may be briefly scanned by Windows Defender

@pytest.mark.asyncio
async def test_mission_manager_concurrent_updates_atomic(clean_dispatch_board):
    """驗證在併發寫入時，MissionManager 的鎖機制能保證原子性，防止 TOCTOU 資料覆寫。"""
    # 建立一個測試用的 identity (使用 DebugAgent 且帶有監督權限)
    identity = AgentIdentity.for_role(
        agent_id="DebugAgent",
        role="Debugger",
        capabilities=["can_supervise_dispatch"]
    )
    
    manager = MissionManager()
    
    # 1. 初始化看板，包含兩個任務
    goal = "Concurrent test goal"
    tasks = [
        {"id": "t1", "description": "Task 1"},
        {"id": "t2", "description": "Task 2"}
    ]
    await manager.initialize_mission(identity, goal, tasks, target_agent_id="DispatchAgent")
    
    # 2. Mock 讀取，在其中注入延遲以人為製造並發交疊的機會
    # 我們包裝內部 unlocked 讀取方法
    original_get = getattr(manager, "_get_current_mission_unlocked", manager.get_current_mission)
    
    async def mocked_get(ident, target_id=None):
        # 呼叫原始方法
        res = await original_get(ident, target_id)
        # 讓出 CPU 控制權，模擬讀取後的非同步處理延遲
        await asyncio.sleep(0.05)
        return res
        
    # 動態包裝以放大並發競態
    if hasattr(manager, "_get_current_mission_unlocked"):
        manager._get_current_mission_unlocked = mocked_get
    else:
        # 如果此時尚未重構，則包裝公開方法
        manager.get_current_mission = mocked_get
    
    # 先將任務狀態轉為 DOING，以滿足 FSM 轉換規則
    await manager.update_task(identity, "t1", TaskStatus.DOING, target_agent_id="DispatchAgent")
    await manager.update_task(identity, "t2", TaskStatus.DOING, target_agent_id="DispatchAgent")

    # 3. 同時啟動兩個協程來更新不同的任務
    task1 = manager.update_task(
        identity=identity,
        task_id="t1",
        status=TaskStatus.DONE,
        target_agent_id="DispatchAgent"
    )
    task2 = manager.update_task(
        identity=identity,
        task_id="t2",
        status=TaskStatus.DONE,
        target_agent_id="DispatchAgent"
    )
    
    # 併發執行這兩個更新操作
    await asyncio.gather(task1, task2)
    
    # 還原 mock
    if hasattr(manager, "_get_current_mission_unlocked"):
        manager._get_current_mission_unlocked = original_get
    else:
        manager.get_current_mission = original_get
    
    # 4. 驗證最終狀態：兩個任務都應該是 DONE
    # 如果鎖機制未生效，其中一個 DONE 會被另一個 TODO 覆蓋，導致只有其中一個是 DONE
    final_board = await manager.get_current_mission(identity, target_agent_id="DispatchAgent")
    assert final_board is not None
    assert final_board.tasks[0].status == TaskStatus.DONE, "Task 1 狀態被並發競態覆寫了！"
    assert final_board.tasks[1].status == TaskStatus.DONE, "Task 2 狀態被並發競態覆寫了！"

"""任務看板工具 (Mission Board Tools).

本模組提供任務初始化、進度讀取與狀態更新的功能，實作於 ToukaMemory 中以確保數據主權。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memory_server.services.mission_manager import MissionManager

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

_mission_manager = MissionManager()

async def mission_tools(action: str, **kwargs: Any) -> Any:
    """任務看板工具分發器。

    Args:
        action (str): 執行動作 (init, get, update)。
        identity (AgentIdentity): 代理人身分對象。
        kwargs: 動作所需的額外參數。

    Returns:
        Any: 執行結果字串或字典。
    """
    identity: AgentIdentity = kwargs.get("identity")
    if not identity:
        return "Error: AgentIdentity is required for mission_tools."

    if action == "init":
        goal = kwargs.get("goal", "")
        tasks = kwargs.get("tasks", [])
        state = await _mission_manager.initialize_mission(identity, goal, tasks)
        return f"✅ Mission initialized: {state.mission_id} with {len(state.tasks)} tasks."
    
    if action == "get":
        state = await _mission_manager.get_current_mission(identity)
        if not state:
            return "No active mission found."
        return state.model_dump()

    if action == "update":
        task_id = kwargs.get("task_id")
        status = kwargs.get("status")
        artifact = kwargs.get("artifact_ref")
        if not task_id or not status:
            return "Error: task_id and status are required for update."
            
        try:
            await _mission_manager.update_task(identity, task_id, status, artifact)
        except ValueError as e:
            return f"❌ Update failed: {e}"
        else:
            return f"✅ Task {task_id} updated to {status}."

    return f"Error: Unknown action '{action}' for mission_tools."

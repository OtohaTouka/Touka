"""Phase 2（提案 §9）：mission 級 finalize 驗收原語——TDD.

需求 5 閉環：全 task DONE 後，主 agent 對照原始 goal 最終確認並**記錄結論**。
- `mission_tools(action="finalize")`（無 verdict）→ 驗收清單（goal＋各 task 產出面）；
- `mission_tools(action="finalize", verdict=...)` → 經 `MissionManager.finalize_mission`
  記錄 `final_verdict`／`finalized_at`（僅全 DONE 可記，鎖內原子）；
- 未全 DONE 一律拒絕並列出未完成任務。
- 刻意不自動執行 verification_cmd（安全；證據執行屬 Phase 3 EvidenceCollector）。
"""

from __future__ import annotations

import pytest
from memory_server.models.mission import MissionState, TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.tools.mission_tools import MISSION_TOOLS_SCHEMA, mission_tools

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner")


async def _make_mission(
    identity: AgentIdentity, *, done: bool = True
) -> MissionManager:
    """建兩個 task 的 mission；done=True 時全部推到 DONE，否則 t2 停在 DOING。"""
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="產出一份驗收測試用的報告",
        initial_tasks=[
            {"id": "t1", "description": "寫初稿", "verification_cmd": "pytest -q"},
            {"id": "t2", "description": "校對定稿"},
        ],
    )
    for task_id in ("t1", "t2"):
        await manager.update_task(identity=identity, task_id=task_id, status=TaskStatus.DOING)
        if done or task_id == "t1":
            await manager.update_task(
                identity=identity,
                task_id=task_id,
                status=TaskStatus.DONE,
                artifact_refs=[f"{task_id}.md"],
            )
    return manager


# --------------------------------------------------------------------------- #
# 1. 模型欄位
# --------------------------------------------------------------------------- #


def test_mission_state_finalize_fields_default_none():
    state = MissionState(mission_id="m1", goal="g", agent_id="A")
    assert state.final_verdict is None
    assert state.finalized_at is None


# --------------------------------------------------------------------------- #
# 2. MissionManager.finalize_mission
# --------------------------------------------------------------------------- #


async def test_finalize_mission_records_verdict_and_persists():
    identity = _identity("FinalizeRecord")
    manager = await _make_mission(identity, done=True)

    state = await manager.finalize_mission(identity, "已對照原始 goal 驗收：兩份產出齊備")
    assert state.final_verdict == "已對照原始 goal 驗收：兩份產出齊備"
    assert state.finalized_at is not None
    assert state.finalized_at > 0

    reloaded = await manager.get_current_mission(identity)
    assert reloaded is not None
    assert reloaded.final_verdict == "已對照原始 goal 驗收：兩份產出齊備"


async def test_finalize_mission_rejects_incomplete():
    identity = _identity("FinalizeIncomplete")
    manager = await _make_mission(identity, done=False)

    with pytest.raises(ValueError, match="t2"):
        await manager.finalize_mission(identity, "不該成功")


async def test_finalize_mission_rejects_empty_verdict():
    identity = _identity("FinalizeEmpty")
    manager = await _make_mission(identity, done=True)

    with pytest.raises(ValueError, match="verdict"):
        await manager.finalize_mission(identity, "   ")


# --------------------------------------------------------------------------- #
# 3. mission_tools(action="finalize")
# --------------------------------------------------------------------------- #


async def test_tool_finalize_without_verdict_returns_checklist():
    identity = _identity("FinalizeChecklist")
    await _make_mission(identity, done=True)

    out = str(await mission_tools(action="finalize", identity=identity))
    assert "產出一份驗收測試用的報告" in out  # 原始 goal
    assert "t1" in out
    assert "t2" in out
    assert "pytest -q" in out  # verification_cmd 揭示（但不執行）
    assert "verdict" in out  # 指示下一步以 verdict 記錄結論


async def test_tool_finalize_incomplete_returns_error_listing():
    identity = _identity("FinalizeToolIncomplete")
    await _make_mission(identity, done=False)

    out = str(await mission_tools(action="finalize", identity=identity))
    assert out.startswith("Error")
    assert "t2" in out


async def test_tool_finalize_with_verdict_records():
    identity = _identity("FinalizeToolVerdict")
    manager = await _make_mission(identity, done=True)

    out = str(
        await mission_tools(
            action="finalize", verdict="達成原始目標，產出已核對", identity=identity
        )
    )
    assert "✅" in out

    reloaded = await manager.get_current_mission(identity)
    assert reloaded is not None
    assert reloaded.final_verdict == "達成原始目標，產出已核對"


async def test_tool_finalize_without_mission_errors():
    identity = _identity("FinalizeNoMission")
    out = str(await mission_tools(action="finalize", identity=identity))
    assert out.startswith("Error")


# --------------------------------------------------------------------------- #
# 4. Schema 廣告
# --------------------------------------------------------------------------- #


def test_finalize_action_in_schema():
    params = MISSION_TOOLS_SCHEMA["function"]["parameters"]
    assert "finalize" in params["properties"]["action"]["enum"]
    assert "verdict" in params["properties"]

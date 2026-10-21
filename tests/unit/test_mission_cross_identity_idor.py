"""E03（早期守衛缺口 backlog）— mission board 跨身分 IDOR else 分支測試。

缺口背景（docs/architecture/proposals/early-adr-guard-coverage-gap-backlog.md · E03）：
    ADR-0044 的跨身分存取控制唯一閘＝MissionManager._resolve_target_agent_id 的
    else 分支——對「非 self、非 DispatchAgent」的任意 target 一律 PermissionError。
    既有測試（test_dispatch_auth.py 等）只蓋 target="DispatchAgent" 分支與 self 存取；
    else 分支零測試——變異證明：把該行改成 `return target_agent_id`（放行跨身分
    IDOR），全部 mission 測試照綠。

本檔鎖住 else 分支（manager 層）＋工具層接線（ToukaProxy mission_tools 無過濾
轉發 target_agent_id、拒絕須浮現於工具回覆）：
  1. 一般身分對任意他人看板 init/get/update → PermissionError
  2. 特權收束：can_supervise_dispatch 只授權 DispatchAgent，對任意 B 仍拒
  3. 特權收束：Owner／人類 admin 也不豁免（else 分支無任何例外）
  4. 工具層 get_board/update_status 帶 target_agent_id=B → 錯誤字串浮現、非成功
  5. 陽性對照：self 存取正常（證明測試環境非壞掉才紅）
"""

from __future__ import annotations

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.tools.mission_tools import mission_tools

_VICTIM = "VictimAgent_E03"


@pytest.fixture
def manager() -> MissionManager:
    return MissionManager()


@pytest.fixture
def regular_identity() -> AgentIdentity:
    return AgentIdentity.for_role("MockAgentA_E03", "Base", capabilities=[])


@pytest.fixture
def supervisor_identity() -> AgentIdentity:
    # 具 can_supervise_dispatch：只該授權 DispatchAgent 板，不該外溢到任意 B
    return AgentIdentity.for_role(
        "MockSupervisor_E03", "Base", capabilities=["can_supervise_dispatch"]
    )


# ===========================================================================
# 1. Manager 層：else 分支（非 self、非 DispatchAgent）一律拒絕
# ===========================================================================


@pytest.mark.asyncio
async def test_cross_identity_access_denied_for_regular_agent(manager, regular_identity):
    """一般 Agent 對任意他人看板 init/get/update 全拒（IDOR else 分支）。"""
    with pytest.raises(PermissionError, match=_VICTIM):
        await manager.initialize_mission(
            identity=regular_identity,
            goal="IDOR write attempt",
            initial_tasks=[],
            target_agent_id=_VICTIM,
        )

    with pytest.raises(PermissionError, match=_VICTIM):
        await manager.get_current_mission(
            identity=regular_identity, target_agent_id=_VICTIM
        )

    with pytest.raises(PermissionError, match=_VICTIM):
        await manager.update_task(
            identity=regular_identity,
            task_id="t1",
            status=TaskStatus.DONE,
            target_agent_id=_VICTIM,
        )


@pytest.mark.asyncio
async def test_supervisor_capability_does_not_extend_beyond_dispatch_agent(
    manager, supervisor_identity
):
    """特權收束：can_supervise_dispatch 只涵蓋 DispatchAgent，任意 B 仍拒。"""
    with pytest.raises(PermissionError, match=_VICTIM):
        await manager.get_current_mission(
            identity=supervisor_identity, target_agent_id=_VICTIM
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "role", "is_human"),
    [
        ("MockOwnerSoul_E03", "Owner", False),
        ("mock-human-admin-e03", "admin", True),
    ],
)
async def test_privileged_roles_are_not_exempt_from_cross_identity_gate(
    manager, agent_id, role, is_human
):
    """特權收束：Owner 靈魂／人類 admin 也不豁免——else 分支無任何角色例外。"""
    privileged = AgentIdentity.for_role(agent_id, role, is_human=is_human)
    with pytest.raises(PermissionError, match=_VICTIM):
        await manager.get_current_mission(
            identity=privileged, target_agent_id=_VICTIM
        )


# ===========================================================================
# 2. 工具層接線（ToukaProxy mission_tools）：拒絕須浮現、不得靜默成功
# ===========================================================================


@pytest.mark.asyncio
async def test_tool_layer_get_board_surfaces_cross_identity_denial(regular_identity):
    """get_board 帶 target_agent_id=B：工具回覆須含拒絕訊息、非看板內容。"""
    result = await mission_tools(
        action="get_board", identity=regular_identity, target_agent_id=_VICTIM
    )
    assert isinstance(result, str)
    assert "Permission denied" in result, (
        "工具層未把跨身分拒絕浮現——mission_tools 無自身過濾、"
        "全靠 manager 的 _resolve_target_agent_id else 分支（E03）"
    )
    assert "📋 Mission" not in result


@pytest.mark.asyncio
async def test_tool_layer_update_status_surfaces_cross_identity_denial(regular_identity):
    """update_status 帶 target_agent_id=B：工具回覆須含拒絕訊息、非 ✅ 成功。"""
    result = await mission_tools(
        action="update_status",
        identity=regular_identity,
        target_agent_id=_VICTIM,
        task_id="t1",
        status="DONE",
    )
    assert isinstance(result, str)
    assert "Permission denied" in result
    assert "✅" not in result


# ===========================================================================
# 3. 陽性對照：self 存取正常（測試環境健全性）
# ===========================================================================


@pytest.mark.asyncio
async def test_self_access_still_works(manager, regular_identity):
    """同身分 init/get 正常——證明拒絕測試非環境壞掉造成。"""
    mission = await manager.initialize_mission(
        identity=regular_identity,
        goal="Self board sanity",
        initial_tasks=[{"id": "t1", "description": "own task"}],
    )
    assert mission.goal == "Self board sanity"

    fetched = await manager.get_current_mission(identity=regular_identity)
    assert fetched is not None
    assert fetched.goal == "Self board sanity"

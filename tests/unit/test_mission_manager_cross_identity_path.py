"""跨身分讀看板：路徑只看目標編號（ADR-0325）。

以前看板在代理人的個人空間，跨身分讀（督導讀 DispatchAgent 看板）要先查真實註冊身分，決定落在
``personal_drive/`` 還是 ``custom/<id>/drive/``——以呼叫端臆測的角色定址時，同一個代理人的看板會
分裂在兩個地方。現在看板在 ``<data>/missions/<雜湊>.json``，路徑只是目標編號的函數，和存取者是誰、
角色是什麼無關。
"""

from __future__ import annotations

from memory_server.services.mission_manager import MissionManager, mission_board_path
from src.models.identity import AgentIdentity

_SUPERVISOR = AgentIdentity.for_role(
    agent_id="SupervisorAgent",
    role="Debugger",
    capabilities=["can_supervise_dispatch"],
)


class TestCrossIdentityMissionPathDeterminism:
    """看板路徑必須是 agent_id 的決定論函數，與存取者身分無關。"""

    def test_owner_self_read_and_cross_read_resolve_same_path(self):
        manager = MissionManager()
        owner = AgentIdentity.for_role(agent_id="SoulAgentX", role="Owner")
        self_path = manager._get_mission_path(owner)
        cross_path = manager._get_mission_path(_SUPERVISOR, target_agent_id="SoulAgentX")
        assert self_path == cross_path == mission_board_path("SoulAgentX")

    def test_dispatch_agent_cross_read_matches_dispatch_self_write(self):
        manager = MissionManager()
        dispatch_self = AgentIdentity.for_role(agent_id="DispatchAgent", role="Base")
        self_path = manager._get_mission_path(dispatch_self)
        cross_path = manager._get_mission_path(_SUPERVISOR, target_agent_id="DispatchAgent")
        assert self_path == cross_path == mission_board_path("DispatchAgent")

    def test_the_role_does_not_change_the_path(self):
        manager = MissionManager()
        as_owner = AgentIdentity.for_role(agent_id="RoleAgent", role="Owner")
        as_base = AgentIdentity.for_role(agent_id="RoleAgent", role="Base")
        assert manager._get_mission_path(as_owner) == manager._get_mission_path(as_base)

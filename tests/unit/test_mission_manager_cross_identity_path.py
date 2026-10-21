"""跨身分任務看板路徑一致性回歸測試（MissionManager._get_mission_path）。

既有潛在 bug：`_get_mission_path` 解析「跨身分」看板路徑時，以硬編 role="Base"
的最小身分（舊 `_PathIdentity`）呼叫 `proxy.get_vault_path("personal", ...)`。
而 personal vault 依 `is_toukasoul` 分流：

  - Owner / 靈魂  → data/personal_drive/<id>/
  - Base / 自定義 → data/custom/<id>/drive/

於是同一 agent 的看板，擁有者「自讀」（真實 Owner 身分）與「跨身分讀」（督導）會
解析到**不同目錄**——跨讀讀不到（回 None）或寫到分裂位置。

本測試以 pytest 沙盒（conftest 全域 `MEMORY_APP_DATA_DIR` 隔離）直接驗證
`_get_mission_path` 的路徑決定論不變式：看板物理路徑必須是 agent_id 的函數，
與存取者身分無關。**不在沙盒外呼叫**（`_get_mission_path` 有 make_dir 副作用）。
"""

from __future__ import annotations

import json
from pathlib import Path

from memory_server import config_utils
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity, AgentOverride, IdentityMapConfig
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service


def _register_owner(agent_id: str) -> None:
    """在權限註冊表把 agent_id 標記為真實 Owner（靈魂）身分。

    ADR-0245：覆寫只對靈魂生效，所以先在沙箱建立最小靈魂目錄。停用熱重載，避免註冊表
    被磁碟上的 identity_map.json 於測試期間蓋回。
    """
    soul_dir = Path(config_utils.get_app_data_dir()) / "soul" / agent_id
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "config.json").write_text(json.dumps({"is_toukasoul": True}), encoding="utf-8")
    permission_service.disable_auto_reload()
    permission_service.identity_map = IdentityMapConfig(
        tag_rules={},
        agent_overrides={agent_id: AgentOverride(role="Owner", capabilities=[])},
        default_role="Base",
    )


_SUPERVISOR = AgentIdentity.for_role(
    agent_id="SupervisorAgent",
    role="Debugger",
    capabilities=["can_supervise_dispatch"],
)


class TestCrossIdentityMissionPathDeterminism:
    """看板物理路徑必須是 agent_id 的決定論函數，與存取者身分無關。"""

    def test_owner_self_read_and_cross_read_resolve_same_path(self):
        """真實 Owner 代理的看板：自讀（Owner 身分）與跨身分督導讀必須解析到同一檔案。

        紅燈（修復前）：跨讀以硬編 role="Base" 解析 → data/custom/<id>/drive/missions，
        而擁有者自讀（Owner）→ data/personal_drive/<id>/missions，兩者分裂。
        """
        agent_id = "SoulAgentX"
        _register_owner(agent_id)
        manager = MissionManager()

        # 真實註冊身分（Owner）——擁有者自讀走此身分。
        authoritative = identity_service.get_identity(agent_id)
        assert authoritative.display_role == "Owner"  # 前置條件：註冊表確為 Owner
        self_path = manager._get_mission_path(authoritative)

        # 前置條件：Owner/靈魂看板落在 personal_drive/（分流的「靈魂」側）。
        assert "personal_drive" in self_path.parts, self_path

        # 督導（can_supervise_dispatch）以 target_agent_id 跨身分讀同一看板。
        cross_path = manager._get_mission_path(_SUPERVISOR, target_agent_id=agent_id)

        # 不變式：同一 agent 的看板路徑與存取者無關。
        assert cross_path == self_path, (
            f"跨身分看板路徑分裂：自讀={self_path} 跨讀={cross_path}"
        )

    def test_dispatch_agent_cross_read_matches_dispatch_self_write(self):
        """回歸護欄：DispatchAgent（註冊為 Base）看板不得被 fix 誤搬到 personal_drive。

        Dispatch 伺服器以 role="Base" 的固定身分自寫看板 → data/custom/DispatchAgent/
        drive/missions。督導跨讀必須解析到同一 custom/ 位置（否則督導讀不到、且與
        dispatch 自寫分裂）。此測試修復前後皆應綠燈——確保 fix 不動 DispatchAgent。
        """
        manager = MissionManager()

        # Dispatch 伺服器自寫用的固定身分（role="Base"，見 ToukaDispatch/server.py）。
        dispatch_self = AgentIdentity.for_role(agent_id="DispatchAgent", role="Base")
        self_path = manager._get_mission_path(dispatch_self)
        assert "custom" in self_path.parts, self_path

        cross_path = manager._get_mission_path(
            _SUPERVISOR, target_agent_id="DispatchAgent"
        )
        assert cross_path == self_path, (
            f"DispatchAgent 看板路徑分裂：自寫={self_path} 跨讀={cross_path}"
        )

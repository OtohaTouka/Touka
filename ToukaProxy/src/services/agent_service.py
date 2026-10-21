"""代理人服務 (AgentService) 模組.

作為系統中所有代理人身分的「單一真實來源 (SSOT)」，負責枚舉、識別與載入
靈魂 (Soul)、自定義模型 (Custom Models) 與原生別名 (Registry) 的身分護照.
實施 ID 歸一化 (ID Unification)，確保全系統統一使用簡潔 ID (移除 ToukaSoul 前綴).
"""

import logging
from pathlib import Path
from typing import Any

from src.config import CONFIG_DIR
from src.models.identity import SYSTEM_AGENT_IDS, AgentIdentity
from src.services.custom_model_service import custom_model_service
from src.services.permission_service import permission_service
from src.services.soul_service import soul_service
from src.utils import config_utils, custom_io, soul_io

logger = logging.getLogger(__name__)


_REGISTRY_FILE = "agents_registry.json"


class RegistryUnreadableError(ValueError):
    """agents_registry.json 存在卻讀不出來；訊息固定."""

    def __init__(self) -> None:
        super().__init__("The agent registry exists but could not be read")


class AgentService:
    """代理人生命週期與註冊管理服務.

    負責管理 L0 (原生), L1 (核心靈魂), L2 (自定義模型) 的身分階層，
    並提供統一的身分識別入口 identifier_agent.
    """

    def __init__(self) -> None:
        """初始化代理人服務，設定註冊表路徑指標."""
        config_root = Path(CONFIG_DIR)
        self.registry_path = config_root / "agents_registry.json"
        self.models_path = config_root / "models.json"

    def _load_registry_raw(self) -> dict[str, list[str]]:
        """讀取原始註冊表內容 (Warden SSOT)."""
        return config_utils.read_config_json_with_fallback("agents_registry.json") or {}

    def registered_ids(self) -> set[str]:
        """agents_registry.json 的所有鍵（collection 撞名判定的已知 ID 來源之一）.

        檔案存在卻讀不出來（或格式不對）時丟 ``RegistryUnreadableError``：撞名判定不能確認就拒絕；
        還沒有這個檔（新安裝）是空集合。
        """
        raw = config_utils.read_config_json_with_fallback(_REGISTRY_FILE)
        if raw is None:
            if config_utils.config_file_exists(_REGISTRY_FILE):
                raise RegistryUnreadableError
            return set()
        if not isinstance(raw, dict):
            raise RegistryUnreadableError
        return set(raw)

    def save_registry(self, registry: dict[str, list[str]]) -> None:
        """持久化註冊表至磁碟 (Warden SSOT)."""
        config_utils.save_config_json("agents_registry.json", registry)
        logger.info("✅ Agents registry saved successfully.")

    def sync_with_permissions(self) -> None:
        """執行權限表與註冊表的 SSOT 同步.

        若權限配置中有定義 Agent 映射但註冊表中缺失，則自動執行領卡與補全.
        """
        registry = self._load_registry_raw()
        if not permission_service.roles_config:
            return

        mappings = permission_service.roles_config.agent_mappings

        updated = False

        for agent_id in mappings:
            # 排除已是 Soul 的 Agent (不需要重複註冊在 registry)
            if not soul_service.is_toukasoul(agent_id) and agent_id not in registry:
                registry[agent_id] = [agent_id]  # 預設別名為 ID 本身
                updated = True
                logger.info(f"🔄 Auto-registering agent identity for: {agent_id}")

        if updated:
            self.save_registry(registry)

    def identify_agent(self, potential_id: str) -> dict[str, Any] | None:
        """全系統統一身分識別入口 (Identity Gateway).

        判定輸入的 ID 是否為系統已知的 Agent，並回傳其身分護照 (Identity Passport).
        護照階層：
        - Level 1 (ToukaSoul): 具備實體靈魂目錄與 md 指令.
        - Level 2 (CustomModel): 具備實體自定義設定與工具配置.
        - Level 0 (Registered): 僅有名稱或別名註冊.

        Args:
            potential_id (str): 待識別的 ID 字串.

        Returns:
            dict[str, Any] | None: 包含身分旗標與能力的護照字典，若不認識則回傳 None。
            若 ID 帶有 ToukaSoul 前綴，將自動糾偏為正規 ID。
        """
        if not potential_id:
            return None

        # 優先權 1: 靈魂目錄 (Level 1)
        # 實施 ID 糾偏：若收到帶有 ToukaSoul 前綴的 ID，自動轉為正式 ID (SSOT)
        normalized_id = potential_id
        if potential_id.startswith("ToukaSoul"):
            normalized_id = potential_id[9:]
            logger.debug(f"🔄 Normalizing legacy ID: {potential_id} -> {normalized_id}")

        if soul_service.is_toukasoul(normalized_id):
            return {
                "agent_id": normalized_id,
                "is_toukasoul": True,
                "is_custom": False,
            }

        # 優先權 2: 自定義模型目錄 (Level 2)
        # 為了檢查是否為自定義模型，我們先構建一個臨時身分物件 (SSOT)
        temp_role = permission_service.get_role_key(normalized_id, is_custom=True)
        temp_identity = AgentIdentity.for_role(normalized_id, temp_role)
        
        _, config = custom_model_service.get_model_data(temp_identity)
        if config:
            return {
                "agent_id": normalized_id,
                "is_toukasoul": False,
                "is_custom": True,
                "enabled_tools": config.get("enabled_tools", []),
                **{k: v for k, v in config.items() if k not in ["id", "name", "enabled_tools"]},
            }

        # 優先權 3: agents_registry.json 中有案底 (Level 0)
        registry = self._load_registry_raw()
        if normalized_id in registry:
            return {
                "agent_id": normalized_id,
                "is_toukasoul": False,
                "is_custom": False,
            }

        return None

    def identify_model_target(self, model_name: str) -> dict[str, Any] | None:
        """從模型名稱（``@名稱``、``toukasoul-`` 前綴或原名）解析目標代理的護照。

        MonitorLayer 與計分卡路由共用，同一個模型名稱兩邊認出同一個目標（ADR-0245 修補）。
        """
        low_model = str(model_name).lower()
        potential_id = None
        if low_model.startswith("@"):
            potential_id = model_name[1:]
        elif "toukasoul-" in low_model:
            potential_id = model_name.split("-", 1)[1]
        elif "toukasoul" in low_model:
            potential_id = model_name.replace("toukasoul", "").replace("ToukaSoul", "")
        return self.identify_agent(potential_id or model_name)

    def holds_agent_role(self, identity: str | AgentIdentity) -> bool:
        """ADR-0245：只有靈魂、DebugAgent、DispatchAgent 能有代理角色與能力。

        靈魂判定沿用 ``soul_service.is_toukasoul``（soul 目錄＋config 標記），不做
        ToukaSoul 前綴糾偏——舊前綴 ID 不算靈魂，以免寫進不會生效的覆寫。
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        return agent_id in SYSTEM_AGENT_IDS or soul_service.is_toukasoul(agent_id)

    def resolve_role_and_caps(self, identity_id: str) -> tuple[str, list[str]]:
        """解析 agent 的角色與能力，套用「類型硬綁定」tag_rules（ADR-0147）.

        靈魂→角色的 SSOT 入口：``identify_agent`` 判定類型（soul 目錄→is_toukasoul、
        custom 目錄→is_custom），把旗標交給 ``permission_service.get_role_key`` 的
        ``tag_rules`` 求值（is_toukasoul→Owner；ADR-0245 起自訂模型無規則→Base；``agent_overrides``
        優先於 tag_rules，如 DebugAgent→Debugger）。未知 agent（passport None）→
        無旗標 → ``default_role``（Base）。

        修此之前，``verify_token`` 等站點呼叫 ``get_role_key(agent_id)`` **不帶旗標**，
        休眠的 tag_rules 從不觸發 → 所有靈魂全塌 Base（「Sumi 掉 Owner」的真因）。

        Args:
            identity_id: 目標 agent 識別碼（字串入口，比照 identify_agent/get_identity）。

        Returns:
            tuple[str, list[str]]: (角色代碼, 能力清單)。
        """
        passport = self.identify_agent(identity_id) or {}
        is_toukasoul = bool(passport.get("is_toukasoul"))
        is_custom = bool(passport.get("is_custom"))
        role = permission_service.get_role_key(
            identity_id, is_toukasoul=is_toukasoul, is_custom=is_custom
        )
        caps = permission_service.get_capabilities_by_id(
            identity_id, is_toukasoul=is_toukasoul, is_custom=is_custom
        )
        return role, caps

    def get_all_agents(self) -> list[dict[str, Any]]:
        """獲取系統中所有可用的 Agent 列表 (The Grand Enum).

        整合四種來源：
        1. 核心靈魂 (Soul Directories).
        2. 註冊表別名 (Registry Aliases).
        3. 自定義模型 (Custom Directories).
        4. 模型定義檔 (models.json) 中的特殊物件 (如 ToukaGroup).

        Returns:
            list[dict[str, Any]]: 格式化後的 Agent 物件清單.
        """
        agents_map: dict[str, dict[str, Any]] = {}

        # 1. 從 soul 目錄載入核心靈魂 (Warden Enum)
        soul_ids = soul_io.list_soul_ids()
        for s_id in soul_ids:
            agents_map[s_id] = {
                "id": s_id,
                "name": s_id,
                "type": "soul",
                "is_group": False,
            }

        # 2. 併入 agents_registry.json 的別名
        registry = self._load_registry_raw()
        for agent_id, aliases in registry.items():
            if agent_id in agents_map:
                agents_map[agent_id]["aliases"] = aliases
            else:
                agents_map[agent_id] = {
                    "id": agent_id,
                    "name": agent_id,
                    "aliases": aliases,
                    "type": "registered",
                    "is_group": False,
                }

        # 3. 併入 自定義模型目錄 (Warden Enum)
        custom_ids = custom_io.list_custom_ids()
        for c_id in custom_ids:
            temp_c_identity = AgentIdentity(agent_id=c_id, agent_role="System")
            _, config = custom_model_service.get_model_data(temp_c_identity)
            if config:
                agents_map[c_id] = {
                    "id": c_id,
                    "name": config.get("name", c_id),
                    "type": "custom",
                    "is_group": False,
                    "enabled_tools": config.get("enabled_tools", []),
                }

        # 4. 併入 models.json 中的特殊模型 (如 ToukaGroup)
        models_data = config_utils.read_config_json("models.json")
        if models_data:
            try:
                for model in models_data.get("models", []):
                    m_id = model.get("id")
                    if m_id == "ToukaGroup":
                        agents_map[m_id] = {
                            "id": m_id,
                            "name": model.get("name", "🌍 靈魂群聊 (ToukaGroup)"),
                            "type": "group",
                            "is_group": True,
                        }
                    elif m_id.startswith("ToukaSoul"):
                        # 處理帶有前綴的舊設定 (Legacy Mapping)
                        core_id = m_id[9:]
                        if core_id in agents_map:
                            # 發現對應實體，合併其漂亮的名稱
                            if model.get("name"):
                                agents_map[core_id]["name"] = model["name"]
                        else:
                            # 實體未載入，保留作為佔位符
                            agents_map[core_id] = {
                                "id": core_id,
                                "name": model.get("name", core_id),
                                "type": "soul_placeholder",
                                "is_group": False,
                            }
                    elif m_id in agents_map and model.get("name"):
                        # 針對已存在 Agent，更新為更漂亮的名稱
                        agents_map[m_id]["name"] = model["name"]
            except Exception:
                logger.exception("Error processing model entry from models.json")

        # 5. 為每個 agent 附「有效角色／能力」（tag_rules-aware；ADR-0158 後續）。
        # IdentityMapping UI 過去對無 override 的 agent 一律顯示 default_role（Base），
        # 完全不知 tag_rules（is_toukasoul→Owner，ADR-0147）的存在——新靈魂 runtime
        # 是 Owner、UI 卻顯示「Base＋能力全灰」，誘使人去寫 override（override 蓋
        # tag_rules＝靜默降級，Sumi 缺 can_supervise_dispatch 即此路徑實跡）。此處以
        # 型別旗標帶入 get_role_key/get_capabilities_by_id（override＞tag_rules＞default
        # 的 SSOT 求值），UI 據此顯示真實角色。
        overrides = permission_service.identity_map.agent_overrides
        for agent in agents_map.values():
            a_type = agent.get("type")
            flags = {
                "is_toukasoul": a_type in ("soul", "soul_placeholder"),
                "is_custom": a_type == "custom",
            }
            agent["effective_role"] = permission_service.get_role_key(agent["id"], **flags)
            agent["effective_capabilities"] = permission_service.get_capabilities_by_id(
                agent["id"], **flags
            )
            agent["has_override"] = agent["id"] in overrides

        return list(agents_map.values())

    def delete_agent_registration(self, identity: AgentIdentity) -> bool:
        """從註冊表中移除 Agent 身分 (Level 0).
        
        若該 Agent 僅為註冊別名而非核心靈魂，則執行物理移除。
        
        Args:
            identity (AgentIdentity): 待移除的身分對象.
            
        Returns:
            bool: 是否執行了移除操作.
        """
        agent_id = identity.agent_id
        registry = self._load_registry_raw()
        if agent_id in registry:
            del registry[agent_id]
            self.save_registry(registry)
            logger.info(f"🗑️ Removed agent registration from registry: {agent_id}")
            return True
        return False


# 全系統單例
agent_service = AgentService()

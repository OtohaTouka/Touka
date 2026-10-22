"""權限服務 (PermissionService) 模組.

作為系統中唯一的「權限事實來源 (SSOT)」，負責管理的所有代理人 (Agent) 的工具調用權限、
角色繼承關係、身分標籤轉譯以及基於策略的決策引擎 (PaC).
實作數據驅動架構、高效記憶體快取與熱重載機制.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import get_config_dir
from src.models.identity import SYSTEM_AGENT_IDS, AgentIdentity, IdentityMapConfig
from src.models.permissions import PermissionsConfig, RoleConfig
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig, SafetyRules, ToolRiskConfig
from src.services.audit_service import audit_service
from src.services.isolation.status import isolation_monitor
from src.services.resource_manager import resource_manager
from src.services.soul_service import soul_service
from src.utils.command_safety import has_chaining_metacharacters
from src.utils.permission_defaults import (
    DEFAULT_PERMISSIONS_CONFIG as DEFAULT_PERMISSIONS_DATA,
)
from src.utils.permission_grants import McpGrant, NameGrant, RoleGrants
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from src.models.identity import AgentOverride, IdentityRule
    from src.models.policy import PolicyDecision
    from src.utils.isolation_policy import IsolationLevel

logger = logging.getLogger(__name__)

# P21 fix: lock to prevent concurrent config reload race conditions
_config_reload_lock = threading.Lock()

# --- [SSOT 預設配置常量 (Fallback Constants)] ---

# 原始資料 SSOT 位於零應用相依的 permission_defaults，供 Guard #32 同源掃描。
# 此處只保留 Pydantic 投影，維持既有服務／測試呼叫端的型別合約。
DEFAULT_PERMISSIONS_MODEL = PermissionsConfig.model_validate(DEFAULT_PERMISSIONS_DATA)

DEFAULT_IDENTITY_MAP = IdentityMapConfig(tag_rules={}, agent_overrides={}, default_role="Base")

DEFAULT_AUTH_POLICIES = AuthPoliciesConfig(
    policies=SafetyRules(critical_blacklist=[]),
    # 降級 fallback：僅 research 唯讀豁免、其餘一律 default dangerous（fail-safe）。
    # 完整分類表以 config/auth_policies.json 為 SSOT（Guard #32 驗證）。
    tool_risk=ToolRiskConfig(default="dangerous", tools={"research": "read_only"}),
)

# ADR-0245：Guest 角色退役。使用者自己的舊檔仍可能有 Guest，讀取時略過並記警告、不改檔：
# identity_map 指派給 Guest 的一律當 Base（無工具；不往下落到其他規則，避免升級）；
# permissions 的 Guest 角色與 inherits 裡的 Guest 移除，不當 Subagent 用。
_RETIRED_ROLE = "Guest"
_RETIRED_ROLE_FALLBACK = "Base"

# ADR-0245：靈魂與 SYSTEM_AGENT_IDS 以外的 ID 一律 Base（無工具）、無能力。
_NON_AGENT_ROLE = "Base"


def _retire_guest_role(config: PermissionsConfig, source: Path) -> PermissionsConfig:
    """移除 permissions 的 Guest 角色與 ``inherits`` 裡的 Guest（不改檔）。"""
    roles = dict(config.roles)
    legacy = roles.pop(_RETIRED_ROLE, None)
    inheritors = [key for key, role in roles.items() if _RETIRED_ROLE in role.inherits]
    if legacy is None and not inheritors:
        return config
    for key in inheritors:
        kept = [parent for parent in roles[key].inherits if parent != _RETIRED_ROLE]
        roles[key] = roles[key].model_copy(update={"inherits": kept})
    logger.warning(
        "AUDIT: %s still uses the retired Guest role (ADR-0245); ignored on read, file "
        "unchanged. Rename the role to Subagent and drop Guest from inherits.",
        source,
    )
    return config.model_copy(update={"roles": roles})


def _retire_guest_assignments(identity_map: IdentityMapConfig, source: Path) -> IdentityMapConfig:
    """identity_map 指派給 Guest 的規則一律改以 Base 處理（不改檔）。"""
    retired: list[str] = []

    def _demote(scope: str, rules: dict[str, Any]) -> dict[str, Any]:
        demoted: dict[str, Any] = {}
        for key, rule in rules.items():
            if rule.role == _RETIRED_ROLE:
                retired.append(f"{scope}.{key}")
                demoted[key] = rule.model_copy(update={"role": _RETIRED_ROLE_FALLBACK})
            else:
                demoted[key] = rule
        return demoted

    tag_rules = _demote("tag_rules", identity_map.tag_rules)
    overrides = _demote("agent_overrides", identity_map.agent_overrides)
    default_role = identity_map.default_role
    if default_role == _RETIRED_ROLE:
        retired.append("default_role")
        default_role = _RETIRED_ROLE_FALLBACK
    if not retired:
        return identity_map
    logger.warning(
        "AUDIT: %s assigns the retired Guest role (ADR-0245) at %s; treated as Base on "
        "read, file unchanged.",
        source,
        ", ".join(retired),
    )
    return identity_map.model_copy(
        update={"tag_rules": tag_rules, "agent_overrides": overrides, "default_role": default_role}
    )


@dataclass(frozen=True)
class PermissionConfigHealth:
    """permissions.json 熱重載健康快照。"""

    degraded: bool = False
    reason: str = ""


class PermissionService:
    """動態權限管理服務中樞.

    負責管理系統中所有基於身分的訪問控制 (RBAC) 與策略執行 (PaC).
    實施「預設監獄」原則，所有未明確允許的行為均被攔截.
    """

    _instance: PermissionService | None = None
    # auth_policies 讀不到時的健康快照（每次載入規則檔時重設）
    _policy_config_health: PermissionConfigHealth = PermissionConfigHealth()

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PermissionService:
        """實作執行緒安全的單例模式."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False  # noqa: SLF001
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """重置單例狀態並清除所有快取（主要供測試使用）."""
        logger.info("🛡️ Resetting PermissionService singleton for testing.")
        if cls._instance:
            cls._instance._initialized = False  # noqa: SLF001
            cls._instance.roles_config = None
            cls._instance.identity_map = None
            cls._instance.policies = None
            cls._instance._mtime_cache = {}  # noqa: SLF001
            cls._instance._permission_config_health = PermissionConfigHealth()  # noqa: SLF001
            cls._instance = None

    def reset_for_test(self) -> None:
        """重置當前實例的狀態，並依據最新配置重新初始化 (SSOT)."""
        logger.info("🛡️ Resetting PermissionService instance for testing.")
        self._initialized = False
        self.__init__(force_init=True)

    def __init__(
        self,
        config_path: str | None = None,
        identity_path: str | None = None,
        policy_path: str | None = None,
        force_init: bool = False,
    ) -> None:
        """初始化權限服務，配置實體檔案路徑並執行首次載入.

        Args:
            config_path: permissions.json 路徑.
            identity_path: identity_map.json 路徑.
            policy_path: auth_policies.json 路徑.
            force_init: 是否強制重新初始化.
        """
        if hasattr(self, "_initialized") and self._initialized and not force_init:
            return

        # 初始化路徑 (轉換為 Path 物件)
        config_root = get_config_dir()
        self.config_path = Path(config_path) if config_path else config_root / "permissions.json"
        self.identity_path = (
            Path(identity_path) if identity_path else config_root / "identity_map.json"
        )
        self.policy_path = (
            Path(policy_path) if policy_path else config_root / "auth_policies.json"
        )

        # 初始化快取狀態
        self.roles_config: PermissionsConfig | None = None
        self.identity_map: IdentityMapConfig | None = None
        self.policies: AuthPoliciesConfig | None = None
        self._mtime_cache: dict[Path, float] = {}
        self._last_reload_check = 0.0
        self._check_interval = 2.0
        self._permission_config_health = PermissionConfigHealth()
        self._ignored_rule_warnings: set[str] = set()

        self.reload_all(startup=True)
        self._initialized = True

    @property
    def permission_config_health(self) -> PermissionConfigHealth:
        """回傳不可變的健康快照：permissions.json 熱重載失敗，或 auth_policies 讀不到。"""
        if self._permission_config_health.degraded:
            return self._permission_config_health
        return self._policy_config_health

    def reload_all(self, *, startup: bool = False) -> bool:
        """重新從磁碟讀取並解析所有權限設定檔。

        Args:
            startup: 首次載入時為 True；非空壞檔會直接拋出並拒絕啟動。

        Returns:
            bool: permissions.json 是否採用了本次讀取結果。
        """
        permissions_reloaded = self._load_config(startup=startup)
        self._load_identity_map()
        self._load_policies()
        if permissions_reloaded:
            logger.info("🛡️ PermissionService cache reloaded.")
        else:
            logger.warning("PermissionService reload completed with last-known-good permissions.")
        return permissions_reloaded

    def _should_reload(self, file_path: Path) -> bool:
        """檢查特定設定檔是否與快取相比有變動.

        Args:
            file_path: 目標檔案路徑.

        Returns:
            bool: 是否需要重新載入.
        """
        if not file_path.exists():
            return False

        current_mtime = file_path.stat().st_mtime
        last_mtime = self._mtime_cache.get(file_path, 0.0)

        if current_mtime > last_mtime:
            self._mtime_cache[file_path] = current_mtime
            return True
        return False

    def disable_auto_reload(self) -> None:
        """停用自動熱重載機制（主要供測試使用）."""
        self._check_interval = 999999.0
        self._last_reload_check = get_current_epoch() + 999999.0

    def _auto_reload_check(self) -> None:
        """執行輕量化的熱重載檢查邏輯."""
        with _config_reload_lock:  # P21 fix: prevent concurrent reload race
            self._auto_reload_check_unlocked()

    def _auto_reload_check_unlocked(self) -> None:
        now = get_current_epoch()
        if now - self._last_reload_check < self._check_interval:
            return

        self._last_reload_check = now
        needs_reload = False

        paths = [self.config_path, self.identity_path, self.policy_path]
        for p in paths:
            if p:
                path_obj = Path(p)
                if path_obj.exists() and self._should_reload(path_obj):
                    needs_reload = True

        if needs_reload:
            self.reload_all()

    def _load_config(self, *, startup: bool) -> bool:
        """載入 permissions.json；啟動 fail-fast，熱重載保留 last-known-good。"""
        if not self.config_path.exists():
            self.roles_config = DEFAULT_PERMISSIONS_MODEL
            self._permission_config_health = PermissionConfigHealth()
            return True

        try:
            raw_text = resource_manager.read_file_text(self.config_path).strip()
            if not raw_text or raw_text == "{}":
                self.roles_config = DEFAULT_PERMISSIONS_MODEL
                self._permission_config_health = PermissionConfigHealth()
                return True

            data = json.loads(raw_text)
            parsed = PermissionsConfig.model_validate(data)
        except Exception:
            if startup or self.roles_config is None:
                logger.critical(
                    "Invalid non-empty permissions config at startup: %s",
                    self.config_path,
                    exc_info=True,
                )
                raise

            self._permission_config_health = PermissionConfigHealth(
                degraded=True,
                reason="permissions_config_reload_failed",
            )
            logger.warning(
                "AUDIT: permissions config reload rejected; "
                "retaining last-known-good config from %s",
                self.config_path,
                exc_info=True,
            )
            return False
        else:
            self.roles_config = _retire_guest_role(parsed, self.config_path)
            self._permission_config_health = PermissionConfigHealth()
            return True

    def _load_identity_map(self) -> None:
        """加載 identity_map.json 配置 (身分轉譯核心)."""
        self._ignored_rule_warnings.clear()
        if not self.identity_path.exists():
            self.identity_map = DEFAULT_IDENTITY_MAP
            return

        try:
            raw_text = resource_manager.read_file_text(self.identity_path).strip()
            if not raw_text or raw_text == "{}":
                self.identity_map = DEFAULT_IDENTITY_MAP
                return

            data = json.loads(raw_text)
            self.identity_map = _retire_guest_assignments(
                IdentityMapConfig.model_validate(data), self.identity_path
            )
        except Exception:
            logger.exception("Failed to load identity map from %s", self.identity_path)
            self.identity_map = DEFAULT_IDENTITY_MAP

    def _load_policies(self) -> None:
        """加載 auth_policies.json 配置 (PaC 核心)."""
        self._policy_config_health = PermissionConfigHealth()
        if not self.policy_path.exists():
            self.policies = DEFAULT_AUTH_POLICIES
            return

        try:
            raw_text = resource_manager.read_file_text(self.policy_path).strip()
            if not raw_text or raw_text == "{}":
                self.policies = DEFAULT_AUTH_POLICIES
                return

            data = json.loads(raw_text)
            self.policies = AuthPoliciesConfig.model_validate(data)
        except Exception as exc:
            if self.policies is None and isinstance(exc, OSError):
                # 檔案在卻讀不到、又沒有上一份可用的（啟動）：不知道檔案擋了什麼，不拿預設或
                # .example 頂替（黑名單會變空），維持「未載入」→ evaluate_policy 全部 DENY。
                self._policy_config_health = PermissionConfigHealth(
                    degraded=True, reason="auth_policies_unreadable"
                )
                logger.warning(
                    "AUDIT: auth policies at %s exist but cannot be read; every tool and "
                    "command is denied, agents can only chat. To recover, fix or restore "
                    "the file (compose: docker compose run --rm touka-init first), then "
                    "restart touka-proxy.",
                    self.policy_path,
                    exc_info=True,
                )
                return
            if self.policies is None:
                logger.exception("Failed to load auth policies from %s", self.policy_path)
                self.policies = DEFAULT_AUTH_POLICIES
                return
            # 熱重載讀到壞檔（ADR-0246）：黑名單保留上一份可用的，風險表退回最嚴的
            # 預設——寫壞規則檔不能清掉黑名單，也不會因此放寬任何一項。
            kept = list(self.policies.policies.critical_blacklist)
            self.policies = DEFAULT_AUTH_POLICIES.model_copy(
                update={"policies": SafetyRules(critical_blacklist=kept)}
            )
            logger.warning(
                "AUDIT: auth policies reload rejected; kept %d last-known-good blacklist "
                "patterns from %s",
                len(kept),
                self.policy_path,
                exc_info=True,
            )

    def _agent_rule(
        self, identity: str | AgentIdentity, *, is_toukasoul: bool, is_custom: bool
    ) -> tuple[IdentityRule | AgentOverride | None, bool]:
        """找出套用的 override／tag 規則，並判定此 ID 能否有代理角色（ADR-0245）.

        只有靈魂與 ``SYSTEM_AGENT_IDS`` 套用規則；其他 ID 的 override、tag 規則與非 Base 的
        default_role 一律略過（設定檔不改）；被略過而原本會給出角色或能力的設定項，每項只記
        一次警告。靈魂須呼叫端帶 ``is_toukasoul`` 旗標，且「原始 ID」經
        ``soul_service.is_toukasoul`` 確認（R-2a：``ToukaSoul<靈魂>`` 這類前綴 ID 會被
        ``identify_agent`` 糾偏後帶上旗標，不能只信旗標）。

        Returns:
            tuple: (套用的規則或 None, 此 ID 是否能有代理角色)。
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        overrides = self.identity_map.agent_overrides
        tag_rules = self.identity_map.tag_rules
        rule: IdentityRule | AgentOverride | None = None
        source = "default_role"
        if agent_id in overrides:
            rule, source = overrides[agent_id], f"agent_overrides.{agent_id}"
        elif is_toukasoul and "is_toukasoul" in tag_rules:
            rule, source = tag_rules["is_toukasoul"], "tag_rules.is_toukasoul"
        elif is_custom and "is_custom" in tag_rules:
            rule, source = tag_rules["is_custom"], "tag_rules.is_custom"

        if agent_id in SYSTEM_AGENT_IDS or (
            is_toukasoul and soul_service.is_toukasoul(agent_id)
        ):
            return rule, True
        if rule is not None:
            grants_something = rule.role != _NON_AGENT_ROLE or bool(rule.capabilities)
        else:
            grants_something = self.identity_map.default_role != _NON_AGENT_ROLE
        if grants_something and source not in self._ignored_rule_warnings:
            self._ignored_rule_warnings.add(source)
            logger.warning(
                "AUDIT: %s %s ignored for %s: only souls, DebugAgent and DispatchAgent hold "
                "agent roles (ADR-0245); treated as Base, file unchanged.",
                self.identity_path,
                source,
                agent_id,
            )
        return None, False

    def get_role_key(
        self, identity: str | AgentIdentity, is_toukasoul: bool = False, is_custom: bool = False
    ) -> str:
        """根據身分 ID 與標籤判定所屬角色代碼 (Identity Construction).

        此方法供 IdentityService 在構建 AgentIdentity 時使用。優先序：override＞tag 規則＞
        default_role；只對靈魂、DebugAgent、DispatchAgent 生效，其他 ID 一律 Base（ADR-0245）。

        Args:
            identity: 經驗證的身分對象或原始 ID.
            is_toukasoul: 是否為正式靈魂.
            is_custom: 是否為自定義模型.

        Returns:
            str: 角色代碼 (如 'Owner', 'Base', 'Debugger').
        """
        self._auto_reload_check()

        if isinstance(identity, AgentIdentity):
            agent_id = identity.agent_id
            # 優先從物件的能力標籤中推導
            is_toukasoul = is_toukasoul or ("is_toukasoul" in identity.capabilities)
            is_custom = is_custom or ("is_custom" in identity.capabilities)
        else:
            agent_id = identity

        rule, holds_agent_role = self._agent_rule(
            agent_id, is_toukasoul=is_toukasoul, is_custom=is_custom
        )
        if rule is not None:
            return rule.role
        return self.identity_map.default_role if holds_agent_role else _NON_AGENT_ROLE

    def resolve_grants(self, role_key: str | None) -> RoleGrants:
        """解析角色在全部權限軸上的有效授權（唯一解析器）。

        ADR-0141：工具政策以 ``agent_role`` 為鍵（人類特權走 router 閘、不進工具表）。
        人類／admin 代打身分的 ``agent_role`` 為 ``None``——與拆分前的 ``"admin"``／
        ``"normal"`` 鍵一樣落到空授權（permissions.json 不含 admin／normal 角色）。

        繼承只累加 allow 面；deny 僅保留角色自身規則。需要跨角色生效的禁令由
        ``PermissionsConfig.global_denied_*`` 地板明確表達。

        Args:
            role_key: agent 角色代碼；``None``（人類／代打，無 agent 軀體）→ 空 fallback.

        Returns:
            RoleGrants: 不可變、已套用繼承與全域地板的授權值物件.
        """
        self._auto_reload_check()
        if not self.roles_config:
            self._load_config(startup=True)

        config = self.roles_config or DEFAULT_PERMISSIONS_MODEL
        roles = config.roles

        def _resolve(current_role: str | None) -> RoleGrants:
            role = roles.get(current_role) or RoleConfig(desc="Base fallback")
            grants = RoleGrants(
                tools=NameGrant.from_lists(role.allowed_tools, role.denied_tools),
                mcp=McpGrant.from_lists(
                    role.allowed_mcp_servers,
                    role.denied_mcp_tools,
                ),
                skills=NameGrant.from_lists(role.allowed_skills),
            )
            for parent_key in role.inherits:
                if parent_key in roles:
                    grants = grants.extend(_resolve(parent_key))
            return grants

        resolved = _resolve(role_key)
        return resolved.with_floor(
            tools=NameGrant.from_lists([], config.global_denied_tools),
            mcp=McpGrant.from_lists([], config.global_denied_mcp_tools),
        )

    def is_authorized(
        self,
        identity: AgentIdentity,
        tool_name: str,
        _args: dict[str, Any],
        ctx: dict[str, Any],
    ) -> bool:
        """全系統統一授權入口 (Gate 2) - 基於身分實體對象 (Identity SSOT).

        Args:
            identity: 經驗證的身分對象.
            tool_name: 目標工具名稱.
            _args: 工具調用參數.
            ctx: 調用上下文.

        Returns:
            bool: 是否核准執行.
        """
        grants = self.resolve_grants(identity.agent_role)

        # 1. 檢查工具權限
        if grants.tools.allows(tool_name):
            return True

        # 2. 檢查特定的 MCP 伺服器
        server_name = ctx.get("mcp_server")
        if server_name:
            return grants.mcp.allows_server(server_name)

        return False

    def is_tool_allowed(self, tool_name: str, allowed_tools: NameGrant) -> bool:
        """基礎工具權限過濾檢查 (Gate 1).

        此方法主要用於請求前的 Schema 過濾.

        Args:
            tool_name: 工具名稱.
            allowed_tools: 已解析的工具授權值物件.

        Returns:
            bool: 是否在白名單中.
        """
        return allowed_tools.allows(tool_name)

    def get_allowed_tools(self, identity: AgentIdentity) -> NameGrant:
        """取得工具授權值物件（用於 Gate 1 - Schema Filtering）。

        Args:
            identity: 經驗證的身分對象.

        Returns:
            NameGrant: 與 Gate 2 共用的不可變工具授權。
        """
        return self.resolve_grants(identity.agent_role).tools

    def is_mcp_server_allowed(
        self,
        server_name: str,
        identity: AgentIdentity,
    ) -> bool:
        """判斷是否獲准掛載特定的 MCP 伺服器 (Identity SSOT).

        Args:
            server_name: MCP 伺服器標識碼.
            identity: 經驗證的身分對象.

        Returns:
            bool: 是否獲准.
        """
        return self.resolve_grants(identity.agent_role).mcp.allows_server(server_name)

    def is_mcp_tool_allowed(
        self,
        server_name: str,
        tool_name: str,
        identity: AgentIdentity,
    ) -> bool:
        """判斷是否獲准調用某 MCP 伺服器的**單一工具**（ADR-0195 軸 B，Identity SSOT）。

        與 `is_mcp_server_allowed` 的分工：後者答「掛不掛這台」，本方法答「這台的這個工具
        能不能用」。兩者皆為**身分面**判斷；供給面的 per-installation 政策（軸 A）由
        `ProtocolServerConfig.allows_tool` 獨立執法，**刻意不在此合流**——軸 A 是
        per-installation 供給面，軸 B 則由 `resolve_grants` 統一解析角色權限；兩軸任一
        拒絕即拒絕，且都能約束 Owner。

        Args:
            server_name: MCP 伺服器鍵。
            tool_name: 裸工具名（非 ``server__tool`` 廣告形式）。
            identity: 經驗證的身分對象。

        Returns:
            bool: 是否獲准。
        """
        return self.resolve_grants(identity.agent_role).mcp.allows(server_name, tool_name)

    def can_awaken(self, identity: AgentIdentity) -> bool:
        """判定該 Agent 是否具備心跳自主喚醒權限 (Identity SSOT).

        Args:
            identity: 經驗證的身分對象.

        Returns:
            bool: 是否具備預授權自主喚醒能力.
        """
        return "heartbeat_awakening" in identity.capabilities

    def get_capabilities_by_id(self, identity: str | AgentIdentity, **_kwargs: Any) -> list[str]:
        """根據 ID 與標籤獲取能力清單 (供 IdentityService 使用)."""
        self._auto_reload_check()

        agent_id = identity.agent_id if isinstance(identity, AgentIdentity) else identity

        rule, _ = self._agent_rule(
            agent_id,
            is_toukasoul=bool(_kwargs.get("is_toukasoul")),
            is_custom=bool(_kwargs.get("is_custom")),
        )
        return rule.capabilities if rule is not None else []

    def can_use_skill(self, identity: AgentIdentity, skill_id: str) -> bool:
        """判定該 Agent 是否獲准裝載特定技能 (Identity SSOT).

        Args:
            identity: 經驗證的身分對象.
            skill_id: 核心技能識別碼.

        Returns:
            bool: 是否核准裝載.
        """
        return self.resolve_grants(identity.agent_role).skills.allows(skill_id)

    def _check_pattern(self, text: str, patterns: list[str]) -> bool:
        """檢查文字內容是否匹配安全攔截的正則清單.

        Args:
            text: 原始文字.
            patterns: 正則表達式清單.

        Returns:
            bool: 是否命中攔截模式.
        """
        if not text:
            return False
        for pattern in patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except Exception:
                logger.exception("Invalid regex pattern '%s' in safety check.", pattern)
        return False

    def evaluate_policy(self, context: PolicyContext) -> PolicyDecision:
        """PaC 核心決策入口：將執行上下文橋接至安全策略引擎.

        此方法執行基於層級與正則的高細粒度決策，並自動同步至非同步審計系統.

        Args:
            context: 完整的策略上下文對象.

        Returns:
            PolicyDecision: 包含決策結果 (CONFIRM/REJECT) 與理由的對象.
        """
        self._auto_reload_check()
        if not self.policies:
            from src.models.policy import PolicyDecision as DecisionModel

            # Tier 1 fail-safe：政策未載入時預設 DENY——設定錯誤不得偽裝成正常審批。
            decision = DecisionModel(effect="DENY", reason="Policy not loaded")
        else:
            decision = self.policies.evaluate(context)

        # 記錄審計日誌（非同步）
        audit_service.log_decision(context, decision)

        return decision

    def get_execution_strategy(
        self,
        tool_name: str,
        args: dict[str, Any],
        identity: AgentIdentity,
        ctx: dict[str, Any],
        *,
        isolation: IsolationLevel | None = None,
    ) -> str:
        """獲取執行策略與決議 (Identity SSOT).

        Args:
            tool_name: 工具名稱.
            args: 調用參數.
            identity: 經驗證的身分對象.
            ctx: 上下文.
            isolation: 政策當下的隔離層級；沒給時取代理人層級（monitor 目前的判定，不看專案）.

        Returns:
            str: 決策效果 (如 'CONFIRM', 'REJECT', 'NEED_APPROVAL').
        """
        # 構建策略上下文（PolicyContext）
        context = PolicyContext(
            actor=identity,
            resource=args.get("command", tool_name),
            action="terminal_execute" if "command" in args else "tool_call",
            environment={
                "auth_level": ctx.get("auth_level"),
                "session_id": ctx.get("session_id"),
                "isolation": (
                    isolation if isolation is not None else isolation_monitor.snapshot().level
                ),
            },
        )

        decision = self.evaluate_policy(context)
        return decision.effect

    def classify_command(
        self,
        command: str,
        identity: AgentIdentity,
        auth_level: str = "Manual",
        isolation: IsolationLevel | None = None,
    ) -> str:
        """代理人撰寫指令的終端政策（execute_command 與互審蒐證共用 SSOT）.

        基礎政策回 ALLOW 但含 shell 串接字元時升級為 CONFIRM（C5）。``isolation`` 沒給時取
        代理人層級（見 ``get_execution_strategy``）。
        """
        strategy = self.get_execution_strategy(
            tool_name="bash",
            args={"command": command},
            identity=identity,
            ctx={"auth_level": auth_level},
            isolation=isolation,
        )
        if strategy == "ALLOW" and has_chaining_metacharacters(command):
            logger.info(
                "[Security] Escalating '%s...' to CONFIRM: shell metacharacters detected",
                command[:60],
            )
            return "CONFIRM"
        return strategy

    def get_tool_action_param(self, tool_name: str) -> str | None:
        """聚合多動詞工具的動詞參數名（非聚合／未列名 → None）.

        供執行層把動詞（如 memory_tools 的 action="search"）抽進
        PolicyContext.environment["tool_action"]——審計只記動詞、不記整包 args。
        刻意不觸發 _auto_reload_check（純快取查詢；隨後的 evaluate_policy 會 reload）。
        """
        if not self.policies:
            return None
        return self.policies.tool_risk.action_param_for(tool_name)

    def update_permissions(self, new_config: dict[str, Any]) -> bool:
        """更新並持久化權限設定至 permissions.json (SSOT).

        Args:
            new_config: 原始配置字典.

        Returns:
            bool: 寫入與快取重載是否成功.
        """
        try:
            content = json.dumps(new_config, indent=4, ensure_ascii=False)
            resource_manager.write_file_text(self.config_path, content)
        except Exception:
            logger.exception("Failed to update permissions config")
            return False
        else:
            if not self._load_config(startup=False):
                return False
            logger.info("✅ Permissions updated in %s", self.config_path)
            return True

    def update_identity(self, new_map: dict[str, Any]) -> bool:
        """更新並持久化身分映射至 identity_map.json (SSOT).

        Args:
            new_map: 原始映射字典.

        Returns:
            bool: 寫入與快取重載是否成功.
        """
        try:
            content = json.dumps(new_map, indent=4, ensure_ascii=False)
            resource_manager.write_file_text(self.identity_path, content)
        except Exception:
            logger.exception("Failed to update identity map")
            return False
        else:
            self._load_identity_map()
            logger.info("✅ Identity map updated in %s", self.identity_path)
            return True


# 全系統授權單例 (The Singleton Gate)
permission_service = PermissionService()

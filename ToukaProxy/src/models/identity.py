"""身分主權 (Identity SSOT) 模型定義.

負責全系統已驗證身分的強型別封裝。
實施「身分物件化 (Identity Objectification)」原則：
1. 嚴禁使用原始 `agent_id: str` 進行權限判定或資源解析。
2. 所有下層服務 (Service/Resource) 必須依賴 `AgentIdentity` 物件。
3. 作為「資源監獄 (Resource Prison)」的核心憑證，錨定物理存取主權。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .permissions import BasePolicyModel

# 角色權限面分離（ADR-0141）：兩個互斥的角色值空間。人類使用者只有 admin/normal；
# agent 只有 Owner/Base/Debugger（＋內部專用 Subagent、System）。拆分要達到的不變式＝
# 人類角色永不出現在 agent 欄位、反之亦然（P6-d validator 落實）。
# ADR-0245：Guest 退役；子代理改用專屬的 Subagent（工具清單照抄原 Guest）。
HUMAN_ROLES = frozenset({"admin", "normal"})
AGENT_ROLES = frozenset({"Owner", "Base", "Subagent", "Debugger", "System"})


class CrossPlaneRoleError(ValueError):
    """人類角色出現在 agent 欄位（或反之）——角色權限面分離的型別層不變式（ADR-0141 §7.1）。

    Subclasses ValueError so Pydantic surfaces it as a 422 validation error（並滿足
    RAISE_STRING_BAN 的自訂驗證例外慣例，非裸 ``raise ValueError("...")``）。訊息在類內
    組裝（raise 點只傳越面角色值），滿足 TRY003。
    """

    def __init__(self, offending_role: str) -> None:
        super().__init__(f"role {offending_role!r} violates role-plane purity")


class AgentIdentity(BaseModel):
    """Agent 身分實體 (SSOT)：不可變的身分描述對象 (SOLID - SRP)。

    角色權限面分離（ADR-0141，絞殺者已收官 P7）：角色由**互斥雙欄位**承載——
    ``human_role``（∈ HUMAN_ROLES）與 ``agent_role``（∈ AGENT_ROLES），以 ``is_human``
    區分。有效角色字串一律讀 ``display_role``（顯示面）或走謂詞（is_admin_authority
    等，權限面）；動態建構用 ``for_role(agent_id, role)`` 依值路由。舊的扁平 ``role``
    計算屬性與 ``role=`` 建構相容層已於 P7 移除（跨面純度由 ``_enforce_plane_purity`` 守恆）。
    """
    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(..., description="唯一的代理人識別碼 (sub)")
    human_role: str | None = Field(
        default=None, description="人類角色 (admin/normal)；非人類身分為 None"
    )
    agent_role: str | None = Field(
        default=None,
        description="Agent RBAC 角色 (Owner/Base/Subagent/Debugger/System)；人類為 None",
    )
    is_human: bool = Field(default=False, description="是否為人類使用者")
    owned_agents: list[str] = Field(
        default_factory=list, description="該使用者擁有的代理人清單 (僅限人類)"
    )
    capabilities: list[str] = Field(
        default_factory=list, description="身分特權標籤 (如 heartbeat_awakening)"
    )

    @property
    def display_role(self) -> str:
        """顯示用的有效角色字串（ADR-0141）：``human_role or agent_role or "Base"``。

        角色權限面分離的**唯一**有效角色讀法（拆分前的扁平 ``role`` 計算屬性已於 P7 移除）。
        純供 log／debug／``ctx["role"]`` 等**顯示面**讀取——**非** ``@computed_field``，不進
        ``model_dump()``／openapi schema。權限判定一律走謂詞（is_admin_authority 等），
        **不得**用本屬性當閘。
        """
        return self.human_role or self.agent_role or "Base"

    # ── 角色權限面謂詞（ADR-0141）：閘改呼叫這些、不再比對扁平 `role` 字串 ──────────
    # P6-d 後 `agent_role` 結構性不可能為 "admin"（跨面純度 validator），故謂詞收斂為純
    # 雙面判定——admin 權威只讀 `human_role`（含合法的 admin 代打：human_role="admin"／
    # is_human=False，P5 Option A）。

    def is_admin_authority(self) -> bool:
        """人類 admin 權威（admin-only 閘）：human_role=="admin"（含 admin 代打）。"""
        return self.human_role == "admin"

    def is_privileged(self) -> bool:
        """三元組 {Owner, Debugger, admin}：人類 admin 或特權 agent（Owner/Debugger）。"""
        return self.is_admin_authority() or self.agent_role in ("Owner", "Debugger")

    def is_owner_or_admin(self) -> bool:
        """{Owner, admin}（刻意排除 Debugger）：人類 admin 或 agent Owner。"""
        return self.is_admin_authority() or self.agent_role == "Owner"

    def is_admin_or_debugger(self) -> bool:
        """{admin, Debugger}（刻意排除 Owner）：人類 admin 或 agent Debugger。"""
        return self.is_admin_authority() or self.agent_role == "Debugger"

    @classmethod
    def from_str(cls, agent_id: str) -> AgentIdentity:
        """從字串建立身分物件 (SSOT 轉型期輔助)。"""
        return cls(agent_id=agent_id)

    @classmethod
    def for_role(
        cls,
        agent_id: str,
        role: str | None,
        *,
        is_human: bool = False,
        **kwargs: Any,
    ) -> AgentIdentity:
        """依角色**值**路由建構（ADR-0141 P7）：取代拆除中的 legacy ``role=`` 建構子相容。

        ``role ∈ HUMAN_ROLES`` → ``human_role``；``∈ AGENT_ROLES`` → ``agent_role``；
        未知值依 ``is_human`` 提示落面（與 before-validator 的 ``role=`` 分支**逐位元相同**）。
        供**動態**建構（如 ``role=get_role_key(...)`` 值不確定時）安全使用：依值路由確保
        ``"admin"``／``"normal"`` 一定進 human 面（即使 ``is_human=False``＝代打不變式），
        絕不誤路由進 agent 面而繞過 ``is_admin_authority``。``role=None`` → 不填任一角色欄位。
        """
        data: dict[str, Any] = {"agent_id": agent_id, "is_human": is_human, **kwargs}
        if role is None:
            pass
        elif role in HUMAN_ROLES:
            data["human_role"] = role
        elif role in AGENT_ROLES:
            data["agent_role"] = role
        elif is_human:
            data["human_role"] = role
        else:
            data["agent_role"] = role
        return cls(**data)

    @model_validator(mode="before")
    @classmethod
    def validate_from_str(cls, value: Any) -> Any:
        """字串轉型（Identity Objectification）：``str`` → ``{"agent_id": …}``。

        角色權限面分離（ADR-0141 P7 收官）：舊式扁平 ``role=`` 建構相容映射已移除——
        角色一律經 ``for_role``（依值路由）或顯式 ``human_role=``/``agent_role=`` 指定。
        Guard #41 於 src 端 AST 禁止 ``AgentIdentity(role=…)`` 建構與 ``.role`` 存取。
        """
        if isinstance(value, str):
            return {"agent_id": value}
        return value

    @model_validator(mode="after")
    def _enforce_plane_purity(self) -> AgentIdentity:
        """ADR-0141 §7.1／P6-d：跨面純度不變式——人類角色不可出現在 agent 欄位、反之亦然。

        只擋**跨面污染**（``human_role ∈ AGENT_ROLES`` 或 ``agent_role ∈ HUMAN_ROLES``），
        不強制窮舉合法值——未知/自訂 agent 角色（identity_map 舊資料）仍可存於 ``agent_role``。
        效果：``agent_role == "admin"`` 結構性不可能，故謂詞的過渡條款成死碼、已單點移除。
        合法 admin 代打（``human_role="admin"``、is_human=False）不受影響（admin ∉ AGENT_ROLES）。
        """
        if self.human_role is not None and self.human_role in AGENT_ROLES:
            raise CrossPlaneRoleError(self.human_role)  # human_role 不可為 agent 角色
        if self.agent_role is not None and self.agent_role in HUMAN_ROLES:
            raise CrossPlaneRoleError(self.agent_role)  # agent_role 不可為人類角色
        return self


class IdentityRule(BaseModel):
    """身份標籤規則"""
    role: str
    capabilities: list[str] = Field(default_factory=list)

class AgentOverride(BaseModel):
    """特定代理人的身分覆蓋"""
    role: str
    capabilities: list[str] = Field(default_factory=list)

class IdentityMapConfig(BasePolicyModel):
    """完整身份映射配置 (用於 API 驗證)"""
    tag_rules: dict[str, IdentityRule]
    agent_overrides: dict[str, AgentOverride]
    default_role: str = "Base"


# ADR-0141 P6-a：identity_map（tag_rules/agent_overrides）只能指派「可指派的 AGENT 角色」＝
# AGENT_ROLES 扣除內部專用、不可經 map 指派的 Subagent（ADR-0245，只給子代理）與 System。
# 人類角色（admin/normal）**永不可**經 identity_map 指派給 agent（否則等同開後門把 agent
# 提升成 admin）。取代拆分前含 admin 的矛盾聯集 VALID_ROLES（DA2 白名單）。
ASSIGNABLE_AGENT_ROLES = AGENT_ROLES - frozenset({"Subagent", "System"})

# ADR-0245：能有代理角色與能力的只有靈魂和這兩個系統代理人；其他 ID（自訂模型、群聊、
# 任意字串）的 override／tag 規則一律不套用，固定 Base、無能力。
SYSTEM_AGENT_IDS = frozenset({"DebugAgent", "DispatchAgent"})


class InvalidIdentityRole(ValueError):
    """Raised when an identity-map role is outside the RBAC whitelist.

    Subclasses ValueError so Pydantic surfaces it as a 422 validation error,
    while satisfying RAISE_STRING_BAN (custom validator exception, per the
    ADR-0042 precedent — not a raw `raise ValueError("...")`).
    """


class IdentityMapUpdateRequest(IdentityMapConfig):
    """API request model for POST /identity with role-whitelist validation.

    Validation lives ONLY here (the API boundary). On-disk identity_map.json is
    still loaded via the unvalidated IdentityMapConfig, so a pre-existing custom
    role can never be silently reset to the default by the loader's fallback.
    """

    @model_validator(mode="after")
    def _validate_roles(self) -> IdentityMapUpdateRequest:
        roles = [rule.role for rule in self.tag_rules.values()]
        roles += [ov.role for ov in self.agent_overrides.values()]
        roles.append(self.default_role)
        invalid = sorted({r for r in roles if r not in ASSIGNABLE_AGENT_ROLES})
        if invalid:
            raise InvalidIdentityRole(
                f"invalid role(s) {invalid}; allowed: {sorted(ASSIGNABLE_AGENT_ROLES)}"
            )
        return self

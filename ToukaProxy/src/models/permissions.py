"""系統權限設定模型 (Permissions Configurations).

定義 RBAC 角色配置、繼承關係與全域權限策略 (PaC)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BasePolicyModel(BaseModel):
    """具備元數據支援的政策基礎模型 (PaC)"""
    version: str | None = Field(None, description="策略版本 (e.g. 1.0.0)")
    author: str | None = Field(None, description="策略建立者")
    created_at: float | None = Field(None, description="建立時間 (UNIX Epoch)")
    updated_at: float | None = Field(None, description="更新時間 (UNIX Epoch)")
    description: str | None = Field(None, description="策略描述")

class RoleConfig(BaseModel):
    """RBAC 角色配置

    註：原 ``default_auth_level`` 欄位已拆除（hitl-unification H1）——後端執行期從未
    讀取（死設定）；HITL 等級的 SSOT 是 per-session 的 ``user_sessions.auth_level``。
    """
    desc: str | None = None
    inherits: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_mcp_servers: list[str] = Field(default_factory=list)
    allowed_skills: list[str] = Field(default_factory=list)
    denied_mcp_tools: list[str] = Field(
        default_factory=list,
        description=(
            "MCP 工具面拒絕清單（ADR-0195 軸 B），形如 `server/tool`、`server/*` 或 `*`。"
            "P2 起只對該角色自身生效；跨角色禁令請使用 global_denied_mcp_tools。"
        ),
    )

class SystemConfig(BaseModel):
    """權限系統全域配置"""
    default_role: str = "Base"
    allow_unregistered_agents: bool = True

class PermissionsConfig(BasePolicyModel):
    """完整權限配置 (SSOT)"""
    system_config: SystemConfig = Field(default_factory=SystemConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    global_denied_tools: list[str] = Field(
        default_factory=list,
        description="全系統原生工具拒絕地板；對所有角色（含 Owner）生效。",
    )
    global_denied_mcp_tools: list[str] = Field(
        default_factory=list,
        description="全系統 MCP 工具拒絕地板；對所有角色（含 Owner）生效。",
    )
    agent_mappings: dict[str, str] = Field(default_factory=dict)
    mcp_servers: list[str] = Field(default_factory=list)

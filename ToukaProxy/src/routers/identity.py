"""身分映射路由 (Identity Router)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import (
    AgentIdentity,  # noqa: TC001
    AgentOverride,  # noqa: TC001
    IdentityMapConfig,  # noqa: TC001
    IdentityMapUpdateRequest,  # noqa: TC001
)
from src.services.agent_service import agent_service
from src.services.authorization_gateway import require_admin_authority, require_privileged
from src.services.permission_service import permission_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/identity", tags=["identity"])

# Require Owner/Debugger/admin to READ identity mappings.
_require_identity_role = require_privileged


# DA2: WRITING the identity map (which defines who is admin) requires admin.
#
# A lower-privilege Owner must not be able to promote any agent to admin.
_require_admin_only = require_admin_authority


def _split_overrides(
    overrides: dict[str, AgentOverride],
) -> tuple[dict[str, AgentOverride], dict[str, AgentOverride]]:
    """依 ADR-0245 把覆寫分成（能有代理角色的 ID, 其他 ID）兩份。"""
    held: dict[str, AgentOverride] = {}
    ignored: dict[str, AgentOverride] = {}
    for agent_id, override in overrides.items():
        target = held if agent_service.holds_agent_role(agent_id) else ignored
        target[agent_id] = override
    return held, ignored


@router.get("")
async def get_identity_map(
    _identity: AgentIdentity = Depends(_require_identity_role),  # noqa: B008
) -> IdentityMapConfig:
    """獲取目前的身分映射設定；只列靈魂、DebugAgent、DispatchAgent 的覆寫（ADR-0245）。"""
    current = permission_service.identity_map
    held, _ = _split_overrides(current.agent_overrides)
    return current.model_copy(update={"agent_overrides": held})


@router.post("")
async def update_identity_map(
    new_map: IdentityMapUpdateRequest,
    _identity: AgentIdentity = Depends(_require_admin_only),  # noqa: B008
) -> dict:
    """更新身分映射設定。

    ADR-0245：覆寫只能寫給靈魂、DebugAgent、DispatchAgent，其他 ID 一律 422。檔內原有、
    讀取時被略過的舊覆寫不在畫面上，存檔時原樣保留（不替 Owner 改設定檔）。
    """
    _, rejected = _split_overrides(new_map.agent_overrides)
    if rejected:
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override="Only souls, DebugAgent and DispatchAgent can have identity overrides.",
            rejected_agent_ids=sorted(rejected),
        )
    _, kept_legacy = _split_overrides(permission_service.identity_map.agent_overrides)
    payload = new_map.model_dump()
    payload["agent_overrides"] = {
        **{agent_id: rule.model_dump() for agent_id, rule in kept_legacy.items()},
        **payload["agent_overrides"],
    }
    success = permission_service.update_identity(payload)
    if success:
        return {"status": "success", "message": "Identity map updated successfully."}
    raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED)


@router.get("/agents")
async def get_all_agents(
    _identity: AgentIdentity = Depends(_require_identity_role),  # noqa: B008
) -> list:
    """列出能有代理角色的 Agent（靈魂、DebugAgent、DispatchAgent；ADR-0245），供 UI 下拉選單。"""
    return [a for a in agent_service.get_all_agents() if agent_service.holds_agent_role(a["id"])]

"""工具權限設定路由 (Permissions Router)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request  # noqa: TC002

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.models.permissions import PermissionsConfig  # noqa: TC001
from src.services.agent_service import agent_service
from src.services.authorization_gateway import require_admin_authority
from src.services.permission_service import permission_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/permissions", tags=["permissions"])

# Grandfather Batch-1 fix: permissions management requires admin
# Require admin role to read/write permission configurations.
_require_admin = require_admin_authority


@router.get("")
async def get_permissions(
    request: Request,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> dict:
    """獲取目前的工具權限設定 (SSOT 來源)，並注入當前活躍的 MCP 伺服器清單"""
    try:
        config = permission_service.roles_config.model_dump()

        protocol_manager = request.app.state.context.get("protocol_manager")
        if protocol_manager:
            config["mcp_servers"] = list(protocol_manager.registry.servers.keys())
        else:
            config["mcp_servers"] = []

    except Exception as e:
        logger.exception("Error in get_permissions")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return config


@router.post("")
async def update_permissions(
    new_config: PermissionsConfig,
    _identity: AgentIdentity = Depends(_require_admin),  # noqa: B008
) -> dict:
    """更新工具權限設定"""
    success = permission_service.update_permissions(new_config.model_dump())
    if success:
        try:
            agent_service.sync_with_permissions()
        except Exception:
            logger.exception("Failed to sync agents after permission update")
        return {"status": "success", "message": "Permissions updated and agents synchronized."}
    raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED)

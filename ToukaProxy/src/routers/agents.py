"""代理人路由器，負責提供可用的代理人清單。"""

from fastapi import APIRouter, Depends

from src.models.user import User

from ..services.agent_service import agent_service
from .auth import get_current_user

router = APIRouter(prefix="/agents", tags=["agents"])

@router.get("")
async def list_agents(current_user: User = Depends(get_current_user)):
    """獲取系統中所有可用的 Agent 名單 (SSOT)"""
    agents = agent_service.get_all_agents()
    if current_user.role == "admin":
        return agents
    
    # Filter for normal users
    return [a for a in agents if a["id"] in current_user.owned_agents or a.get("type") not in ("soul", "custom")]

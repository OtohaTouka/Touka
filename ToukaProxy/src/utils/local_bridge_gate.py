"""誰拿得到本機檔案工具（ADR-0311）.

條件現在和「使用者各自授權的 MCP」相同，但刻意另寫一份：MCP 那邊放寬時，這裡不跟著放寬。
只看伺服器設定的 ctx 欄位，不讀模型參數或客戶端標頭。純函式（禁令 18）。
"""

from __future__ import annotations

from typing import Any, Final

from src.models.identity import AgentIdentity
from src.utils.mcp_request_identity import GROUP_MEMBER_FLAG, GROUP_MODEL
from src.utils.presence import is_human_present

AVAILABILITY_OFFERED: Final = "offered"
AVAILABILITY_OFFLINE: Final = "offline"
AVAILABILITY_HIDDEN: Final = "hidden"


def local_files_allowed(ctx: Any, *, request_model: str | None = None) -> bool:
    """人在場、和靈魂的前景對話，而且知道背後是哪個人。"""
    identity = ctx.get("identity")
    return (
        isinstance(identity, AgentIdentity)
        and not identity.is_human
        and identity.agent_id == ctx.get("agent_id")
        and bool(ctx.get("is_toukasoul"))
        and is_human_present(ctx)
        and request_model != GROUP_MODEL
        and not ctx.get(GROUP_MEMBER_FLAG)
        and bool(ctx.get("human_principal_id"))
    )


def local_files_availability(
    ctx: Any,
    *,
    request_model: str | None,
    pairing: Any,
    online: bool,
) -> str:
    """列出工具（offered）、不列但告訴配對本人原因（offline）、什麼都不說（hidden）。"""
    if (
        pairing is None
        or not local_files_allowed(ctx, request_model=request_model)
        or ctx.get("human_principal_id") != pairing.human_id
    ):
        return AVAILABILITY_HIDDEN
    return AVAILABILITY_OFFERED if online else AVAILABILITY_OFFLINE

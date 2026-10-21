"""依身分把「vault-jailed」MCP 伺服器的檔案根目錄注入為當下 agent 自己的 vault（SSOT）。

某些 MCP 伺服器（如 mcp-docgen）的 read 工具接受一個「輸入根目錄」參數，用來把檔案存取鎖在
給定目錄內。為實現 **per-agent 隔離**，派發前由此處依**已驗證身分**把該參數覆寫成「當下 agent
自己的 vault 根」——模型給的值一律被蓋掉（override，非「缺則補」），故模型無法指向他人 vault。

哪些伺服器受管、其根目錄參數叫什麼，由 ``ProtocolServerConfig.vault_jail_arg`` 衍生（SSOT），
不在此硬寫伺服器名。這是可泛化的「vault-jailed MCP」機制——未來其他檔案 MCP 設定 ``vault_jail_arg``
即同享 per-agent 隔離。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.exceptions import ErrorCode, ToukaException
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.protocols.registry import ProtocolRegistry

logger = logging.getLogger(__name__)


def _assert_safe_agent_segment(seg: str) -> None:
    """agent_id 直接用於組 vault 路徑，必須為**單一安全路徑段**（拒 ``..``/分隔符/空值）。

    否則惡意/被污染的 agent_id 會讓注入的 base 逃出自己 vault、讀到他人資料（縱深防禦，
    鏡像 ``upload_service`` 的同名守衛）。
    """
    name = (seg or "").strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)


def resolve_vault_base(identity: AgentIdentity) -> str | None:
    """當下 agent 自己的 personal vault 根（與 ``access_personal_drive`` 同一監獄）。

    上傳端點存的 ``file_ref`` relpath（``outputs/uploads/...``）即相對此根，故工具帶該 relpath、
    base 帶此根，組合後正好落在該 agent 自己的檔案上。
    """
    _assert_safe_agent_segment(identity.agent_id)
    return resource_manager.get_vault_path("personal", identity)


def apply(
    registry: ProtocolRegistry,
    server_name: str,
    arguments: dict[str, Any],
    identity: AgentIdentity,
) -> None:
    """就地把 vault-jailed 伺服器的根目錄參數覆寫成當下 agent vault 根（in-place 改 ``arguments``）。

    非 vault-jailed 伺服器（設定無 ``vault_jail_arg``）一律不動。fail-closed：vault-jailed
    卻解不出 vault 根 → 拋例外、不放行（絕不讓模型給的根目錄生效）。
    """
    cfg = registry.get_server(server_name)
    jail_arg = getattr(cfg, "vault_jail_arg", None) if cfg else None
    if not jail_arg:
        return
    base = resolve_vault_base(identity)
    if not base:
        # 解不出 vault → 寧可拒，也不讓模型/預設根目錄讀到非預期位置。
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    arguments[jail_arg] = base  # 覆寫（模型給的值被蓋掉，無法指向他人 vault）
    # 派發層對所有 MCP 工具注入的 agent_id，外部檔案 MCP 並未宣告——移除以免引數 schema 衝突。
    arguments.pop("agent_id", None)

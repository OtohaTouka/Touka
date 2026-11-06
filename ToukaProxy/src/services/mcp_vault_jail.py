"""vault-jailed MCP 伺服器的工具在 Touka 這端以固定原因拒絕（ADR-0293；ADR-0115 修訂）.

某些 MCP 伺服器（如 mcp-docgen）的工具接受一個「根目錄」參數（``vault_jail_arg``），以前派發前
把它覆寫成當下 agent 的個人空間根。這類伺服器自己的讀寫不經 ResourceManager：交出的根是連結時
會跟著走；而 docgen 的 ``base_dir`` 同時是讀與寫的根，交別的資料夾會讓產出落錯地方，不注入又會
用預設的整個 data 根。所以 ADR-0293 起不再交出任何根：設了 ``vault_jail_arg`` 的伺服器（stdio、
HTTP、經不經隔離都一樣），它的工具一律回「目前不能用」、不送出。安全的讀寫另開任務。

哪些伺服器受管由 ``ProtocolServerConfig.vault_jail_arg`` 衍生（SSOT），不在此硬寫伺服器名。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.protocols.registry import ProtocolRegistry

VAULT_JAILED_REASON = "vault_jailed_tool_unavailable"


class VaultJailedToolUnavailableError(Exception):
    """設了 ``vault_jail_arg`` 的伺服器：這個工具目前不能用（固定原因，不送出）."""

    MESSAGE = "This tool cannot be used right now."
    reason = VAULT_JAILED_REASON

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


def apply(registry: ProtocolRegistry, server_name: str) -> None:
    """``server_name`` 設了 ``vault_jail_arg`` 就丟 ``VaultJailedToolUnavailableError``.

    沒設的伺服器與不認得的名字一律不動；不解析、不回傳任何路徑。
    """
    cfg = registry.get_server(server_name)
    if cfg is not None and getattr(cfg, "vault_jail_arg", None):
        raise VaultJailedToolUnavailableError

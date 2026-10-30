"""vault-jailed MCP：設了 ``vault_jail_arg`` 的伺服器一律以固定原因拒絕（ADR-0293）。

不再交出任何根（不呼叫 ``get_vault_path``，也沒有回傳路徑的函式）；拒絕靠的就是
``vault_jail_arg``，所以設定 API 與快照不可丟失這個欄位的守衛照舊。只用 sandbox，零真實資料變更。
"""
from __future__ import annotations

import pytest
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers.mcp import MCPServerItem, _preserve_masked_env, get_mcp_config_path
from src.services import mcp_vault_jail as J
from src.services.resource_manager import resource_manager
from src.services.settings_snapshot_service import _clean_mcp_servers


class _Reg:
    """最小假註冊表：name → ProtocolServerConfig。"""

    def __init__(self, servers: dict[str, ProtocolServerConfig]) -> None:
        self._servers = servers

    def get_server(self, name: str) -> ProtocolServerConfig | None:
        return self._servers.get(name)


def _docgen_reg() -> _Reg:
    cfg = ProtocolServerConfig(command="uvx", args=["mcp-docgen"], vault_jail_arg="base_dir")
    return _Reg({"mcp-docgen": cfg})


def test_protocol_server_config_accepts_vault_jail_arg():
    cfg = ProtocolServerConfig(command="x", args=[], vault_jail_arg="base_dir")
    assert cfg.vault_jail_arg == "base_dir"
    assert ProtocolServerConfig(command="x", args=[]).vault_jail_arg is None


def test_apply_refuses_every_vault_jailed_server(monkeypatch):
    """設了 vault_jail_arg（stdio 或 HTTP）一律丟固定原因，不解析任何路徑."""
    resolved: list[object] = []
    monkeypatch.setattr(resource_manager, "get_vault_path", lambda *a, **_k: resolved.append(a))
    http = ProtocolServerConfig(url="http://127.0.0.1:9/mcp", vault_jail_arg="base_dir")
    for reg, name in ((_docgen_reg(), "mcp-docgen"), (_Reg({"docs": http}), "docs")):
        with pytest.raises(J.VaultJailedToolUnavailableError) as refused:
            J.apply(reg, name)
        assert refused.value.reason == J.VAULT_JAILED_REASON
        assert str(refused.value) == J.VaultJailedToolUnavailableError.MESSAGE
    assert resolved == []


def test_apply_ignores_non_vault_jailed_server():
    J.apply(_Reg({"other": ProtocolServerConfig(command="x", args=[])}), "other")


def test_apply_unknown_server_is_noop():
    J.apply(_Reg({}), "nope")


def test_no_function_hands_out_a_root():
    assert not hasattr(J, "resolve_vault_base")


# ── 複審加固：MCP 設定 API 不可丟失 vault_jail_arg（否則一次存檔就關閉隔離）──────────
def test_mcp_config_model_roundtrips_vault_jail_arg():
    item = MCPServerItem(command="uvx", args=["mcp-docgen"], vault_jail_arg="base_dir")
    assert item.model_dump()["vault_jail_arg"] == "base_dir"  # GET 不被 extra='forbid' 整份拒
    assert MCPServerItem(command="x", args=[]).vault_jail_arg is None


def test_save_restores_vault_jail_arg_when_client_omits_it():
    incoming = {"mcpServers": {"mcp-docgen": {"command": "uvx", "args": [], "env": None}}}
    existing = {"mcp-docgen": {"command": "uvx", "args": [], "vault_jail_arg": "base_dir"}}
    out = _preserve_masked_env(incoming, existing)
    assert out["mcpServers"]["mcp-docgen"]["vault_jail_arg"] == "base_dir"  # 自磁碟還原


def test_shipped_docgen_config_carries_vault_jail_policy():
    """漂移守衛：出貨 server_config.json 的 docgen 若註冊，必帶 vault_jail_arg。"""
    reg = ProtocolRegistry(str(get_mcp_config_path()))
    cfg = reg.get_server("mcp-docgen")
    if cfg is not None:  # docgen 未必在所有環境註冊；有則必須帶 jail 政策
        assert cfg.vault_jail_arg == "base_dir"


def test_snapshot_import_restores_vault_jail_arg():
    # 外來/舊快照（replace）無 vault_jail_arg → 自目標既有還原，隔離不被清掉。
    incoming = {"mcpServers": {"mcp-docgen": {"command": "uvx", "args": [], "env": {}}}}
    current = {
        "mcpServers": {"mcp-docgen": {"command": "uvx", "args": [], "vault_jail_arg": "base_dir"}}
    }
    out = _clean_mcp_servers(incoming, current)
    assert out["mcpServers"]["mcp-docgen"]["vault_jail_arg"] == "base_dir"

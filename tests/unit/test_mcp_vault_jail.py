"""vault-jailed MCP 派發注入（Phase B）：依身分把受管伺服器的「輸入根目錄」覆寫成 agent vault。

只用 DebugAgent + sandbox，零真實 agent 資料變更。
"""
from __future__ import annotations

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.protocols.registry import ProtocolRegistry, ProtocolServerConfig
from src.routers.mcp import MCPServerItem, _preserve_masked_env, get_mcp_config_path
from src.services import mcp_vault_jail as J
from src.services.settings_snapshot_service import _clean_mcp_servers

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


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


def test_apply_overrides_root_for_vault_jailed_server():
    reg = _docgen_reg()
    args = {"input_path": "outputs/uploads/s/r.pdf", "agent_id": "DebugAgent", "base_dir": "/etc"}
    J.apply(reg, "mcp-docgen", args, DEBUG)
    assert args["base_dir"] == J.resolve_vault_base(DEBUG)  # 覆寫成 vault 根
    assert args["base_dir"] != "/etc"  # 模型給的惡意根被蓋掉（tamper-proof）
    assert "agent_id" not in args  # 外部檔案 MCP 不需要、已移除
    assert args["input_path"] == "outputs/uploads/s/r.pdf"  # 其餘參數不動


def test_apply_ignores_non_vault_jailed_server():
    reg = _Reg({"other": ProtocolServerConfig(command="x", args=[])})
    args = {"x": 1, "agent_id": "DebugAgent"}
    J.apply(reg, "other", args, DEBUG)
    assert args == {"x": 1, "agent_id": "DebugAgent"}  # 完全不動（含 agent_id 保留）


def test_apply_unknown_server_is_noop():
    args = {"x": 1}
    J.apply(_Reg({}), "nope", args, DEBUG)
    assert args == {"x": 1}


def test_resolve_vault_base_rejects_unsafe_agent_id():
    evil = AgentIdentity.for_role(agent_id="../personal_drive/Jin", role="Base")
    with pytest.raises(ToukaException):
        J.resolve_vault_base(evil)


def test_apply_fail_closed_on_unsafe_identity():
    args = {"base_dir": "/whatever"}
    evil = AgentIdentity.for_role(agent_id="../x", role="Base")
    with pytest.raises(ToukaException):
        J.apply(_docgen_reg(), "mcp-docgen", args, evil)


def test_resolve_vault_base_returns_debugagent_vault():
    base = J.resolve_vault_base(DEBUG)
    assert base and "DebugAgent" in base


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

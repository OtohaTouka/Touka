"""軸 A 治理欄位的落盤存活測試（ADR-0195）。

`enabled_tools`／`disabled_tools` 與 `vault_jail_arg` 屬同一族「政策欄位」：任何**不認得它**
的寫入路徑都不得靜默把它沖掉，否則一次例行存檔或一份舊快照就讓 `create_refund` 重新打開
（即 vault_jail_arg 當初那條「jail fails open on routine save」複審 High 的同型缺陷）。

本檔逐一釘死三條寫入路徑：
1. `MCPServerItem` 契約——``extra='forbid'`` 下漏宣告即整份 GET 被打成空設定頁。
2. `_preserve_masked_env`（POST /mcp/ui/config）。
3. `_clean_mcp_servers`（設定快照匯入，含 replace 模式）。
"""
from __future__ import annotations

from src.protocols.registry import ProtocolServerConfig
from src.routers.mcp import MCPServerItem, _preserve_masked_env
from src.services.settings_snapshot_service import _clean_mcp_servers

# --- 契約層 ---------------------------------------------------------------


def test_server_item_roundtrips_governance_fields():
    item = MCPServerItem(
        command="npx", args=["x"], enabled_tools=["a"], disabled_tools=["b"]
    )
    dumped = item.model_dump()
    assert dumped["enabled_tools"] == ["a"]
    assert dumped["disabled_tools"] == ["b"]


def test_server_item_defaults_are_all_open():
    """未設＝全開：既有條目升級後不得整批消失。"""
    item = MCPServerItem(command="npx", args=[])
    assert item.enabled_tools is None
    assert item.disabled_tools == []


def test_protocol_config_defaults_match_api_contract():
    """registry 與 API 兩個 Pydantic 契約的預設值必須一致，否則來回一趟語意就漂移。"""
    cfg = ProtocolServerConfig(command="npx", args=[])
    assert cfg.enabled_tools is None
    assert cfg.disabled_tools == []
    assert cfg.allows_tool("anything") is True


# --- 寫入路徑 1：例行存檔 -------------------------------------------------


def test_save_restores_governance_when_client_omits_them():
    """舊前端／不認得欄位的客戶端存檔一次，治理政策不得消失。"""
    incoming = {"mcpServers": {"stripe": {"command": "npx", "args": [], "env": None}}}
    existing = {
        "stripe": {
            "command": "npx",
            "args": [],
            "enabled_tools": ["list_customers"],
            "disabled_tools": ["create_refund"],
        }
    }
    out = _preserve_masked_env(incoming, existing)["mcpServers"]["stripe"]
    assert out["enabled_tools"] == ["list_customers"]
    assert out["disabled_tools"] == ["create_refund"]


def test_save_accepts_explicit_all_open():
    """``["*"]`` 是「全部開啟」的明示值——這是使用者能真正清掉白名單的唯一途徑。

    送 ``None`` 會被上面那條還原規則接住（刻意如此：不認得欄位的客戶端送的正是 None），
    故 UI 的「全部開啟」必須送 ``["*"]``，不能送 None。
    """
    incoming = {"mcpServers": {"stripe": {"command": "npx", "args": [], "enabled_tools": ["*"]}}}
    existing = {"stripe": {"enabled_tools": ["list_customers"]}}
    out = _preserve_masked_env(incoming, existing)["mcpServers"]["stripe"]
    assert out["enabled_tools"] == ["*"]


def test_save_accepts_explicit_all_closed():
    """``[]`` ＝明示全關，不得被誤判成「未設定」而還原成舊值。"""
    incoming = {"mcpServers": {"stripe": {"command": "npx", "args": [], "enabled_tools": []}}}
    existing = {"stripe": {"enabled_tools": ["list_customers"]}}
    out = _preserve_masked_env(incoming, existing)["mcpServers"]["stripe"]
    assert out["enabled_tools"] == []


# --- 寫入路徑 2：快照匯入 -------------------------------------------------


def test_snapshot_import_restores_governance():
    """匯入 ADR-0195 之前的舊快照（replace 模式）不得把治理政策清掉。"""
    incoming = {"mcpServers": {"stripe": {"command": "npx", "args": [], "env": {}}}}
    current = {
        "mcpServers": {
            "stripe": {
                "command": "npx",
                "args": [],
                "enabled_tools": ["list_customers"],
                "disabled_tools": ["create_refund"],
            }
        }
    }
    out = _clean_mcp_servers(incoming, current)["mcpServers"]["stripe"]
    assert out["enabled_tools"] == ["list_customers"]
    assert out["disabled_tools"] == ["create_refund"]


def test_snapshot_import_does_not_mutate_source():
    """回填必須作用在深拷貝上——就地改寫會汙染呼叫端手上的快照物件。"""
    incoming = {"mcpServers": {"stripe": {"command": "npx", "args": []}}}
    current = {"mcpServers": {"stripe": {"disabled_tools": ["create_refund"]}}}
    _clean_mcp_servers(incoming, current)
    assert "disabled_tools" not in incoming["mcpServers"]["stripe"]

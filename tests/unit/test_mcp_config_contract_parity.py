"""MCP 設定的雙契約 parity 守衛（ADR-0195）。

`server_config.json` 有**兩個**互不相識的 Pydantic 契約：

- `ProtocolServerConfig`（runtime 讀取端）——pydantic 預設 ignore，未宣告的欄位靜默消失。
- `MCPServerItem`（HTTP wire 端）——``extra='forbid'``，未宣告的欄位讓整份
  `MCPConfigResponse.model_validate` 拋 ValidationError，而 GET 端把它吞掉回
  ``{"mcpServers": {}}``：**MCP 設定頁整片空白，只有一行 warning log**。使用者下一次存檔
  就把那份「空」寫回磁碟＝真實資料抹除。

兩者任一漏宣告都不會有任何測試自然亮紅——本檔就是那道機器執法。它同時封死
`vault_jail_arg` 當初那次事故的整個家族（政策欄位在來回中靜默丟失）。
"""

from __future__ import annotations

import json

from src.protocols.registry import ProtocolServerConfig
from src.routers.mcp import (
    MCPConfigResponse,
    MCPServerItem,
    _prepare_config_for_save,
)
from src.utils.mcp_tool_policy import POLICY_FIELDS, carry_over_policy

# runtime 端獨有、刻意不上 wire 的欄位（目前無）。有意排除者列於此並註明理由，
# 空集合代表「兩契約應完全同形」。
_RUNTIME_ONLY: frozenset[str] = frozenset()


def test_wire_contract_covers_every_runtime_field():
    """``MCPServerItem`` 必須涵蓋 ``ProtocolServerConfig`` 的每個欄位。

    漏一個＝磁碟上帶該欄位的設定會讓 GET /mcp/ui/config 回空 dict（設定頁空白）。
    """
    runtime = set(ProtocolServerConfig.model_fields) - _RUNTIME_ONLY
    wire = set(MCPServerItem.model_fields)
    missing = runtime - wire
    assert not missing, (
        f"MCPServerItem 缺少 ProtocolServerConfig 的欄位 {sorted(missing)}——"
        "extra='forbid' 會讓含該欄位的 server_config.json 整份 GET 變成空設定頁。"
    )


def test_runtime_contract_covers_every_wire_field():
    """反向亦然：wire 收得下、runtime 看不見＝欄位存得進磁碟卻永不生效（零錯誤日誌）。"""
    missing = set(MCPServerItem.model_fields) - set(ProtocolServerConfig.model_fields)
    assert not missing, (
        f"ProtocolServerConfig 缺少 MCPServerItem 的欄位 {sorted(missing)}——"
        "設定存得進磁碟但 runtime 完全看不見。"
    )


def test_workload_authority_fields_survive_wire_and_runtime_contracts():
    """P8-B authority selector is policy, not disposable UI state。"""
    full = {
        "url": "https://mcp.example.com/mcp",
        "auth_mode": "oauth_client_credentials",
        "auth_profile_id": "profile-a",
        "workload_authority_kind": "owner_delegation",
        "workload_delegation_id": "delegation-a",
        "workload_source_installation_id": "source-a",
    }

    wire = MCPServerItem.model_validate(full)
    runtime = ProtocolServerConfig.model_validate(full)

    assert wire.workload_authority_kind == "owner_delegation"
    assert wire.workload_delegation_id == "delegation-a"
    assert wire.workload_source_installation_id == "source-a"
    assert runtime.workload_authority_kind == "owner_delegation"
    assert runtime.workload_delegation_id == "delegation-a"
    assert runtime.workload_source_installation_id == "source-a"


def test_policy_fields_declared_on_both_contracts():
    """政策欄位（治理＋隔離）必須同時存在於兩個契約上。"""
    for field in POLICY_FIELDS:
        assert field in ProtocolServerConfig.model_fields, field
        assert field in MCPServerItem.model_fields, field


def test_full_shape_survives_both_contracts():
    """行為級對照：一份帶滿所有欄位的條目，兩個契約都必須收得下且不丟值。

    就算日後有人把上面兩條靜態 parity 放寬，這條仍會亮——GET 端的失效路徑是
    `MCPConfigResponse.model_validate` 拋錯後被吞成空 dict，故直接驗證它不拋。
    """
    full = {
        "command": "npx",
        "args": ["-y", "pkg@1.0.0"],
        "env": {"TOKEN": "x"},
        "vault_jail_arg": "base_dir",
        "enabled": True,
        "enabled_tools": ["a"],
        "disabled_tools": ["b"],
        "protocol_mode": "2026-07-28",
        "auth_mode": "oauth_user",
        "auth_profile_id": "profile-a",
        "capability_policy": {"tools": "allow"},
    }
    wire = MCPConfigResponse.model_validate({"mcpServers": {"srv": full}})
    assert wire.mcpServers["srv"].enabled_tools == ["a"]
    assert wire.mcpServers["srv"].disabled_tools == ["b"]
    assert wire.mcpServers["srv"].vault_jail_arg == "base_dir"
    assert wire.mcpServers["srv"].protocol_mode == "2026-07-28"
    assert wire.mcpServers["srv"].auth_mode == "oauth_user"
    assert wire.mcpServers["srv"].auth_profile_id == "profile-a"
    assert wire.mcpServers["srv"].capability_policy == {"tools": "allow"}

    runtime = ProtocolServerConfig(**json.loads(json.dumps(full)))
    assert runtime.enabled_tools == ["a"]
    assert runtime.disabled_tools == ["b"]
    assert runtime.protocol_mode == "2026-07-28"
    assert runtime.auth_mode == "oauth_user"
    assert runtime.auth_profile_id == "profile-a"
    assert runtime.capability_policy == {"tools": "allow"}
    assert runtime.allows_tool("a") is True
    assert runtime.allows_tool("b") is False


# --- 整條目重寫路徑的政策保全 -------------------------------------------


def test_carry_over_policy_preserves_all_policy_fields():
    """`register_server`／技能安裝／演化部署三條路徑共用的保全函式。"""
    new = {"command": "python", "args": [], "enabled": True}
    old = {
        "enabled_tools": ["a"],
        "disabled_tools": ["b"],
        "vault_jail_arg": "base_dir",
        "protocol_mode": "2026-07-28",
        "auth_mode": "oauth_user",
        "auth_profile_id": "profile-a",
        "capability_policy": {"tools": "allow"},
        "sse_compat_enabled": True,
    }
    out = carry_over_policy(new, old)
    assert out["enabled_tools"] == ["a"]
    assert out["disabled_tools"] == ["b"]
    assert out["vault_jail_arg"] == "base_dir"
    assert out["protocol_mode"] == "2026-07-28"
    assert out["auth_mode"] == "oauth_user"
    assert out["auth_profile_id"] == "profile-a"
    assert out["capability_policy"] == {"tools": "allow"}
    assert "sse_compat_enabled" not in out


def test_partial_ui_save_preserves_omitted_policy_fields():
    """部分設定更新不得用模型預設值覆蓋既有認證與工具政策。"""
    config = MCPConfigResponse.model_validate({"mcpServers": {"srv": {"command": "echo"}}})
    existing = {
        "srv": {
            "command": "echo",
            "protocol_mode": "2026-07-28",
            "auth_mode": "oauth_user",
            "auth_profile_id": "profile-a",
            "capability_policy": {"tools": "allow"},
            "sse_compat_enabled": True,
        }
    }

    saved = _prepare_config_for_save(config, existing)

    assert saved["mcpServers"]["srv"]["protocol_mode"] == "2026-07-28"
    assert saved["mcpServers"]["srv"]["auth_mode"] == "oauth_user"
    assert saved["mcpServers"]["srv"]["auth_profile_id"] == "profile-a"
    assert saved["mcpServers"]["srv"]["capability_policy"] == {"tools": "allow"}
    assert "sse_compat_enabled" not in saved["mcpServers"]["srv"]


def test_carry_over_policy_does_not_override_explicit_values():
    """呼叫端明示設值（含 ``[]`` 明示全關）一律優先於磁碟既值。"""
    out = carry_over_policy({"enabled_tools": []}, {"enabled_tools": ["a"]})
    assert out["enabled_tools"] == []


def test_carry_over_policy_noop_for_new_server():
    out = carry_over_policy({"command": "x"}, None)
    assert out == {"command": "x"}


def test_carry_over_policy_never_mutates_its_inputs():
    """必須回傳副本：就地改寫會讓呼叫端的「原始 config」與「帶回政策後的 entry」變成
    同一個物件，於是用錯變數（例如熱掛載傳原始 config）今天靠別名僥倖正確、日後無聲斷裂，
    且任何測試都殺不掉那個變異。"""
    new = {"command": "python"}
    old = {"disabled_tools": ["create_refund"]}
    out = carry_over_policy(new, old)
    assert out is not new
    assert "disabled_tools" not in new  # 輸入未被污染
    assert out["disabled_tools"] == ["create_refund"]

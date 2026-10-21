"""MCP 政策在重註冊、演化部署與市集安裝的保護。

| 路徑 | 同名覆寫可達？ | 本檔測什麼 |
|---|---|---|
| `mcp_hub_service.register_server` | ✅ upsert，無守衛 | 政策在重註冊後存活 |
| `evolution.request_mcp_creation` | ❌ 已部署就早退 | 保留條目及政策、不熱掛載 |
| `system.request_skill_install` | ❌ 已安裝就早退 | 保留原設定 |

演化在移除舊條目後才能重新部署，仍須重新審批；市集的 carry_over 是純防禦。
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from src.config import get_config_dir
from src.models.identity import AgentIdentity
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import evolution_command
from src.services.mcp_hub_service import MCPHubService
from src.tools.builtin.evolution import MCP_CONFIG_FILENAME, request_mcp_creation
from src.tools.builtin.system import MARKET_FILENAME, request_skill_install

_IDENTITY = AgentIdentity.for_role(agent_id="Wirer", role="Owner", is_human=False)

_POLICY = {
    "enabled_tools": ["list_customers"],
    "disabled_tools": ["create_refund"],
    "vault_jail_arg": "base_dir",
}


def _seed_config(servers: dict[str, Any]) -> None:
    """把一份 server_config.json 播進沙箱 config 目錄（config_utils 的讀寫根）。"""
    path = get_config_dir() / MCP_CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _read_config() -> dict[str, Any]:
    return json.loads((get_config_dir() / MCP_CONFIG_FILENAME).read_text(encoding="utf-8"))


# ── 接線點 1：MCPHubService.register_server（創建引擎 / POST /v1/creation/mcp）──


def test_register_server_preserves_policy_on_reregister(tmp_path):
    """同名重註冊：command/args 更新，但政策欄位不得被整條目重寫抹掉。"""
    cfg = tmp_path / "server_config.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"acme": {"command": "old", "args": [], **_POLICY}}}),
        encoding="utf-8",
    )
    MCPHubService(config_path=str(cfg)).register_server("acme", "python", ["server.py"])

    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["acme"]
    assert entry["command"] == "python"  # 陽性對照：該更新的有更新
    assert entry["args"] == ["server.py"]
    assert entry["enabled_tools"] == ["list_customers"]
    assert entry["disabled_tools"] == ["create_refund"]
    assert entry["vault_jail_arg"] == "base_dir"


def test_register_server_does_not_invent_policy_for_new_entry(tmp_path):
    """全新伺服器：不得憑空生出政策欄位（未設＝全開，不是全關）。"""
    cfg = tmp_path / "server_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    MCPHubService(config_path=str(cfg)).register_server("fresh", "python", [])

    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["fresh"]
    assert "enabled_tools" not in entry
    assert "disabled_tools" not in entry


def test_register_server_leaves_other_servers_untouched(tmp_path):
    """upsert 語意：只動目標條目，鄰居的政策不受影響。"""
    cfg = tmp_path / "server_config.json"
    cfg.write_text(
        json.dumps(
            {"mcpServers": {
                "acme": {"command": "x", "args": []},
                "other": {"command": "y", "args": [], **_POLICY},
            }}
        ),
        encoding="utf-8",
    )
    MCPHubService(config_path=str(cfg)).register_server("acme", "python", [])

    other = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["other"]
    assert other["disabled_tools"] == ["create_refund"]


# ── 接線點 2：evolution.request_mcp_creation（自我演化熱部署）─────────────


class _FakeProtocolManager:
    """只記錄熱掛載收到的設定——用來證明落盤與執行期是同一份。"""

    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []

    async def add_server(self, name: str, config: Any) -> bool:
        self.added.append((name, config))
        return True


@pytest.fixture
def _evolution_harness(monkeypatch, tmp_path):
    """沙盒中保留實際部署路徑與設定，熱掛載只記錄、不啟動程序。"""
    layout = replace(evolution_command.current_layout(), data_root=tmp_path / "owned")
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    monkeypatch.setattr(
        EvolutionPathGuard, "MANAGED_BASE_DIR", layout.data_root / "mcp_servers" / "managed"
    )
    for name, content in {
        "spec.json": '{"name": "Acme"}', "server.py": "pass", "requirements.txt": ""
    }.items():
        EvolutionPathGuard.write_tool_file("acme-tool", name, content)
    root = EvolutionPathGuard.get_tool_root("acme-tool")
    _seed_config({"acme-tool": {
        "command": "python", "args": [str(root / "server.py")], **_POLICY
    }})
    return _FakeProtocolManager()


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_redeploy_refused_preserves_entry_and_policy(mock_approval, _evolution_harness):
    """已部署能力必須以明確理由拒絕，原條目與政策一併保留。"""
    mock_approval.return_value = (True, {})
    before = _read_config()

    result = await request_mcp_creation(
        "acme-tool", identity=_IDENTITY, _interactive=True,
        _ctx={"protocol_manager": _evolution_harness},
    )

    error = json.loads(result)
    assert error["error_class"] == "validation_error"
    assert error["reason"] == "skill_deployed"
    assert "new skill_id" in error["suggestion"]
    assert "remove the deployed server in settings" in error["suggestion"]
    assert _read_config() == before
    mock_approval.assert_not_awaited()
    assert _evolution_harness.added == []


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_redeploy_refused_without_hotswap(
    mock_approval, _evolution_harness
):
    """即使伺服器已改名或停用，重新部署也不能熱掛載一份較寬鬆的設定。"""
    mock_approval.return_value = (True, {})
    entry = _read_config()["mcpServers"]["acme-tool"]
    _seed_config({"renamed": {**entry, "enabled": False}})
    before = _read_config()

    result = await request_mcp_creation(
        "acme-tool", identity=_IDENTITY, _interactive=True,
        _ctx={"protocol_manager": _evolution_harness},
    )

    assert json.loads(result)["reason"] == "skill_deployed"
    assert _evolution_harness.added == []
    assert _read_config() == before
    mock_approval.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_deploy_after_removal_requires_fresh_approval(mock_approval, _evolution_harness):
    """舊條目移除後才能重新部署，仍須核准目前程式，落盤與熱掛載使用同一設定。"""
    _seed_config({})
    mock_approval.return_value = (False, {})
    kwargs = {
        "identity": _IDENTITY, "_interactive": True,
        "_ctx": {"protocol_manager": _evolution_harness},
    }
    assert "Rejected" in await request_mcp_creation("acme-tool", **kwargs)
    assert _read_config() == {"mcpServers": {}}
    assert _evolution_harness.added == []

    mock_approval.reset_mock()
    mock_approval.return_value = (True, {})
    result = await request_mcp_creation("acme-tool", **kwargs)

    assert result.startswith("Evolution Success")
    mock_approval.assert_awaited_once()
    assert mock_approval.call_args.kwargs["extra_data"]["source_code"] == "pass"
    entry = _read_config()["mcpServers"]["acme-tool"]
    assert len(_evolution_harness.added) == 1
    name, config = _evolution_harness.added[0]
    assert name == "acme-tool"
    assert config.command == entry["command"]
    assert config.args == entry["args"]
    assert config.enabled == entry["enabled"]
    assert not _POLICY.keys() & entry.keys()


# ── 接線點 3：system.request_skill_install（技能市集安裝）────────────────


@pytest.mark.asyncio
@patch("src.tools.builtin.system.request_approval")
async def test_skill_install_early_returns_on_already_installed(mock_approval):
    """誠實界定：此路徑靠「已安裝就早退」保護，`carry_over_policy` 在此恆為純防禦。

    早退＝連寫入都不發生，故政策不可能被抹。這條同時釘住那道守衛本身——它一旦被放寬
    （例如日後支援「重裝/升級」），抹除就會無聲回歸，而屆時擋著的就只剩 carry_over。
    """
    mock_approval.return_value = (True, {})
    _seed_config({"market-skill": {"command": "python", "args": [], **_POLICY}})
    (get_config_dir() / MARKET_FILENAME).write_text(
        json.dumps(
            {"skills": {"market-skill": {"name": "S", "description": "d",
                                         "mcp_config": {"command": "new", "args": []}}}}
        ),
        encoding="utf-8",
    )

    result = await request_skill_install("market-skill", identity=_IDENTITY, _interactive=True)

    assert "already installed" in result.lower()
    mock_approval.assert_not_awaited()  # 早退在審批之前，零副作用
    entry = _read_config()["mcpServers"]["market-skill"]
    assert entry["command"] == "python"  # 未被覆寫
    assert entry["disabled_tools"] == ["create_refund"]

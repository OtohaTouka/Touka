"""MCP enabled 停用閘測試（ADR-0188，F4）。

`server_config.json` 的 ``enabled: false`` 必須在 **runtime registry 咽喉**被執法：
停用者不載入 registry → ``initialize_all`` 不 spawn 子進程、``ensure_connected``
（``connect_mcp_server`` 工具與技能接線的共同入口）fail-closed、``resolve_tool``
解析不到＝派發斷路。dispatcher ``<mcp_registry>`` 清單排除停用者為既有生產行為
（skill_service ``cfg.get("enabled", True)``），先前無測，一併釘住。
"""
from __future__ import annotations

import json

import pytest
from src.config import get_config_dir
from src.protocols import manager as manager_mod
from src.protocols.manager import ProtocolManager
from src.protocols.registry import ProtocolRegistry
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service


def _write_config(tmp_path, servers: dict) -> str:
    path = tmp_path / "server_config.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(path)


def _servers() -> dict:
    return {
        "alpha-on": {"command": "echo", "args": ["alpha-on"], "enabled": True},
        "beta-off": {"command": "echo", "args": ["beta-off"], "enabled": False},
        "gamma-on": {"command": "echo", "args": ["gamma-on"]},
    }


class _FakeStack:
    async def aclose(self) -> None:
        return None


def _fake_connect_recording(bucket: list):
    async def _connect(config, **_kwargs):
        bucket.append(config.args[0])
        return object(), _FakeStack()

    return _connect


def test_registry_excludes_disabled_servers(tmp_path):
    """enabled:false 不載入 runtime registry（咽喉執法）；缺席＝預設啟用。"""
    reg = ProtocolRegistry(_write_config(tmp_path, _servers()))
    assert set(reg.get_all_servers()) == {"alpha-on", "gamma-on"}
    assert reg.get_server("beta-off") is None


def test_registry_reload_honors_toggle(tmp_path):
    """toggle 後 load_config 重載：停用→消失、再啟用→回歸。"""
    path = _write_config(tmp_path, _servers())
    reg = ProtocolRegistry(path)
    assert reg.get_server("beta-off") is None
    servers = _servers()
    servers["beta-off"]["enabled"] = True
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    reg.load_config()
    assert reg.get_server("beta-off") is not None


@pytest.mark.asyncio
async def test_initialize_all_never_spawns_disabled(tmp_path, monkeypatch):
    """行為級：真 ProtocolManager.initialize_all——停用者零 spawn（修前照樣起子進程）。"""
    connected: list[str] = []
    monkeypatch.setattr(
        manager_mod, "connect_mcp_server", _fake_connect_recording(connected)
    )

    async def _no_ide():
        return {}

    monkeypatch.setattr(manager_mod, "discover_ide_servers", _no_ide)
    mgr = ProtocolManager(ProtocolRegistry(_write_config(tmp_path, _servers())))
    await mgr.initialize_all()
    try:
        assert sorted(connected) == ["alpha-on", "gamma-on"]
        assert "beta-off" not in mgr._active_sessions
    finally:
        await mgr.cleanup()


@pytest.mark.asyncio
async def test_ensure_connected_disabled_fail_closed(tmp_path, monkeypatch):
    """動態掛載入口（connect_mcp_server 工具／技能接線同走 ensure_connected）fail-closed。"""
    connected: list[str] = []
    monkeypatch.setattr(
        manager_mod, "connect_mcp_server", _fake_connect_recording(connected)
    )
    mgr = ProtocolManager(ProtocolRegistry(_write_config(tmp_path, _servers())))
    ok = await mgr.ensure_connected("beta-off")
    assert ok is False
    assert connected == []


def test_dispatcher_registry_listing_excludes_disabled():
    """dispatcher `<mcp_registry>` 清單排除停用者（既有生產行為，首次釘測）。"""
    resource_manager.write_json(
        get_config_dir() / "server_config.json", {"mcpServers": _servers()}
    )
    skill_service.sync_system_dispatcher()
    md = (
        get_config_dir() / "skills" / "system-mcp-dispatcher" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "alpha-on" in md
    assert "gamma-on" in md
    assert "beta-off" not in md

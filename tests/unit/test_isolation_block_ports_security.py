"""封鎖埠 B 的 profile 端（iso-c2 C2-2 U2e；設計 2.3 版 §2.10）.

- profile 用呼叫端傳進來的 B（monitor 的 ``block_ports(location)``：核心 B ∪ 已知的上游埠，溢位時
  只有核心 B）；B 超過上限一律拒絕（``block_ports_overflow``），不截斷：截斷會丟掉高號的埠。
- Settings 上游清單的結構測試（設計 LOW-3、審查 r4 LOW-5）：Settings 裡每個名稱以 ``_URL`` 結尾的
  欄位或 property，以及模型層每個 ``ProviderSpec.route_url_attr``，要嘛在上游清單、要嘛在明列的
  排除清單（各有理由）；以後新增上游網址卻沒歸類，這裡就轉紅。
全部用 tmp_path 建的假版面；不連網。
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING

import pytest
from src.config import Settings
from src.models.identity import AgentIdentity
from src.sandbox_kernel.spec import MAX_BLOCK_PORTS
from src.services.isolation import block_ports as bp
from src.services.isolation import profiles
from src.services.isolation.profiles import (
    BLOCK_PORTS_OVERFLOW,
    ProfileError,
    SandboxLayout,
    blocked_ports,
    build_agent_command_profile,
    build_evolution_profile,
)
from src.services.isolation.status import IsolationMonitor
from src.services.providers.registry import PROVIDER_SPECS

from tests.support.isolation import match_state_root

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
SKILL = "probe_skill"
# 不進上游推導的網址（各有理由）
EXCLUDED_URLS = {
    "SEARXNG_BASE_URL": "只進 proxy 的 B（執行器連不到 SearXNG），由 profiles.blocked_ports 處理",
    "UI_DEFAULT_URL": "Touka 自己的埠，由 profiles.blocked_ports 處理",
    "HEARTBEAT_EXTERNAL_URL": "Touka 自己的埠，由 profiles.blocked_ports 處理",
    "GROK_DEFAULT_URL": "config 模組的固定公網常數（不是 Settings 欄位、不能設定）",
}


@pytest.fixture(autouse=True)
def _rm_state_matches_layout(tmp_path, monkeypatch):
    """RM 的 ST 指向本檔版面的 ST（ADR-0293 P7：兩者不同時 ST 的存取函式拒絕）."""
    match_state_root(monkeypatch, tmp_path / "install" / "st")


def _layout(root: Path) -> SandboxLayout:
    install = root / "install"
    data = install / "data"
    for sub in ("personal_drive/Alice", f"mcp_servers/managed/{SKILL}"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("ToukaProxy/config/skills", "host_root", "st"):
        (install / sub).mkdir(parents=True, exist_ok=True)
    return SandboxLayout(
        data_root=data,
        config_dir=install / "ToukaProxy" / "config",
        install_root=install,
        proxy_root=install / "ToukaProxy",
        env_file=install / "host_root" / ".env",
        state_root=install / "st",
        kernel_dir=install / "kernel",
        executor_dir=None,
        mirror_roots=(install / "host_root",),
        in_container=False,
    )


# --- profile 用 monitor 的 B、溢位不截斷 ------------------------------------------------------


async def test_profiles_use_the_b_they_are_given(tmp_path):
    """上游埠（例如 LiteLLM 4000、自訂連線 4200）由 monitor 算好傳進來，profile 照用."""
    layout = _layout(tmp_path)
    ports = (2375, 2376, 3001, 4000, 4200, 8000, 8082, 8083)
    agent = await build_agent_command_profile(
        ALICE, layout.data_root / "personal_drive" / "Alice", layout=layout, block_ports=ports
    )
    root = layout.data_root / "mcp_servers" / "managed" / SKILL
    evolution = build_evolution_profile(SKILL, root, layout=layout, block_ports=ports)
    assert agent.block_ports == evolution.block_ports == ports


@pytest.mark.parametrize("builder", ["agent", "evolution"])
async def test_b_over_the_limit_is_refused_not_truncated(tmp_path, builder):
    layout = _layout(tmp_path)
    ports = tuple(range(1, MAX_BLOCK_PORTS + 2))
    with pytest.raises(ProfileError) as raised:
        if builder == "agent":
            drive = layout.data_root / "personal_drive" / "Alice"
            await build_agent_command_profile(ALICE, drive, layout=layout, block_ports=ports)
        else:
            root = layout.data_root / "mcp_servers" / "managed" / SKILL
            build_evolution_profile(SKILL, root, layout=layout, block_ports=ports)
    assert raised.value.reason == BLOCK_PORTS_OVERFLOW


def test_touka_ports_are_never_truncated(monkeypatch):
    """上限比 Touka 自己的埠還小時也不截斷（高號的預設埠不能被丟掉）；組 profile 時才拒絕."""
    monkeypatch.setattr(profiles, "MAX_BLOCK_PORTS", 3)
    ports = blocked_ports("executor")
    assert len(ports) > 3 and {8000, 8082, 8083, 3001, 2375, 2376} <= set(ports)


# --- Settings 上游清單的結構測試（LOW-3、r4 LOW-5） ---------------------------------------------


def _settings_url_names() -> set[str]:
    fields = {name for name in Settings.model_fields if name.endswith("_URL")}
    props = {
        name for name, value in inspect.getmembers(Settings)
        if name.endswith("_URL") and isinstance(value, property)
    }
    return fields | props


def test_every_settings_url_is_an_upstream_or_explicitly_excluded():
    names = _settings_url_names()
    assert "GEMINI_BASE_URL" in names  # property 也要掃到
    unclassified = names - set(bp.SETTINGS_UPSTREAM_URLS) - set(EXCLUDED_URLS)
    assert unclassified == set(), "新的 *_URL 要加進上游清單或排除清單（附理由）"


def test_every_provider_route_url_is_an_upstream_or_explicitly_excluded():
    """模型層新增供應商時，只改 registry 的 ``route_url_attr`` 也會被抓到."""
    attrs = {spec.route_url_attr for spec in PROVIDER_SPECS if spec.route_url_attr}
    assert attrs, "registry 沒有任何 route_url_attr：掃描方式失效"
    assert attrs - set(bp.SETTINGS_UPSTREAM_URLS) - set(EXCLUDED_URLS) == set()


def test_exclusions_and_upstreams_do_not_overlap():
    assert set(bp.SETTINGS_UPSTREAM_URLS).isdisjoint(EXCLUDED_URLS)



# --- MCP registry（server_config.json）的遠端 url 也是上游來源：全部條目，不看 enabled -----------

HEADER_MARKER = "registry-header-secret-value"


def _monitor(layout: SandboxLayout) -> IsolationMonitor:
    return IsolationMonitor(
        layout_provider=lambda: layout, platform="linux", start_background=False
    )


def _write_registry(layout: SandboxLayout, text: str) -> None:
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    (layout.config_dir / bp.MCP_REGISTRY_FILE).write_text(text, encoding="utf-8")


async def test_registry_remote_urls_are_blocked_even_when_disabled(tmp_path):
    layout = _layout(tmp_path)
    servers = {
        "local_http": {"url": "http://localhost:9300/mcp", "enabled": False,
                       "headers": {"Authorization": f"Bearer {HEADER_MARKER}"}},
        "lan": {"url": "http://192.168.1.20:9301/mcp"},
        "public": {"url": "https://mcp.example.com:9302/mcp"},
        "stdio": {"command": "python", "args": ["server.py"]},
    }
    _write_registry(layout, json.dumps({"mcpServers": servers}))
    monitor = _monitor(layout)
    await monitor.refresh()
    for location in ("proxy", "executor"):
        ports = set(monitor.block_ports(location))
        assert {9300, 9301} <= ports and 9302 not in ports


async def test_unreadable_registry_keeps_the_level_and_logs_only_the_type(tmp_path, caplog):
    """額外來源（同設計：記憶體裡的 registry 讀不到時就是沒有）：不改層級；log 不帶原文."""
    layout = _layout(tmp_path)
    broken = '{"mcpServers": {"x": {"url": "http://localhost:9300", "h": "' + HEADER_MARKER
    _write_registry(layout, broken)
    monitor = _monitor(layout)
    with caplog.at_level(logging.WARNING):
        await monitor.refresh()
    assert monitor.snapshot().block_ports_issue is None
    assert 9300 not in monitor.block_ports("executor")
    assert "JSONDecodeError" in caplog.text and HEADER_MARKER not in caplog.text


@pytest.mark.parametrize("document", [[], {"mcpServers": []}, {"mcpServers": {"x": 3}}])
def test_registry_structure_errors_are_unreadable(tmp_path, document):
    layout = _layout(tmp_path)
    _write_registry(layout, json.dumps(document))
    result = bp.read_registry_ports(layout.config_dir / bp.MCP_REGISTRY_FILE)
    assert (result.ok, result.error) == (False, bp.INVALID_STRUCTURE)


def test_missing_registry_has_no_upstreams(tmp_path):
    result = bp.read_registry_ports(_layout(tmp_path).config_dir / bp.MCP_REGISTRY_FILE)
    assert (result.ok, result.ports) == (True, frozenset())

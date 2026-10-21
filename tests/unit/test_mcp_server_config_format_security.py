"""``server_config.json`` 的格式不變，而且每個寫入點都先寫隔離設定檔（ADR-0275 §1）。

1. 格式：在這裡**凍結** ``60956fda`` 的 ``MCPServerItem`` 欄位名（字面量，不從執行中的模型讀，
   免得以後加欄位時測試跟著放寬），用 ``extra="forbid"`` 驗證每個寫入點寫完後的每一筆。
   舊版的設定頁（``MCPServerItem`` 是 forbid）因此一定讀得懂；``origin``、``sandbox_paths``
   不會出現在這個檔。
2. 順序：新增或改程式時先寫隔離設定檔、再寫 ``server_config.json``；刪除時反過來。中途失敗
   只會留下「對不上、被忽略」的紀錄，不會出現「條目已寫、來源沒寫」而被當成第三方。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import src.main as main_module
from pydantic import ConfigDict, create_model
from src.models.identity import AgentIdentity
from src.routers import mcp as mcp_router
from src.services import settings_snapshot_service as snapshot_service
from src.services.mcp_hub_service import MCPHubService
from src.services.mcp_isolation_store import MCP_ISOLATION_FILENAME
from src.services.skill_service import skill_service
from src.tools.builtin import evolution as evolution_tools
from src.tools.builtin import system as system_tools
from src.utils import config_utils
from src.utils.skill_mcp_wiring import migrate_stale_server_keys

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

SERVER_CONFIG = "server_config.json"

# 60956fda 的 routers/mcp.py MCPServerItem 欄位（凍結；不要改成從模型讀）
FROZEN_60956FDA_FIELDS = (
    "command",
    "args",
    "env",
    "url",
    "headers",
    "enabled",
    "vault_jail_arg",
    "enabled_tools",
    "disabled_tools",
    "protocol_mode",
    "auth_mode",
    "auth_profile_id",
    "workload_authority_kind",
    "workload_delegation_id",
    "workload_source_installation_id",
    "capability_policy",
    "cache_enabled",
    "cache_max_entries",
    "cache_default_ttl_ms",
    "subscriptions_enabled",
    "subscription_max_backoff_seconds",
    "approved_header_tools",
    "icons_enabled",
    "telemetry_enabled",
)
LegacyServerItem = create_model(
    "LegacyServerItem",
    __config__=ConfigDict(extra="forbid"),
    **dict.fromkeys(FROZEN_60956FDA_FIELDS, (Any, None)),
)


def assert_legacy_format(config_path: Path) -> dict[str, Any]:
    """每一筆都只含 60956fda 的欄位；回傳全部條目。"""
    servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]
    for entry in servers.values():
        LegacyServerItem.model_validate(entry)
    return servers


@pytest.fixture
def write_order(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """記下兩個檔的寫入先後（原子置換的 ``replace`` 與直接 ``open('w')`` 都算）。

    測試準備資料之後要先 ``clear()``，只留受測寫入點的寫入。
    """
    order: list[str] = []
    watched = {SERVER_CONFIG, MCP_ISOLATION_FILENAME}
    real_replace, real_open = Path.replace, Path.open

    def _replace(self: Path, target: Any) -> Any:
        if Path(target).name in watched:
            order.append(Path(target).name)
        return real_replace(self, target)

    def _open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name in watched and "w" in mode:
            order.append(self.name)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", _replace)
    monkeypatch.setattr(Path, "open", _open)
    return order


ADDS_FIRST = [MCP_ISOLATION_FILENAME, SERVER_CONFIG]
DELETES_FIRST = [SERVER_CONFIG, MCP_ISOLATION_FILENAME]

_HUB = {
    "hub": [
        {
            "id": "curated-demo",
            "name": "Demo",
            "source": "curated",
            "command": "npx",
            "args": ["-y", "@acme/demo"],
            "env_requirements": ["DEMO_TOKEN"],
        }
    ]
}


def _hub_service(tmp_path: Path, servers: dict[str, Any] | None = None) -> MCPHubService:
    config = tmp_path / SERVER_CONFIG
    config.write_text(json.dumps({"mcpServers": servers or {}}), encoding="utf-8")
    hub = tmp_path / "mcp_hub.json"
    hub.write_text(json.dumps(_HUB), encoding="utf-8")
    return MCPHubService(hub_path=str(hub), config_path=str(config))


async def test_hub_install_writes_the_record_first_and_keeps_the_format(
    tmp_path: Path, write_order: list[str]
) -> None:
    service = _hub_service(tmp_path)
    write_order.clear()

    result = await service.install("curated-demo", {"DEMO_TOKEN": "t"})

    assert result["status"] == "success"
    assert write_order == ADDS_FIRST
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == {"demo"}


@pytest.mark.parametrize("origin", ["creation", "scaffold"])
def test_creation_commit_writes_the_record_first_and_keeps_the_format(
    tmp_path: Path, write_order: list[str], origin: str
) -> None:
    service = _hub_service(tmp_path, {"keep": {"command": "node", "args": ["a.js"]}})
    write_order.clear()

    service.register_server("made", "python", ["server.py"], {"K": "v"}, origin=origin)

    assert write_order == ADDS_FIRST
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == {"keep", "made"}


# ── 技能市集、演化部署（config_utils 的設定目錄） ─────────────────────────────

_MARKET = {
    "skills": {
        "weather": {
            "name": "Weather",
            "description": "d",
            "mcp_config": {"command": "npx", "args": ["-y", "@acme/weather"]},
        }
    }
}
_IDENTITY = AgentIdentity.for_role(agent_id="Installer", role="Owner", is_human=False)


def prepare_skill_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[], Awaitable[str]]:
    """技能市集安裝 ``weather``：設定目錄指到 tmp、審批卡以 mock 核准；回傳要執行的動作。"""
    _config_utils_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(system_tools, "request_approval", AsyncMock(return_value=(True, {})))
    return lambda: system_tools.request_skill_install(
        "weather", identity=_IDENTITY, _interactive=True, _emit_event=AsyncMock()
    )


def prepare_evolution_deploy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[], Awaitable[str]]:
    """演化部署 ``calc``：程式資料夾在 tmp、審批卡以 mock 核准；回傳要執行的動作。"""
    _config_utils_dir(monkeypatch, tmp_path)
    guard = MagicMock()
    guard.tool_file_exists.return_value = True
    guard.read_tool_file.side_effect = lambda _sid, name: (
        "print('hi')" if name == "server.py" else '{"name": "Calc", "description": "d"}'
    )
    guard.get_tool_root.return_value = tmp_path / "managed" / "calc"
    guard.get_tool_root.return_value.mkdir(parents=True, exist_ok=True)
    (guard.get_tool_root.return_value / "server.py").write_bytes(b"print('hi')")
    (guard.get_tool_root.return_value / "requirements.txt").write_bytes(b"")
    guard.is_windows.return_value = False
    monkeypatch.setattr(evolution_tools, "EvolutionPathGuard", guard)
    monkeypatch.setattr(evolution_tools, "request_approval", AsyncMock(return_value=(True, {})))
    return lambda: evolution_tools.request_mcp_creation(
        "calc", identity=_IDENTITY, _interactive=True, _emit_event=AsyncMock()
    )


def _config_utils_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """設定（config_utils）與隔離設定檔（get_config_dir）都指到 tmp。"""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(system_tools, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(evolution_tools, "get_config_dir", lambda: tmp_path)
    (tmp_path / SERVER_CONFIG).write_text(
        json.dumps({"mcpServers": {"keep": {"command": "node", "args": ["a.js"]}}}),
        encoding="utf-8",
    )
    (tmp_path / "skill_market.json").write_text(json.dumps(_MARKET), encoding="utf-8")


@pytest.mark.parametrize(
    ("prepare", "name"),
    [(prepare_skill_install, "weather"), (prepare_evolution_deploy, "calc")],
    ids=["skill-market", "evolution-deploy"],
)
async def test_agent_tool_write_points_write_the_record_first_and_keep_the_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_order: list[str],
    prepare: Callable[..., Callable[[], Awaitable[str]]],
    name: str,
) -> None:
    action = prepare(monkeypatch, tmp_path)
    write_order.clear()

    await action()

    assert write_order == ADDS_FIRST
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == {"keep", name}


# ── 設定頁存檔：新增、改程式先寫紀錄；刪除先寫設定 ─────────────────────────────

_FILES = {"command": "npx", "args": ["-y", "@acme/files"], "env": {"TOKEN": "a"}}


def prepare_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, servers: dict[str, Any]
) -> Callable[[dict[str, Any], AgentIdentity], Awaitable[dict[str, Any]]]:
    """設定頁存檔指到 tmp；不重載真的 registry、不同步調度員技能。回傳存檔動作。"""
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    monkeypatch.setattr(mcp_router, "get_mcp_config_path", lambda: tmp_path / "server_config.json")
    monkeypatch.setattr(main_module.registry, "load_config", lambda: None)
    monkeypatch.setattr(main_module.protocol_manager, "initialize_all", AsyncMock())
    monkeypatch.setattr(skill_service, "sync_system_dispatcher", lambda: None)

    async def _save(posted: dict[str, Any], identity: AgentIdentity) -> dict[str, Any]:
        body = mcp_router.MCPConfigResponse.model_validate({"mcpServers": posted})
        return (await mcp_router.save_mcp_config(body, identity)).model_dump()

    return _save


@pytest.mark.parametrize(
    ("before", "posted", "expected"),
    [
        ({}, {"files": _FILES}, ADDS_FIRST),
        ({"files": _FILES}, {"files": {**_FILES, "args": ["-y", "@acme/v2"]}}, ADDS_FIRST),
        ({"files": _FILES, "keep": _FILES}, {"keep": _FILES}, DELETES_FIRST),
    ],
    ids=["add", "change-program", "delete"],
)
async def test_settings_save_write_order_and_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_order: list[str],
    before: dict[str, Any],
    posted: dict[str, Any],
    expected: list[str],
) -> None:
    save = prepare_save(monkeypatch, tmp_path, before)
    write_order.clear()

    await save(posted, _IDENTITY)

    assert write_order == expected
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == set(posted)


def test_key_migration_write_order_and_format(tmp_path: Path, write_order: list[str]) -> None:
    """改名：先把紀錄複製到新名稱、再寫設定、最後才刪舊名稱的紀錄。"""
    (tmp_path / SERVER_CONFIG).write_text(
        json.dumps({"mcpServers": {"curated-mcp-docgen": {"command": "uvx", "args": ["d"]}}}),
        encoding="utf-8",
    )
    hub = {"hub": [{"id": "curated-mcp-docgen", "name": "mcp-docgen", "source": "curated"}]}
    (tmp_path / "mcp_hub.json").write_text(json.dumps(hub), encoding="utf-8")
    write_order.clear()

    migrate_stale_server_keys(config_dir=tmp_path)

    assert write_order == [*ADDS_FIRST, MCP_ISOLATION_FILENAME]
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == {"mcp-docgen"}


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_snapshot_import_keeps_the_format_and_write_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_order: list[str], mode: str
) -> None:
    """手造快照帶 origin／sandbox_paths 鍵：寫進來的條目仍是 60956fda 的格式。"""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(snapshot_service, "_RELOADERS", {})
    (tmp_path / SERVER_CONFIG).write_text(
        json.dumps({"mcpServers": {"gone": _FILES, "files": _FILES}}), encoding="utf-8"
    )
    smuggled = {**_FILES, "origin": "hub", "sandbox_paths": [{"path": "/", "access": "rw"}]}
    sections = {"mcp_servers": {"data": {"mcpServers": {"files": smuggled, "fresh": smuggled}}}}
    snapshot = {
        "schema_version": snapshot_service.SCHEMA_VERSION,
        "export_kind": snapshot_service.EXPORT_KIND,
        "manifest": {"sha256": snapshot_service._canonical_sha256(sections)},
        "sections": sections,
    }
    write_order.clear()

    snapshot_service.apply_snapshot(snapshot, mode=mode)

    removed = mode == "replace"
    assert write_order == [*ADDS_FIRST, *([MCP_ISOLATION_FILENAME] if removed else [])]
    expected = {"files", "fresh"} if removed else {"gone", "files", "fresh"}
    assert set(assert_legacy_format(tmp_path / SERVER_CONFIG)) == expected


async def test_records_follow_the_config_dir_even_when_config_utils_is_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔離設定檔的位置來自 ``get_config_dir()``；把 config_utils 換成假物件的測試不會在工作目錄
    建出 ``MagicMock/...`` 這種資料夾。"""
    deploy = prepare_evolution_deploy(monkeypatch, tmp_path)
    fake = MagicMock()
    fake.read_config_json.return_value = {"mcpServers": {}}
    monkeypatch.setattr(evolution_tools, "config_utils", fake)

    await deploy()

    saved = json.loads((tmp_path / MCP_ISOLATION_FILENAME).read_text(encoding="utf-8"))
    assert saved["servers"]["calc"]["origin"] == "evolution"

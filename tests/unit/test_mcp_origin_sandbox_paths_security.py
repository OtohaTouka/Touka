"""MCP 來源（origin）與額外允許的資料夾的寫入規則（ADR-0275 §1、§3）。

- 每個寫入點依**程式路徑**寫 origin：Hub 安裝 ``hub``、創建 ``creation``／``scaffold``、
  技能市集 ``skill``、演化部署 ``evolution``、設定頁新增 ``manual``（非人類 ``agent``）。
- Hub 安裝遇到同名、紀錄是自建的條目時拒絕（LOW-a）：唯一會讓條目變寬的寫入窗口。
- 改來源或資料夾只經 ``PUT /mcp/{name}/isolation``：人類請求者、CSRF、``bound`` 相符、
  只能改成 ``agent``、自建不收資料夾、逐筆過永不授權。
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
import src.main as main_module
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.mcp_isolation import MAX_SANDBOX_PATHS, SandboxPathGrant
from src.routers import mcp as mcp_router
from src.services import settings_snapshot_service as snapshot_service
from src.services.identity_router import get_verified_identity
from src.services.isolation.profiles import current_layout
from src.services.mcp_hub_service import MCPHubService
from src.services.mcp_isolation_store import (
    MCP_ISOLATION_FILENAME,
    McpIsolationStore,
    assign,
    bound_of,
)
from src.tools.builtin import system as system_tools
from src.utils import config_utils, mcp_tool_policy
from src.utils.mcp_origin import personal_drive_root
from src.utils.skill_mcp_wiring import migrate_stale_server_keys

from tests.unit.test_mcp_server_config_format_security import (
    prepare_evolution_deploy,
    prepare_save,
    prepare_skill_install,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_HUB = {
    "hub": [
        {
            "id": "curated-demo",
            "name": "Demo",
            "source": "curated",
            "command": "npx",
            "args": ["-y", "@acme/demo"],
        }
    ]
}
_AGENT_ENTRY = {"command": "python", "args": ["/data/personal_drive/a/server.py"]}


def _hub_service(tmp_path: Path, servers: dict[str, Any] | None = None) -> MCPHubService:
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": servers or {}}), encoding="utf-8"
    )
    (tmp_path / "mcp_hub.json").write_text(json.dumps(_HUB), encoding="utf-8")
    return MCPHubService(
        hub_path=str(tmp_path / "mcp_hub.json"),
        config_path=str(tmp_path / "server_config.json"),
    )


def _servers(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "server_config.json").read_text(encoding="utf-8"))["mcpServers"]


# ── Hub 安裝 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("started", [False, True, None])
async def test_skill_install_reports_actual_start(monkeypatch, tmp_path, started):
    prepare_skill_install(monkeypatch, tmp_path)
    pm = Mock(add_server=AsyncMock(return_value=started)) if started is not None else None
    monkeypatch.setattr(main_module.app.state, "context", {"protocol_manager": pm}, raising=False)
    result = await system_tools.request_skill_install(
        "weather", identity=AgentIdentity.for_role(agent_id="Installer", role="Owner"),
        _interactive=True, _emit_event=AsyncMock(), _ctx={"protocol_manager": pm},
    )
    assert "saved; not started" in result if not started else "started" in result
    if pm:
        pm.add_server.assert_awaited_once()


async def test_hub_install_records_hub_bound_to_the_written_entry(tmp_path: Path) -> None:
    await _hub_service(tmp_path).install("curated-demo", {})

    entry = _servers(tmp_path)["demo"]
    view = McpIsolationStore(tmp_path).record_for("demo", entry)
    assert view.state == "ok"
    assert view.effective_origin == "hub"


@pytest.mark.parametrize("origin", ["creation", "agent", "evolution", "unknown-origin"])
async def test_hub_install_refuses_to_replace_a_self_built_entry(
    tmp_path: Path, origin: str
) -> None:
    """LOW-a：先寫 hub 紀錄再寫設定的中途，舊的代理人條目會掉成第三方；所以直接拒絕。"""
    service = _hub_service(tmp_path, {"demo": _AGENT_ENTRY})
    store = McpIsolationStore(tmp_path)
    store.mutate(assign("demo", origin, bound_of(_AGENT_ENTRY), fresh=True))
    before_config = (tmp_path / "server_config.json").read_bytes()
    before_records = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    with pytest.raises(ToukaException) as raised:
        await service.install("curated-demo", {})

    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.detail["message"] == "mcp_hub_install_over_self_built"
    assert (tmp_path / "server_config.json").read_bytes() == before_config
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before_records


async def test_hub_install_refuses_a_stale_self_built_record_too(tmp_path: Path) -> None:
    """自建紀錄對不上條目（有人手改）時仍是自建，一樣不讓 Hub 蓋掉。"""
    edited = {**_AGENT_ENTRY, "args": ["/data/personal_drive/a/other.py"]}
    service = _hub_service(tmp_path, {"demo": edited})
    store = McpIsolationStore(tmp_path)
    store.mutate(assign("demo", "creation", bound_of(_AGENT_ENTRY), fresh=True))

    with pytest.raises(ToukaException):
        await service.install("curated-demo", {})
    assert _servers(tmp_path)["demo"] == edited


@pytest.mark.parametrize("origin", ["hub", "manual", "skill", None])
async def test_hub_reinstall_over_third_party_or_unrecorded_is_allowed(
    tmp_path: Path, origin: str | None
) -> None:
    old = {"command": "npx", "args": ["-y", "@acme/old"]}
    service = _hub_service(tmp_path, {"demo": old})
    if origin is not None:
        McpIsolationStore(tmp_path).mutate(assign("demo", origin, bound_of(old), fresh=True))

    await service.install("curated-demo", {})

    entry = _servers(tmp_path)["demo"]
    assert entry["args"] == ["-y", "@acme/demo"]
    assert McpIsolationStore(tmp_path).record_for("demo", entry).effective_origin == "hub"


# ── 創建 COMMIT ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", ["creation", "scaffold"])
def test_register_server_records_the_origin_its_caller_passes(
    tmp_path: Path, origin: str
) -> None:
    _hub_service(tmp_path).register_server("made", "python", ["server.py"], origin=origin)

    entry = _servers(tmp_path)["made"]
    view = McpIsolationStore(tmp_path).record_for("made", entry)
    assert view.state == "ok"
    assert view.effective_origin == origin


def test_register_server_defaults_to_creation(tmp_path: Path) -> None:
    """呼叫端沒帶 origin（今天的創建 COMMIT）時當自建，不當第三方。"""
    _hub_service(tmp_path).register_server("made", "python", ["server.py"])

    entry = _servers(tmp_path)["made"]
    assert McpIsolationStore(tmp_path).record_for("made", entry).effective_origin == "creation"


# ── 技能市集、演化部署 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prepare", "name", "origin"),
    [(prepare_skill_install, "weather", "skill"), (prepare_evolution_deploy, "calc", "evolution")],
    ids=["skill-market", "evolution-deploy"],
)
async def test_agent_tool_write_points_record_their_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[..., Callable[[], Awaitable[str]]],
    name: str,
    origin: str,
) -> None:
    await prepare(monkeypatch, tmp_path)()

    entry = _servers(tmp_path)[name]
    view = McpIsolationStore(tmp_path).record_for(name, entry)
    assert view.state == "ok"
    assert view.effective_origin == origin


async def test_evolution_redeploy_keeps_the_record_self_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拒絕覆寫同名服務；條目與原有來源紀錄都完整保留。"""
    deploy = prepare_evolution_deploy(monkeypatch, tmp_path)
    hub_entry = {"command": "npx", "args": ["-y", "@acme/calc"]}
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": {"calc": hub_entry}}), encoding="utf-8"
    )
    McpIsolationStore(tmp_path).mutate(assign("calc", "hub", bound_of(hub_entry), fresh=True))

    before_config = (tmp_path / "server_config.json").read_bytes()
    before_records = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    result = json.loads(await deploy())

    entry = _servers(tmp_path)["calc"]
    assert result["reason"] == "name_taken"
    assert (tmp_path / "server_config.json").read_bytes() == before_config
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before_records
    assert McpIsolationStore(tmp_path).record_for("calc", entry).effective_origin == "hub"


async def test_skill_install_refuses_when_the_records_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """紀錄讀不到時不新增伺服器（沒辦法先寫來源）。"""
    install = prepare_skill_install(monkeypatch, tmp_path)
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")
    before = (tmp_path / "server_config.json").read_bytes()

    result = await install()

    assert "error" in result.lower()
    assert (tmp_path / "server_config.json").read_bytes() == before


# ── 設定頁存檔（/mcp/ui/config）：請求與回應照今天，只多隔離設定檔的副作用 ────────

HUMAN = AgentIdentity.for_role(agent_id="owner@touka.io", role="admin", is_human=True)
AGENT = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False)
_THIRD = {"command": "npx", "args": ["-y", "@acme/files"], "env": {"TOKEN": "a"}}


def _record(tmp_path: Path, name: str, origin: str, entry: dict[str, Any], paths: int = 0) -> None:
    grants = tuple(
        SandboxPathGrant(path=str(tmp_path / f"p{i}"), access="ro") for i in range(paths)
    )

    def apply(records: dict[str, Any]) -> None:
        assign(name, origin, bound_of(entry), fresh=True)(records)
        records[name] = records[name].model_copy(update={"sandbox_paths": grants})

    McpIsolationStore(tmp_path).mutate(apply)


def _view(tmp_path: Path, name: str) -> Any:
    return McpIsolationStore(tmp_path).record_for(name, _servers(tmp_path)[name])


@pytest.mark.parametrize(("identity", "origin"), [(HUMAN, "manual"), (AGENT, "agent")])
async def test_save_records_new_names_as_manual_or_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: AgentIdentity, origin: str
) -> None:
    """設定頁新增：人類寫 manual；非人類（例如非 admin 以 X-Agent-ID 代打靈魂）寫 agent。"""
    save = prepare_save(monkeypatch, tmp_path, {})

    response = await save({"files": _THIRD}, identity)

    assert response["status"] == "success"
    assert response["isolation_cleared"] == []
    assert _view(tmp_path, "files").effective_origin == origin


async def test_save_new_name_overwrites_an_orphan_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOW-c：刪掉後還沒清的同名紀錄不能把舊來源或舊資料夾帶回來。"""
    save = prepare_save(monkeypatch, tmp_path, {})
    _record(tmp_path, "files", "manual", _THIRD, paths=2)

    await save({"files": _THIRD}, AGENT)

    view = _view(tmp_path, "files")
    assert view.effective_origin == "agent"
    assert view.usable_sandbox_paths == ()


@pytest.mark.parametrize(
    ("origin", "identity", "expected"),
    [
        ("creation", HUMAN, "creation"),
        ("agent", HUMAN, "agent"),
        ("evolution", AGENT, "evolution"),
        ("hub", HUMAN, "hub"),
        ("manual", HUMAN, "manual"),
        ("hub", AGENT, "agent"),
        ("skill", AGENT, "agent"),
        ("scaffold", AGENT, "agent"),
        (None, AGENT, "agent"),
        (None, HUMAN, None),
    ],
)
async def test_save_rebinds_changed_programs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None,
    identity: AgentIdentity,
    expected: str | None,
) -> None:
    """程式變了：自建類照舊；第三方類只有人類才保留、否則改成 agent；資料夾清掉並回報。"""
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD})
    if origin is not None:
        _record(tmp_path, "files", origin, _THIRD, paths=1)
    changed = {**_THIRD, "args": ["-y", "@acme/other"]}

    response = await save({"files": changed}, identity)

    view = _view(tmp_path, "files")
    assert view.effective_origin == expected
    assert view.usable_sandbox_paths == ()
    has_folder = origin is not None and origin not in {"creation", "agent", "evolution"}
    assert response["isolation_cleared"] == (["files"] if has_folder else [])


async def test_save_without_program_change_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只改 token、停用或工具政策：紀錄與資料夾都留著。"""
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD, paths=1)
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    response = await save({"files": {**_THIRD, "env": {"TOKEN": "b"}, "enabled": False}}, HUMAN)

    assert response["isolation_cleared"] == []
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before


@pytest.mark.parametrize("origin", ["hub", "scaffold", None])
@pytest.mark.parametrize("env", [{"UV_FIND_LINKS": "./wheels"}, {"TOKEN": "b"}, {}])
async def test_agent_env_change_revokes_origin_and_folders(tmp_path, monkeypatch, origin, env):
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD})
    if origin is not None:
        _record(tmp_path, "files", origin, _THIRD, paths=1)
    response = await save({"files": {**_THIRD, "env": env}}, AGENT)
    assert _view(tmp_path, "files").effective_origin == "agent"
    assert _view(tmp_path, "files").usable_sandbox_paths == ()
    assert response["isolation_cleared"] == (["files"] if origin else [])


async def test_agent_masked_env_round_trip_keeps_grants(tmp_path, monkeypatch):
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "hub", _THIRD, paths=1)
    await save({"files": {**_THIRD, "env": {"TOKEN": "••••••••"}}}, AGENT)
    assert _view(tmp_path, "files").effective_origin == "hub"
    assert len(_view(tmp_path, "files").usable_sandbox_paths) == 1


async def test_save_does_not_revalidate_a_stale_third_party_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有人手改了指令（紀錄對不上），之後一次沒改程式的存檔不能把舊的第三方標記重新綁上。"""
    edited = {**_THIRD, "args": ["-y", "@evil/files"]}
    save = prepare_save(monkeypatch, tmp_path, {"files": edited})
    _record(tmp_path, "files", "manual", _THIRD, paths=1)

    await save({"files": edited}, HUMAN)

    assert _view(tmp_path, "files").state == "stale"


async def test_save_forgets_deleted_names_after_writing_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD, "keep": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD)
    _record(tmp_path, "keep", "hub", _THIRD)

    await save({"keep": _THIRD}, HUMAN)

    assert set(McpIsolationStore(tmp_path).load().records) == {"keep"}


async def test_save_refuses_new_names_when_records_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = prepare_save(monkeypatch, tmp_path, {"keep": _THIRD})
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")
    before = (tmp_path / "server_config.json").read_bytes()

    with pytest.raises(ToukaException) as raised:
        await save({"keep": _THIRD, "files": _THIRD}, HUMAN)

    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR
    assert (tmp_path / "server_config.json").read_bytes() == before


async def test_save_without_program_change_works_when_records_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """讀不到時只有「要寫這個檔」的存檔會失敗；停用一台照樣可以存。"""
    save = prepare_save(monkeypatch, tmp_path, {"keep": _THIRD})
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")

    response = await save({"keep": {**_THIRD, "enabled": False}}, HUMAN)

    assert response["status"] == "success"
    assert _servers(tmp_path)["keep"]["enabled"] is False


async def test_save_refuses_when_server_config_exists_but_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定檔讀不到時不當成空的：否則每一台都變成「新名稱」、自建的紀錄被改成 manual。"""
    save = prepare_save(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "creation", _THIRD)
    config = tmp_path / "server_config.json"
    before = config.read_bytes()
    real_open = Path.open

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.resolve() == config.resolve():
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    with pytest.raises(ToukaException) as raised:
        await save({"files": _THIRD}, HUMAN)
    monkeypatch.setattr(Path, "open", real_open)

    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR
    assert config.read_bytes() == before
    assert McpIsolationStore(tmp_path).record_for("files", _THIRD).effective_origin == "creation"


# ── 舊 key 遷移（skill_mcp_wiring.migrate_stale_server_keys） ─────────────────────

_DOCGEN = {"command": "uvx", "args": ["mcp-docgen"]}


def prepare_key_migration(tmp_path: Path, servers: dict[str, Any] | None = None) -> None:
    """``curated-mcp-docgen`` 這個舊 key 等著被改名成 ``mcp-docgen``。"""
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": servers or {"curated-mcp-docgen": _DOCGEN}}), encoding="utf-8"
    )
    hub = {"hub": [{"id": "curated-mcp-docgen", "name": "mcp-docgen", "source": "curated"}]}
    (tmp_path / "mcp_hub.json").write_text(json.dumps(hub), encoding="utf-8")


def test_key_migration_moves_the_record_to_the_new_name(tmp_path: Path) -> None:
    prepare_key_migration(tmp_path)
    _record(tmp_path, "curated-mcp-docgen", "hub", _DOCGEN, paths=1)

    assert migrate_stale_server_keys(config_dir=tmp_path) == [("curated-mcp-docgen", "mcp-docgen")]

    view = McpIsolationStore(tmp_path).record_for("mcp-docgen", _servers(tmp_path)["mcp-docgen"])
    assert view.state == "ok"
    assert view.effective_origin == "hub"
    assert len(view.usable_sandbox_paths) == 1
    assert set(McpIsolationStore(tmp_path).load().records) == {"mcp-docgen"}


def test_key_migration_does_not_adopt_an_orphan_record_at_the_new_name(tmp_path: Path) -> None:
    """舊名稱沒有紀錄時，新名稱上的孤兒紀錄（含資料夾）不能套上去。"""
    prepare_key_migration(tmp_path)
    _record(tmp_path, "mcp-docgen", "manual", _DOCGEN, paths=1)

    migrate_stale_server_keys(config_dir=tmp_path)

    assert McpIsolationStore(tmp_path).record_for("mcp-docgen", _DOCGEN).state == "none"


def test_key_migration_is_skipped_when_records_are_unreadable(tmp_path: Path) -> None:
    prepare_key_migration(tmp_path)
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")
    before = (tmp_path / "server_config.json").read_bytes()

    with pytest.raises(ToukaException) as raised:
        migrate_stale_server_keys(config_dir=tmp_path)

    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR
    assert (tmp_path / "server_config.json").read_bytes() == before


# ── 匯入設定快照：不匯入隔離設定檔，兩個鍵在 incoming 就拿掉 ──────────────────────


def prepare_snapshot_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current: dict[str, Any]
) -> Callable[..., dict[str, Any]]:
    """快照匯入指到 tmp（config_utils 的設定目錄），不跑熱載入；回傳匯入動作。"""
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(snapshot_service, "_RELOADERS", {})
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": current}), encoding="utf-8"
    )

    def _import(servers: dict[str, Any], **options: Any) -> dict[str, Any]:
        sections = {"mcp_servers": {"data": {"mcpServers": servers}}}
        snapshot = {
            "schema_version": snapshot_service.SCHEMA_VERSION,
            "export_kind": snapshot_service.EXPORT_KIND,
            "manifest": {"sha256": snapshot_service._canonical_sha256(sections)},
            "sections": sections,
        }
        return snapshot_service.apply_snapshot(snapshot, **options)

    return _import


_SMUGGLED = {
    **_THIRD,
    "origin": "hub",
    "sandbox_paths": [{"path": "/", "access": "rw"}],
}


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_import_drops_origin_and_sandbox_paths_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})

    run({"files": _SMUGGLED, "fresh": _SMUGGLED}, mode=mode)

    for entry in _servers(tmp_path).values():
        assert "origin" not in entry
        assert "sandbox_paths" not in entry


def test_import_forgets_old_records_of_newly_added_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOW-d：匯入新加入的名稱，同名同程式的孤兒紀錄（含資料夾）不能套上去。"""
    run = prepare_snapshot_import(monkeypatch, tmp_path, {})
    _record(tmp_path, "files", "manual", _THIRD, paths=1)

    run({"files": _THIRD})

    assert McpIsolationStore(tmp_path).record_for("files", _THIRD).state == "none"


def test_import_keeps_records_of_existing_names_and_relies_on_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD, paths=1)
    changed = {**_THIRD, "args": ["-y", "@evil/files"]}

    run({"files": changed})

    view = McpIsolationStore(tmp_path).record_for("files", _servers(tmp_path)["files"])
    assert view.state == "stale"
    assert view.effective_origin is None
    assert view.usable_sandbox_paths == ()


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_import_env_change_revokes_origin_and_folders(tmp_path, monkeypatch, mode):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "hub", _THIRD, paths=1)
    run({"files": {**_THIRD, "env": {"GIT_SSH_COMMAND": "sh ./agent/script"}}}, mode=mode)
    assert _view(tmp_path, "files").effective_origin == "agent"
    assert _view(tmp_path, "files").usable_sandbox_paths == ()


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_import_masked_env_keeps_origin_and_folders(tmp_path, monkeypatch, mode):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "hub", _THIRD, paths=1)
    run({"files": {**_THIRD, "env": {"TOKEN": "••••••••"}}}, mode=mode)
    assert _view(tmp_path, "files").effective_origin == "hub"
    assert len(_view(tmp_path, "files").usable_sandbox_paths) == 1


@pytest.mark.parametrize("mode", ["merge", "replace"])
@pytest.mark.parametrize("invalid", [None, [], "not-an-entry", 7])
@pytest.mark.parametrize("dry_run", [False, True])
def test_import_refuses_invalid_entry_without_losing_valid_entries(
    tmp_path, monkeypatch, mode, invalid, dry_run
):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "hub", _THIRD, paths=1)
    before = (tmp_path / "server_config.json").read_bytes()
    records_before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()
    result = run({"files": invalid, "fresh": _THIRD}, mode=mode, dry_run=dry_run)
    assert result["applied"] == ["mcp_servers"]
    assert any("mcp:files" in warning and "拒絕" in warning for warning in result["warnings"])
    assert _servers(tmp_path)["files"] == _THIRD
    if dry_run:
        assert (tmp_path / "server_config.json").read_bytes() == before
        assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == records_before
    else:
        assert _servers(tmp_path)["fresh"] == _THIRD
    assert _view(tmp_path, "files").effective_origin == "hub"
    assert len(_view(tmp_path, "files").usable_sandbox_paths) == 1


@pytest.mark.parametrize("invalid", [None, [], "not-an-entry", 7])
def test_import_over_invalid_existing_entry_revokes_stale_grants(tmp_path, monkeypatch, invalid):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": invalid})
    _record(tmp_path, "files", "hub", _THIRD, paths=1)
    run({"files": _THIRD})
    assert _servers(tmp_path)["files"] == _THIRD
    assert _view(tmp_path, "files").effective_origin == "agent"
    assert _view(tmp_path, "files").usable_sandbox_paths == ()


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_invalid_import_over_invalid_existing_entry_still_reports_refusal(
    tmp_path, monkeypatch, mode
):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": None})
    result = run({"files": [], "fresh": _THIRD}, mode=mode)
    assert any("mcp:files" in warning and "拒絕" in warning for warning in result["warnings"])
    assert _servers(tmp_path)["fresh"] == _THIRD
    assert not isinstance(_servers(tmp_path).get("files"), dict)


@pytest.mark.parametrize("invalid", [None, [], "not-an-object", 7])
def test_import_refuses_invalid_mcp_wrapper_before_writing(tmp_path, monkeypatch, invalid):
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    before = (tmp_path / "server_config.json").read_bytes()
    with pytest.raises(ToukaException) as caught:
        run(invalid)
    assert caught.value.code == ErrorCode.VALIDATION_ERROR
    assert "mcpServers" in caught.value.detail["message"]
    assert (tmp_path / "server_config.json").read_bytes() == before


def test_import_replace_forgets_removed_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD, "keep": _THIRD})
    _record(tmp_path, "files", "creation", _THIRD)
    _record(tmp_path, "keep", "hub", _THIRD)

    run({"keep": _THIRD}, mode="replace")

    assert set(McpIsolationStore(tmp_path).load().records) == {"keep"}


def test_import_dry_run_touches_no_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "creation", _THIRD)
    _record(tmp_path, "fresh", "manual", _THIRD, paths=1)
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    run({"fresh": _THIRD}, mode="replace", dry_run=True)

    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before


def test_import_of_new_names_refuses_when_records_are_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = prepare_snapshot_import(monkeypatch, tmp_path, {})
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")
    before = (tmp_path / "server_config.json").read_bytes()

    with pytest.raises(ToukaException) as raised:
        run({"files": _THIRD})

    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR
    assert (tmp_path / "server_config.json").read_bytes() == before


def test_snapshot_export_carries_no_isolation_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_snapshot_import(monkeypatch, tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD, paths=1)

    snapshot = snapshot_service.build_snapshot()

    text = json.dumps(snapshot)
    assert "sandbox_paths" not in text
    assert '"origin"' not in text


# ── GET／PUT /mcp/{name}/isolation（設計 v2.1 §1.4；PUT 只寫隔離設定檔，下次重啟那台才生效） ──

DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
BASE = AgentIdentity.for_role(agent_id="Nobody", role="Base", is_human=False)
_REMOTE = {"url": "https://mcp.example.com/mcp"}
_HERE = Path(__file__).resolve().parent


@pytest.fixture
def isolation_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Any]:
    """回傳 ``call(method, name, identity, json=None, headers=None)``；設定目錄在 tmp。"""
    monkeypatch.setattr(mcp_router, "get_mcp_config_path", lambda: tmp_path / "server_config.json")
    monkeypatch.setattr(main_module.app.state, "context", {
        "protocol_manager": main_module.protocol_manager,
    }, raising=False)
    monkeypatch.setattr(main_module.protocol_manager, "schedule_relaunch", Mock(), raising=False)
    client = TestClient(main_module.app)

    def _call(
        method: str,
        name: str,
        identity: AgentIdentity,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        main_module.app.dependency_overrides[get_verified_identity] = lambda: identity
        try:
            return client.request(
                method, f"/v1/mcp/{name}/isolation", json=json_body, headers=headers or {}
            )
        finally:
            main_module.app.dependency_overrides.pop(get_verified_identity, None)

    return _call


def _servers_file(tmp_path: Path, servers: dict[str, Any]) -> None:
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _detail(response: Any) -> str:
    return response.json()["detail"]["message"]


def test_get_isolation_reports_codes_for_each_state(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    edited = {**_THIRD, "args": ["-y", "@evil/files"]}
    _servers_file(
        tmp_path,
        {"files": _THIRD, "made": _THIRD, "stale": edited, "bare": _THIRD, "remote": _REMOTE},
    )
    _record(tmp_path, "files", "manual", _THIRD, paths=1)
    _record(tmp_path, "made", "creation", _THIRD, paths=1)
    _record(tmp_path, "stale", "hub", _THIRD, paths=1)
    _record(tmp_path, "remote", "manual", _REMOTE, paths=1)

    got = {
        name: isolation_api("GET", name, HUMAN).json()
        for name in ("files", "made", "stale", "bare", "remote")
    }

    assert got["files"]["class"] == "third_party"
    assert got["files"]["origin"] == "manual"
    assert got["files"]["record_state"] == "ok"
    assert [g["path"] for g in got["files"]["sandbox_paths"]] == [str(tmp_path / "p0")]
    assert got["files"]["bound"] == bound_of(_THIRD)
    assert got["files"]["can_mark_agent"] is True
    assert got["made"]["class"] == "self"
    assert got["made"]["sandbox_paths"] == []
    assert got["made"]["can_mark_agent"] is False
    assert got["stale"]["record_state"] == "stale"
    assert got["stale"]["origin"] is None
    assert got["stale"]["sandbox_paths"] == []
    assert got["stale"]["bound"] == bound_of(edited)
    assert got["bare"]["record_state"] == "none"
    assert got["bare"]["can_mark_agent"] is True
    assert got["remote"]["class"] == "remote"
    assert got["remote"]["sandbox_paths"] == []
    assert got["remote"]["can_mark_agent"] is False


def test_get_isolation_when_records_are_unreadable_reports_self_built(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")

    got = isolation_api("GET", "files", HUMAN).json()

    assert got["record_state"] == "unreadable"
    assert got["class"] == "self"
    assert got["can_mark_agent"] is False


def test_get_isolation_classifies_with_the_reference_heuristic(
    tmp_path: Path, isolation_api: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """分類用 ``classify``：引用代理人個人空間的檔案就是自建，紀錄寫第三方也一樣、資料夾不給；
    相對路徑依 proxy 的工作目錄（未隔離子行程繼承的 cwd）補成絕對路徑."""
    drive = personal_drive_root(current_layout()) / "Alice"
    drive.mkdir(parents=True, exist_ok=True)
    entries = {
        "abs": {"command": "python", "args": [str(drive / "server.py")]},
        "rel": {"command": "python", "args": ["./server.py"]},
    }
    _servers_file(tmp_path, entries)
    for name, entry in entries.items():
        _record(tmp_path, name, "manual", entry, paths=1)
    monkeypatch.chdir(drive)

    got = {name: isolation_api("GET", name, HUMAN).json() for name in entries}

    for body in got.values():
        assert (body["class"], body["origin"], body["sandbox_paths"]) == ("self", "manual", [])


def test_get_isolation_treats_a_malformed_entry_as_self_built(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    """分類讀不懂條目的結構（例如 args 裡不是字串）：當自建，資料夾不給."""
    entry = {"command": "npx", "args": ["-y", 1]}
    _servers_file(tmp_path, {"odd": entry})
    _record(tmp_path, "odd", "manual", entry, paths=1)

    body = isolation_api("GET", "odd", HUMAN).json()

    assert (body["class"], body["sandbox_paths"]) == ("self", [])


def test_get_isolation_needs_a_privileged_identity_and_a_known_server(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})

    assert isolation_api("GET", "files", BASE).status_code == 403
    assert isolation_api("GET", "files", DEBUGGER).status_code == 200
    missing = isolation_api("GET", "nope", HUMAN)
    assert missing.status_code == 422
    assert _detail(missing) == "mcp_server_not_found"


@pytest.mark.parametrize("identity", [AGENT, DEBUGGER, BASE])
def test_put_isolation_requires_a_human_requester(
    tmp_path: Path, isolation_api: Callable[..., Any], identity: AgentIdentity
) -> None:
    """Depends 形式：非人類一律 403（連 body 壞掉也先 403）。"""
    _servers_file(tmp_path, {"files": _THIRD})

    good = isolation_api("PUT", "files", identity, {"bound": bound_of(_THIRD), "origin": "agent"})
    broken = isolation_api("PUT", "files", identity, {"bound": "nope"})

    assert good.status_code == 403
    assert broken.status_code == 403
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_put_isolation_rejects_cross_site_requests(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})

    response = isolation_api(
        "PUT",
        "files",
        HUMAN,
        {"bound": bound_of(_THIRD), "origin": "agent"},
        {"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_put_isolation_requires_the_current_bound(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    """條目在畫面打開之後被改過：拒絕，要重新看一次（422 mcp_entry_changed）。"""
    _servers_file(tmp_path, {"files": _THIRD})
    old = bound_of({**_THIRD, "args": ["-y", "@acme/old"]})

    response = isolation_api("PUT", "files", HUMAN, {"bound": old, "origin": "agent"})

    assert response.status_code == 422
    assert _detail(response) == "mcp_entry_changed"
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


@pytest.mark.parametrize("origin", ["manual", "hub", "skill", None])
def test_put_marks_third_party_or_unrecorded_as_agent_and_drops_folders(
    tmp_path: Path, isolation_api: Callable[..., Any], origin: str | None
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})
    if origin is not None:
        _record(tmp_path, "files", origin, _THIRD, paths=1)

    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": "agent"})

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "agent"
    assert body["class"] == "self"
    assert body["applies_on"] == "background_relaunch"
    view = McpIsolationStore(tmp_path).record_for("files", _THIRD)
    assert view.effective_origin == "agent"
    assert McpIsolationStore(tmp_path).load().records["files"].sandbox_paths == ()


@pytest.mark.parametrize("origin", ["creation", "evolution", "agent", "scaffold"])
def test_put_refuses_to_change_a_self_built_or_scaffold_origin(
    tmp_path: Path, isolation_api: Callable[..., Any], origin: str
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", origin, _THIRD)
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": "agent"})

    assert response.status_code == 422
    assert _detail(response) == "mcp_origin_change_refused"
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before


@pytest.mark.parametrize("origin", ["manual", "hub", "creation"])
def test_put_never_widens_the_origin(
    tmp_path: Path, isolation_api: Callable[..., Any], origin: str
) -> None:
    """來源只能改成 agent；其他值（含改回第三方）在請求驗證就被擋下。"""
    _servers_file(tmp_path, {"files": _THIRD})

    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": origin})

    assert response.status_code == 422
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_put_saves_folders_for_third_party_after_the_never_grant_check(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD)
    repo = tmp_path / "repo"
    repo.mkdir()

    response = isolation_api(
        "PUT",
        "files",
        HUMAN,
        {"bound": bound_of(_THIRD), "sandbox_paths": [{"path": str(repo), "access": "ro"}]},
    )

    assert response.status_code == 200
    assert response.json()["sandbox_paths"] == [{"path": str(repo), "access": "ro"}]
    view = McpIsolationStore(tmp_path).record_for("files", _THIRD)
    assert view.effective_origin == "manual"
    assert [g.path for g in view.usable_sandbox_paths] == [str(repo)]


def test_put_without_sandbox_paths_keeps_them_and_empty_list_clears_them(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    """``sandbox_paths`` 沒送（None）保留原值；送 ``[]`` 才清空（和 ``enabled_tools`` 同語意）。"""
    _servers_file(tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD, paths=2)

    kept = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD)})
    assert kept.status_code == 200
    assert len(kept.json()["sandbox_paths"]) == 2

    cleared = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "sandbox_paths": []})
    assert cleared.status_code == 200
    assert cleared.json()["sandbox_paths"] == []
    view = McpIsolationStore(tmp_path).record_for("files", _THIRD)
    assert view.effective_origin == "manual"
    assert view.usable_sandbox_paths == ()


def test_put_refuses_folders_for_self_built_servers(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"made": _THIRD})
    _record(tmp_path, "made", "creation", _THIRD)
    repo = tmp_path / "repo"
    repo.mkdir()

    response = isolation_api(
        "PUT",
        "made",
        HUMAN,
        {"bound": bound_of(_THIRD), "sandbox_paths": [{"path": str(repo), "access": "rw"}]},
    )

    assert response.status_code == 422
    assert _detail(response) == "mcp_sandbox_paths_refused"


@pytest.mark.parametrize("reference", ["absolute", "relative"])
def test_put_refuses_folders_when_the_heuristic_says_self_built(
    tmp_path: Path,
    isolation_api: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    """「自建不收資料夾」看 ``classify`` 的結果：紀錄寫第三方、但條目引用代理人個人空間的檔案
    （相對路徑依 proxy 的工作目錄）時也拒絕，紀錄不寫（只會更嚴）."""
    drive = personal_drive_root(current_layout()) / "Alice"
    drive.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(drive)
    script = str(drive / "server.py") if reference == "absolute" else "./server.py"
    entry = {"command": "python", "args": [script]}
    _servers_file(tmp_path, {"files": entry})
    _record(tmp_path, "files", "manual", entry)
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()
    repo = tmp_path / "repo"
    repo.mkdir()

    response = isolation_api(
        "PUT",
        "files",
        HUMAN,
        {"bound": bound_of(entry), "sandbox_paths": [{"path": str(repo), "access": "ro"}]},
    )

    assert response.status_code == 422
    assert _detail(response) == "mcp_sandbox_paths_refused"
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before


def test_put_refuses_a_protected_folder_and_names_its_index(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    """逐筆過永不授權：Touka 的設定、資料夾一律拒絕，整個請求不寫。"""
    _servers_file(tmp_path, {"files": _THIRD})
    _record(tmp_path, "files", "manual", _THIRD)
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()
    repo = tmp_path / "repo"
    repo.mkdir()
    layout = current_layout()
    grants = [
        {"path": str(repo), "access": "ro"},
        {"path": str(layout.config_dir), "access": "ro"},
    ]

    body = {"bound": bound_of(_THIRD), "sandbox_paths": grants}
    response = isolation_api("PUT", "files", HUMAN, body)

    assert response.status_code == 422
    assert _detail(response).startswith("mcp_sandbox_path_refused:1:")
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before


@pytest.mark.parametrize(
    "grants",
    [
        [{"path": "relative/dir", "access": "ro"}],
        [{"path": str(_HERE / "x"), "access": "rwx"}],
        [{"path": str(_HERE / f"x{i}"), "access": "ro"} for i in range(MAX_SANDBOX_PATHS + 1)],
    ],
    ids=["relative", "bad-access", "too-many"],
)
def test_put_rejects_malformed_folders(
    tmp_path: Path, isolation_api: Callable[..., Any], grants: list[dict[str, str]]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})

    body = {"bound": bound_of(_THIRD), "sandbox_paths": grants}
    response = isolation_api("PUT", "files", HUMAN, body)

    assert response.status_code == 422
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_put_fails_closed_when_records_are_unreadable(
    tmp_path: Path, isolation_api: Callable[..., Any]
) -> None:
    _servers_file(tmp_path, {"files": _THIRD})
    (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")

    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": "agent"})

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PERSISTENCE_ERROR"
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_text(encoding="utf-8") == "{broken"


def test_put_schedules_background_relaunch(
    tmp_path: Path, isolation_api: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功存檔排背景重連，不阻塞等待握手。"""
    _servers_file(tmp_path, {"files": _THIRD})
    restart = AsyncMock()
    monkeypatch.setattr(main_module.protocol_manager, "restart_server", restart)

    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": "agent"})

    assert response.status_code == 200
    restart.assert_not_called()
    main_module.protocol_manager.schedule_relaunch.assert_called_once_with("files")


def test_put_without_manager_fails_before_writing(tmp_path, isolation_api, monkeypatch):
    _servers_file(tmp_path, {"files": _THIRD})
    monkeypatch.setattr(main_module.app.state, "context", {})
    response = isolation_api("PUT", "files", HUMAN, {"bound": bound_of(_THIRD), "origin": "agent"})
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "SYSTEM_ERROR"
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


@pytest.mark.parametrize("broken", [False, True])
def test_failed_put_never_schedules(tmp_path, isolation_api, broken):
    _servers_file(tmp_path, {"files": _THIRD})
    if broken:
        (tmp_path / MCP_ISOLATION_FILENAME).write_text("{broken", encoding="utf-8")
    body = {"bound": bound_of(_THIRD) if broken else "0" * 64, "origin": "agent"}
    response = isolation_api("PUT", "files", HUMAN, body)
    assert response.status_code == (500 if broken else 422)
    main_module.protocol_manager.schedule_relaunch.assert_not_called()



def test_origin_and_sandbox_paths_are_not_carried_over() -> None:
    """不在 POLICY_FIELDS：整條目重寫不會把舊的來源或資料夾帶過去（只由寫入點寫）。"""
    assert "origin" not in mcp_tool_policy.POLICY_FIELDS
    assert "sandbox_paths" not in mcp_tool_policy.POLICY_FIELDS
    old = {"command": "npx", "origin": "hub", "sandbox_paths": [{"path": "/", "access": "rw"}]}
    merged = mcp_tool_policy.carry_over_policy({"command": "python"}, old)
    assert "origin" not in merged
    assert "sandbox_paths" not in merged

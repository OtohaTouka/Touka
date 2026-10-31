"""部署凍結、同技能互斥與審批內容核對；所有檔案都在 pytest 沙盒。"""

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.protocols import manager as manager_mod
from src.protocols.mcp.owner import McpConnectionAborted
from src.protocols.registry import ProtocolRegistry
from src.services.evolution_security import EvolutionPathGuard
from src.services.isolation import evolution_command
from src.services.isolation.mcp_launch import McpLaunch, McpLaunchInfo
from src.services.isolation.mcp_profiles import mcp_classification_bases, mcp_state_dirs
from src.services.isolation.profiles import ProfileError
from src.services.isolation.runners import IsolationUnavailableError
from src.tools.builtin import evolution
from src.utils import config_utils
from src.utils.mcp_origin import ALL_SKILLS_DEPLOYED, DeployedSkills

from tests.support.isolation import match_state_root
from tests.support.links import make_dir_link


@pytest.fixture
def workspace(monkeypatch, tmp_path, zone_data_root):
    layout = replace(evolution_command.current_layout(), data_root=zone_data_root)
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    root = Path(EvolutionPathGuard.get_tool_root("sample"))
    for name, value in {"server.py": "pass", "requirements.txt": "", "spec.json": "{}"}.items():
        EvolutionPathGuard.write_tool_file("sample", name, value)
    return root, tmp_path / "server_config.json"


async def invoke(entry, **kwargs):
    calls = {
        "propose": lambda: evolution.propose_new_capability("sample", "N", "D", {}, **kwargs),
        "write": lambda: evolution.write_mcp_implementation("sample", "new", **kwargs),
        "verify": lambda: evolution.verify_mcp_locally("sample", "ping", {}, **kwargs),
        "merged": lambda: evolution.evolution_implement_and_verify(
            "sample", "new", "ping", {}, **kwargs
        ),
        "deploy": lambda: evolution.request_mcp_creation("sample", _interactive=True, **kwargs),
    }
    return await calls[entry]()


@pytest.mark.parametrize("entry", ["propose", "write", "verify", "merged"])
@pytest.mark.parametrize("source", ["renamed", "disabled", "unreadable", "invalid"])
async def test_deployed_entries_refuse_before_any_write(workspace, entry, source):
    root, config = workspace
    if source in {"renamed", "disabled"}:
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "different-name": {
                            "command": "python",
                            "args": [str(root / "server.py")],
                            "enabled": source != "disabled",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    elif source == "unreadable":
        config.mkdir()
    else:
        config.write_text("not json", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    result = json.loads(await invoke(entry))
    assert result["reason"] == (
        "config_unreadable" if source in {"unreadable", "invalid"} else "skill_deployed"
    )
    if source in {"unreadable", "invalid"}:
        assert "config" in result["suggestion"].lower()
        assert "read" in result["suggestion"].lower()
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


async def test_profile_refuses_deployed_even_without_tool_gate(workspace, monkeypatch):
    root, config = workspace
    config.write_text(
        json.dumps({"mcpServers": {"renamed": {"command": str(root / ".venv/python")}}})
    )
    broker = AsyncMock()
    monkeypatch.setattr(evolution_command.runner_broker, "acquire", broker)
    with pytest.raises(evolution_command.EvolutionRefused, match="skill_deployed"):
        await evolution_command.acquire_evolution_launch(None, "sample")
    broker.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_filesystem_root_grant_does_not_block_skill_evolution(workspace, enabled):
    root, config = workspace
    config.write_text(json.dumps({"mcpServers": {"filesystem": {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                                  "@TOUKA_APPDATA@"], "enabled": enabled,
    }}}), encoding="utf-8")
    evolution_command.refuse_deployed("sample")
    result = await evolution.propose_new_capability("sample", "N", "D", {})
    assert "skill_deployed" not in result
    assert json.loads((root / "spec.json").read_text(encoding="utf-8"))["name"] == "N"


@pytest.mark.parametrize("category", ["third", "self"])
@pytest.mark.parametrize("where", ["command", "arg", "env"])
async def test_state_home_relative_deployment_refuses_before_write(
    workspace, monkeypatch, tmp_path, category, where,
):
    root, config = workspace
    layout = replace(evolution_command.current_layout(), state_root=tmp_path / "state")
    match_state_root(monkeypatch, layout.state_root)  # 版面與 RM 用同一個 ST（ADR-0293 P7）
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    monkeypatch.setattr(evolution_command, "proxy_base_dir", lambda: tmp_path / "cwd")
    home, _ = mcp_state_dirs(category, "renamed", layout=layout, location="proxy", create=False)
    relative = os.path.relpath(root / "server.py", home)
    entry = {"command": relative if where == "command" else "python"}
    if where == "arg":
        entry["args"] = [relative]
    elif where == "env":
        entry["env"] = {"PYTHONPATH": relative}
    config.write_text(json.dumps({"mcpServers": {"renamed": entry}}), encoding="utf-8")
    before = (root / "server.py").read_bytes()
    result = json.loads(await invoke("write"))
    assert result["reason"] == "skill_deployed"
    assert (root / "server.py").read_bytes() == before
    assert not home.exists()


async def test_same_physical_skill_under_path_alias_is_frozen(workspace):
    root, config = workspace
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "renamed": {
                        "command": "python",
                        "args": [str(root / "server.py")],
                    }
                }
            }
        )
    )
    result = await evolution.propose_new_capability("sample/../sample", "N", "D", {})
    assert json.loads(result)["reason"] == "invalid_skill_id"


@pytest.mark.parametrize("skill", ["sample/x", "sample/../sample", "SAMPLE/x"])
@pytest.mark.parametrize("deployed", [False, True])
async def test_nested_ids_refused_before_lock_and_write(workspace, monkeypatch, skill, deployed):
    root, config = workspace
    if deployed:
        config.write_text(json.dumps({"mcpServers": {"nested": {
            "command": "python", "args": [str(root / "x/server.py")],
        }}}))
    lock = MagicMock(side_effect=AssertionError("invalid id reached lock"))
    monkeypatch.setattr(evolution, "skill_lock", lock)
    result = await evolution.propose_new_capability(skill, "N", "D", {})
    assert json.loads(result)["reason"] == "invalid_skill_id"
    assert not (root / "x").exists()
    broker = AsyncMock()
    monkeypatch.setattr(evolution_command.runner_broker, "acquire", broker)
    with pytest.raises(Exception, match="invalid_skill_id") as raised:
        await evolution_command.acquire_evolution_launch(None, skill)
    assert not isinstance(raised.value, IsolationUnavailableError)
    broker.assert_not_awaited()


@pytest.mark.parametrize("alias", ["SAMPLE", "physical-alias"])
async def test_physical_alias_freeze_and_lock(workspace, monkeypatch, alias):
    root, config = workspace
    other = Path(EvolutionPathGuard.get_tool_root(alias))
    other.mkdir(exist_ok=True)
    original = os.path.samefile
    pair = {os.path.normcase(str(root)), os.path.normcase(str(other))}
    monkeypatch.setattr(os.path, "samefile", lambda a, b: (
        True if {os.path.normcase(str(a)), os.path.normcase(str(b))} == pair else original(a, b)
    ))
    async with evolution_command.skill_lock("sample"):
        result = await evolution.propose_new_capability(alias, "N", "D", {})
        assert json.loads(result)["reason"] == "skill_busy"
    config.write_text(json.dumps({"mcpServers": {"renamed": {
        "command": "python", "args": [str(root / "server.py")],
    }}}))
    result = await evolution.propose_new_capability(alias, "N", "D", {})
    assert json.loads(result)["reason"] == "skill_deployed"


@pytest.mark.parametrize("deployed_child", [False, True])
async def test_ancestor_and_descendant_roots_are_frozen(workspace, deployed_child):
    root, config = workspace
    deployed = root / "x" if deployed_child else root
    config.write_text(json.dumps({"mcpServers": {"nested": {
        "command": "python", "args": [str(deployed / "server.py")],
    }}}))
    # 子資料夾當技能 id 在第 0 步就不合格（P9），仍是拒絕
    reason = "skill_deployed" if deployed_child else "invalid_skill_id"
    with pytest.raises(evolution_command.EvolutionRefused, match=reason):
        evolution_command.refuse_deployed("sample" if deployed_child else "sample/x")


@pytest.mark.parametrize("filename", ["server.py", "requirements.txt"])
async def test_approval_hash_preserves_line_endings(workspace, monkeypatch, filename):
    root, _ = workspace
    (root / filename).write_bytes(b"a\r\nb\r\n")

    async def card(**_kwargs):
        (root / filename).write_bytes(b"a\nb\n")
        return True, {}

    monkeypatch.setattr(evolution, "request_approval", card)
    store = MagicMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    assert json.loads(await invoke("deploy"))["reason"] == "skill_changed"
    store.assert_not_called()


async def test_unreadable_approval_content_is_refused(workspace, monkeypatch):
    root, _ = workspace
    (root / "requirements.txt").unlink()
    (root / "requirements.txt").mkdir()
    card = AsyncMock()
    monkeypatch.setattr(evolution, "request_approval", card)
    assert json.loads(await invoke("deploy"))["reason"] == "skill_changed"
    card.assert_not_awaited()


@pytest.mark.parametrize("change", ["files", "name"])
async def test_final_checks_are_off_loop_and_leave_no_record(workspace, monkeypatch, change):
    root, config = workspace
    loop_thread = threading.get_ident()
    original = evolution._deployment_config
    checks = []

    def read(skill):
        checks.append(threading.get_ident())
        if len(checks) == 2:
            if change == "files":
                (root / "server.py").write_text("changed")
            else:
                config.write_text(json.dumps({"mcpServers": {"sample": {"command": "taken"}}}))
        return original(skill)

    monkeypatch.setattr(evolution, "_deployment_config", read)
    monkeypatch.setattr(evolution, "request_approval", AsyncMock(return_value=(True, {})))
    store = MagicMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    result = json.loads(await invoke("deploy"))
    assert result["reason"] == ("skill_changed" if change == "files" else "name_taken")
    store.assert_not_called()
    assert len(checks) == 2
    assert all(thread != loop_thread for thread in checks)


@pytest.mark.parametrize("operation", ["write", "verify"])
async def test_late_freeze_is_validation_not_isolation_error(workspace, monkeypatch, operation):
    root, config = workspace
    original = evolution.acquire_evolution_launch

    async def acquire(grant, skill):
        config.write_text(json.dumps({"mcpServers": {"renamed": {
            "command": "python", "args": [str(root / "server.py")],
        }}}))
        return await original(grant, skill)

    monkeypatch.setattr(evolution, "acquire_evolution_launch", acquire)
    result = json.loads(await invoke(operation))
    assert result["error_class"] == "validation_error"
    assert result["reason"] == "skill_deployed"


async def test_combined_internal_deploy_reuses_lock(workspace, monkeypatch):
    monkeypatch.setattr(
        evolution, "write_mcp_implementation", AsyncMock(return_value="Development Success"),
    )
    monkeypatch.setattr(
        evolution, "_verify_mcp", AsyncMock(return_value=evolution.VerifyOutcome(True, "ok")),
    )

    async def card(**_kwargs):
        assert len(evolution_command._skill_locks) == 1
        busy = json.loads(await evolution.propose_new_capability("sample", "N", "D", {}))
        assert busy["reason"] == "skill_busy"
        return True, {}

    monkeypatch.setattr(evolution, "request_approval", card)
    store = MagicMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    result = await evolution.evolution_implement_and_verify(
        "sample", "pass", "ping", {}, submit_for_deploy=True, _interactive=True,
    )
    assert "Evolution Success" in result
    assert json.loads(workspace[1].read_text())["mcpServers"]["sample"]["enabled"]
    assert not evolution_command._skill_locks


async def test_taken_deployment_name_refused_before_card(workspace, monkeypatch):
    _, config = workspace
    original = {"mcpServers": {"sample": {"command": "unrelated", "enabled": False}}}
    config.write_text(json.dumps(original))
    card = AsyncMock(return_value=(True, {}))
    monkeypatch.setattr(evolution, "request_approval", card)
    assert json.loads(await invoke("deploy"))["reason"] == "name_taken"
    card.assert_not_awaited()
    assert json.loads(config.read_text()) == original


@pytest.mark.parametrize("entry", ["propose", "write", "verify", "merged", "deploy"])
async def test_busy_skill_refuses_immediately(workspace, monkeypatch, entry):
    entered, leave = asyncio.Event(), asyncio.Event()

    async def card(**_kwargs):
        entered.set()
        await leave.wait()
        return False, {}

    monkeypatch.setattr(evolution, "request_approval", card)
    pending = asyncio.create_task(invoke("deploy"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(invoke(entry, _evolution_lock={"held": True}), 2)
        assert json.loads(result)["reason"] == "skill_busy"
    finally:
        leave.set()
        await pending
    assert "Success" in await invoke("propose")


@pytest.mark.parametrize("filename", ["server.py", "requirements.txt"])
async def test_changed_approval_content_is_not_deployed(workspace, monkeypatch, filename):
    root, _ = workspace

    async def card(**_kwargs):
        (root / filename).write_text("changed", encoding="utf-8")
        return True, {}

    monkeypatch.setattr(evolution, "request_approval", card)
    save = MagicMock()
    monkeypatch.setattr(config_utils, "save_config_json", save)
    assert json.loads(await invoke("deploy"))["reason"] == "skill_changed"
    save.assert_not_called()


async def test_name_taken_during_approval_does_not_change_isolation_record(workspace, monkeypatch):
    _, config = workspace

    async def card(**_kwargs):
        config.write_text(json.dumps({"mcpServers": {"sample": {"command": "occupied"}}}))
        return True, {}

    store = MagicMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    monkeypatch.setattr(evolution, "request_approval", card)
    assert json.loads(await invoke("deploy"))["reason"] == "name_taken"
    store.assert_not_called()


async def test_lock_capacity_and_cancellation_release(monkeypatch):
    monkeypatch.setattr(evolution_command, "MAX_SKILL_LOCKS", 1)
    entered = asyncio.Event()

    async def occupy():
        async with evolution_command.skill_lock("occupied"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(occupy())
    await entered.wait()
    try:
        with pytest.raises(evolution_command.EvolutionRefused, match="skill_busy"):
            async with evolution_command.skill_lock("second"):
                pytest.fail("capacity was bypassed")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with evolution_command.skill_lock("second"):
        assert len(evolution_command._skill_locks) == 1
    assert not evolution_command._skill_locks


@pytest.mark.parametrize("started", [True, False])
async def test_deployment_reports_manager_outcome(workspace, monkeypatch, started):
    monkeypatch.setattr(evolution, "request_approval", AsyncMock(return_value=(True, {})))
    store = MagicMock()
    store.return_value.amutate = AsyncMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    manager = MagicMock(add_server=AsyncMock(return_value=started))
    result = await invoke("deploy", _ctx={"protocol_manager": manager})
    manager.add_server.assert_awaited_once()
    assert ("not started" in result) is (not started)
    assert json.loads(workspace[1].read_text())["mcpServers"]["sample"]["enabled"]


async def test_saved_deployment_with_aborted_transport_does_not_report_started(
    workspace, monkeypatch
):
    monkeypatch.setattr(evolution, "request_approval", AsyncMock(return_value=(True, {})))
    registry = ProtocolRegistry("__no_hot_mount_config__.json")
    manager = manager_mod.ProtocolManager(registry)
    launch = McpLaunch(McpLaunchInfo("self", "evolution", "ok", "launched", "landlock"))
    monkeypatch.setattr(manager._launcher, "plan_server_launch", AsyncMock(return_value=launch))
    transport = AsyncMock(side_effect=McpConnectionAborted())
    monkeypatch.setattr(manager_mod, "connect_mcp_server", transport)
    try:
        result = await invoke("deploy", _ctx={"protocol_manager": manager})
        assert "saved; not started" in result
        transport.assert_awaited_once()
        assert json.loads(workspace[1].read_text())["mcpServers"]["sample"]["enabled"]
        assert manager.handle_pool.get(manager.handle_key_for("sample", None)) is None
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("skill", [
    "sample.", "a:b", "CON", "nul", "PRN", "AUX", "COM1", "lpt9", "CONIN$",
    "_sample", "a" * 65, "工具", "sample\n", None, 5, {}, [],
])
async def test_invalid_ids_refused_before_lock(workspace, monkeypatch, skill):
    lock = MagicMock(side_effect=AssertionError("invalid id reached lock"))
    monkeypatch.setattr(evolution, "skill_lock", lock)
    result = json.loads(await evolution.propose_new_capability(skill, "N", "D", {}))
    assert result["reason"] == "invalid_skill_id"
    with pytest.raises(evolution_command.EvolutionRefused, match="invalid_skill_id"):
        evolution_command.validate_skill_id(skill)
    lock.assert_not_called()


@pytest.mark.parametrize("skill", ["a", "A-9_ok", "a" * 64, "COM10", "null"])
def test_valid_skill_ids(skill):
    evolution_command.validate_skill_id(skill)


@pytest.mark.parametrize("alias", ["sample.", "SAMPLE ", "sample. /child"])
def test_nonexistent_path_aliases_overlap(tmp_path, alias):
    root = tmp_path / "sample"
    other = tmp_path / Path(alias)
    assert evolution_command._overlapping_roots(root, other)
    assert evolution_command._overlapping_roots(other, root)
    assert not evolution_command._overlapping_roots(root, tmp_path / "sample-other")


def test_a_link_loop_counts_as_overlapping(tmp_path, monkeypatch):
    """連結迴圈時 ``Path.resolve`` 丟 ``RuntimeError``（不是 ``OSError``），也當重疊."""

    def loop(self, strict=False):  # noqa: ANN001, ANN202, ARG001
        raise RuntimeError

    monkeypatch.setattr(Path, "resolve", loop)
    assert evolution_command._overlapping_roots(tmp_path / "sample", tmp_path / "other")


async def test_deployment_without_context_keeps_verified_identity(workspace, monkeypatch):
    identity = AgentIdentity.for_role("Alice", "Owner")
    card = AsyncMock(return_value=(True, {}))
    monkeypatch.setattr(evolution, "request_approval", card)
    monkeypatch.setattr(evolution, "McpIsolationStore", MagicMock())
    result = await invoke("deploy", identity=identity, agent_id="wrong", _ctx=None)
    assert "Evolution Success" in result
    assert "not started" in result
    assert card.call_args.kwargs["identity"] is identity
    assert json.loads(workspace[1].read_text())["mcpServers"]["sample"]["enabled"]


async def test_missing_binary_snapshot_cannot_deploy(workspace, monkeypatch):
    root, config = workspace
    (root / "requirements.txt").unlink()

    async def approve(**_kwargs):
        (root / "requirements.txt").write_bytes(b"")
        return True, {}

    monkeypatch.setattr(evolution, "request_approval", approve)
    store = MagicMock()
    monkeypatch.setattr(evolution, "McpIsolationStore", store)
    assert json.loads(await invoke("deploy"))["reason"] == "skill_changed"
    store.assert_not_called()
    assert not config.exists()


# ── refuse_deployed 的特殊值與名稱比對（ADR-0293 PR 2，Q3、N3）──────────────────


@pytest.fixture
def empty_config(monkeypatch, tmp_path, zone_data_root):
    monkeypatch.setattr(config_utils, "CONFIG_DIR", tmp_path)
    return zone_data_root


def _deployed(monkeypatch, value):
    monkeypatch.setattr(evolution_command, "deployed_skills", lambda *_a, **_k: value)


def test_every_deployed_refuses(empty_config, monkeypatch):
    _deployed(monkeypatch, ALL_SKILLS_DEPLOYED)
    with pytest.raises(evolution_command.EvolutionRefused, match="skill_deployed"):
        evolution_command.refuse_deployed("sample")


@pytest.mark.parametrize("name", ["sample", "SAMPLE", "sample.", "Sample. ", "sample "])
def test_deployed_name_with_the_same_key_refuses(empty_config, monkeypatch, name):
    _deployed(monkeypatch, DeployedSkills(frozenset({name})))
    with pytest.raises(evolution_command.EvolutionRefused, match="skill_deployed"):
        evolution_command.refuse_deployed("sample")


def test_bad_deployed_names_do_not_stop_other_skills(empty_config, monkeypatch):
    _deployed(monkeypatch, DeployedSkills(frozenset({"soul", "a:b", "a/b", "..", "", "x" * 80})))
    evolution_command.refuse_deployed("sample")


def test_bad_skill_id_is_refused_first(empty_config, monkeypatch):
    """第 0 步：全部算已部署時，名稱不合格仍先回 ``invalid_skill_id``（不是 ``skill_deployed``）."""
    _deployed(monkeypatch, ALL_SKILLS_DEPLOYED)
    with pytest.raises(evolution_command.EvolutionRefused, match="invalid_skill_id"):
        evolution_command.refuse_deployed("soul")


def test_overlapping_deployed_folder_refuses(empty_config, monkeypatch, tmp_path):
    managed = empty_config / "mcp_servers" / "managed"
    (managed / "sample" / "sub").mkdir(parents=True)
    make_dir_link(managed / "other", managed / "sample" / "sub")
    _deployed(monkeypatch, DeployedSkills(frozenset({"other"})))
    with pytest.raises(evolution_command.EvolutionRefused, match="skill_deployed"):
        evolution_command.refuse_deployed("sample")


def test_classification_profile_error_is_config_unreadable(empty_config, monkeypatch, tmp_path):
    """版面與 RM 的 ST 不同（P7）：真的 ``mcp_classification_bases`` 丟 ``ProfileError``."""
    (tmp_path / "server_config.json").write_text(
        json.dumps({"mcpServers": {"renamed": {"command": "python", "args": ["x.py"]}}}),
        encoding="utf-8",
    )
    layout = replace(
        evolution_command.current_layout(),
        data_root=empty_config,
        state_root=tmp_path / "another_state",
    )
    monkeypatch.setattr(evolution_command, "current_layout", lambda: layout)
    with pytest.raises(ProfileError):
        mcp_classification_bases("renamed", layout=layout, base_dir=tmp_path)
    with pytest.raises(evolution_command.EvolutionRefused, match="config_unreadable"):
        evolution_command.refuse_deployed("sample")


def test_overlap_sees_a_link_to_a_sibling_subfolder(tmp_path):
    first = tmp_path / "A"
    (first / "sub").mkdir(parents=True)
    make_dir_link(tmp_path / "B", first / "sub")
    assert evolution_command._overlapping_roots(str(first), str(tmp_path / "B"))
    assert evolution_command._overlapping_roots(str(tmp_path / "B"), str(first))
    assert not evolution_command._overlapping_roots(str(first), str(tmp_path / "C"))


def test_overlap_counts_an_os_error_as_overlap(tmp_path, monkeypatch):
    first, second = str(tmp_path / "x"), str(tmp_path / "y")
    original = os.path.realpath

    def broken(path, *args, **kwargs):  # 只對這兩條路徑失敗，測試的絆線照常
        if os.fspath(path) in {first, second}:
            raise OSError("unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(evolution_command.os.path, "realpath", broken)
    assert evolution_command._overlapping_roots(first, second)

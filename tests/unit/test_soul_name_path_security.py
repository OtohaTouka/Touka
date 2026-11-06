"""靈魂與代理人的名稱不能被當成路徑，也不能用別名指到別人的資料夾（sec-9 PR 1b）。

名稱會接成 ``<data>/soul/<名稱>`` 這類資料夾。在 Windows 上，``\\\\主機\\分享`` 這類名稱接上去會
變成網路路徑，``exists()``、``resolve()`` 會先連出去；不分大小寫的檔案系統（Windows、macOS）上，
只差大小寫、8.3 短檔名的名稱會指到同一個資料夾。修好之後：

- 名稱一律先過 ``is_zone_segment``，再碰檔案系統；
- 已經有的資料夾，名稱要逐字出現在目錄列表裡（不在列表、檔案系統卻說存在，就是別名）；
- ``is_owner`` 對找不到擁有者的目標，只放行逐字已知的代理人；
- 非人類只能用編輯改逐字存在、已標成靈魂、和自己同一位擁有者的靈魂（除錯身分不看擁有者）。

兩道防線：``_no_network_paths`` 讓任何碰到哨兵主機 ``192.0.2.1``（TEST-NET，不會真的連出去）
的檔案系統呼叫都記下並失敗；``_writes_stay_in_sandbox`` 讓寫到 tmp_path 以外的動作直接失敗。
只用 tmp_path 的 data 根（``zone_data_root``）與 mock，不碰 data/。
"""

from __future__ import annotations

import builtins
import ctypes
import errno
import io
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from memory_server.services import manager as memory_manager_module
from memory_server.services.manager import MemoryManager
from memory_server.tools import core_file_ops
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig, SoulCreateRequest, SoulDeleteRequest, SoulUpdateRequest
from src.routers import souls as souls_router
from src.routers import stats as stats_router
from src.services import authorization_gateway, avatar_service, memory_collection_guard
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.resource_manager import resource_manager
from src.services.soul_service import soul_service
from src.utils import custom_io, soul_io
from src.utils.time_utils import get_current_epoch

SENTINEL = "192.0.2.1"
UNC = "\\\\192.0.2.1\\share\\x"
UNC_FORWARD = "//192.0.2.1/share/x"
BAD_NAMES = ["..", ".", "a/b", "a\\b", UNC, UNC_FORWARD]
WINDOWS_BAD_NAMES = ["con", "C:foo", "a:b", "name.", "name "]
WINDOWS = sys.platform == "win32"
ADMIN = AgentIdentity.for_role(agent_id="zzAdminUser", role="admin", is_human=True)
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
OWNER_A = AgentIdentity.for_role(agent_id="zzAgentOfA", role="Owner")
SYSTEM = AgentIdentity.for_role(agent_id="zzSystemScan", role="System")

# ── 防線：不連外 ──────────────────────────────────────────────────────────────
_FS_CALLS = [
    (os, "stat"), (os, "lstat"), (os, "scandir"), (os, "listdir"), (os, "mkdir"),
    (os, "makedirs"), (os, "open"), (os, "rmdir"), (os, "unlink"), (os, "remove"),
    (os, "rename"), (os, "replace"), (os, "readlink"), (os, "access"),
    (os.path, "exists"), (os.path, "lexists"), (os.path, "isdir"), (os.path, "isfile"),
    (os.path, "islink"), (os.path, "realpath"), (builtins, "open"), (io, "open"),
]


def _mentions_sentinel(value: object) -> bool:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif isinstance(value, os.PathLike):
        value = os.fspath(value)
    return isinstance(value, str) and SENTINEL in value.replace("/", "\\")


@pytest.fixture(autouse=True)
def _no_network_paths(monkeypatch):
    touched: list[str] = []
    for owner, name in _FS_CALLS:
        real = getattr(owner, name)

        def guarded(*args, _real=real, _name=name, **kwargs):
            if args and _mentions_sentinel(args[0]):
                touched.append(f"{_name}({args[0]!r})")
                raise OSError(errno.EHOSTUNREACH, "network path in a test", str(args[0]))
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, guarded)
    yield
    assert touched == [], f"touched network paths: {touched}"


# ── 防線：寫入只在沙盒裡 ───────────────────────────────────────────────────────
_RM_WRITES = {
    "write_file_text": (0,), "write_file_binary": (0,), "write_json": (0,), "make_dir": (0,),
    "remove_dir": (0,), "remove_file": (0,), "move_file": (0, 1),
}
_PATH_WRITES = ("write_text", "write_bytes", "mkdir", "replace", "rename", "unlink", "rmdir")


@pytest.fixture(autouse=True)
def _writes_stay_in_sandbox(monkeypatch, tmp_path):
    allowed = tmp_path.resolve()

    def check(target: object, what: str) -> None:
        if _mentions_sentinel(target):
            pytest.fail(f"{what} on a network path: {target!r}")
        if not Path(os.fspath(target)).resolve().is_relative_to(allowed):
            pytest.fail(f"{what} outside the sandbox: {target!r}")

    for name, positions in _RM_WRITES.items():
        real = getattr(resource_manager, name)

        def rm_guarded(*args, _real=real, _name=name, _positions=positions, **kwargs):
            for index in _positions:
                check(args[index], _name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(resource_manager, name, rm_guarded)
    for name in _PATH_WRITES:
        real = getattr(Path, name)

        def path_guarded(self, *args, _real=real, _name=name, **kwargs):
            check(self, f"Path.{_name}")
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, path_guarded)
    for owner, name in ((os, "replace"), (shutil, "copy2"), (shutil, "rmtree"), (shutil, "move")):
        real = getattr(owner, name)

        def fn_guarded(*args, _real=real, _name=name, **kwargs):
            check(args[-1] if _name in ("copy2", "move", "replace") else args[0], _name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(owner, name, fn_guarded)


# ── 輔助 ──────────────────────────────────────────────────────────────────────
def _make_soul(data: Path, name: str, *, is_soul: bool = True, base: str = "soul") -> Path:
    folder = data / base / name
    folder.mkdir(parents=True)
    if base == "soul":
        (folder / "config.json").write_text(json.dumps({"is_toukasoul": is_soul}), "utf-8")
        (folder / "soul.md").write_text(f"persona of {name}", "utf-8")
    return folder


def _case_insensitive(root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    probe = root / "zzCaseProbe"
    probe.mkdir()
    try:
        return (root / "ZZCASEPROBE").exists()
    finally:
        probe.rmdir()


def _short_name(path: Path) -> str | None:
    """Windows 的 8.3 短檔名；沒有（或和長名一樣）時回 None."""
    if not WINDOWS:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024):
        return None
    short = Path(buffer.value).name
    return short if short.casefold() != path.name.casefold() else None


def _users(monkeypatch, *owned: tuple[str, list[str]]) -> None:
    now = int(get_current_epoch())
    users = [
        SimpleNamespace(id=uid, owned_agents=agents, last_active_at=now) for uid, agents in owned
    ]
    monkeypatch.setattr(AuthService, "get_all_users", lambda self: users)


def _known(monkeypatch, *, souls=(), customs=(), registry=()) -> None:
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: list(souls))
    monkeypatch.setattr(custom_io, "list_custom_ids", lambda: list(customs))
    monkeypatch.setattr(
        custom_io, "custom_file_exists", lambda identity, _name: identity.agent_id in customs
    )
    monkeypatch.setattr(agent_service, "registered_ids", lambda: set(registry))


def _code(exc: pytest.ExceptionInfo) -> ErrorCode:
    assert isinstance(exc.value, ToukaException)
    return exc.value.code


@pytest.fixture
def soul_ops(monkeypatch):
    """靈魂端點的外部依賴換成假的：密碼重驗直接過、記憶與指令清理只記錄."""
    calls: list[str] = []
    monkeypatch.setattr(souls_router, "_verify_admin_stepup", lambda *_a, **_k: None)
    monkeypatch.setattr(souls_router, "assert_memory_cleared", AsyncMock())
    monkeypatch.setattr(souls_router, "assert_timeline_cleared", AsyncMock())
    monkeypatch.setattr(souls_router, "wipe_agent_homes", AsyncMock())
    monkeypatch.setattr(souls_router.terminal, "stop_agent_commands", AsyncMock(return_value=0))
    monkeypatch.setattr(
        souls_router.session_service, "invalidate_agent_sessions", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        memory_collection_guard, "assert_id_free", lambda identity, **_k: calls.append("free")
    )
    monkeypatch.setattr(
        soul_io, "purge_soul_storage", lambda agent_id: calls.append(f"purge:{agent_id}") or {}
    )
    return calls


# ── A. 名稱必須是單一安全段 ────────────────────────────────────────────────────
@pytest.mark.parametrize("name", BAD_NAMES + (WINDOWS_BAD_NAMES if WINDOWS else []))
def test_soul_lookup_refuses_names_that_are_not_one_segment(zone_data_root, name):
    assert soul_service.is_toukasoul(name) is False
    assert soul_service.is_toukasoul(AgentIdentity(agent_id=name, agent_role="Base")) is False
    assert soul_service.get_soul_data(name) == (None, {})


@pytest.mark.parametrize("vault", ["soul", "custom_soul", "legacy"])
@pytest.mark.parametrize("name", BAD_NAMES)
def test_vault_paths_refuse_names_that_are_not_one_segment(zone_data_root, vault, name):
    identity = AgentIdentity(agent_id=name, agent_role="Owner")
    assert resource_manager.get_vault_path(vault, identity) is None


@pytest.mark.parametrize("name", BAD_NAMES)
def test_stats_path_refuses_names_that_are_not_one_segment(zone_data_root, name):
    with pytest.raises(ToukaException) as exc:
        resource_manager.get_stats_path(AgentIdentity(agent_id=name))
    assert _code(exc) is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("name", ["DebugAgent", "DispatchAgent", "zz-soul_1", "b3f1c2d4-uuid"])
def test_existing_names_keep_their_paths(zone_data_root, name):
    identity = AgentIdentity(agent_id=name)
    legacy = resource_manager.get_vault_path("legacy", identity)
    assert legacy == str(zone_data_root / "legacy" / name)
    custom = resource_manager.get_vault_path("custom_soul", identity)
    assert custom == str(zone_data_root / "custom" / name)
    assert resource_manager.get_stats_path(identity) == str(zone_data_root / "stats" / name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_avatar_lookup_refuses_names_that_are_not_one_segment(zone_data_root, name):
    assert avatar_service._safe_candidate(name, "png") is None
    assert avatar_service.find_avatar(name) is None


def test_public_avatar_endpoint_does_not_reach_the_network(zone_data_root):
    response = TestClient(app).get(f"/v1/users/{quote(UNC, safe='')}/avatar")
    assert response.status_code == 404


def test_avatar_of_a_real_user_still_works(zone_data_root):
    user_id = "6f1d7a52-3c1e-4c9a-9a51-0d1b2c3d4e5f"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    assert avatar_service.save_avatar(user_id, png) == "png"
    found = avatar_service.find_avatar(user_id)
    assert found is not None
    assert found[0].name == f"{user_id}.png"


@pytest.mark.parametrize("name", BAD_NAMES)
def test_memory_legacy_dir_does_not_rebuild_a_refused_path(zone_data_root, name):
    identity = AgentIdentity(agent_id=name)
    with pytest.raises(memory_manager_module.VaultPathRefusedError):
        MemoryManager().get_legacy_dir(identity, resource_manager)
    assert MemoryManager().get_soul_dir(identity, resource_manager) is None
    assert not (zone_data_root / "legacy").exists() or os.listdir(zone_data_root / "legacy") == []


@pytest.mark.parametrize("name", BAD_NAMES)
def test_memory_standalone_fallback_checks_the_name_too(zone_data_root, name):
    identity = AgentIdentity(agent_id=name)
    with pytest.raises(memory_manager_module.VaultPathRefusedError):
        MemoryManager().get_legacy_dir(identity)
    assert MemoryManager().get_soul_dir(identity) is None


# ── B. 靈魂名稱：逐字出現在列表 ────────────────────────────────────────────────
def test_existing_soul_is_found_by_its_exact_name(zone_data_root):
    _make_soul(zone_data_root, "zzRealSoulName")
    assert soul_service.is_toukasoul("zzRealSoulName") is True
    persona, config = soul_service.get_soul_data("zzRealSoulName")
    assert persona == "persona of zzRealSoulName"
    assert config["is_toukasoul"] is True
    soul_io.check_soul_name(AgentIdentity(agent_id="zzRealSoulName"))
    soul_io.check_soul_name(AgentIdentity(agent_id="zzNotThereYet"))


def _aliases(data: Path, folder: Path) -> list[str]:
    names = []
    if _case_insensitive(data / "probe-root"):
        names.append(folder.name.swapcase())
    short = _short_name(folder)
    if short:
        names.append(short)
    return names


@pytest.mark.parametrize("base", ["soul", "legacy", "personal_drive"])
def test_aliases_of_an_existing_folder_are_refused(zone_data_root, base):
    folder = _make_soul(zone_data_root, "zzLongSoulFolderName", base=base)
    aliases = _aliases(zone_data_root, folder)
    if not aliases:
        pytest.skip("case-sensitive file system without 8.3 names")
    for alias in aliases:
        with pytest.raises(ToukaException) as exc:
            soul_io.check_soul_name(AgentIdentity(agent_id=alias))
        assert _code(exc) is ErrorCode.VALIDATION_ERROR, alias
        if base == "soul":
            assert soul_service.is_toukasoul(alias) is False, alias
            assert soul_service.get_soul_data(alias) == (None, {}), alias
            assert (agent_service.identify_agent(alias) or {}).get("is_toukasoul") is not True


def test_alias_check_uses_the_file_system_not_casefold(zone_data_root, monkeypatch):
    """Linux CI 也測得到：存在但不在列表（模擬不分大小寫的檔案系統）就是別名."""
    _make_soul(zone_data_root, "zzRealSoulName")
    alias = zone_data_root / "soul" / "ZZREALSOULNAME"
    real_exists = Path.exists

    def exists(self, *args, **kwargs):
        return True if self == alias else real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    with pytest.raises(ToukaException) as exc:
        soul_io.check_soul_name(AgentIdentity(agent_id="ZZREALSOULNAME"))
    assert _code(exc) is ErrorCode.VALIDATION_ERROR


def test_names_that_differ_only_by_case_both_work_on_a_case_sensitive_system(zone_data_root):
    if _case_insensitive(zone_data_root / "probe-root"):
        pytest.skip("needs a case-sensitive file system")
    _make_soul(zone_data_root, "zzFoo")
    _make_soul(zone_data_root, "zzfoo")
    for name in ("zzFoo", "zzfoo"):
        soul_io.check_soul_name(AgentIdentity(agent_id=name))
        assert soul_service.is_toukasoul(name) is True


def test_incomplete_listing_refuses_the_name(zone_data_root, monkeypatch):
    _make_soul(zone_data_root, "zzRealSoulName")

    def broken(*_a, **_k):
        failed = "cannot list"
        raise OSError(failed)

    monkeypatch.setattr(resource_manager, "list_child_names", broken)
    with pytest.raises(ToukaException) as exc:
        soul_io.check_soul_name(AgentIdentity(agent_id="zzRealSoulName"))
    assert _code(exc) is ErrorCode.SOUL_VAULT_ERROR


@pytest.mark.parametrize("name", BAD_NAMES)
def test_check_soul_name_refuses_names_that_are_not_one_segment(zone_data_root, name):
    with pytest.raises(ToukaException) as exc:
        soul_io.check_soul_name(AgentIdentity(agent_id=name))
    assert _code(exc) is ErrorCode.VALIDATION_ERROR


# ── 靈魂三個端點 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", [UNC, "a\\b", "C:foo" if WINDOWS else "a\\c"])
async def test_create_soul_refuses_bad_names(zone_data_root, soul_ops, name):
    with pytest.raises(ToukaException) as exc:
        await souls_router.create_soul(
            SoulCreateRequest(agent_id=name, config=SoulConfig(base_model=None)), ADMIN
        )
    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    assert "free" not in soul_ops  # 名稱檢查先於撞名檢查（不碰任何資料）


async def test_create_soul_refuses_the_short_name_of_an_existing_soul(zone_data_root, soul_ops):
    folder = _make_soul(zone_data_root, "zzLongSoulFolderName")
    short = _short_name(folder)
    if not short:
        pytest.skip("no 8.3 short name here")
    with pytest.raises(ToukaException) as exc:
        await souls_router.create_soul(
            SoulCreateRequest(agent_id=short, config=SoulConfig(base_model=None)), ADMIN
        )
    assert _code(exc) is ErrorCode.VALIDATION_ERROR


async def test_admin_update_through_an_alias_is_refused(zone_data_root, soul_ops):
    folder = _make_soul(zone_data_root, "zzLongSoulFolderName")
    aliases = _aliases(zone_data_root, folder)
    if not aliases:
        pytest.skip("case-sensitive file system without 8.3 names")
    for alias in aliases:
        with pytest.raises(ToukaException) as exc:
            await souls_router.update_soul(alias, SoulUpdateRequest(persona="HIJACK"), ADMIN)
        assert _code(exc) is ErrorCode.VALIDATION_ERROR, alias
    assert (folder / "soul.md").read_text("utf-8") == "persona of zzLongSoulFolderName"


@pytest.mark.parametrize("name", [UNC, "a\\b"])
async def test_admin_update_refuses_bad_names(zone_data_root, soul_ops, name):
    with pytest.raises(ToukaException) as exc:
        await souls_router.update_soul(name, SoulUpdateRequest(persona="x"), ADMIN)
    assert _code(exc) is ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("name", ["debugagent", "DEBUGAGENT", "Debugagent"])
async def test_debug_agent_cannot_be_deleted_by_any_spelling(zone_data_root, soul_ops, name):
    with pytest.raises(ToukaException) as exc:
        await souls_router.delete_soul(name, SoulDeleteRequest(admin_password="pw"), ADMIN)
    assert _code(exc) is ErrorCode.AUTH_PERMISSION_DENIED
    assert not any(call.startswith("purge:") for call in soul_ops)


@pytest.mark.parametrize("base", ["soul", "legacy", "personal_drive"])
async def test_delete_through_an_alias_is_refused(zone_data_root, soul_ops, base):
    folder = _make_soul(zone_data_root, "zzLongSoulFolderName", base=base)
    aliases = _aliases(zone_data_root, folder)
    if not aliases:
        pytest.skip("case-sensitive file system without 8.3 names")
    for alias in aliases:
        with pytest.raises(ToukaException) as exc:
            await souls_router.delete_soul(alias, SoulDeleteRequest(admin_password="pw"), ADMIN)
        assert _code(exc) is ErrorCode.VALIDATION_ERROR, alias
    assert folder.exists()
    assert not any(call.startswith("purge:") for call in soul_ops)


@pytest.mark.parametrize("name", [UNC, "a\\b"])
async def test_delete_refuses_bad_names(zone_data_root, soul_ops, name):
    with pytest.raises(ToukaException) as exc:
        await souls_router.delete_soul(name, SoulDeleteRequest(admin_password="pw"), ADMIN)
    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    assert not any(call.startswith("purge:") for call in soul_ops)


# ── D. 第 5 題：非人類只能編輯已存在、和自己同一位擁有者的靈魂 ────────────────────
async def test_non_humans_cannot_create_a_soul_by_editing(zone_data_root, soul_ops, monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA", "zzNewSoul"]))
    for caller in (OWNER_A, DEBUGGER):
        with pytest.raises(ToukaException) as exc:
            await souls_router.update_soul(
                "zzNewSoul", SoulUpdateRequest(config=SoulConfig(), persona="p"), caller
            )
        assert _code(exc) is ErrorCode.AUTH_PERMISSION_DENIED
    assert not (zone_data_root / "soul" / "zzNewSoul").exists()


async def test_non_humans_cannot_write_into_or_convert_a_folder_that_is_not_a_soul(
    zone_data_root, soul_ops, monkeypatch
):
    folder = _make_soul(zone_data_root, "zzPlainFolder", is_soul=False)
    _users(monkeypatch, ("userA", ["zzAgentOfA", "zzPlainFolder"]))
    for caller in (OWNER_A, DEBUGGER):
        for request in (
            SoulUpdateRequest(persona="p"),
            SoulUpdateRequest(config=SoulConfig(is_toukasoul=True)),
        ):
            with pytest.raises(ToukaException) as exc:
                await souls_router.update_soul("zzPlainFolder", request, caller)
            assert _code(exc) is ErrorCode.AUTH_PERMISSION_DENIED
    assert json.loads((folder / "config.json").read_text("utf-8")) == {"is_toukasoul": False}
    assert (folder / "soul.md").read_text("utf-8") == "persona of zzPlainFolder"


async def test_owner_cannot_edit_a_soul_of_another_user_or_nobody(
    zone_data_root, soul_ops, monkeypatch
):
    _make_soul(zone_data_root, "zzSoulOfB")
    _make_soul(zone_data_root, "zzOwnerless")
    _users(monkeypatch, ("userA", ["zzAgentOfA"]), ("userB", ["zzSoulOfB"]))
    _known(monkeypatch, souls=["zzAgentOfA", "zzSoulOfB", "zzOwnerless"])
    for target in ("zzSoulOfB", "zzOwnerless"):
        with pytest.raises(ToukaException) as exc:
            await souls_router.update_soul(target, SoulUpdateRequest(persona="HIJACK"), OWNER_A)
        assert _code(exc) is ErrorCode.AUTH_PERMISSION_DENIED, target
        assert (zone_data_root / "soul" / target / "soul.md").read_text("utf-8") != "HIJACK"


async def test_owner_edits_itself_and_souls_of_the_same_user(zone_data_root, soul_ops, monkeypatch):
    _make_soul(zone_data_root, "zzAgentOfA")
    _make_soul(zone_data_root, "zzSisterSoul")
    _users(monkeypatch, ("userA", ["zzAgentOfA", "zzSisterSoul"]))
    _known(monkeypatch, souls=["zzAgentOfA", "zzSisterSoul"])
    for target in ("zzAgentOfA", "zzSisterSoul"):
        result = await souls_router.update_soul(target, SoulUpdateRequest(persona="new"), OWNER_A)
        assert result["status"] == "success"
        assert (zone_data_root / "soul" / target / "soul.md").read_text("utf-8") == "new"


async def test_debugger_edits_any_existing_soul(zone_data_root, soul_ops, monkeypatch):
    _make_soul(zone_data_root, "zzSoulOfB")
    _users(monkeypatch, ("userB", ["zzSoulOfB"]))
    result = await souls_router.update_soul("zzSoulOfB", SoulUpdateRequest(persona="fix"), DEBUGGER)
    assert result["status"] == "success"
    assert (zone_data_root / "soul" / "zzSoulOfB" / "soul.md").read_text("utf-8") == "fix"


async def test_admin_still_creates_and_converts_by_editing(zone_data_root, soul_ops, monkeypatch):
    _make_soul(zone_data_root, "zzPlainFolder", is_soul=False)
    monkeypatch.setattr(souls_router, "get_active_brain", lambda _identity: None)
    for target in ("zzBrandNew", "zzPlainFolder"):
        result = await souls_router.update_soul(
            target, SoulUpdateRequest(config=SoulConfig(is_toukasoul=True), persona="p"), ADMIN
        )
        assert result["status"] == "success"
        config = json.loads((zone_data_root / "soul" / target / "config.json").read_text("utf-8"))
        assert config["is_toukasoul"] is True
    assert soul_ops.count("free") == 2  # 用編輯建立或轉換時也檢查 collection 撞名


# ── C. is_owner ────────────────────────────────────────────────────────────────
def test_owner_path_does_not_treat_an_alias_as_ownerless(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA"]), ("userB", ["zzSoulOfB"]))
    _known(monkeypatch, souls=["zzAgentOfA", "zzSoulOfB"])
    target = AgentIdentity(agent_id="zzsoulofb")
    assert resource_manager.is_owner(target, OWNER_A) is False


def test_owner_path_does_not_allow_unknown_targets(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA"]))
    _known(monkeypatch, souls=["zzAgentOfA"])
    for name in ("zzNobodyKnows", "ZZLONG~1"):
        assert resource_manager.is_owner(AgentIdentity(agent_id=name), OWNER_A) is False, name


def test_owner_path_still_allows_known_ownerless_targets(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA"]))
    _known(monkeypatch, souls=["zzAgentOfA"], customs=["zzPublicModel"], registry=["zzRegistered"])
    for name in ("zzPublicModel", "zzRegistered", "DispatchAgent"):
        assert resource_manager.is_owner(AgentIdentity(agent_id=name), OWNER_A) is True, name


def test_exact_owners_win_over_names_that_differ_only_by_case(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA", "zzFoo"]), ("userB", ["zzfoo"]))
    _known(monkeypatch, souls=["zzAgentOfA", "zzFoo", "zzfoo"])
    assert resource_manager.is_owner(AgentIdentity(agent_id="zzfoo"), OWNER_A) is False
    assert resource_manager.is_owner(AgentIdentity(agent_id="zzFoo"), OWNER_A) is True


def test_human_and_system_paths_are_unchanged(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA"]))
    _known(monkeypatch, souls=["zzAgentOfA"])
    human = AgentIdentity.for_role(agent_id="userA", role="normal", is_human=True)
    user = SimpleNamespace(owned_agents=["zzAgentOfA"])
    monkeypatch.setattr(AuthService, "get_user_by_id", lambda self, _uid: user)
    assert resource_manager.is_owner(AgentIdentity(agent_id="zzagentofa"), human) is False
    assert resource_manager.is_owner(AgentIdentity(agent_id="zzAgentOfA"), human) is True
    assert resource_manager.is_owner(AgentIdentity(agent_id="zzNobodyKnows"), SYSTEM) is True


def test_shares_human_owner(monkeypatch):
    _users(monkeypatch, ("userA", ["zzAgentOfA", "zzSister"]), ("userB", ["zzSoulOfB"]))
    shares = authorization_gateway.shares_human_owner
    assert shares(AgentIdentity(agent_id="zzSister"), OWNER_A) is True
    assert shares(AgentIdentity(agent_id="zzSoulOfB"), OWNER_A) is False
    assert shares(AgentIdentity(agent_id="zzsister"), OWNER_A) is False
    assert shares(AgentIdentity(agent_id="zzNobody"), OWNER_A) is False


# ── 舊對話統計與同步 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("endpoint", ["get_legacy_sync_stats", "trigger_legacy_sync"])
async def test_legacy_endpoints_check_the_name_first(zone_data_root, monkeypatch, endpoint):
    _users(monkeypatch, ("userA", ["zzAgentOfA"]))
    _known(monkeypatch, souls=["zzAgentOfA"])
    monkeypatch.setattr(agent_service, "get_all_agents", lambda: [{"id": "zzAgentOfA"}])
    handler = getattr(stats_router, endpoint)
    with pytest.raises(ToukaException) as exc:
        await handler(UNC, ADMIN)
    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    with pytest.raises(ToukaException) as exc:
        await handler("zzNobodyKnows", OWNER_A)
    assert _code(exc) is ErrorCode.AUTH_PERMISSION_DENIED
    assert not (zone_data_root / "legacy" / "zzNobodyKnows").exists()


# ── E. 檔案工具與專案根目錄（Windows 的網路路徑） ─────────────────────────────────
@pytest.mark.skipif(not WINDOWS, reason="UNC paths only exist on Windows")
@pytest.mark.parametrize("target", [UNC, UNC_FORWARD, "\\/192.0.2.1/share/x"])
def test_file_tools_refuse_network_paths_before_resolving(tmp_path, target):
    with pytest.raises(core_file_ops.PathGuardError):
        core_file_ops._enforce_root_bound(target, str(tmp_path))


@pytest.mark.skipif(not WINDOWS, reason="UNC paths only exist on Windows")
async def test_project_root_on_a_network_share_is_refused_before_resolving():
    assert await resource_manager.is_within_workspace(UNC) is False

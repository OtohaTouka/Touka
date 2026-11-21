"""刪掉的靈魂留下的封存與個人空間不給同名的新靈魂（ADR-0328，同名接手 PR 3b；禁令 20 對應安全測試）.

修補前：刪除靈魂時 ``legacy/<名字>``（舊對話庫的封存）或 ``personal_drive/<名字>`` 沒刪成，
或刪掉的自訂模型用「切分存入舊對話」存過封存，之後建同名靈魂時，新靈魂直接讀到別人的舊封存與
舊檔案（兩者都只看名字）。

修補後（``agent_leftovers.clear_for_new_soul`` 最後一步）：名字不屬於任何代理人時，殘留的封存
不跟隨連結地搬到 ``data/retired/legacy/<毫秒時間>-<名字>``（只改名、不複製；只有管理員碰得到），
殘留的個人空間刪掉（根是連結只刪連結）；搬不走、刪不掉就不建立。

conftest 沙盒、真的靈魂路由；殘留直接寫進沙盒的 data 根模擬刪除沒做完。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import mission_board_path
from memory_server.utils.no_follow import LinkRefusedError
from src.config import get_config_dir
from src.core.exceptions import ErrorCode
from src.main import app
from src.models.identity import AgentIdentity
from src.services import agent_leftovers
from src.services import resource_manager as rm_module
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.utils import async_db, soul_io

from tests.support.links import make_dir_link
from tests.support.zone_roots import point_zone_roots

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

SOUL = "ArcSoul"
NAMES = [SOUL]
_BACKUP_SCRIPT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"
STAMPED = re.compile(r"^\d{13}-ArcSoul$")


@pytest.fixture
async def sandbox() -> AsyncIterator[Path]:
    """靈魂路由要 conftest 沙盒；本檔用到的名字、退役資料夾在結束時清掉."""
    data = Path(resource_manager.get_base_path())
    await async_db.init_db_async()
    _drop(data / "retired")  # 共用的 session 沙盒：從空的退役資料夾開始
    yield data
    for agent_id in NAMES:
        mission_board_path(agent_id).unlink(missing_ok=True)
        for base in ("soul", "personal_drive", "legacy", "stats"):
            _drop(data / base / agent_id)
    _drop(data / "retired")


@pytest.fixture(autouse=True)
def registry(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """models.json、agents_registry.json 從空的開始；沒有心跳位址（建立時的排程那步不連網路）."""
    _ = sandbox
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_EXTERNAL_URL", None)
    config = Path(get_config_dir())
    files = [config / "models.json", config / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()
    yield config / "agents_registry.json"
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")
    model_manager.load_initial_config()


@pytest.fixture
def people() -> Iterator[SimpleNamespace]:
    """管理員（用編輯建立要重新輸入密碼）."""
    pw = "ArcTakeoverPass123!"
    auth = AuthService()
    users = auth.users_file_path
    before = users.read_bytes() if users.exists() else None
    admin = auth.create_user("arc-admin@test.com", pw, role="admin")
    yield SimpleNamespace(pw=pw, admin=AgentIdentity.for_role(admin.id, "admin", is_human=True))
    if before is None:
        users.unlink(missing_ok=True)
    else:
        users.write_bytes(before)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """data 根外面的資料夾（連結的另一端）."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "secret.txt").write_text("outside", encoding="utf-8")
    return folder


def _drop(path: Path) -> None:
    """測試收尾：連結只拿掉連結本身，資料夾整棵刪，檔案刪掉."""
    if path.is_symlink():
        path.unlink()
    elif path.is_junction():
        os.rmdir(path)
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _create_soul(people: SimpleNamespace) -> Any:
    return _send("POST", "/v1/souls", {"agent_id": SOUL, "config": {}}, people.admin)


def _edit_create(people: SimpleNamespace) -> Any:
    body = {"config": {"is_toukasoul": True}, "admin_password": people.pw}
    return _send("POST", f"/v1/souls/{SOUL}", body, people.admin)


def _leftover_archive(data: Path) -> None:
    folder = data / "legacy" / SOUL
    folder.mkdir(parents=True)
    (folder / "archive.db").write_text("old archive", encoding="utf-8")


def _leftover_drive(data: Path) -> None:
    folder = data / "personal_drive" / SOUL
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("old notes", encoding="utf-8")


def _retired(data: Path) -> list[Path]:
    folder = data / "retired" / "legacy"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


# --------------------------------------------------------------------------- #
# 建立靈魂時
# --------------------------------------------------------------------------- #


async def test_create_retires_old_archive(sandbox: Path, people: SimpleNamespace) -> None:
    _leftover_archive(sandbox)
    response = _create_soul(people)
    assert response.status_code == 200, response.text
    assert list((sandbox / "legacy" / SOUL).iterdir()) == []  # 新靈魂的封存是空的
    retired = _retired(sandbox)
    assert len(retired) == 1
    assert STAMPED.fullmatch(retired[0].name)
    assert (retired[0] / "archive.db").read_text(encoding="utf-8") == "old archive"


async def test_create_removes_leftover_drive(sandbox: Path, people: SimpleNamespace) -> None:
    _leftover_drive(sandbox)
    response = _create_soul(people)
    assert response.status_code == 200, response.text
    assert list((sandbox / "personal_drive" / SOUL).iterdir()) == []
    assert _retired(sandbox) == []  # 個人空間刪掉、不搬


async def test_linked_leftover_drive_only_unlinks(
    sandbox: Path, people: SimpleNamespace, outside: Path
) -> None:
    (sandbox / "personal_drive").mkdir(exist_ok=True)
    make_dir_link(sandbox / "personal_drive" / SOUL, outside)
    response = _create_soul(people)
    assert response.status_code == 200, response.text
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"
    drive = sandbox / "personal_drive" / SOUL
    assert not drive.is_symlink()
    assert not drive.is_junction()
    assert list(drive.iterdir()) == []


@pytest.mark.parametrize("broken", ["archive", "drive"])
async def test_create_refuses_when_leftovers_stay(
    broken: str,
    sandbox: Path,
    people: SimpleNamespace,
    registry: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搬不走、刪不掉：不建立。封存那邊是真的失敗；個人空間的區內刪除造不出失敗，換掉回傳值."""
    _leftover_archive(sandbox)
    _leftover_drive(sandbox)
    if broken == "archive":
        (sandbox / "retired").write_text("not a folder", encoding="utf-8")  # 退役資料夾建不起來
    else:
        monkeypatch.setattr(soul_io, "remove_soul_drive", lambda _agent_id: "error")
    response = _create_soul(people)
    assert response.status_code != 200
    assert _code(response) == ErrorCode.SOUL_VAULT_ERROR.value
    assert not (sandbox / "soul" / SOUL).exists()
    assert SOUL not in json.loads(registry.read_text(encoding="utf-8"))
    assert (sandbox / "personal_drive" / SOUL / "notes.txt").is_file()
    if broken == "archive":
        assert (sandbox / "legacy" / SOUL / "archive.db").is_file()


async def test_claimed_name_keeps_files(
    sandbox: Path, people: SimpleNamespace, registry: Path
) -> None:
    """名字還在登記表（有人用）：算修復，封存與個人空間是它的，不動."""
    registry.write_text(json.dumps({SOUL: [SOUL]}), encoding="utf-8")
    _leftover_archive(sandbox)
    _leftover_drive(sandbox)
    response = _create_soul(people)
    assert response.status_code == 200, response.text
    assert (sandbox / "legacy" / SOUL / "archive.db").is_file()
    assert (sandbox / "personal_drive" / SOUL / "notes.txt").is_file()
    assert _retired(sandbox) == []


async def test_edit_create_retires_too(sandbox: Path, people: SimpleNamespace) -> None:
    _leftover_archive(sandbox)
    _leftover_drive(sandbox)
    response = _edit_create(people)
    assert response.status_code == 200, response.text
    retired = _retired(sandbox)
    assert len(retired) == 1
    assert (retired[0] / "archive.db").is_file()
    assert not (sandbox / "personal_drive" / SOUL / "notes.txt").exists()


async def test_linked_archive_is_refused(
    sandbox: Path, people: SimpleNamespace, outside: Path
) -> None:
    (sandbox / "legacy").mkdir(exist_ok=True)
    make_dir_link(sandbox / "legacy" / SOUL, outside)
    response = _create_soul(people)
    assert response.status_code != 200
    assert _code(response) == ErrorCode.SOUL_VAULT_ERROR.value
    assert not (sandbox / "soul" / SOUL).exists()
    link = sandbox / "legacy" / SOUL
    assert link.is_symlink() or link.is_junction()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"
    assert _retired(sandbox) == []


# --------------------------------------------------------------------------- #
# 搬移本身（resource_manager.retire_legacy_dir）
# --------------------------------------------------------------------------- #


def _data_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    point_zone_roots(monkeypatch, root, root.parent / "state")


def test_retire_works_under_a_linked_data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data 根的路徑上有連結（Linux 連到別顆磁碟、macOS 的 /var、Windows junction）照樣搬得動."""
    real = tmp_path / "real"
    (real / "legacy" / SOUL).mkdir(parents=True)
    (real / "legacy" / SOUL / "a.txt").write_text("a", encoding="utf-8")
    make_dir_link(tmp_path / "data_link", real)
    _data_at(monkeypatch, tmp_path / "data_link")
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is True
    moved = sorted((real / "retired" / "legacy").iterdir())
    assert [p.name for p in moved if STAMPED.fullmatch(p.name)] == [moved[0].name]
    assert (moved[0] / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (real / "legacy" / SOUL).exists()


def test_retire_moves_without_copying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "legacy" / SOUL).mkdir(parents=True)
    before = os.stat(tmp_path / "legacy" / SOUL)
    _data_at(monkeypatch, tmp_path)
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is True
    (moved,) = (tmp_path / "retired" / "legacy").iterdir()
    assert os.path.samestat(os.stat(moved), before)


def test_retire_without_archive_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _data_at(monkeypatch, tmp_path)
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is False
    assert not (tmp_path / "retired").exists()


def test_long_name_retires_with_a_short_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """時間加名字超過 200 bytes：改用名字的雜湊（名字太長時改名會失敗、這個名字永遠建不起來）."""
    name = "長" * 70  # 210 bytes
    (tmp_path / "legacy" / name).mkdir(parents=True)
    _data_at(monkeypatch, tmp_path)
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=name)) is True
    (moved,) = (tmp_path / "retired" / "legacy").iterdir()
    assert re.fullmatch(r"\d{13}-sha-[0-9a-f]{16}", moved.name)


def test_retire_picks_a_free_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一毫秒撞名加 -2…；試到上限仍撞名就丟錯、封存留在原地."""
    _data_at(monkeypatch, tmp_path)
    monkeypatch.setattr(rm_module, "get_current_epoch_ms", lambda: 1_700_000_000_000)
    taken = tmp_path / "retired" / "legacy"
    (taken / f"1700000000000-{SOUL}").mkdir(parents=True)
    (tmp_path / "legacy" / SOUL).mkdir(parents=True)
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is True
    assert (taken / f"1700000000000-{SOUL}-2").is_dir()
    (tmp_path / "legacy" / SOUL).mkdir()
    for n in range(3, rm_module.RETIRE_NAME_ATTEMPTS + 1):
        (taken / f"1700000000000-{SOUL}-{n}").mkdir()
    with pytest.raises(FileExistsError):
        resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL))
    assert (tmp_path / "legacy" / SOUL).is_dir()


def test_retire_follows_a_linked_legacy_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理員把整個 ``legacy`` 連到別處：沒有殘留時照常（不再被拒），有殘留時從連結的另一端搬."""
    real_legacy = tmp_path / "elsewhere"
    real_legacy.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    make_dir_link(data / "legacy", real_legacy)
    _data_at(monkeypatch, data)
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is False
    (real_legacy / SOUL).mkdir()
    (real_legacy / SOUL / "a.txt").write_text("a", encoding="utf-8")
    assert resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL)) is True
    (moved,) = (data / "retired" / "legacy").iterdir()
    assert (moved / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (real_legacy / SOUL).exists()


@pytest.mark.parametrize("where", ["archive", "retired"])
def test_retire_refuses_links(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: Path
) -> None:
    """``legacy/<名字>`` 或 ``retired`` 是連結：拒絕、不跟隨，兩邊都不動."""
    _data_at(monkeypatch, tmp_path)
    (tmp_path / "legacy").mkdir()
    if where == "archive":
        make_dir_link(tmp_path / "legacy" / SOUL, outside)
    else:
        (tmp_path / "legacy" / SOUL).mkdir()
        make_dir_link(tmp_path / "retired", outside)
    with pytest.raises(LinkRefusedError):
        resource_manager.retire_legacy_dir(AgentIdentity(agent_id=SOUL))
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (tmp_path / "legacy" / SOUL).exists()
    if where == "archive":
        assert not (tmp_path / "retired").exists()  # 拒絕在建退役資料夾之前


@pytest.mark.parametrize("folder", ["legacy", "soul", "retired"])
def test_linked_protected_folder_stays_protected(
    folder: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理員把整個受保護資料夾連到別處：連結另一端的檔案照樣受保護（代理人寫入要被擋）."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    make_dir_link(data / folder, real)
    _data_at(monkeypatch, data)
    assert resource_manager.is_protected_path(data / folder / SOUL / "x.json")
    assert resource_manager.is_protected_path(real / SOUL / "x.json")
    assert not resource_manager.is_protected_path(tmp_path / "elsewhere_backup" / "x.json")


def test_retired_dir_is_protected(sandbox: Path) -> None:
    expected = Path(resource_manager.get_base_path()).resolve() / "retired"
    assert resource_manager.get_retired_dir() == expected
    assert resource_manager.is_protected_path(sandbox / "retired" / "legacy" / "x")
    assert not resource_manager.is_protected_path(sandbox / "retired_backup" / "x")


def test_backup_and_restore_carry_retired_archives(zone_data_root: Path, tmp_path: Path) -> None:
    """退役的封存是人類對話的舊段：備份要收、還原要放回（manage_backup 的資料夾清單是寫死的）."""
    spec = importlib.util.spec_from_file_location(
        "manage_backup_retired_under_test", _BACKUP_SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "retired" in mod.SANCTUARY_FOLDERS
    kept = zone_data_root / "retired" / "legacy" / f"1700000000000-{SOUL}" / "archive.db"
    kept.parent.mkdir(parents=True)
    kept.write_text("old archive", encoding="utf-8")
    project = tmp_path.resolve() / "project"
    config = project / "ToukaProxy" / "config"
    config.mkdir(parents=True)
    env = project / ".env"
    env.write_text(f"SANCTUARY_DATA_PATH={zone_data_root.as_posix()}\n", encoding="utf-8")
    resolver = mod.PathResolver(project, env_path=env, data_root=zone_data_root, config_dir=config)
    output = mod.BackupEngine(resolver).run_backup(tmp_path / "backups", "Touka_Backup")
    with zipfile.ZipFile(output) as zf:
        assert f"data/retired/legacy/1700000000000-{SOUL}/archive.db" in zf.namelist()
    fresh = tmp_path.resolve() / "fresh_data"
    mod.RestoreEngine(resolver).run_restore(str(output), str(fresh))
    restored = fresh / "retired" / "legacy" / f"1700000000000-{SOUL}" / "archive.db"
    assert restored.read_text(encoding="utf-8") == "old archive"

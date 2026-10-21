"""快照列舉的單一來源與三個端點收緊（清除連備份的前置）.

L01～L03、E13 對照設計 §12-2。快照目錄一律換成 tmp_path，不碰 repo 的 backups。
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.system_restore import RestoreStatus
from src.routers import system as system_router
from src.routers.auth import get_current_user
from src.services import backup_scheduler_service as scheduler
from src.services import system_backup
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager


def _zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data/x.txt", b"x")
    return path


def _admin() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="zz-admin", role="admin", is_human=True)


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    root = tmp_path / "backups"
    root.mkdir()
    return root


# --- L01 列舉規則 -------------------------------------------------------------


def test_l01_listing_rule(backups: Path) -> None:
    _zip(backups / "Touka_Backup_1.zip")
    _zip(backups / "Touka_Backup_2.ZIP")
    _zip(backups / ".hidden.zip")
    _zip(backups / "sub" / "nested.zip")
    (backups / "dir.zip").mkdir()
    (backups / "notes.txt").write_text("x")
    (backups / ".Touka_Backup_1.zip.memscrub-abcd1234.tmp").write_bytes(b"x")
    names = [p.name for p in system_backup.list_snapshot_files(backups)]
    assert names == ["Touka_Backup_1.zip", "Touka_Backup_2.ZIP"]


def test_l01_links_and_reparse_points_are_not_listed(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = _zip(backups / "link.zip")
    reparse = _zip(backups / "reparse.zip")
    plain = _zip(backups / "plain.zip")
    real_lstat = Path.lstat

    def fake_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == link:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        if self == reparse:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    assert [p.name for p in system_backup.list_snapshot_files(backups)] == [plain.name]
    assert system_backup.is_listed_snapshot("link.zip", backups) is None
    assert system_backup.is_listed_snapshot("reparse.zip", backups) is None
    assert system_backup.is_listed_snapshot("plain.zip", backups) == plain


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        ".x.zip",
        "a/b.zip",
        "a\\b.zip",
        "../x.zip",
        "x.zip.txt",
        "x",
        "x:.zip",
        "a:b.zip",
    ],
)
def test_l01_rejected_names(backups: Path, name: str) -> None:
    assert system_backup.is_listed_snapshot(name, backups) is None


def test_l01_missing_directory_lists_nothing(tmp_path: Path) -> None:
    assert system_backup.list_snapshot_files(tmp_path / "nope") == []


# --- L02 清單、統計、修剪共用 -------------------------------------------------


def test_l02_list_and_stats_use_the_rule(backups: Path) -> None:
    _zip(backups / "Touka_Backup_1.zip")
    _zip(backups / ".hidden.zip")
    (backups / "dir.zip").mkdir()
    app.dependency_overrides[get_verified_identity] = _admin
    try:
        with patch("src.routers.system._get_backup_dir", return_value=backups):
            client = TestClient(app, raise_server_exceptions=False)
            listed = client.get("/system/backups").json()
            stats = client.get("/system/backups/stats").json()
    finally:
        app.dependency_overrides.clear()
    assert [b["filename"] for b in listed] == ["Touka_Backup_1.zip"]
    assert stats["count"] == 1


def test_l02_prune_uses_the_rule(backups: Path) -> None:
    for i in range(3):
        path = _zip(backups / f"{system_backup.AUTO_BACKUP_PREFIX}_{i}.zip")
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
    hidden = _zip(backups / f".{system_backup.AUTO_BACKUP_PREFIX}_old.zip")
    os.utime(hidden, (1_600_000_000, 1_600_000_000))
    removed = scheduler.prune_old_auto_backups(backups, 1)
    assert sorted(p.name for p in removed) == [
        f"{system_backup.AUTO_BACKUP_PREFIX}_0.zip",
        f"{system_backup.AUTO_BACKUP_PREFIX}_1.zip",
    ]
    assert hidden.exists()


# --- L03 create_backup 回傳檔名 -----------------------------------------------


class _Done:
    stdout = "Backup completed: somewhere"


def _fake_backup(backups: Path, monkeypatch: pytest.MonkeyPatch, new_names: list[str]) -> None:
    script = backups.parent / "manage_backup.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(system_backup, "backup_script", lambda: script)
    monkeypatch.setattr(system_backup, "backup_dir", lambda: backups)

    def run(*_args: object, **_kwargs: object) -> _Done:
        for name in new_names:
            _zip(backups / name)
        return _Done()

    monkeypatch.setattr(system_backup, "run_sync", run)


def test_l03_create_backup_returns_only_the_new_file_name(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(backups / "Touka_Backup_old.zip")
    _fake_backup(backups, monkeypatch, ["Touka_Backup_new.zip"])
    result = system_backup.create_backup()
    assert result == {
        "status": "success",
        "message": "Backup created successfully",
        "filename": "Touka_Backup_new.zip",
    }
    assert "output" not in result


@pytest.mark.parametrize("new_names", [[], ["a.zip", "b.zip"]])
def test_l03_unclear_new_file_gives_none(
    backups: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    new_names: list[str],
) -> None:
    _fake_backup(backups, monkeypatch, new_names)
    with caplog.at_level(logging.WARNING):
        result = system_backup.create_backup()
    assert result["filename"] is None
    assert "new snapshot" in caplog.text


# --- E13 還原、下載、刪除只接受清單上的檔名 ------------------------------------


def _restore(backups: Path, filename: str) -> object:
    user = AuthService().create_user(
        f"zz-restore-{abs(hash(filename))}@touka.io", "correct_pw_123", role="admin"
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with (
            patch("src.routers.system._get_backup_dir", return_value=backups),
            patch("src.routers.system._oauth_restore_destination_populated", return_value=True),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            return client.post(
                "/system/restore/run",
                json={"filename": filename, "current_password": "correct_pw_123"},
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("filename", ["sub/nested.zip", ".hidden.zip", "notes.txt", "dir.zip"])
def test_e13_restore_rejects_names_outside_the_list(backups: Path, filename: str) -> None:
    _zip(backups / "sub" / "nested.zip")
    _zip(backups / ".hidden.zip")
    (backups / "notes.txt").write_text("x")
    (backups / "dir.zip").mkdir()
    response = _restore(backups, filename)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"


def test_e13_restore_accepts_upper_case_zip(backups: Path) -> None:
    _zip(backups / "Touka_Backup_UP.ZIP")
    response = _restore(backups, "Touka_Backup_UP.ZIP")
    # 名稱關卡過了，下一關（OAuth 衝突，測試替身固定回真）才擋
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MCP_OAUTH_RESTORE_CONFLICT"


@pytest.mark.parametrize("filename", [".hidden.zip", "notes.txt", "dir.zip"])
def test_e13_delete_and_download_reject_names_outside_the_list(
    backups: Path, filename: str
) -> None:
    _zip(backups / ".hidden.zip")
    (backups / "notes.txt").write_text("x")
    (backups / "dir.zip").mkdir()
    app.dependency_overrides[get_verified_identity] = _admin
    try:
        with patch("src.routers.system._get_backup_dir", return_value=backups):
            client = TestClient(app, raise_server_exceptions=False)
            deleted = client.delete(f"/system/backups/{filename}")
            downloaded = client.post(
                f"/system/backups/{filename}/download", json={"passphrase": "x" * 16}
            )
    finally:
        app.dependency_overrides.clear()
    assert deleted.status_code == 404
    assert downloaded.status_code == 404
    assert (backups / ".hidden.zip").exists()


def test_e13_delete_takes_the_backup_lock(backups: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _zip(backups / "Touka_Backup_DEL.ZIP")
    seen: list[bool] = []
    real_remove = resource_manager.remove_file

    def spy(path: object, *args: object, **kwargs: object) -> None:
        seen.append(system_backup.backup_execution_lock.locked())
        real_remove(path, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "remove_file", spy)
    app.dependency_overrides[get_verified_identity] = _admin
    try:
        with patch("src.routers.system._get_backup_dir", return_value=backups):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.delete("/system/backups/Touka_Backup_DEL.ZIP")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert seen == [True]
    assert not target.exists()


def test_restore_in_progress_reflects_the_last_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="running"))
    assert system_router.restore_in_progress() is True
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    assert system_router.restore_in_progress() is False


# --- 成果審修正：連結的快照資料夾、create_backup 列目錄失敗（R4、R12） ---------------------


def test_l05_linked_snapshot_folder_is_not_followed(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(backups / "Touka_Backup_1.zip")
    real_lstat = Path.lstat

    def fake_lstat(self: Path, *args: object, **kwargs: object) -> object:
        if self == backups:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    assert system_backup.list_snapshot_files(backups) == []
    with pytest.raises(NotADirectoryError):
        system_backup.list_snapshot_files(backups, strict=True)
    assert system_backup.list_snapshot_files(backups.parent / "missing", strict=True) == []


def test_l06_listing_failure_after_the_backup_is_a_backup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system_backup, "run_sync", lambda *_a, **_k: None)

    def refuse(*_a: object, **_k: object) -> list:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(system_backup, "list_snapshot_files", refuse)
    with pytest.raises(ToukaException) as exc:
        system_backup.create_backup()
    assert exc.value.code is ErrorCode.SYSTEM_BACKUP_FAILED


def test_l07_alternate_data_stream_names_are_refused(backups: Path) -> None:
    # Windows 上 x:.zip 是檔案 x 的替代資料流，不會出現在清單上，名稱本身就要擋
    for name in ("x:.zip", "Touka_Backup_1.zip:hidden.zip", "a:b.zip"):
        assert system_backup.is_snapshot_name(name) is False
        assert system_backup.is_listed_snapshot(name, backups) is None


# --- 刪除快照等備份鎖時斷線，稽核照樣留下（R9） ---------------------------------------


async def test_l08_delete_audit_survives_a_dropped_request(
    backups: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lock = asyncio.Lock()
    monkeypatch.setattr(system_backup, "backup_execution_lock", lock)
    monkeypatch.setattr(system_router, "_get_backup_dir", lambda: backups)
    monkeypatch.setattr(system_router, "verify_origin", lambda _r: None)
    target = _zip(backups / "Touka_Backup_DROP.zip")
    async with lock:  # 清除或備份正在跑
        task = asyncio.create_task(
            system_router.delete_backup("Touka_Backup_DROP.zip", None, _admin())
        )
        await asyncio.sleep(0.05)
        task.cancel()  # 使用者等不及、連線被切斷
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(200):
        if not target.exists() and "deleted backup" in caplog.text:
            break
        await asyncio.sleep(0.02)
    assert not target.exists()
    assert "AUDIT: admin" in caplog.text and "Touka_Backup_DROP.zip" in caplog.text

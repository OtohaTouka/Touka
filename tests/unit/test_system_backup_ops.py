"""軌1 Phase 1 — 系統備份運維：還原密碼閘 + 刪除 + 統計 (TDD).

Covers:
  - POST /system/restore/run password gate: wrong pw → 401, non-admin → 403,
    valid admin pw + missing file → 404 (proves the gate passed before the file check),
    missing current_password → 422.  NEVER reaches execute_restore (os._exit) — all
    paths use a non-existent filename or are rejected at the gate.
  - DELETE /system/backups/{filename}: happy delete + not-found.
  - GET /system/backups/stats: count/total/free shape.
"""

from __future__ import annotations

import glob
import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import pyzipper
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import system as system_router
from src.routers.auth import get_current_user
from src.services.auth_service import AuthService
from src.services.backup_crypto import encrypt_backup_zip
from src.services.identity_router import get_verified_identity


def _clear() -> None:
    app.dependency_overrides.clear()


def _make_plain_backup(path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(".env", b"ANTHROPIC_API_KEY=sk-secret")
        z.writestr("data/ui_sessions.db", b"sqlitedata")


def _admin_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)


def _normal_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="someuser", role="normal", is_human=True)


def _symlink_supported() -> bool:
    """本平台能否建立 symlink（Windows 無開發者模式時 os.symlink 會 OSError）。"""
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "t"
        target.write_text("x")
        try:
            (Path(d) / "l").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
        return True


_SYMLINK_OK = _symlink_supported()


# ── restore password gate ───────────────────────────────────────────────────

def test_restore_wrong_password_401():
    user = AuthService().create_user("restore_wrong@touka.io", "correct_pw_123", role="admin")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/system/restore/run", json={"filename": "x.zip", "current_password": "WRONG"})
    _clear()
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_restore_non_admin_403():
    user = AuthService().create_user("restore_norm@touka.io", "correct_pw_123", role="normal")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post(
        "/system/restore/run", json={"filename": "x.zip", "current_password": "correct_pw_123"}
    )
    _clear()
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


def test_restore_valid_password_missing_file_404(tmp_path):
    user = AuthService().create_user("restore_ok@touka.io", "correct_pw_123", role="admin")
    app.dependency_overrides[get_current_user] = lambda: user
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/restore/run",
            json={"filename": "nope.zip", "current_password": "correct_pw_123"},
        )
    _clear()
    # password gate passed → reached the file check → not found
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"


def test_restore_requires_password_field_422():
    user = AuthService().create_user("restore_422@touka.io", "correct_pw_123", role="admin")
    app.dependency_overrides[get_current_user] = lambda: user
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/system/restore/run", json={"filename": "x.zip"})
    _clear()
    assert resp.status_code == 422


def test_restore_rejects_populated_oauth_destination_before_background_task(tmp_path):
    """A populated OAuth store must fail synchronously, not after HTTP 202."""
    backup = tmp_path / "Touka_Backup_oauth.zip"
    _make_plain_backup(backup)
    user = AuthService().create_user(
        "restore_oauth@touka.io",
        "correct_pw_123",
        role="admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    with (
        patch("src.routers.system._get_backup_dir", return_value=tmp_path),
        patch(
            "src.routers.system._oauth_restore_destination_populated",
            return_value=True,
        ),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/system/restore/run",
            json={
                "filename": backup.name,
                "current_password": "correct_pw_123",
            },
        )
    _clear()
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MCP_OAUTH_RESTORE_CONFLICT"


def test_restore_destination_detects_workload_oauth_state(tmp_path):
    """P8-B store/key must participate in the synchronous collision fence."""
    (tmp_path / "mcp_oauth_workload.sqlite3").write_bytes(b"workload-state")
    with (
        patch.object(system_router.app_config, "CONFIG_DIR", tmp_path),
        patch.object(system_router.app_config, "TOUKA_MCP_OAUTH_KEY", ""),
        patch.object(system_router.app_config, "TOUKA_MCP_OAUTH_RETIRED_KEYS", "[]"),
        patch.object(system_router.app_config, "TOUKA_MCP_OAUTH_WORKLOAD_KEY", ""),
        patch.object(
            system_router.app_config,
            "TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS",
            "[]",
        ),
    ):
        assert system_router._oauth_restore_destination_populated() is True


# ── delete ───────────────────────────────────────────────────────────────────

def test_delete_backup_happy(tmp_path):
    f = tmp_path / "Touka_Backup_x.zip"
    f.write_bytes(b"PK\x03\x04dummy")
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.delete("/system/backups/Touka_Backup_x.zip")
    _clear()
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deleted"
    assert not f.exists()


def test_delete_backup_not_found_404(tmp_path):
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.delete("/system/backups/ghost.zip")
    _clear()
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"


def test_delete_backup_rejects_non_zip(tmp_path):
    # 非 .zip 檔即使存在於 backups/ 也不可刪（只認備份檔）。
    f = tmp_path / "secret.txt"
    f.write_text("not a backup")
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.delete("/system/backups/secret.txt")
    _clear()
    assert resp.status_code == 404
    assert f.exists()  # 未被刪除


def test_delete_backup_non_admin_403(tmp_path):
    app.dependency_overrides[get_verified_identity] = _normal_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.delete("/system/backups/x.zip")
    _clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


def test_backup_stats_non_admin_403(tmp_path):
    app.dependency_overrides[get_verified_identity] = _normal_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/system/backups/stats")
    _clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


# ── Phase 2: encrypted download (ADR-0108) ───────────────────────────────────

def test_encrypt_backup_zip_roundtrip(tmp_path):
    plain = tmp_path / "Touka_Backup_x.zip"
    _make_plain_backup(plain)
    enc = encrypt_backup_zip(plain, "pass-phrase-123")
    try:
        with pyzipper.AESZipFile(str(enc), "r") as z:
            z.setpassword(b"pass-phrase-123")
            assert z.read(".env") == b"ANTHROPIC_API_KEY=sk-secret"
            assert z.read("data/ui_sessions.db") == b"sqlitedata"
        with pyzipper.AESZipFile(str(enc), "r") as z:  # wrong passphrase → fail
            z.setpassword(b"WRONG-pass")
            with pytest.raises(Exception):  # noqa: B017,PT011
                z.read(".env")
    finally:
        enc.unlink(missing_ok=True)


def test_download_backup_happy_streams_aes_zip(tmp_path):
    plain = tmp_path / "Touka_Backup_x.zip"
    _make_plain_backup(plain)
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/Touka_Backup_x.zip/download",
            json={"passphrase": "pass-phrase-123"},
        )
    _clear()
    assert resp.status_code == 200, resp.text
    # 回應內容是可用密碼開啟的 AES zip，且內含原始條目
    with pyzipper.AESZipFile(io.BytesIO(resp.content), "r") as z:
        z.setpassword(b"pass-phrase-123")
        assert z.read(".env") == b"ANTHROPIC_API_KEY=sk-secret"
        assert z.read("data/ui_sessions.db") == b"sqlitedata"


def test_download_backup_short_passphrase_422(tmp_path):
    plain = tmp_path / "Touka_Backup_x.zip"
    _make_plain_backup(plain)
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/Touka_Backup_x.zip/download", json={"passphrase": "short"}
        )
    _clear()
    assert resp.status_code == 422


def test_download_backup_non_admin_403(tmp_path):
    app.dependency_overrides[get_verified_identity] = _normal_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post("/system/backups/x.zip/download", json={"passphrase": "pass-phrase-123"})
    _clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


def test_download_backup_missing_404(tmp_path):
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post("/system/backups/ghost.zip/download", json={"passphrase": "pass-phrase-123"})
    _clear()
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"


def test_download_backup_passphrase_below_12_422(tmp_path):
    # 此加密檔含全系統機密 → 密碼短語下限 12（複審 #6）。
    plain = tmp_path / "Touka_Backup_x.zip"
    _make_plain_backup(plain)
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/Touka_Backup_x.zip/download", json={"passphrase": "elevenchar1"}
        )  # 11 字
    _clear()
    assert resp.status_code == 422


def test_download_backup_rejects_non_zip(tmp_path):
    # E58（ADR-0108，批⑤-A）：姊妹對稱——delete 有 test_delete_backup_rejects_non_zip，
    # download 缺。非 .zip 檔即使存在於 backups/ 也不可下載（只認備份檔）：回 404，
    # 且原檔不被讀取加密外洩。刪 system.py:306 `.endswith(".zip")` → secret.txt 被送去
    # encrypt_backup_zip → 非 zip → 500（非 404）→ 紅。
    f = tmp_path / "secret.txt"
    f.write_text("not a backup")
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/secret.txt/download", json={"passphrase": "pass-phrase-123"}
        )
    _clear()
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"
    assert f.exists()  # 未被讀取/加密


@pytest.mark.skipif(not _SYMLINK_OK, reason="平台不支援 symlink（Windows 無開發者模式）")
def test_download_backup_rejects_symlink_escape(tmp_path):
    # E58 路徑穿越縱深防禦：backups/ 內的 symlink 指向監獄外真實 .zip 時，
    # validate_no_symlink_escape 必須擋下——`.exists()` 擋不住（symlink 目標真的存在）。
    # 刪 system.py:307 該子句 → 逃逸 symlink 被加密串流、全系統機密外洩 → 紅。
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_zip = outside / "escape.zip"
    _make_plain_backup(secret_zip)  # 監獄外真實存在的有效 .zip
    (backup_dir / "link.zip").symlink_to(secret_zip)  # backups/ 內、目標在監獄外

    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=backup_dir):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/link.zip/download", json={"passphrase": "pass-phrase-123"}
        )
    _clear()
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_NOT_FOUND"


def test_download_cleanup_on_encryption_failure_no_temp_leak(tmp_path):
    # 複審 #1/#3/#4：加密中途失敗（壞 zip）→ 500，且不留孤兒 touka_enc_ 暫存檔。
    bad = tmp_path / "Touka_Backup_bad.zip"
    bad.write_bytes(b"this is not a valid zip")  # zipfile 開啟即 BadZipFile
    pattern = str(Path(tempfile.gettempdir()) / "touka_enc_*")
    before = set(glob.glob(pattern))
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/system/backups/Touka_Backup_bad.zip/download",
            json={"passphrase": "pass-phrase-123"},
        )
    _clear()
    assert resp.status_code == 500  # 加密失敗
    assert set(glob.glob(pattern)) == before, "encryption failure leaked a temp file"


# ── stats ─────────────────────────────────────────────────────────────────────

def test_backup_stats(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"x" * 1024)
    (tmp_path / "b.zip").write_bytes(b"y" * 2048)
    app.dependency_overrides[get_verified_identity] = _admin_identity
    with patch("src.routers.system._get_backup_dir", return_value=tmp_path):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.get("/system/backups/stats")
    _clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    assert body["total_size_mb"] >= 0
    assert body["free_disk_mb"] > 0
    assert body["oldest"] and body["newest"]

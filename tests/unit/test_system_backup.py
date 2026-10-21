"""軌B Batch 6：系統備份服務 SSOT（create_backup）——成功/缺腳本/子程序失敗（零-API，mock subprocess）。"""
from __future__ import annotations

import subprocess

import pytest

from src.core.exceptions import ToukaException
from src.services import system_backup


class _FakeCompleted:
    stdout = "backup written: backups/touka-backup.zip"


def test_create_backup_success(monkeypatch, tmp_path):
    script = tmp_path / "manage_backup.py"
    script.write_text("# stub", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(system_backup, "backup_script", lambda: script)
    monkeypatch.setattr(system_backup, "backup_dir", lambda: backups)

    def _run(*_a, **_k):
        (backups / "touka-backup.zip").write_bytes(b"PK\x03\x04")
        return _FakeCompleted()

    monkeypatch.setattr(system_backup.subprocess, "run", _run)

    result = system_backup.create_backup()
    assert result["status"] == "success"
    # 回傳這次新增的快照檔名，不回傳腳本輸出（有絕對路徑）
    assert result["filename"] == "touka-backup.zip"
    assert "output" not in result


def test_create_backup_missing_script(monkeypatch, tmp_path):
    monkeypatch.setattr(system_backup, "backup_script", lambda: tmp_path / "absent.py")
    with pytest.raises(ToukaException):
        system_backup.create_backup()


def test_create_backup_subprocess_failure(monkeypatch, tmp_path):
    script = tmp_path / "manage_backup.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(system_backup, "backup_script", lambda: script)

    def _boom(*_a, **_k):
        raise subprocess.CalledProcessError(1, "cmd", stderr="boom")

    monkeypatch.setattr(system_backup.subprocess, "run", _boom)
    with pytest.raises(ToukaException):
        system_backup.create_backup()

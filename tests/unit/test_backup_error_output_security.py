"""快照失敗的回應不帶腳本輸出（禁令 23）。

修補前 ``create_backup`` 把腳本 stderr 原樣放進 ``SYSTEM_BACKUP_FAILED`` 的 ``error_output``，
經 ``POST /v1/system/backup/run``（以及重置、設定匯入前的快照）回給前端：裡面可能有資料根等
絕對路徑、以及像金鑰的字串。修補後回應只有錯誤碼；stderr 只進伺服器 log，寫之前先用
``redact_secrets_from_content`` 消毒。還原失敗本來就只記狀態與錯誤碼
（``test_restore_status_security``）。
全部離線：腳本子程序以假的 ``run_sync`` 取代。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import system as system_router
from src.services import system_backup
from src.services.identity_router import get_verified_identity

_LEAKY_PATH = "leak-sanctuary/data/vault/core_memory.db"
_LEAKY_KEY = "sk-backupleak0123456789abcdef"
_LEAKY_STDERR = (
    "Traceback (most recent call last):\n"
    f'  File "{_LEAKY_PATH}", line 1\n'
    f"RuntimeError: cannot read .env line ANTHROPIC_API_KEY={_LEAKY_KEY}\n"
)
_ADMIN = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)


@pytest.fixture
def failing_script(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(cmd: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr=_LEAKY_STDERR)

    monkeypatch.setattr(system_backup, "run_sync", _fail)


def test_backup_failure_detail_carries_no_script_output(failing_script):
    with pytest.raises(ToukaException) as excinfo:
        system_backup.create_backup()

    assert excinfo.value.code == ErrorCode.SYSTEM_BACKUP_FAILED
    assert "error_output" not in excinfo.value.detail
    for leaked in (_LEAKY_PATH, _LEAKY_KEY, "Traceback"):
        assert leaked not in str(excinfo.value.detail)


def test_backup_endpoint_response_has_no_paths_or_keys(failing_script, monkeypatch):
    monkeypatch.setattr(system_router, "verify_origin", lambda _request: None)
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    try:
        resp = TestClient(app, raise_server_exceptions=False).post("/v1/system/backup/run")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"]["code"] == "SYSTEM_BACKUP_FAILED"
    for leaked in (_LEAKY_PATH, _LEAKY_KEY, "Traceback", "core_memory"):
        assert leaked not in resp.text


def test_script_stderr_is_logged_only_after_redaction(failing_script, caplog):
    with caplog.at_level(logging.ERROR, logger=system_backup.__name__), pytest.raises(
        ToukaException
    ):
        system_backup.create_backup()

    assert "<redacted>" in caplog.text  # stderr 有進 log，但先消毒
    assert _LEAKY_KEY not in caplog.text

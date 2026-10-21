"""還原失敗要讓畫面知道（iso-c1 H1；禁令 20 對應的安全測試）。

修補前：還原在背景執行，失敗只記 log；``POST /system/restore/run`` 不管成敗都回 accepted，
前端等 15 秒就重整，使用者不知道還原失敗。修補後伺服器在記憶體記住最後一次還原的結果
（成功會重啟、紀錄跟著消失；失敗時進程還活著、紀錄留著），新增唯讀
``GET /system/restore/status``（``/v1`` 與根路徑對等）回 idle／running／failed 與錯誤碼。

安全面釘住：沒登入 401、非 admin 403；回應只有狀態與錯誤碼，不帶路徑或例外原文（禁令 23）。
全部離線：還原腳本以假的 ``run_sync`` 取代，不碰真資料、不結束進程。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.system_restore import RestoreStatus
from src.routers import system as system_router
from src.routers.auth import get_current_user
from src.services import identity_router
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity

_STATUS_PATHS = ["/system/restore/status", "/v1/system/restore/status"]
_LEAKY_STDERR = "sk-restore-leak-0123456789"
_ADMIN = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)


@pytest.fixture(autouse=True)
def _fresh_restore_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每個測試從「本次啟動後沒有還原紀錄」開始，結束時清掉依賴覆寫。"""
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    yield
    app.dependency_overrides.clear()


def _client_as(identity: AgentIdentity) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


def _run_failing_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, error: Exception
) -> None:
    """跑一次會失敗的背景還原；例外帶路徑與像金鑰的字串，用來確認不會流到 API。"""

    def _fail(_cmd: Any, **_kwargs: Any) -> None:
        raise error

    exits: list[int] = []
    monkeypatch.setattr(system_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_router, "run_sync", _fail)
    monkeypatch.setattr(system_router.os, "_exit", exits.append)
    system_router._execute_restore(
        tmp_path / "manage_backup.py", tmp_path / "leak-dir" / "Touka_Backup_leak.zip"
    )
    assert exits == []


# ── 認證閘 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _STATUS_PATHS)
def test_status_without_token_is_401(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """外部來源、沒帶憑證 → 401，不發臨時卡。"""
    monkeypatch.setattr(identity_router, "is_request_from_trusted_local", lambda _request: False)
    resp = TestClient(app, raise_server_exceptions=False).get(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.parametrize(
    "identity",
    [
        AgentIdentity.for_role(agent_id="someuser", role="normal", is_human=True),
        AgentIdentity.for_role(agent_id="SoulOwner", role="Owner", capabilities=[]),
    ],
    ids=["normal-human", "owner-agent"],
)
@pytest.mark.parametrize("path", _STATUS_PATHS)
def test_status_non_admin_is_403(path: str, identity: AgentIdentity) -> None:
    resp = _client_as(identity).get(path)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_ADMIN_REQUIRED"


@pytest.mark.parametrize("path", _STATUS_PATHS)
def test_admin_sees_idle_before_any_restore(path: str) -> None:
    resp = _client_as(_ADMIN).get(path)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "idle", "error_code": None}


# ── 失敗會被記住、不洩漏細節 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["python", "leak-dir"], stderr=_LEAKY_STDERR),
        OSError(f"cannot open leak-dir/Touka_Backup_leak.zip: {_LEAKY_STDERR}"),
    ],
    ids=["script-exit-nonzero", "os-error"],
)
def test_failed_restore_reports_code_without_details(
    error: Exception, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _run_failing_restore(monkeypatch, tmp_path, error)

    resp = _client_as(_ADMIN).get("/v1/system/restore/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "failed", "error_code": "SYSTEM_BACKUP_FAILED"}
    for leaked in (_LEAKY_STDERR, "leak-dir", "Touka_Backup_leak", "manage_backup", tmp_path.name):
        assert leaked not in resp.text


def test_accepted_restore_reports_running_and_clears_old_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """送出新的還原後狀態是 running——上一次的失敗不會讓前端誤判這一次。"""
    _run_failing_restore(monkeypatch, tmp_path, OSError("previous failure"))
    backup = tmp_path / "Touka_Backup_status.zip"
    backup.write_bytes(b"not-really-a-zip")
    user = AuthService().create_user("restore_status@touka.io", "correct_pw_123", role="admin")
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(system_router, "_get_backup_dir", lambda: tmp_path)
    monkeypatch.setattr(system_router, "_oauth_restore_destination_populated", lambda: False)
    monkeypatch.setattr(system_router, "_execute_restore", lambda *args: queued.append(args))
    app.dependency_overrides[get_current_user] = lambda: user
    client = _client_as(_ADMIN)

    accepted = client.post(
        "/system/restore/run",
        json={"filename": backup.name, "current_password": "correct_pw_123"},
    )
    status = client.get("/v1/system/restore/status")

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert len(queued) == 1
    assert status.json() == {"state": "running", "error_code": None}

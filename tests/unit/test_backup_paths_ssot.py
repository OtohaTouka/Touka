"""系統快照的路徑 SSOT：腳本在 ToukaProxy/scripts；Proxy 把資料根、.env、設定目錄、輸出目錄
當參數交給腳本（快照與還原同一組）；快照目錄就是 .env 所在資料夾底下的 backups，
快照、清單、下載、還原、排程清理五處共用。

修補前 ``_backup_script()`` 指向不存在的 ``<repo>/scripts/manage_backup.py``：手動、排程、
重置前快照全部「Backup script missing」，每靈魂重置一律中止。容器裡腳本自己猜不到 .env 與資料根。
全部在 tmp_path；真的跑腳本的測試先把它複製到 Docker 式位置（往上沒有 .env）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.config import resolve_env_file_path, settings
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import system as system_router
from src.routers.auth import get_current_user
from src.services import backup_scheduler_service as scheduler
from src.services import system_backup
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset.primitives import StorePurger
from src.services.auth_service import AuthService
from src.services.dispatch_supervisor import DispatchSupervisor
from src.services.identity_router import get_verified_identity

_REPO = Path(__file__).resolve().parents[2]
_ADMIN = AgentIdentity.for_role(agent_id="Admin", role="admin", is_human=True)


class _DataRoot:
    """只換資料根（resource_manager.get_base_path），其他 Proxy 路徑照 SSOT。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get_base_path(self) -> str:
        return str(self._root)


@pytest.fixture
def proxy_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Docker 式配置：資料根、.env（host_root）、設定目錄各在別處；路徑由 Proxy 的 SSOT 給。"""
    data = tmp_path / "data"
    (data / "soul" / "Alice").mkdir(parents=True)
    (data / "soul" / "Alice" / "soul.md").write_text("persona", encoding="utf-8")
    env = tmp_path / "host_root" / ".env"
    env.parent.mkdir()
    env.write_text("NORMAL_SETTING=kept\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(system_backup, "resource_manager", _DataRoot(data))
    monkeypatch.setattr(system_backup, "resolve_env_file_path", lambda: env)
    monkeypatch.setattr(system_backup, "get_config_dir", lambda: config)
    return {"data": data, "env": env, "config": config, "out": env.parent / "backups"}


def _docker_like_script(tmp_path: Path) -> Path:
    """把腳本放到 app/ToukaProxy/scripts（映像裡的位置）；往上沒有任何 .env。"""
    scripts = tmp_path / "app" / "ToukaProxy" / "scripts"
    scripts.mkdir(parents=True)
    return Path(shutil.copy2(system_backup.backup_script(), scripts / "manage_backup.py"))


def _capture_run(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[list[str], dict]]:
    seen: list[tuple[list[str], dict]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        seen.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(module, "run_sync", _fake_run)
    return seen


def _fake_store(purged: list[str]) -> StorePurger:
    async def _purge(candidate: str, _guard: Any) -> int:
        purged.append(candidate)
        return 1

    async def _remaining(_candidate: str, _guard: Any) -> int:
        return 0

    return StorePurger("fake", _purge, _remaining)


# ── 腳本路徑：一個 SSOT，而且真的存在 ──────────────────────────────────────────


def test_backup_script_resolves_to_the_tracked_script():
    script = system_backup.backup_script()

    assert script == _REPO / "ToukaProxy" / "scripts" / "manage_backup.py"
    assert script.is_file()
    assert not hasattr(system_router, "_get_scripts_dir")


def test_restore_endpoint_runs_the_same_script(monkeypatch, tmp_path):
    backup = tmp_path / "Touka_Backup_x.zip"
    backup.write_bytes(b"zip")
    user = AuthService().create_user("ssot_restore@touka.io", "correct_pw_123", role="admin")
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(system_router, "_get_backup_dir", lambda: tmp_path)
    monkeypatch.setattr(system_router, "_oauth_restore_destination_populated", lambda: False)
    monkeypatch.setattr(system_router, "_execute_restore", lambda *args: queued.append(args))
    # 端點會把狀態記成 running；替身不會跑完，測試結束要還原，否則之後的重置會被當成還原中擋下
    monkeypatch.setattr(system_router, "_last_restore", system_router._last_restore)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_verified_identity] = lambda: _ADMIN
    try:
        resp = TestClient(app, raise_server_exceptions=False).post(
            "/system/restore/run",
            json={"filename": backup.name, "current_password": "correct_pw_123"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert queued[0][0] == system_backup.backup_script()


# ── 快照目錄：.env 所在資料夾的 backups，五處共用 ───────────────────────────────


def test_backup_dir_is_backups_next_to_the_env_file():
    assert system_backup.backup_dir() == resolve_env_file_path().parent / "backups"
    assert system_router._get_backup_dir is system_backup.backup_dir  # 清單／下載／刪除／還原
    assert scheduler.backups_dir is system_backup.backup_dir  # 排程清理


def test_snapshot_list_restore_and_prune_follow_the_env_file(monkeypatch, proxy_paths):
    seen = _capture_run(monkeypatch, system_backup)

    system_backup.create_backup()

    cmd = seen[0][0]
    output_dir = cmd[cmd.index("--output-dir") + 1]
    assert Path(output_dir) == proxy_paths["out"]
    assert system_router._get_backup_dir() == proxy_paths["out"]
    assert scheduler.backups_dir() == proxy_paths["out"]


# ── 參數：快照與還原吃同一組 Proxy 路徑 ───────────────────────────────────────


def test_create_backup_and_restore_pass_the_same_proxy_paths(monkeypatch, proxy_paths, tmp_path):
    seen = _capture_run(monkeypatch, system_backup)
    restore_seen = _capture_run(monkeypatch, system_router)
    monkeypatch.setattr(system_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(system_router.os, "_exit", lambda _code: None)

    system_backup.create_backup()
    system_router._execute_restore(system_backup.backup_script(), tmp_path / "b.zip")

    expected = [
        "--data-root", str(proxy_paths["data"]),
        "--env-file", str(proxy_paths["env"]),
        "--config-dir", str(proxy_paths["config"]),
        "--output-dir", str(proxy_paths["out"]),
    ]  # fmt: skip
    (backup_cmd, backup_kwargs), (restore_cmd, restore_kwargs) = seen[0], restore_seen[0]
    assert backup_cmd[-len(expected) :] == expected
    assert restore_cmd[-len(expected) :] == expected
    assert restore_cmd[2:4] == ["restore", "--file"]
    # 腳本要載入記憶庫的快照工具：PYTHONPATH 與 dispatch 子程序同一個 SSOT（child_python_env）
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "synthetic-dispatch-token")
    dispatch_path = DispatchSupervisor().build_child_env()["PYTHONPATH"]
    assert str(_REPO / "ToukaMemory" / "src") in dispatch_path.split(os.pathsep)
    for kwargs in (backup_kwargs, restore_kwargs):
        assert kwargs["env"]["PYTHONPATH"] == dispatch_path


# ── 從重置打進去 ─────────────────────────────────────────────────────────────


async def test_reset_is_not_aborted_by_a_missing_backup_script(monkeypatch, proxy_paths):
    """腳本路徑對了，重置的「備份優先」就不會因 Backup script missing 中止（子程序 mock）。"""
    _capture_run(monkeypatch, system_backup)
    purged: list[str] = []
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, _a: c)
    monkeypatch.setattr(O, "ALL_STORES", (_fake_store(purged),))

    report = await O.reset_agent_data("Jin", _ADMIN)

    assert report.backup is not None
    assert report.backup["status"] == "success"
    assert purged == ["Jin"]


async def test_real_snapshot_in_docker_like_layout_contains_the_data(
    monkeypatch, proxy_paths, tmp_path
):
    """真的跑腳本：往上找不到 .env、.env 在別處、資料根由參數給 → 快照成功且含資料區。"""
    script = _docker_like_script(tmp_path)
    monkeypatch.setattr(system_backup, "backup_script", lambda: script)

    result = await system_backup.run_backup_job(system_backup.create_backup)

    assert result["status"] == "success"
    archives = list(proxy_paths["out"].glob("Touka_Backup_*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        names = set(archive.namelist())
        assert archive.read(".env").decode("utf-8") == "NORMAL_SETTING=kept\n"
    assert "data/soul/Alice/soul.md" in names


async def test_missing_data_root_fails_snapshot_and_aborts_reset(
    monkeypatch, proxy_paths, tmp_path, caplog
):
    """資料根不存在（例如沒掛上）就直接失敗，不能產出不含資料的空快照；重置因此什麼都不刪。"""
    script = _docker_like_script(tmp_path)
    monkeypatch.setattr(system_backup, "backup_script", lambda: script)
    monkeypatch.setattr(system_backup, "resource_manager", _DataRoot(tmp_path / "not-mounted"))
    purged: list[str] = []
    monkeypatch.setattr(O, "assert_agent_resettable", lambda c, _a: c)
    monkeypatch.setattr(O, "ALL_STORES", (_fake_store(purged),))

    with pytest.raises(ToukaException) as excinfo:
        await O.reset_agent_data("Jin", _ADMIN)

    assert excinfo.value.code == ErrorCode.SYSTEM_BACKUP_FAILED
    assert "data root not found" in caplog.text  # 不是別的原因失敗（原因只在伺服器 log）
    assert purged == []
    assert not proxy_paths["out"].exists() or not any(proxy_paths["out"].glob("*.zip"))


def test_script_failure_is_reported_as_backup_failed(monkeypatch, proxy_paths):
    def _fail(cmd: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(system_backup, "run_sync", _fail)

    with pytest.raises(ToukaException) as excinfo:
        system_backup.create_backup()
    assert excinfo.value.code == ErrorCode.SYSTEM_BACKUP_FAILED

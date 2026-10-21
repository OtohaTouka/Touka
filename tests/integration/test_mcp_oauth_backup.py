"""P8-A backup/restore exclusion and conflict oracles."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ToukaProxy" / "scripts" / "manage_backup.py"


def _load_manage_backup():
    spec = importlib.util.spec_from_file_location("mcp_oauth_manage_backup", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "sanctuary"
    data_dir.mkdir()
    config_dir = tmp_path / "ToukaProxy" / "config"
    config_dir.mkdir(parents=True)
    (tmp_path / "ToukaHeartbeat").mkdir()
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"SANCTUARY_DATA_PATH={data_dir.as_posix()}",
                "NORMAL_SETTING=preserved",
                "TOUKA_MCP_OAUTH_KEY=v1:current:key-canary",
                'TOUKA_MCP_OAUTH_RETIRED_KEYS=["retiring-key-canary"]',
                "TOUKA_MCP_OAUTH_ROTATION_PHASE=state-drain",
                "TOUKA_MCP_OAUTH_KEY_EPOCH=9",
                "TOUKA_MCP_OAUTH_WORKLOAD_KEY=v1:workload:key-canary",
                'TOUKA_MCP_OAUTH_WORKLOAD_RETIRED_KEYS=["workload-retiring-canary"]',
                "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE=state-drain",
                "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH=4",
                "",
            )
        ),
        encoding="utf-8",
    )
    return data_dir, config_dir


def test_backup_zip_filters_oauth_keys_and_excludes_runtime_database(tmp_path: Path):
    module = _load_manage_backup()
    _data_dir, config_dir = _workspace(tmp_path)
    (config_dir / "mcp_oauth_profiles.json").write_text("[]", encoding="utf-8")
    for suffix in ("", "-wal", "-shm"):
        (config_dir / f"mcp_oauth.sqlite3{suffix}").write_text(
            f"db-canary{suffix}",
            encoding="utf-8",
        )
        (config_dir / f"mcp_oauth_workload.sqlite3{suffix}").write_text(
            f"workload-db-canary{suffix}",
            encoding="utf-8",
        )
    output = module.BackupEngine(module.PathResolver(tmp_path)).run_backup(
        tmp_path / "backups", "Touka_Backup"
    )

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        env_text = archive.read(".env").decode("utf-8")
    assert "NORMAL_SETTING=preserved" in env_text
    assert "SANCTUARY_DATA_PATH=" in env_text
    assert "key-canary" not in env_text
    assert "retiring-key-canary" not in env_text
    assert "TOUKA_MCP_OAUTH_ROTATION_PHASE" not in env_text
    assert "TOUKA_MCP_OAUTH_KEY_EPOCH" not in env_text
    assert "workload:key-canary" not in env_text
    assert "workload-retiring-canary" not in env_text
    assert "TOUKA_MCP_OAUTH_WORKLOAD_ROTATION_PHASE" not in env_text
    assert "TOUKA_MCP_OAUTH_WORKLOAD_KEY_EPOCH" not in env_text
    assert "config/proxy/mcp_oauth_profiles.json" in names
    assert all("mcp_oauth.sqlite3" not in name for name in names)
    assert all("mcp_oauth_workload.sqlite3" not in name for name in names)


def test_restore_refuses_populated_oauth_destination_before_writing(tmp_path: Path):
    module = _load_manage_backup()
    _data_dir, config_dir = _workspace(tmp_path)
    database = config_dir / "mcp_oauth.sqlite3"
    database.write_bytes(b"existing-oauth-state")
    original_env = (tmp_path / ".env").read_bytes()
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(".env", "SANCTUARY_DATA_PATH=elsewhere\n")

    with pytest.raises(module.OAuthRestoreConflictError):
        module.RestoreEngine(module.PathResolver(tmp_path)).run_restore(source)

    assert database.read_bytes() == b"existing-oauth-state"
    assert (tmp_path / ".env").read_bytes() == original_env


def test_restore_refuses_populated_workload_oauth_destination(tmp_path: Path):
    module = _load_manage_backup()
    _data_dir, config_dir = _workspace(tmp_path)
    (tmp_path / ".env").write_text(
        "SANCTUARY_DATA_PATH=elsewhere\n",
        encoding="utf-8",
    )
    database = config_dir / "mcp_oauth_workload.sqlite3"
    database.write_bytes(b"existing-workload-oauth-state")
    original_env = (tmp_path / ".env").read_bytes()
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(".env", "SANCTUARY_DATA_PATH=elsewhere\n")

    with pytest.raises(module.OAuthRestoreConflictError):
        module.RestoreEngine(module.PathResolver(tmp_path)).run_restore(source)

    assert database.read_bytes() == b"existing-workload-oauth-state"
    assert (tmp_path / ".env").read_bytes() == original_env

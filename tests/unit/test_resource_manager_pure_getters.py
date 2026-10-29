"""TDD tests verifying that ResourceManager getters do NOT create directories.

Only write operations should have the side-effect of creating missing directories.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from memory_server.utils.zone_paths import Anchor
from src.models.identity import AgentIdentity
from src.services.resource_manager import ResourceManager


@pytest.fixture
def resource_mgr() -> ResourceManager:
    return ResourceManager()


@pytest.fixture
def clean_app_data(tmp_path: Path) -> Path:
    """Provides a completely clean temporary application data directory."""
    return tmp_path


def test_get_vault_path_custom_soul_is_pure(
    resource_mgr: ResourceManager, clean_app_data: Path
) -> None:
    """get_vault_path("custom_soul") should return path without creating it on disk."""
    identity = AgentIdentity.for_role(agent_id="CustomAgent", role="Base", is_human=False)
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(clean_app_data)):
        path_str = resource_mgr.get_vault_path("custom_soul", identity)
        assert path_str is not None

        path = Path(path_str)
        assert not path.exists(), f"Path {path} should not be created by get_vault_path"


def test_agent_drive_for_a_custom_model_is_pure(
    resource_mgr: ResourceManager, zone_data_root: Path
) -> None:
    """非靈魂的個人空間是 ``custom/<id>/drive``；``agent_drive`` 不建資料夾（ADR-0293）."""
    identity = AgentIdentity.for_role(agent_id="CustomAgent", role="Base", is_human=False)
    drive = resource_mgr.agent_drive(identity)
    assert drive is not None
    assert drive.anchor is Anchor.CUSTOM_DRIVE
    assert drive.parts == ("CustomAgent", "drive")
    assert not (zone_data_root / "custom").exists()


def test_get_stats_path_is_pure(resource_mgr: ResourceManager, clean_app_data: Path) -> None:
    """get_stats_path() should return path without creating it."""
    identity = AgentIdentity.for_role(agent_id="CustomAgent", role="Base", is_human=False)
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(clean_app_data)):
        path_str = resource_mgr.get_stats_path(identity)
        assert path_str is not None

        path = Path(path_str)
        assert not path.exists(), f"Path {path} should not be created by get_stats_path"


def test_get_logs_path_is_pure(resource_mgr: ResourceManager, clean_app_data: Path) -> None:
    """get_logs_path() should return path without creating it."""
    identity = AgentIdentity.for_role(agent_id="CustomAgent", role="Base", is_human=False)
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(clean_app_data)):
        path_str = resource_mgr.get_logs_path(identity)
        assert path_str is not None

        path = Path(path_str)
        assert not path.exists(), f"Path {path} should not be created by get_logs_path"


def test_get_temp_path_is_pure(resource_mgr: ResourceManager, clean_app_data: Path) -> None:
    """get_temp_path() should return path without creating it."""
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(clean_app_data)):
        path_str = resource_mgr.get_temp_path("test_sub")
        assert path_str is not None

        path = Path(path_str)
        assert not path.exists(), f"Path {path} should not be created by get_temp_path"


def test_write_operations_still_create_parents(
    resource_mgr: ResourceManager, clean_app_data: Path
) -> None:
    """Write operations should successfully create parent directories on demand."""
    identity = AgentIdentity.for_role(agent_id="CustomAgent", role="Base", is_human=False)
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(clean_app_data)):
        # 1. test write_file_text（代理人資料夾以外的位置；區內的寫入只收 ZonePath，ADR-0293）
        file_path = Path(resource_mgr.get_stats_path(identity)) / "sub" / "test.txt"

        assert not file_path.parent.exists()

        resource_mgr.write_file_text(file_path, "hello world")

        assert file_path.parent.exists()
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == "hello world"


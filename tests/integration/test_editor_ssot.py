"""Unit tests for the ResourceManager I/O Proxy and its Soul Protection architecture.

（原本也測 ``text_editor``；那個工具沒有呼叫端，ADR-0293 刪除。）
"""

from pathlib import Path

import pytest
from src.services.resource_manager import ResourceManager


@pytest.fixture
def temp_workspace(zone_data_root: Path) -> Path:
    """這個測試的 data 根（ADR-0293 T3）."""
    return zone_data_root


@pytest.fixture
def resource_manager_mock(temp_workspace):
    return ResourceManager()


class TestResourceManagerIOProxy:
    """測試 ResourceManager I/O 代理方法的保護邏輯."""

    def test_is_protected_path(self, resource_manager_mock, temp_workspace):
        legacy_path = temp_workspace / "legacy" / "test_agent" / "file.txt"
        soul_path = temp_workspace / "soul" / "test_agent" / "file.txt"
        normal_path = temp_workspace / "personal_drive" / "test_agent" / "file.txt"

        assert resource_manager_mock.is_protected_path(legacy_path) is True
        assert resource_manager_mock.is_protected_path(soul_path) is True
        assert resource_manager_mock.is_protected_path(normal_path) is False

    def test_write_text_protection(self, resource_manager_mock, temp_workspace):
        legacy_path = temp_workspace / "legacy" / "test_agent" / "file.txt"
        
        with pytest.raises(PermissionError, match="Soul Protection Error"):
            resource_manager_mock.write_file_text(legacy_path, "test content")

    def test_copy_file_protection(self, resource_manager_mock, temp_workspace):
        normal_path = temp_workspace / "notes" / "test_agent" / "file.txt"
        legacy_path = temp_workspace / "legacy" / "test_agent" / "file.txt"
        
        resource_manager_mock.write_file_text(normal_path, "test")
        
        with pytest.raises(PermissionError, match="Soul Protection Error"):
            resource_manager_mock.copy_file(normal_path, legacy_path)

    def test_normal_io_operations(self, resource_manager_mock, temp_workspace):
        normal_path = temp_workspace / "notes" / "test_agent" / "file.txt"
        backup_path = temp_workspace / "notes" / "test_agent" / "file.txt.bak"
        
        # Test write and read
        resource_manager_mock.write_file_text(normal_path, "hello")
        assert resource_manager_mock.read_file_text(normal_path) == "hello"
        assert resource_manager_mock.exists(normal_path) is True
        
        # Test copy
        resource_manager_mock.copy_file(normal_path, backup_path)
        assert resource_manager_mock.exists(backup_path) is True
        assert resource_manager_mock.read_file_text(backup_path) == "hello"
        
        # Test move
        resource_manager_mock.move_file(backup_path, normal_path)
        assert resource_manager_mock.read_file_text(normal_path) == "hello"


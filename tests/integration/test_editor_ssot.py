"""Unit tests for Editor SSOT compliance and ResourceManager I/O Proxy.

Verifies that the text_editor tool properly uses the ResourceManager for all I/O
operations and correctly enforces the Soul Protection architecture.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity
from src.services.resource_manager import ResourceManager
from src.tools.editor import text_editor


@pytest.fixture
def mock_identity():
    return AgentIdentity(agent_id="test_agent")


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def resource_manager_mock(temp_workspace):
    rm = ResourceManager()
    # Mock get_app_data_dir to return our temp workspace
    with patch("src.services.resource_manager.get_app_data_dir", return_value=str(temp_workspace)):
        yield rm


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
        normal_path = temp_workspace / "personal_drive" / "test_agent" / "file.txt"
        legacy_path = temp_workspace / "legacy" / "test_agent" / "file.txt"
        
        resource_manager_mock.write_file_text(normal_path, "test")
        
        with pytest.raises(PermissionError, match="Soul Protection Error"):
            resource_manager_mock.copy_file(normal_path, legacy_path)

    def test_normal_io_operations(self, resource_manager_mock, temp_workspace):
        normal_path = temp_workspace / "personal_drive" / "test_agent" / "file.txt"
        backup_path = temp_workspace / "personal_drive" / "test_agent" / "file.txt.bak"
        
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


@pytest.mark.asyncio
class TestEditorSSOTCompliance:
    """測試 editor.py 是否正確服從 SSOT 與保護機制."""

    async def test_text_editor_soul_protection(self, mock_identity, temp_workspace):
        # 模擬 _get_safe_path 回傳一個 legacy 路徑
        legacy_path = temp_workspace / "legacy" / "test_agent" / "sys.txt"
        
        with patch("src.tools.editor._get_safe_path", return_value=legacy_path), \
             patch("src.tools.editor.resource_manager.is_protected_path", return_value=True):
            
            # 嘗試寫入操作
            result = await text_editor(
                command="create",
                path="sys.txt",
                identity=mock_identity,
                file_text="hack"
            )
            assert "Soul Protection Error" in result
            assert "prohibited" in result

    async def test_text_editor_normal_workflow(self, mock_identity, temp_workspace):
        normal_path = temp_workspace / "personal_drive" / "test_agent" / "code.py"
        
        with patch("src.tools.editor._get_safe_path", return_value=normal_path), \
             patch("src.tools.editor.resource_manager") as mock_rm:
            
            mock_rm.is_protected_path.return_value = False
            mock_rm.exists.return_value = False
            
            # 1. Test create
            result = await text_editor(
                command="create",
                path="code.py",
                identity=mock_identity,
                file_text="print('hello')"
            )
            assert "Success" in result
            mock_rm.write_file_text.assert_called_once_with(normal_path, "print('hello')")
            
            # 2. Test view
            mock_rm.exists.return_value = True
            mock_rm.read_file_text.return_value = "print('hello')"
            
            result = await text_editor(
                command="view",
                path="code.py",
                identity=mock_identity
            )
            assert "1: print('hello')" in result

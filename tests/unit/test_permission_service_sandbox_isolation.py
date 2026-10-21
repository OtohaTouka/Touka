"""TDD tests verifying that PermissionService resets correctly across different sandbox directories.

Ensures that switching TOUKA_CONFIG_DIR dynamically resets paths and loads the new configs.
"""
from __future__ import annotations

import json
import os

import pytest
from src.services.permission_service import permission_service


@pytest.fixture
def base_permissions_data() -> dict:
    return {
        "roles": {
            "Owner": {
                "desc": "Superuser",
                "allowed_tools": ["*"],
                "allowed_mcp_servers": ["*"],
                "allowed_skills": ["*"],
            }
        },
        "system_config": {"default_role": "Base"},
        "agent_mappings": {},
        "mcp_servers": [],
    }


def test_permission_service_sandbox_isolation(tmp_path, base_permissions_data: dict) -> None:
    """Verifies that PermissionService re-initializes and loads config from new TOUKA_CONFIG_DIR."""
    # Backup original environment
    original_config_dir = os.environ.get("TOUKA_CONFIG_DIR")  # noqa: ENV_SSOT_VIOLATION

    try:
        from src.services.resource_manager import resource_manager

        # 1. Setup Sandbox 1
        sandbox1 = tmp_path / "sandbox1"
        resource_manager.make_dir(sandbox1)
        
        # Use deepcopy to prevent side-effects on base_permissions_data
        import copy
        config1 = copy.deepcopy(base_permissions_data)
        config1["roles"]["Role1"] = {
            "desc": "Sandbox 1 Role",
            "allowed_tools": ["tool1"],
        }
        resource_manager.write_file_text(
            sandbox1 / "permissions.json", json.dumps(config1)
        )
        
        # Point environment to Sandbox 1 and reset service
        os.environ["TOUKA_CONFIG_DIR"] = str(sandbox1)  # noqa: ENV_SSOT_VIOLATION
        permission_service.reset_for_test()

        # Assert Sandbox 1 role is loaded
        assert permission_service.roles_config is not None
        assert "Role1" in permission_service.roles_config.roles
        assert "Role2" not in permission_service.roles_config.roles

        # 2. Setup Sandbox 2
        sandbox2 = tmp_path / "sandbox2"
        resource_manager.make_dir(sandbox2)
        config2 = copy.deepcopy(base_permissions_data)
        config2["roles"]["Role2"] = {
            "desc": "Sandbox 2 Role",
            "allowed_tools": ["tool2"],
        }
        resource_manager.write_file_text(
            sandbox2 / "permissions.json", json.dumps(config2)
        )

        # Point environment to Sandbox 2 and reset service again
        os.environ["TOUKA_CONFIG_DIR"] = str(sandbox2)  # noqa: ENV_SSOT_VIOLATION
        permission_service.reset_for_test()

        # Assert Sandbox 2 role is loaded and Sandbox 1 role is gone
        assert permission_service.roles_config is not None
        assert "Role2" in permission_service.roles_config.roles
        assert "Role1" not in permission_service.roles_config.roles

    finally:
        # Restore environment
        if original_config_dir is not None:
            os.environ["TOUKA_CONFIG_DIR"] = original_config_dir  # noqa: ENV_SSOT_VIOLATION
        else:
            os.environ.pop("TOUKA_CONFIG_DIR", None)  # noqa: ENV_SSOT_VIOLATION
        
        # Reset once more to restore original configurations
        permission_service.reset_for_test()

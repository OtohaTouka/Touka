"""Unit tests for ResourceManager: path isolation, traversal defence, and is_owner logic.

Merged from:
  - test_resource_guard.py     (path isolation & traversal, 3 tests)
  - test_resource_manager_permissions.py (is_owner layered permissions, 6 tests)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from memory_server.utils.zone_paths import Anchor
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.resource_manager import ResourceManager

from tests.conftest import SESSION_TMP_DATA_ROOT


# 強制重寫 AuthService 構造函數，確保在 ResourceManager 測試中徹底落實沙盒隔離
def _sandboxed_auth_init(self, users_file_path=None):
    if users_file_path:
        self.users_file_path = Path(users_file_path)
    else:
        self.users_file_path = Path(SESSION_TMP_DATA_ROOT) / "config" / "users.json"


@pytest.fixture(autouse=True)
def _sandbox_auth_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limit the synthetic constructor to this module's individual tests."""
    monkeypatch.setattr(AuthService, "__init__", _sandboxed_auth_init)


@pytest.fixture
def resource_mgr():
    """ResourceManager instance."""
    return ResourceManager()


@pytest.fixture
def mock_app_data(zone_data_root):
    """Minimal app_data directory tree for path tests (ADR-0293 T3)."""
    (zone_data_root / "soul" / "Alice").mkdir(parents=True)
    (zone_data_root / "personal_drive" / "Alice").mkdir(parents=True)
    (zone_data_root / "personal_drive" / "Stranger").mkdir(parents=True)
    return zone_data_root


# ── Path isolation & traversal defence ─────────────────────────────────────


def test_vault_path_isolation(resource_mgr, mock_app_data):
    """Different identities are locked to their own sub-directories.

    個人空間改由 ``agent_drive`` 給 ``ZonePath``（ADR-0293；``get_vault_path("personal")``
    不再交出字串）。
    """
    alice = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    stranger = AgentIdentity.for_role(agent_id="Stranger", role="Base")

    alice_drive = resource_mgr.agent_drive(alice)
    assert alice_drive.anchor is Anchor.PERSONAL_DRIVE
    assert alice_drive.parts == ("Alice",)

    stranger_drive = resource_mgr.agent_drive(stranger)
    assert stranger_drive.parts[0] == "Stranger"
    assert "Alice" not in stranger_drive.parts


def test_path_traversal_defence(resource_mgr, mock_app_data):
    """The drive stays at the agent's own segment; an id that is not a segment gets none."""
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    assert resource_mgr.agent_drive(identity).parts == ("Alice",)
    escaping = AgentIdentity.for_role(agent_id="..", role="Owner")
    assert resource_mgr.agent_drive(escaping) is None


def test_vault_root_access_control(resource_mgr, mock_app_data):
    """Owner can access stats root; Base role cannot."""
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    base_user = AgentIdentity.for_role(agent_id="UserA", role="Base")

    assert resource_mgr.get_vault_root("stats", owner) is not None
    assert resource_mgr.get_vault_root("stats", base_user) is None


# ── is_owner layered permissions ────────────────────────────────────────────


def test_is_owner_admin_bypass(resource_mgr):
    """Admin role has global access bypass."""
    admin = AgentIdentity.for_role(agent_id="admin_uuid", role="admin", is_human=True)
    target = AgentIdentity.for_role(agent_id="AnyAgent", role="base", is_human=False)
    assert resource_mgr.is_owner(target, admin) is True


def test_is_owner_system_bypass(resource_mgr):
    """System role has global access bypass (used for background scans)."""
    system = AgentIdentity.for_role(agent_id="system", role="System", is_human=False)
    target = AgentIdentity.for_role(agent_id="AnyAgent", role="base", is_human=False)
    assert resource_mgr.is_owner(target, system) is True


def test_is_owner_self_sovereignty(resource_mgr, monkeypatch):
    """An agent has sovereignty over itself."""
    agent = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False)
    target = AgentIdentity.for_role(agent_id="Alice", role="base", is_human=False)
    with patch("src.services.auth_service.AuthService.get_all_users", return_value=[]):
        assert resource_mgr.is_owner(target, agent) is True


def test_is_owner_human_ownership_success(resource_mgr):
    """Human user can access agents they own."""
    human = AgentIdentity.for_role(agent_id="user_uuid", role="user", is_human=True)
    target = AgentIdentity.for_role(agent_id="Alice", role="base", is_human=False)

    mock_user = MagicMock()
    mock_user.owned_agents = ["Alice", "Bob"]
    with patch("src.services.auth_service.AuthService.get_user_by_id", return_value=mock_user):
        assert resource_mgr.is_owner(target, human) is True


def test_is_owner_human_ownership_fail(resource_mgr):
    """Human user cannot access agents they do not own."""
    human = AgentIdentity.for_role(agent_id="user_uuid", role="user", is_human=True)
    target = AgentIdentity.for_role(agent_id="Bob", role="base", is_human=False)

    mock_user = MagicMock()
    mock_user.owned_agents = ["Alice"]
    with patch("src.services.auth_service.AuthService.get_user_by_id", return_value=mock_user):
        assert resource_mgr.is_owner(target, human) is False


def test_is_owner_non_human_base_no_crash(resource_mgr):
    """Non-human Base role must return False (not crash) when owner lookup returns None."""
    temp = AgentIdentity.for_role(agent_id="CustomModel123", role="Base", is_human=False)
    target = AgentIdentity.for_role(agent_id="SomeTarget", role="base", is_human=False)

    with patch("src.services.auth_service.AuthService.get_user_by_id", return_value=None):
        try:
            result = resource_mgr.is_owner(target, temp)
            assert result is False
        except AttributeError:
            pytest.fail("is_owner crashed with AttributeError when user not found")

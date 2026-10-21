"""Unit tests for souls router RBAC - 靈魂端點權限過濾測試.

TDD: Verifies that /souls listing correctly enforces RBAC.
- Admin can see ALL souls.
- Regular user can only see souls in their owned_agents list.
- Agent identity (non-human) gets empty list.

使用 main app + dependency override 方式，確保與 Python 3.14 / FastAPI 相容。
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity

# ---------------------------------------------------------------------------
# Identity factories
# ---------------------------------------------------------------------------

def _admin(owned: list[str] | None = None) -> AgentIdentity:
    return AgentIdentity.for_role(
        agent_id="admin_user",
        role="admin",
        is_human=True,
        owned_agents=owned or [],
        capabilities=[],
    )


def _user(owned: list[str]) -> AgentIdentity:
    return AgentIdentity.for_role(
        agent_id="regular_user",
        role="user",
        is_human=True,
        owned_agents=owned,
        capabilities=[],
    )


def _agent() -> AgentIdentity:
    return AgentIdentity.for_role(
        agent_id="NormalAgent",
        role="Base",
        is_human=False,
        owned_agents=[],
        capabilities=[],
    )


FAKE_SOULS = ["Alice", "Bob", "Charlie"]


@pytest.fixture
def soul_sandbox(tmp_path: Path) -> Path:
    """建立隔離的假靈魂目錄 (沙盒保護，不觸碰真實 data/)."""
    soul_root = tmp_path / "data" / "soul"
    for soul_id in FAKE_SOULS:
        soul_dir = soul_root / soul_id
        soul_dir.mkdir(parents=True, exist_ok=True)
        (soul_dir / "soul.md").write_text(f"I am {soul_id}", encoding="utf-8")
        (soul_dir / "config.json").write_text("{}", encoding="utf-8")
    return soul_root


def _get_soul_ids(client: TestClient) -> set[str]:
    resp = client.get("/souls")
    assert resp.status_code == 200
    return {s["id"] for s in resp.json()["souls"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSoulsRbac:
    """靈魂端點的 RBAC 行為驗證。

    透過 main app + dependency override 隔離真實資料。
    """

    def _make_client(self, identity: AgentIdentity, soul_root: Path) -> TestClient:
        app.dependency_overrides[get_verified_identity] = lambda: identity
        client = TestClient(app, raise_server_exceptions=True)
        return client, app

    def test_admin_sees_all_souls(self, soul_sandbox: Path):
        """Admin 角色應獲取所有靈魂 (完整清單)."""
        identity = _admin()
        app.dependency_overrides[get_verified_identity] = lambda: identity

        try:
            with patch("src.routers.souls.resource_manager") as mock_rm:
                mock_rm.get_vault_root.return_value = str(soul_sandbox)
                mock_rm.exists.side_effect = lambda p: Path(p).exists()
                mock_rm.read_file_text.side_effect = lambda p: Path(p).read_text(encoding="utf-8")
                mock_rm.read_json.return_value = {}

                with TestClient(app) as client:
                    soul_ids = _get_soul_ids(client)
        finally:
            app.dependency_overrides.clear()

        assert soul_ids == set(FAKE_SOULS), (
            f"Admin should see all souls {FAKE_SOULS}, got {soul_ids}"
        )

    def test_user_only_sees_owned_souls(self, soul_sandbox: Path):
        """一般使用者只能看到自己 owned_agents 中的靈魂."""
        identity = _user(owned=["Alice"])
        app.dependency_overrides[get_verified_identity] = lambda: identity

        try:
            with patch("src.routers.souls.resource_manager") as mock_rm:
                mock_rm.get_vault_root.return_value = str(soul_sandbox)
                mock_rm.exists.side_effect = lambda p: Path(p).exists()
                mock_rm.read_file_text.side_effect = lambda p: Path(p).read_text(encoding="utf-8")
                mock_rm.read_json.return_value = {}

                with TestClient(app) as client:
                    soul_ids = _get_soul_ids(client)
        finally:
            app.dependency_overrides.clear()

        assert soul_ids == {"Alice"}, (
            f"User owning only 'Alice' should see {{'Alice'}}, got {soul_ids}"
        )

    def test_user_with_no_owned_souls_sees_empty(self, soul_sandbox: Path):
        """未擁有任何靈魂的一般使用者應獲得空清單."""
        identity = _user(owned=[])
        app.dependency_overrides[get_verified_identity] = lambda: identity

        try:
            with patch("src.routers.souls.resource_manager") as mock_rm:
                mock_rm.get_vault_root.return_value = str(soul_sandbox)
                mock_rm.exists.side_effect = lambda p: Path(p).exists()
                mock_rm.read_file_text.side_effect = lambda p: Path(p).read_text(encoding="utf-8")
                mock_rm.read_json.return_value = {}

                with TestClient(app) as client:
                    resp = client.get("/souls")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["souls"] == [], (
            "User with no owned agents should see empty soul list"
        )

    def test_agent_identity_sees_empty(self, soul_sandbox: Path):
        """非人類 Agent 身分請求 /souls 應獲得空清單."""
        identity = _agent()
        app.dependency_overrides[get_verified_identity] = lambda: identity

        try:
            with patch("src.routers.souls.resource_manager") as mock_rm:
                mock_rm.get_vault_root.return_value = str(soul_sandbox)
                mock_rm.exists.side_effect = lambda p: Path(p).exists()
                mock_rm.read_file_text.side_effect = lambda p: Path(p).read_text(encoding="utf-8")
                mock_rm.read_json.return_value = {}

                with TestClient(app) as client:
                    resp = client.get("/souls")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["souls"] == [], (
            "Non-human agent identity should see empty soul list"
        )

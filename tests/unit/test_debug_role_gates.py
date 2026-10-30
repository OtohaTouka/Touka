"""TDD — debug 端點角色閘：人類 admin 應放行（context-snapshot／export／tools）。

實機症狀（2026-07-07）：Debug Center「Context」分頁介面正常但內容全空白——人類 admin
登入被 context-snapshot 角色閘拒絕，且拒絕回 200＋空 blocks（靜默）。IDOR 修復
（test_idor_soul_sandbox_security.py 檔頭）當時的閘含 admin，ADR-0133 重寫後遺失。

本檔釘死兩件事：
(1) admin 三端點放行——context-snapshot／export／tools 與 ``_require_debug_role``
    三元組（Owner/Debugger/admin）對齊；
(2) snapshot GET 的拒絕改 403 fail-loud（與 export 一致），不再 200＋空 blocks
    ——靜默拒絕會把權限問題偽裝成「組裝壞掉」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager

from tests.support.zone_roots import point_zone_roots

ADMIN = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)
BASE = AgentIdentity.for_role(agent_id="BaseUser", role="Base", is_human=True)


@pytest.fixture
def admin_client():
    app.dependency_overrides[get_verified_identity] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def base_client():
    app.dependency_overrides[get_verified_identity] = lambda: BASE
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestContextSnapshotRoleGate:
    def test_admin_allowed(self, admin_client, monkeypatch):
        """人類 admin 讀 DebugAgent 快照必須放行（is_owner 對人類 admin 絕對放行）。"""

        async def _fake_snap(*_a, **_k):
            return {"blocks": ["ok"]}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _fake_snap
        )
        r = admin_client.get("/v1/debug/context-snapshot?agent_id=DebugAgent")
        assert r.status_code == 200
        assert r.json() == {"blocks": ["ok"]}

    def test_low_role_denied_403_fail_loud(self, base_client, monkeypatch):
        """低權角色拒絕必須 403（fail-loud），不得 200＋空 blocks 偽裝成組裝失敗。"""
        reached = {"v": False}

        async def _spy(*_a, **_k):
            reached["v"] = True
            return {"blocks": ["SECRET"]}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _spy
        )
        r = base_client.get("/v1/debug/context-snapshot?agent_id=DebugAgent")
        assert r.status_code == 403
        assert "SECRET" not in r.text
        assert reached["v"] is False  # 角色閘要擋在 service 之前

    def test_export_admin_allowed(self, admin_client, monkeypatch, tmp_path):
        async def _fake_snap(*_a, **_k):
            return {"blocks": []}

        monkeypatch.setattr(
            "src.services.context_snapshot_service.get_context_snapshot", _fake_snap
        )
        monkeypatch.setattr(
            "src.services.context_snapshot_service.render_snapshot_markdown",
            lambda snap: "MARKDOWN",
        )
        point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
        monkeypatch.setattr(resource_manager, "write_file_text", lambda *a, **k: None)
        r = admin_client.get("/v1/debug/context-snapshot/export?agent_id=DebugAgent")
        assert r.status_code == 200
        assert "MARKDOWN" in r.text


class TestDebugToolsRoleGate:
    def test_admin_allowed(self, admin_client):
        """人類 admin 讀工具清單必須放行（拒絕路徑回 200＋空 tools，故以哨兵區分）。"""
        admin_client.app.state.context["builtin_tools_schema"] = [
            {"function": {"name": "sentinel_tool", "description": "x"}}
        ]
        r = admin_client.get("/v1/debug/tools")
        assert r.status_code == 200
        names = [
            (t.get("function") or {}).get("name") for t in r.json().get("tools", [])
        ]
        assert "sentinel_tool" in names

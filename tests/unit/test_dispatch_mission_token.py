"""ADR-0064：ToukaDispatch mission_token 機制測試（TDD）。

驗證：
  1. create_mission_token 回傳唯一 token，可 resolve 到 mission_id
  2. revoke_mission_token 後 validate_mission_token 回傳 None
  3. clear_mission_tokens 清空所有 mission_token
  4. create_external_session 建立帶 is_external=True + bound_mission_id 的 session
  5. 一般 create_session 建立的 session is_external=False, bound_mission_id=None
  6. dispatch_handshake 接受 mission_token，建立外部 session
  7. dispatch_handshake 拒絕偽造 mission_token
  8. dispatch_initialize_mission 拒絕外部 session 呼叫
  9. dispatch_initialize_mission 回傳 Mission ID 和 Mission Token
"""

from __future__ import annotations

import pytest
from session_manager import FIXED_DISPATCH_AGENT_ID, SessionManager

# ---------------------------------------------------------------------------
# SessionManager mission_token 單元測試
# ---------------------------------------------------------------------------


class TestMissionToken:
    def test_create_mission_token_resolves_to_mission_id(self):
        """create_mission_token 回傳唯一 token，validate_mission_token 可還原 mission_id。"""
        mgr = SessionManager()
        mission_id = "mission-abc-123"
        token = mgr.create_mission_token(mission_id)
        assert isinstance(token, str)
        assert len(token) > 20
        assert mgr.validate_mission_token(token) == mission_id

    def test_revoke_mission_token_returns_none(self):
        """revoke 後 validate_mission_token 回傳 None。"""
        mgr = SessionManager()
        token = mgr.create_mission_token("mission-xyz")
        mgr.revoke_mission_token(token)
        assert mgr.validate_mission_token(token) is None

    def test_clear_mission_tokens_invalidates_all(self):
        """clear_mission_tokens 後，所有既有 mission_token 均失效。"""
        mgr = SessionManager()
        t1 = mgr.create_mission_token("mission-1")
        t2 = mgr.create_mission_token("mission-2")
        mgr.clear_mission_tokens()
        assert mgr.validate_mission_token(t1) is None
        assert mgr.validate_mission_token(t2) is None

    def test_create_external_session_has_correct_flags(self):
        """create_external_session 建立的 session 應帶 is_external=True 和 bound_mission_id。"""
        mgr = SessionManager()
        mission_id = "mission-external"
        session_token = mgr.create_external_session(mission_id, {"name": "claude-code"})
        info = mgr.validate(session_token)
        assert info is not None
        assert info.is_external is True
        assert info.bound_mission_id == mission_id
        assert info.agent_id == FIXED_DISPATCH_AGENT_ID

    def test_internal_session_is_not_external(self):
        """一般 create_session 建立的 session is_external=False，bound_mission_id=None。"""
        mgr = SessionManager()
        session_token = mgr.create_session({"name": "Alice"})
        info = mgr.validate(session_token)
        assert info is not None
        assert info.is_external is False
        assert info.bound_mission_id is None


# ---------------------------------------------------------------------------
# dispatch_handshake with mission_token
# ---------------------------------------------------------------------------


class TestHandshakeMissionToken:
    @pytest.mark.asyncio
    async def test_handshake_accepts_mission_token(self, zone_data_root, monkeypatch):
        """dispatch_handshake 可接受 mission_token，回傳成功並建立外部 session。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")

        from ToukaDispatch import server as ds

        mission_id = "test-mission-456"
        mission_token = ds._session_mgr.create_mission_token(mission_id)

        result = await ds.dispatch_handshake(
            dispatch_token=mission_token,
            caller_name="ExternalAI",
        )
        assert "✅" in result
        assert "Session token:" in result

        session_token = result.split("Session token:")[-1].strip()
        info = ds._session_mgr.validate(session_token)
        assert info is not None
        assert info.is_external is True
        assert info.bound_mission_id == mission_id

    @pytest.mark.asyncio
    async def test_handshake_rejects_fake_mission_token(self, zone_data_root, monkeypatch):
        """偽造的 mission_token 應被拒絕。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(
            dispatch_token="totally-fake-mission-token",
            caller_name="Attacker",
        )
        assert "Unauthorized" in result or "Error" in result


# ---------------------------------------------------------------------------
# dispatch_initialize_mission 外部 session 限制
# ---------------------------------------------------------------------------


class TestInitializeMissionRestrictions:
    @pytest.mark.asyncio
    async def test_external_session_cannot_initialize_mission(self, zone_data_root, monkeypatch):
        """外部 session 不得呼叫 dispatch_initialize_mission，應回傳錯誤。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")

        from memory_server import config_utils

        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        mission_id = "existing-mission"
        ds._session_mgr.create_mission_token(mission_id)
        external_session = ds._session_mgr.create_external_session(
            mission_id, {"name": "ExternalAI"}
        )

        result = await ds.dispatch_initialize_mission(
            session_token=external_session,
            goal="Try to hijack the mission",
        )
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_initialize_mission_returns_mission_id_and_token(
        self, zone_data_root, monkeypatch
    ):
        """dispatch_initialize_mission 回傳中應包含 Mission ID 和 Mission Token。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "internal-secret")

        from memory_server import config_utils

        from ToukaDispatch import server as ds

        config_utils._reset_resource_proxy_cache()

        handshake = await ds.dispatch_handshake(
            dispatch_token="internal-secret",
            caller_name="Alice",
        )
        session_token = handshake.split("Session token:")[-1].strip()

        result = await ds.dispatch_initialize_mission(
            session_token=session_token,
            goal="Test mission",
            tasks=[{"id": "t1", "description": "Do something"}],
        )
        assert "✅" in result
        assert "Mission ID:" in result
        assert "Mission Token:" in result

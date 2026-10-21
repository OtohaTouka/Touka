"""TDD: ToukaDispatch SessionManager 與 Handshake 機制測試 (Vuln 1 修復驗證).

驗證：
  1. SessionManager.create_session 返回唯一 token
  2. SessionManager.validate 正確驗證有效/過期/不存在 token
  3. SessionInfo.agent_id 永遠為 DispatchAgent（Vuln 1 修復核心）
  4. dispatch_handshake 工具：DISPATCH_TOKEN 驗證並換取 session token
  5. 操作工具使用 session token，拒絕直接使用 DISPATCH_TOKEN 或任何無效 token
"""

from __future__ import annotations

import pytest
from session_manager import FIXED_DISPATCH_AGENT_ID, SessionInfo, SessionManager

# ===========================================================================
# 1. SessionManager 單元測試
# ===========================================================================


class TestSessionManager:
    """SessionManager 純邏輯單元測試（不需要 MCP server）。"""

    def test_create_returns_non_empty_token(self):
        """create_session 應回傳非空字串 token。"""
        mgr = SessionManager()
        token = mgr.create_session({"name": "TestCaller"})
        assert isinstance(token, str)
        assert len(token) > 16

    def test_validate_valid_token_returns_session_info(self):
        """validate 對有效 token 應回傳 SessionInfo 物件。"""
        mgr = SessionManager()
        token = mgr.create_session({"name": "TestCaller"})
        info = mgr.validate(token)
        assert info is not None
        assert isinstance(info, SessionInfo)

    def test_session_agent_id_is_always_dispatch_agent(self):
        """SessionInfo.agent_id 永遠固定為 DispatchAgent，不受 caller_info 影響（Vuln 1 修復）。"""
        mgr = SessionManager()
        # 即便 caller_info 宣稱為任意高權限身份，session.agent_id 仍必須是 DispatchAgent
        for spoofed_name in ("DebugAgent", "Alice", "admin", "root", "can_supervise_dispatch"):
            token = mgr.create_session({"name": spoofed_name})
            info = mgr.validate(token)
            assert info is not None
            assert info.agent_id == FIXED_DISPATCH_AGENT_ID, (
                f"caller '{spoofed_name}' should NOT be able to set agent_id via caller_info"
            )

    def test_validate_unknown_token_returns_none(self):
        """validate 對不存在的 token 應回傳 None。"""
        mgr = SessionManager()
        assert mgr.validate("nonexistent-token-xyz") is None

    def test_validate_expired_token_returns_none(self):
        """validate 對 ttl=-1 的立即過期 session 應回傳 None。"""
        mgr = SessionManager(ttl=-1)
        token = mgr.create_session({"name": "ExpiredCaller"})
        assert mgr.validate(token) is None

    def test_caller_info_is_not_retained_in_session_store(self):
        """ADR-0201：caller 自述欄位不進 credential store，只保留授權分面。"""
        mgr = SessionManager()
        caller_info = {"name": "ClaudeCode", "version": "1.2.3"}
        token = mgr.create_session(caller_info)
        info = mgr.validate(token)
        assert info is not None
        assert not hasattr(info, "caller_info")
        assert "ClaudeCode" not in repr(mgr._sessions)


# ===========================================================================
# 2. dispatch_handshake 工具測試
# ===========================================================================


class TestDispatchHandshake:
    """dispatch_handshake 工具：server-side DISPATCH_TOKEN 驗證換取 session token（Vuln 3 修復）。"""

    @pytest.mark.asyncio
    async def test_handshake_succeeds_when_dispatch_token_configured(
        self, tmp_path, monkeypatch
    ):
        """DISPATCH_TOKEN 已設定時，呼叫方無需傳遞密鑰即可換取 session token。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "valid-dispatch-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(
            dispatch_token="valid-dispatch-secret",
            caller_name="ClaudeCode",
            caller_version="1.0",
        )
        assert "✅" in result
        assert "Session token:" in result

    @pytest.mark.asyncio
    async def test_handshake_session_token_differs_from_dispatch_token(
        self, tmp_path, monkeypatch
    ):
        """回傳的 session token 必須是獨立隨機值，不得等於 DISPATCH_TOKEN env var（密鑰隔離）。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "my-dispatch-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(
            dispatch_token="my-dispatch-secret", caller_name="ClaudeCode"
        )
        assert "Session token:" in result
        session_token = result.split("Session token:")[-1].strip()
        assert session_token != "my-dispatch-secret", (
            "session token must be a distinct random value, not the DISPATCH_TOKEN itself"
        )

    @pytest.mark.asyncio
    async def test_handshake_rejects_when_dispatch_token_not_configured(
        self, tmp_path, monkeypatch
    ):
        """DISPATCH_TOKEN 環境變數為空時，handshake 應拒絕所有請求。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(dispatch_token="any-attempt", caller_name="AnyTool")
        assert "Unauthorized" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_handshake_rejects_wrong_dispatch_token(self, tmp_path, monkeypatch):
        """呼叫方傳入錯誤 dispatch_token 應被拒絕（ADR-0050）。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "correct-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(
            dispatch_token="wrong-secret", caller_name="Attacker"
        )
        assert "Unauthorized" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_handshake_rejects_empty_dispatch_token_param(self, tmp_path, monkeypatch):
        """呼叫方傳入空字串 dispatch_token 應被拒絕，即使 env var 已設定（ADR-0050）。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "correct-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(dispatch_token="", caller_name="Attacker")
        assert "Unauthorized" in result or "Error" in result


# ===========================================================================
# 3. 操作工具 Session Token 驗證測試
# ===========================================================================


class TestDispatchToolSessionValidation:
    """操作工具必須使用 session token，不得直接接受 DISPATCH_TOKEN。"""

    @pytest.mark.asyncio
    async def test_task_tool_rejects_direct_dispatch_token(self, tmp_path, monkeypatch):
        """直接使用 DISPATCH_TOKEN 作為 session_token 呼叫操作工具應被拒絕。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "actual-dispatch-token")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        # DISPATCH_TOKEN 本身不是 session token，不會在 _session_mgr 中找到
        result = await ds.dispatch_get_board(session_token="actual-dispatch-token")
        assert "Unauthorized" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_task_tool_accepts_valid_session_token(self, tmp_path, monkeypatch):
        """合法 session token 應能成功呼叫操作工具，不回傳 Unauthorized。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from memory_server import config_utils

        config_utils._reset_resource_proxy_cache()

        from ToukaDispatch import server as ds

        # 先握手取得 session token
        handshake_result = await ds.dispatch_handshake(
            dispatch_token="dispatch-secret",
            caller_name="TestTool",
        )
        assert "Session token:" in handshake_result
        session_token = handshake_result.split("Session token:")[-1].strip()

        result = await ds.dispatch_get_board(session_token=session_token)
        assert "Unauthorized" not in result
        assert "Error: Unauthorized" not in result

    @pytest.mark.asyncio
    async def test_task_tool_rejects_expired_session(self, tmp_path, monkeypatch):
        """過期的 session token 應被拒絕，回傳 Unauthorized 提示重新握手。"""
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-secret")
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))

        from ToukaDispatch import server as ds

        # 用 ttl=-1 取代模組單例，使 session 立即過期
        monkeypatch.setattr(ds, "_session_mgr", SessionManager(ttl=-1))

        # 直接建立已過期的 session（繞過 handshake）
        expired_token = ds._session_mgr.create_session({"name": "ExpiredTool"})

        result = await ds.dispatch_get_board(session_token=expired_token)
        assert "Unauthorized" in result or "Error" in result

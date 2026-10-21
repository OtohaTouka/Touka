"""TDD: ToukaDispatch 專用 MCP 派發伺服器工具測試 (C1 + Vuln 1/2/3 修復驗證).

驗證：
  1. DISPATCH_TOKEN server-side 驗證（空 env var 拒絕所有）
  2. 無 FORCE_AGENT_ID 機制（靜態確認 C1 修復）
  3. 三個操作工具不含 agent_id 參數（靜態確認 Vuln 1 修復）
  4. dispatch_handshake 含 dispatch_token 參數（靜態確認 ADR-0050 修復）
  5. 操作工具使用 session token（handshake 後可正常存取 DispatchAgent 看板）
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DISPATCH_DIR = Path(__file__).resolve().parents[2] / "ToukaDispatch"


# ===========================================================================
# 1. 靜態代碼檢查（C1 + Vuln 1 修復驗證）
# ===========================================================================


class TestDispatchServerStaticChecks:
    """ToukaDispatch/server.py 靜態代碼檢查。"""

    def test_no_force_agent_id_in_dispatch_server(self):
        """dispatch server 不得讀取 FORCE_AGENT_ID 環境變數（C1 修復）。"""
        server_file = DISPATCH_DIR / "server.py"
        assert server_file.exists(), "ToukaDispatch/server.py 必須存在"
        src = server_file.read_text(encoding="utf-8")
        assert 'get("FORCE_AGENT_ID"' not in src, (
            "C1 修復：dispatch server 不得有 os.environ.get('FORCE_AGENT_ID') 呼叫"
        )

    def test_dispatch_server_tools_have_no_agent_id_param(self):
        """三個操作工具（非 handshake）不得含 agent_id 參數（Vuln 1 修復）。"""
        server_file = DISPATCH_DIR / "server.py"
        tree = ast.parse(server_file.read_text(encoding="utf-8"))
        tool_names = {
            "dispatch_initialize_mission",
            "dispatch_update_task",
            "dispatch_get_board",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in tool_names:
                param_names = {a.arg for a in node.args.args}
                assert "agent_id" not in param_names, (
                    f"{node.name} must not have agent_id parameter (Vuln 1 fix): "
                    "callers cannot claim arbitrary identities"
                )

    def test_no_auto_capability_injection_in_dispatch_server(self):
        """dispatch server 不得有自動注入 can_supervise_dispatch 的邏輯（C1 修復）。"""
        server_file = DISPATCH_DIR / "server.py"
        src = server_file.read_text(encoding="utf-8")
        assert "capabilities.append" not in src, (
            "C1 修復：dispatch server 不得動態 append capabilities"
        )

    def test_dispatch_handshake_has_dispatch_token_param(self):
        """dispatch_handshake 必須含 dispatch_token 參數，確保認證不被繞過（ADR-0050）。"""
        server_file = DISPATCH_DIR / "server.py"
        tree = ast.parse(server_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_handshake":
                param_names = {a.arg for a in node.args.args}
                assert "dispatch_token" in param_names, (
                    "ADR-0050: dispatch_handshake MUST accept dispatch_token "
                    "to enforce parameter-level authentication"
                )


# ===========================================================================
# 2. Token 驗證測試（改用 session token API）
# ===========================================================================


class TestDispatchToolAuth:
    """dispatch tool 的 session token 驗證測試。"""

    @pytest.mark.asyncio
    async def test_invalid_session_token_returns_unauthorized(self, tmp_path, monkeypatch):
        """無效 session token 直接呼叫操作工具應回傳 Unauthorized。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "correct-secret-token")

        from ToukaDispatch import server as ds

        result = await ds.dispatch_get_board(session_token="invalid-session-token")
        assert "Unauthorized" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_empty_dispatch_token_env_rejects_handshake(self, tmp_path, monkeypatch):
        """DISPATCH_TOKEN 環境變數為空時，handshake 應拒絕所有請求。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "")

        from ToukaDispatch import server as ds

        result = await ds.dispatch_handshake(
            dispatch_token="any-attempt", caller_name="TestAgent"
        )
        assert "Unauthorized" in result or "Error" in result


# ===========================================================================
# 3. 操作工具功能測試（handshake → session token → 工具）
# ===========================================================================


async def _do_handshake(
    ds: object,
    caller_name: str = "TestTool",
    dispatch_token: str = "dispatch-test-token",
) -> str:
    """Helper：執行 handshake 並回傳 session token 字串（ADR-0050：傳入 dispatch_token）。"""
    result = await ds.dispatch_handshake(
        dispatch_token=dispatch_token, caller_name=caller_name
    )
    assert "Session token:" in result, f"Handshake failed: {result}"
    return result.split("Session token:")[-1].strip()


class TestDispatchToolOperations:
    """操作工具透過 session token 正常存取 DispatchAgent 看板。"""

    @pytest.mark.asyncio
    async def test_initialize_mission_with_session_token(self, tmp_path, monkeypatch):
        """有效 session token 應能初始化 DispatchAgent 看板。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-test-token")

        from memory_server import config_utils

        config_utils._reset_resource_proxy_cache()

        from ToukaDispatch import server as ds

        session_token = await _do_handshake(ds)
        result = await ds.dispatch_initialize_mission(
            session_token=session_token,
            goal="Authorized dispatch board init",
            tasks=[{"id": "t1", "description": "dispatch task one"}],
        )
        assert "Mission Initialized" in result

    @pytest.mark.asyncio
    async def test_get_board_returns_mission_after_init(self, tmp_path, monkeypatch):
        """初始化後 dispatch_get_board 應回傳看板內容。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-test-token")

        from memory_server import config_utils

        config_utils._reset_resource_proxy_cache()

        from ToukaDispatch import server as ds

        session_token = await _do_handshake(ds)

        await ds.dispatch_initialize_mission(
            session_token=session_token,
            goal="Board read test goal",
            tasks=[{"id": "t1", "description": "own task"}],
        )

        result = await ds.dispatch_get_board(session_token=session_token)
        assert "Board read test goal" in result

    @pytest.mark.asyncio
    async def test_update_task_blocked_without_blocker_detail(self, tmp_path, monkeypatch):
        """更新任務為 BLOCKED 時若無 blocker_detail 應回傳錯誤。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-test-token")

        from memory_server import config_utils

        config_utils._reset_resource_proxy_cache()

        from ToukaDispatch import server as ds

        session_token = await _do_handshake(ds)

        await ds.dispatch_initialize_mission(
            session_token=session_token,
            goal="BLOCKED test",
            tasks=[{"id": "t1", "description": "will be blocked"}],
        )

        result = await ds.dispatch_update_task(
            session_token=session_token,
            task_id="t1",
            status="BLOCKED",
            # blocker_detail 故意省略
        )
        assert "Error" in result or "blocker_detail" in result.lower()

    @pytest.mark.asyncio
    async def test_identity_is_always_dispatch_agent_not_caller(self, tmp_path, monkeypatch):
        """無論 handshake 的 caller_name 為何，看板存取路徑均為 DispatchAgent（Vuln 1 驗證）。"""
        monkeypatch.setenv("MEMORY_APP_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DISPATCH_TOKEN", "dispatch-test-token")

        from memory_server import config_utils

        config_utils._reset_resource_proxy_cache()

        from ToukaDispatch import server as ds

        # caller_name 宣稱為 DebugAgent（高權限），但看板應是 DispatchAgent 的
        session_token = await _do_handshake(ds, caller_name="DebugAgent")

        result = await ds.dispatch_initialize_mission(
            session_token=session_token,
            goal="Identity fixation test",
            tasks=[{"id": "t1", "description": "test task"}],
        )
        assert "Mission Initialized" in result

        # 驗證看板屬於 DispatchAgent（讀取到的 mission 來自 DispatchAgent 路徑）
        board = await ds.dispatch_get_board(session_token=session_token)
        assert "Identity fixation test" in board

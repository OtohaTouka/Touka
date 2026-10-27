"""Sovereign identity chain integration test."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.config import settings
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.services.tool_executor import ToolExecutor


@pytest.mark.asyncio
async def test_sovereign_identity_elevation_and_isolation():
    """驗證 Owner 權限提升與路徑隔離。

    當 Guest (Caller) 呼叫 Alice (Actor/Owner) 時，
    工具執行環境應自動提升至 Alice 的 Owner 權限，並隔離在其路徑下。
    """
    # 1. 準備模擬環境
    mock_pm = MagicMock()

    async def mock_owner_tool(identity: AgentIdentity = None, **_kwargs):
        """Mock tool that requires Owner permission."""
        return f"Access Granted for {identity.agent_id if identity else 'Unknown'}"

    mock_tool_reg = {
        "update_my_soul": mock_owner_tool,
        "vault_tools": AsyncMock(return_value="Vault OK"),
    }
    mock_mem_reg = {}
    mock_plugin_reg = {}

    # 2. 建立 ToolExecutor
    executor = ToolExecutor(
        mock_pm, mock_tool_reg, mock_mem_reg, mock_plugin_reg, enforce_advertised=False
    )

    def mock_identify(agent_id_str):
        if agent_id_str == "Alice":
            return {
                "agent_id": "Alice",
                "is_toukasoul": True,
                "is_custom": False,
            }
        return None

    # 3. 模擬請求情境
    caller_identity = AgentIdentity.for_role(
        agent_id="Alice", role="Owner",
        capabilities=["heartbeat_awakening"],
    )
    ctx = {
        "agent_id": "Alice",
        "identity": caller_identity,
        "is_toukasoul": True,
        "request_id": "req_123",
        # 本測試驗證的是身分鏈（identity 傳遞），非 HITL 閘門——H1 後 Manual 對
        # 非唯讀工具 fail-closed，故顯式用 YOLO 讓真實政策放行。
        "auth_level": "YOLO",
    }

    sandbox_dir = settings.MEMORY_APP_DATA_DIR

    with (
        patch.object(
            agent_service, "identify_agent", side_effect=mock_identify
        ),
        patch(
            "src.services.resource_manager.get_app_data_dir",
            return_value=sandbox_dir,
        ),
    ):
        # 執行 A: 驗證權限提升
        results = []
        async for event in executor.execute(
            "update_my_soul", {"persona": "new"}, "req_123", ctx
        ):
            results.append(event)

        # 斷言：執行成功 (因為 Alice 是 Owner)
        last_result = next(
            r for r in reversed(results) if r["type"] == "tool_result"
        )
        assert last_result["status"] == "success", (
            f"Owner tool blocked! Result: {last_result['result']}"
        )
        assert "Access Granted for Alice" in last_result["result"]

        # 執行 B: 驗證身分隔離 (vault_tools 應傳遞 Alice 的身分)
        vault_mock = mock_tool_reg["vault_tools"]
        vault_mock.reset_mock()

        async for _event in executor.execute(
            "vault_tools",
            {"vault": "personal", "action": "list"},
            "req_123",
            ctx,
        ):
            pass

        # 驗證 vault_tools 被呼叫且收到正確的 identity
        assert vault_mock.called, "vault_tools was not called"
        call_kwargs = vault_mock.call_args.kwargs
        passed_identity = call_kwargs.get("identity")
        assert passed_identity is not None, (
            f"Identity not passed to vault_tools: {call_kwargs}"
        )
        assert passed_identity.agent_id == "Alice", (
            f"Identity resolved incorrectly: {passed_identity.agent_id}"
        )

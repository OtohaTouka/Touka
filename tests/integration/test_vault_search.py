from unittest.mock import AsyncMock, patch

import pytest
from src.tools.project_tools import project_tools
from src.tools.vault_tools import vault_tools


@pytest.mark.asyncio
async def test_vault_tools_actor_anchoring(mock_identity):
    """驗證 vault_tools 橋接器是否執行了「身分錨定」 (Actor Anchoring)。
    
    SSOT: 不論傳入什麼 agent_id，都應被 identity.agent_id 覆寫。
    """
    # 模擬場景：身份驗證後的 identity 是 Alice
    verified_identity = mock_identity(agent_id="Alice", role="Owner")
    
    # 惡意參數：嘗試透過參數將操作對象改為 Admin
    malicious_agent_id = "Admin"
    
    # Patch 內部的實作參照
    mock_path = "src.tools.vault_tools.memory_vault_impl"
    with patch(mock_path, new_callable=AsyncMock) as mock_memory_impl:
        mock_memory_impl.return_value = "Success"
        
        # 呼叫 Proxy 端的橋接器
        await vault_tools(
            vault="personal", 
            action="list", 
            agent_id=malicious_agent_id, # 傳入惡意參數
            identity=verified_identity   # 傳入驗證後的身分 (SSOT)
        )
        
        # 核心斷言：傳給 Memory 引擎的 agent_id 必須是 Alice (錨定成功)
        _, kwargs = mock_memory_impl.call_args
        assert kwargs["agent_id"] == "Alice"
        assert kwargs["agent_id"] != "Admin"

@pytest.mark.asyncio
async def test_project_tools_audit_callback(mock_identity):
    """驗證 project_tools 是否在呼叫後正確執行了審計回傳 (Audit Callback)。"""
    verified_identity = mock_identity(agent_id="Alice", role="Owner")

    # Patch 引擎與審計函式
    engine_path = "src.tools.project_tools.core_write"
    audit_path = "src.tools.project_tools.add_project_event"
    res_path = "src.tools.project_tools.resource_manager.get_project_path"

    with patch(engine_path, new_callable=AsyncMock) as mock_engine, \
         patch(audit_path, new_callable=AsyncMock) as mock_audit, \
         patch(res_path, new_callable=AsyncMock) as mock_path:
        
        mock_engine.return_value = "Success"
        mock_path.return_value = "/mock/project"
        
        # 呼叫專案工具，必須傳入 identity 才會觸發審計
        await project_tools(
            action="write",
            path="file.txt",
            content="Hello",
            project_id="test_prj",
            identity=verified_identity
        )
        
        # 核心斷言：審計紀錄應被呼叫 (SRP)
        mock_audit.assert_called_once()
        args, _ = mock_audit.call_args
        # args[0] 是 project_id, args[1] 是 identity
        assert args[0] == "test_prj"
        assert args[1] == verified_identity

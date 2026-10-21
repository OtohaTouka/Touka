"""Touka 系統韌性測試.

驗證系統在依賴項 (如記憶系統) 損毀時的反應與降級機制。
"""

import importlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src import main


@pytest.fixture
def clean_system_state():
    """確保測試開始前系統處於未降級狀態 (SSOT)。"""
    main.SYSTEM_DEGRADED = False
    main.DEGRADED_REASON = ""
    yield
    main.SYSTEM_DEGRADED = False
    main.DEGRADED_REASON = ""


@pytest.mark.asyncio
async def test_memory_system_missing_degradation(clean_system_state):
    """驗證當找不到 memory_server 時，系統能進入降級模式。"""
    with patch("importlib.util.find_spec") as mock_find:
        mock_find.side_effect = (
            lambda n: None if "memory_server" in n else importlib.util.find_spec(n)
        )
        
        # 手動呼叫載入邏輯而非 reload 全量模組
        main.load_memory_system()

        assert main.SYSTEM_DEGRADED is True
        assert "Memory System module not found" in main.DEGRADED_REASON


@pytest.mark.asyncio
async def test_api_reports_degraded_state(clean_system_state):
    """驗證 API 根路徑能正確回報降級狀態。"""
    app = main.app
    with TestClient(app) as client:
        # 模擬進入降級模式
        main.SYSTEM_DEGRADED = True
        main.DEGRADED_REASON = "Manual Test Degradation"

        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "DEGRADED"
        assert "Manual Test" in data["degraded_reason"]


@pytest.mark.asyncio
async def test_memory_dependency_error_degradation(clean_system_state):
    """驗證當 memory_server 存在但內部依賴出錯時，系統能進入降級模式。"""
    # 這裡我們模擬 import_module 拋出錯誤
    with patch("importlib.import_module") as mock_import:
        mock_import.side_effect = ImportError("Missing dependency 'qdrant_client'")
        
        main.load_memory_system()

        assert main.SYSTEM_DEGRADED is True
        assert "Memory System dependency error" in main.DEGRADED_REASON

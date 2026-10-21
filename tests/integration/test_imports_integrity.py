import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_proxy_identity_router_import():
    """驗證 ToukaProxy 認證路由匯入，確保執行時期型別定義完整。"""
    try:
        from src.services.identity_router import get_verified_identity

        assert get_verified_identity is not None
    except NameError as e:
        pytest.fail(
            f"偵測到 NameError: {e}。可能是因為型別定義被放在 TYPE_CHECKING 內但執行時期需要使用。"
        )
    except ImportError as e:
        pytest.fail(f"偵測到 ImportError: {e}")


def test_heartbeat_awaken_import(monkeypatch: pytest.MonkeyPatch):
    """驗證 ToukaHeartbeat 啟動腳本匯入。"""

    # 模擬 Heartbeat 執行環境
    heartbeat_path = str(Path(__file__).parent.parent.parent / "ToukaHeartbeat")
    monkeypatch.syspath_prepend(heartbeat_path)

    # Mock 外部依賴以利於僅驗證內部匯入結構
    for module_name in (
        "apscheduler",
        "apscheduler.schedulers",
        "apscheduler.schedulers.asyncio",
        "apscheduler.triggers",
        "apscheduler.triggers.cron",
        "apscheduler.triggers.date",
        "apscheduler.triggers.interval",
        "fastapi",
        "fastapi.middleware",
        "fastapi.middleware.cors",
        "pydantic_settings",
    ):
        monkeypatch.setitem(sys.modules, module_name, MagicMock())
    monkeypatch.delitem(sys.modules, "awaken", raising=False)

    try:
        # 直接匯入 awaken 模組
        awaken = importlib.import_module("awaken")
        # 修正：awaken 模組目前使用 storage (StorageService) 進行配置管理
        assert awaken.storage is not None
    except ImportError as e:
        pytest.fail(f"偵測到 ImportError: {e}. 可能是因為使用了錯誤的相對匯入。")

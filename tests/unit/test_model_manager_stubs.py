"""測試 ModelManager 的動態模型發現方法.

驗證 fetch 方法正確委派至 fetcher 模組，並透過 _merge_models 合併結果。
驗證 check_models_health 正確更新 last_seen。
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from src.services.models.manager import ModelManager


def _make_manager(cache: list | None = None) -> ModelManager:
    """建立一個乾淨的 ModelManager (跳過初始化 IO)。"""
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = list(cache or [])
    mgr._blacklist = []
    return mgr


@pytest.mark.asyncio
async def test_fetch_latest_gemini_models_delegates_to_fetcher():
    """fetch_latest_gemini_models 應委派至 fetcher 並合併結果。"""
    mgr = _make_manager()
    mock_result = [{"id": "gemini-2.5-flash", "object": "model", "owned_by": "google"}]

    with patch(
        "src.services.models.manager.fetch_gemini_models",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_fetch:
        await mgr.fetch_latest_gemini_models()

    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1
    assert mgr._models_cache[0]["id"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_fetch_latest_openai_models_delegates_to_fetcher():
    """fetch_latest_openai_models 應委派至 fetcher 並合併結果。"""
    mgr = _make_manager()
    mock_result = [{"id": "gpt-4o", "object": "model", "owned_by": "openai"}]

    with patch(
        "src.services.models.manager.fetch_openai_models",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_fetch:
        await mgr.fetch_latest_openai_models()

    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1


@pytest.mark.asyncio
async def test_fetch_latest_grok_models_delegates_to_fetcher():
    """fetch_latest_grok_models 應委派至 fetcher 並合併結果。"""
    mgr = _make_manager()
    mock_result = [{"id": "grok-3", "object": "model", "owned_by": "xai"}]

    with patch(
        "src.services.models.manager.fetch_grok_models",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_fetch:
        await mgr.fetch_latest_grok_models()

    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1


@pytest.mark.asyncio
async def test_fetch_custom_connections_models_reads_config():
    """fetch_custom_connections_models 應讀取 connections.json 並逐一抓取。"""
    mgr = _make_manager()

    connections_data = {
        "connections": [
            {
                "id": "local-ollama",
                "url": "http://localhost:11434",
                "apiKey": "ollama",
                "provider": "ollama",
                "enabled": True,
            },
            {
                "id": "disabled-conn",
                "url": "http://disabled",
                "apiKey": "",
                "provider": "custom",
                "enabled": False,
            },
        ]
    }
    mock_compat_result = [
        {"id": "ollama/llama3", "object": "model", "owned_by": "ollama"}
    ]

    with patch(
        "src.services.models.manager.config_utils"
    ) as mock_cfg, patch(
        "src.services.models.manager.fetch_openai_compatible_models",
        new_callable=AsyncMock,
        return_value=mock_compat_result,
    ) as mock_fetch:
        mock_cfg.config_exists.return_value = True
        mock_cfg.read_config_json.return_value = connections_data
        await mgr.fetch_custom_connections_models()

    # 應只對 enabled 的連線發出請求
    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1
    assert mgr._models_cache[0]["id"] == "ollama/llama3"


@pytest.mark.asyncio
async def test_fetch_latest_anthropic_models_delegates_to_fetcher():
    """fetch_latest_anthropic_models 應委派至 fetcher 並合併結果。"""
    mgr = _make_manager()
    mock_result = [{"id": "claude-opus-4-7", "object": "model", "owned_by": "anthropic"}]

    with patch(
        "src.services.models.manager.fetch_anthropic_models",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_fetch:
        await mgr.fetch_latest_anthropic_models()

    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1
    assert mgr._models_cache[0]["id"] == "claude-opus-4-7"


_NATIVE_FETCHERS = [
    "src.services.models.manager.fetch_gemini_models",
    "src.services.models.manager.fetch_openai_models",
    "src.services.models.manager.fetch_grok_models",
    "src.services.models.manager.fetch_anthropic_models",
    "src.services.models.manager.fetch_deepseek_models",
]
_KEY_ATTRS = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
)


@pytest.mark.asyncio
async def test_refresh_calls_keyed_discoverable_fetchers(monkeypatch):
    """refresh 應對「金鑰已設定」的可發現供應商呼叫對應 fetcher（registry SSOT 驅動）。"""
    mgr = _make_manager()
    # 設定全部官方供應商金鑰 → 5 個 native fetcher 全部被呼叫
    for attr in ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setattr(f"src.config.{attr}", "test-key", raising=False)
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(patch(t, new_callable=AsyncMock, return_value=[]))
            for t in _NATIVE_FETCHERS
        ]
        mock_cfg = stack.enter_context(
            patch("src.services.models.manager.config_utils")
        )
        mock_cfg.config_exists.return_value = False
        await mgr.refresh_all_dynamic_models()
    for m in mocks:
        m.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_skips_providers_without_keys(monkeypatch):
    """無金鑰的供應商不應被呼叫（不對外發無謂請求）；連線發現仍照常執行。"""
    mgr = _make_manager()
    for attr in _KEY_ATTRS:
        monkeypatch.setattr(f"src.config.{attr}", None, raising=False)
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(patch(t, new_callable=AsyncMock, return_value=[]))
            for t in _NATIVE_FETCHERS
        ]
        # 連線發現須仍被呼叫一次
        conn = stack.enter_context(
            patch.object(mgr, "fetch_custom_connections_models", new_callable=AsyncMock)
        )
        await mgr.refresh_all_dynamic_models()
    for m in mocks:
        m.assert_not_awaited()
    conn.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_latest_deepseek_models_delegates_to_fetcher():
    """fetch_latest_deepseek_models 應委派至 fetcher 並合併結果。"""
    mgr = _make_manager()
    mock_result = [{"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"}]

    with patch(
        "src.services.models.manager.fetch_deepseek_models",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_fetch:
        await mgr.fetch_latest_deepseek_models()

    mock_fetch.assert_awaited_once()
    assert len(mgr._models_cache) == 1
    assert mgr._models_cache[0]["id"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_check_models_health_updates_last_seen():
    """check_models_health 應對健康模型更新 last_seen。"""
    cache = [
        {"id": "gemini-2.5-flash", "owned_by": "google", "last_seen": 100.0},
        {"id": "gpt-4o", "owned_by": "openai", "last_seen": 100.0},
    ]
    mgr = _make_manager(cache)

    # validate_model_health 回傳 (models, [True, False])
    with patch(
        "src.services.models.manager.validate_model_health",
        new_callable=AsyncMock,
        return_value=(cache, [True, False]),
    ):
        await mgr.check_models_health()

    # 健康的模型 last_seen 應被更新
    gemini = next(m for m in mgr._models_cache if m["id"] == "gemini-2.5-flash")
    gpt4o = next(m for m in mgr._models_cache if m["id"] == "gpt-4o")
    assert gemini["last_seen"] > 100.0
    assert gpt4o["last_seen"] == 100.0  # 不健康的不更新


@pytest.mark.asyncio
async def test_check_models_health_graceful_on_empty():
    """空快取時 check_models_health 應不爆炸。"""
    mgr = _make_manager([])
    await mgr.check_models_health()
    assert mgr._models_cache == []

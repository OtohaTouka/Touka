"""TDD：refresh_all_dynamic_models 須回傳新發現模型數量（Bug 2 修復）。

涵蓋：
- 找到 N 個新模型時回傳值必須是 N
- 全部 fetcher 回空陣列時回傳 0
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from src.services.models.manager import ModelManager  # noqa: E402

# 註冊表驅動的發現會「金鑰存在才呼叫」對應 fetcher，故測試需設金鑰並 mock 全部 5 個。
_NATIVE_FETCHER_TARGETS = [
    "src.services.models.manager.fetch_gemini_models",
    "src.services.models.manager.fetch_openai_models",
    "src.services.models.manager.fetch_grok_models",
    "src.services.models.manager.fetch_anthropic_models",
    "src.services.models.manager.fetch_deepseek_models",
]
_KEY_ATTRS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")


def _make_manager() -> ModelManager:
    """建立乾淨的 ModelManager，跳過初始化 IO。"""
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = []
    mgr._blacklist = []
    return mgr


def _enable_all_keys(monkeypatch) -> None:
    """設定所有官方供應商金鑰，使 registry 驅動的發現會呼叫對應 fetcher。"""
    for attr in _KEY_ATTRS:
        monkeypatch.setattr(f"src.config.{attr}", "test-key", raising=False)


@pytest.mark.asyncio
async def test_refresh_returns_new_count_of_discovered_models(monkeypatch):
    """OpenAI fetcher 回傳 1 個新模型時，refresh_all_dynamic_models 應回傳 1。"""
    mgr = _make_manager()
    _enable_all_keys(monkeypatch)
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(patch(t, new_callable=AsyncMock, return_value=[]))
            for t in _NATIVE_FETCHER_TARGETS
        ]
        mocks[1].return_value = [{"id": "gpt-5", "owned_by": "openai"}]
        mock_cfg = stack.enter_context(patch("src.services.models.manager.config_utils"))
        mock_cfg.config_exists.return_value = False
        mock_cfg.save_config_json.return_value = None

        count = await mgr.refresh_all_dynamic_models()

    assert count == 1, f"Expected 1 new model, got {count}"


@pytest.mark.asyncio
async def test_refresh_returns_zero_when_no_new_models(monkeypatch):
    """全部 fetcher 回空陣列時，refresh_all_dynamic_models 應回傳 0。"""
    mgr = _make_manager()
    _enable_all_keys(monkeypatch)
    with ExitStack() as stack:
        for t in _NATIVE_FETCHER_TARGETS:
            stack.enter_context(patch(t, new_callable=AsyncMock, return_value=[]))
        mock_cfg = stack.enter_context(patch("src.services.models.manager.config_utils"))
        mock_cfg.config_exists.return_value = False

        count = await mgr.refresh_all_dynamic_models()

    assert count == 0, f"Expected 0, got {count}"

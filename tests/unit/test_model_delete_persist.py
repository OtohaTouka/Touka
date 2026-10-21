"""TDD：delete_model 必須傳遞 _save_config 失敗（Bug 1 修復）。

涵蓋：
- _save_config 在 save_config_json 失敗時必須 re-raise
- delete_model 在磁碟寫入失敗時不得靜默回傳 True
- _merge_models 必須吸收 _save_config 失敗（fetch 流程不因磁碟問題崩潰）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from src.models.identity import AgentIdentity  # noqa: E402
from src.services.models.manager import ModelManager  # noqa: E402


def _make_manager(cache: list | None = None) -> ModelManager:
    """建立乾淨的 ModelManager，跳過初始化 IO。"""
    with patch.object(ModelManager, "_load_blacklist"), \
         patch.object(ModelManager, "load_initial_config"):
        mgr = ModelManager()
    mgr._models_cache = list(cache or [])
    mgr._blacklist = []
    return mgr


def test_save_config_raises_on_write_failure():
    """_save_config 在 save_config_json 失敗時必須 re-raise，不得靜默吞掉。"""
    mgr = _make_manager()
    with patch("src.services.models.manager.config_utils.save_config_json",
               side_effect=OSError("file locked")):
        with pytest.raises(OSError, match="file locked"):
            mgr._save_config()


def test_delete_model_raises_when_save_fails():
    """delete_model 若磁碟寫入失敗必須向上傳遞例外，不得靜默回傳 True。"""
    mgr = _make_manager(cache=[{"id": "m1", "name": "Model 1", "enabled": False}])
    identity = AgentIdentity.for_role(agent_id="m1", role="Owner", capabilities=[])
    with patch.object(mgr, "_save_config", side_effect=OSError("file locked")):
        with pytest.raises(OSError):
            mgr.delete_model(identity)


def test_merge_models_absorbs_save_failure():
    """_merge_models 對 _save_config 失敗只記錄警告，模型仍應留在記憶體中（不讓 fetch 崩潰）。"""
    mgr = _make_manager()
    with patch.object(mgr, "_save_config", side_effect=OSError("file locked")):
        mgr._merge_models([{"id": "gpt-5", "owned_by": "openai"}])
    assert any(m["id"] == "gpt-5" for m in mgr._models_cache), (
        "_merge_models 必須在 save 失敗後仍保留模型於記憶體"
    )

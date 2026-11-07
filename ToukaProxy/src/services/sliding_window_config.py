"""滑動視窗設定的讀取（從 sliding_window_service 拆出，ADR-0305）.

主線組法（mainline_service）也要讀前情上限，但不能匯入剪裁服務（會經摘要員繞回群聊派送）；
讀設定只靠 resource_manager。
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.models.sliding_window import SlidingWindowConfig
from src.services.resource_manager import resource_manager

logger = logging.getLogger(__name__)

_CONFIG_FILE_NAME = "sliding_window.json"
_DEFAULT_CONFIG = SlidingWindowConfig()


def load_sliding_window_config() -> SlidingWindowConfig:
    """從 sliding_window.json 載入設定，失效時回退預設值。"""
    try:
        base = resource_manager.get_base_path()
        config_path = Path(base) / "config" / _CONFIG_FILE_NAME
        data = resource_manager.read_json(config_path)
        if data:
            return SlidingWindowConfig.model_validate(data)
    except Exception:
        logger.debug("SlidingWindowService: config not found, using defaults")
    return _DEFAULT_CONFIG

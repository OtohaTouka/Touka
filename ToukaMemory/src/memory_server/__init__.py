"""Touka 記憶伺服器配置初始化 (Package Initialization)."""

import logging

from .config_utils import (
    _reset_resource_proxy_cache,
    bootstrap_env,
    get_app_data_dir,
    get_current_epoch,
    get_resource_proxy,
)

logger = logging.getLogger(__name__)

# --- 環境變數與路徑初始化 (SSOT Bootstrap) ---
bootstrap_env()

# 提供別名以相容內部舊有匯入命名
_get_app_data_dir = get_app_data_dir

__all__ = [
    "get_app_data_dir",
    "_get_app_data_dir",
    "get_current_epoch",
    "bootstrap_env",
    "get_resource_proxy",
    "_reset_resource_proxy_cache",
]

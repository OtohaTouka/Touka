"""Touka 記憶伺服器配置工具集 (SSOT Configuration Utilities).

本模組為 ToukaMemory 的內部事實來源，負責處理環境變數解析與路徑尋址，
確保模組在獨立運行時具備自主的引導能力，同時符合全域靜態檢查規範。
"""

import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
DOCKER_APP_DATA_DIR = "/app/data"


def bootstrap_env() -> None:
    """優先載入顯式環境來源；未指定時保留既有向上搜尋。"""
    explicit = os.environ.get("TOUKA_ENV_FILE")
    if explicit:
        load_dotenv(dotenv_path=Path(explicit))
        return

    # 1. 優先嘗試載入當前工作目錄的 .env
    load_dotenv()

    # 2. 如果沒有關鍵變數，則向上搜尋 .env (最多三層)
    if not os.environ.get("MEMORY_APP_DATA_DIR"):
        current = Path.cwd().absolute()
        for _ in range(3):
            env_file = current / ".env"
            if env_file.exists():
                load_dotenv(dotenv_path=env_file)
                if os.environ.get("MEMORY_APP_DATA_DIR"):
                    logger.info("SSOT: Loaded environment from %s", env_file)
                    break
            current = current.parent


def get_app_data_dir() -> str:
    """獲取應用程式資料存儲根目錄 (App Data Directory)."""
    # 確保環境變數已載入
    if not os.environ.get("MEMORY_APP_DATA_DIR"):
        bootstrap_env()

    configured_dir = Path(os.path.normpath(os.environ.get("MEMORY_APP_DATA_DIR", "data")))
    if Path("/.dockerenv").exists() and not configured_dir.is_absolute():
        return DOCKER_APP_DATA_DIR

    return str(configured_dir)


def get_workspace_root() -> str:
    """獲取工作區根目錄。"""
    if not os.environ.get("WORKSPACE_ROOT"):
        bootstrap_env()

    # 優先使用環境變數
    root = os.environ.get("WORKSPACE_ROOT") or os.environ.get("HOST_WORKSPACE_ROOT")
    if root:
        return root

    # 備援：動態探測專案根目錄 (基於專案結構：Touka/ToukaMemory/src/memory_server/config_utils.py)
    return str(Path(__file__).parents[3].resolve())


def get_embedding_model_name() -> str:
    """Return the SentenceTransformer model name to use for embeddings (SSOT).

    Reads EMBEDDING_MODEL env var; falls back to the English-optimised default.
    Set EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2 for CJK workloads.
    """
    return os.environ.get("EMBEDDING_MODEL", "").strip() or "all-MiniLM-L6-v2"


def get_current_epoch() -> float:
    """獲取目前的 UNIX Epoch Time (SSOT).

    在整合模式下優先使用 Proxy 的中心化時間工具，否則降級至原生 time.time()。
    """
    try:
        from src.utils.time_utils import get_current_epoch as get_central_epoch

        return get_central_epoch()
    except (ImportError, ModuleNotFoundError):
        return time.time()


# ---------------------------------------------------------------------------
# Resource Proxy：依賴反轉的 I/O 抽象層 (SSOT)
# ---------------------------------------------------------------------------
# ADR-0116：ToukaMemory 收斂為「整合模式單一部署」，退役獨立 MCP 模式。
# 故移除原本的 _FallbackResourceProxy（純 pathlib/os 降級代理，且其 get_vault_path→None、
# is_protected_path→False 等同關閉身分隔離與路徑保護）。get_resource_proxy() 只回傳
# ToukaProxy 的真實 ResourceManager；取不到即 fail-loud（整合模式下此匯入必成功）。
# 依賴反轉接縫（protocols.IResourceProxy 介面、本工廠、各呼叫端）刻意保留，維持解耦與可測試性。

_RESOURCE_PROXY_CACHE: Any = None


def get_resource_proxy() -> Any:
    """獲取資源代理 (SSOT)：ToukaProxy 的 ResourceManager（完整 I/O＋身分隔離＋路徑保護）。

    ADR-0116：整合模式單一部署，不再有獨立 MCP 降級代理。取不到 ResourceManager 即
    fail-loud（拋 ImportError），杜絕靜默降級至無保護的 I/O。
    """
    global _RESOURCE_PROXY_CACHE
    if _RESOURCE_PROXY_CACHE is None:
        from src.services.resource_manager import resource_manager

        _RESOURCE_PROXY_CACHE = resource_manager
    return _RESOURCE_PROXY_CACHE


def _reset_resource_proxy_cache() -> None:
    """重置資源代理快取（僅供測試環境使用）。"""
    global _RESOURCE_PROXY_CACHE
    _RESOURCE_PROXY_CACHE = None

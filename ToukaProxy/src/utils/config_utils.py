"""設定檔讀寫工具 (Config Warden)。

負責 ToukaProxy 中所有 JSON 設定檔的持久化管理。
提供對 mcp_config.json 與 skill_market.json 的專屬支援。
"""

import json
import logging
from typing import Any

from src.config import CONFIG_DIR

logger = logging.getLogger(__name__)
MCP_CONFIG_FILENAME = "server_config.json"


def config_exists(filename: str) -> bool:
    """檢查設定檔是否存在。"""
    path = CONFIG_DIR / filename
    return path.exists()


def read_config_json(filename: str) -> dict[str, Any] | None:
    """讀取 JSON 設定檔。"""
    path = CONFIG_DIR / filename
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception(f"Error reading config {filename}")
        return None


def read_config_json_with_fallback(filename: str) -> dict[str, Any] | None:
    """讀取 JSON 設定檔，並具備 UTF-16 回退機制。"""
    path = CONFIG_DIR / filename
    if not path.exists():
        return None

    try:
        # 優先嘗試 UTF-8
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            # 回退到 UTF-16
            with path.open(encoding="utf-16") as f:
                return json.load(f)
        except Exception:
            logger.exception(f"Failed to read config {filename} with fallback")
            return None


def config_file_exists(filename: str) -> bool:
    """設定檔在不在（分得出「還沒有」與「讀不出來」）。"""
    return (CONFIG_DIR / filename).exists()


def save_config_json(filename: str, data: dict[str, Any]) -> None:
    """儲存 JSON 設定檔，並自動建立目錄。"""
    path = CONFIG_DIR / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception:
        logger.exception(f"Error saving config {filename}")
        raise


def read_text_config(filename: str) -> str | None:
    """讀取純文字設定檔。"""
    path = CONFIG_DIR / filename
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.exception(f"Error reading text config {filename}")
        return None


def write_text_config(filename: str, content: str) -> None:
    """寫入純文字設定檔。"""
    path = CONFIG_DIR / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception(f"Error writing text config {filename}")
        raise

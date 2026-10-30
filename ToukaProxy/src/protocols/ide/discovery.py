"""Gemini CLI Companion 相容的 ToukaIDE MCP discovery。

POSIX 上連線資訊檔在大家共用的暫存區（ADR-0294）：``gemini``、``ide`` 兩層
資料夾與檔案都要是自己的、別人不能寫，而且不是連結，不符合就略過並記一行警告，
免得別人放一個假的把 Touka 導去別處。檔案以不跟隨連結的方式開啟（遇到 FIFO 不會
卡住），檢查的是開好的那一個檔案，沒有「先查再開」的時間差；上層暫存資料夾就算
沒有 sticky 位元、``gemini`` 被換掉，開到的檔案擁有者也不會是自己。
Windows 的暫存區每人一份，照舊。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from memory_server.utils.no_follow import open_read_no_follow

logger = logging.getLogger(__name__)

_TOUKA_UI_NAME = "ToukaUI"
_DEFAULT_MODERN_ENDPOINT = "/mcp"
_MODERN_PROTOCOL_MODE = "2026-07-28"
_GROUP_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH


def _current_uid() -> int | None:
    """POSIX 回目前使用者；Windows 回 None（暫存區每人一份，不檢查擁有者）。"""
    return os.getuid() if hasattr(os, "getuid") else None


def _default_ide_dir() -> Path:
    # 先解析掉暫存區路徑上的連結（macOS 的 /var 是連結），之後每一段都不跟隨
    return Path(tempfile.gettempdir()).resolve() / "gemini" / "ide"


def _is_private(info: os.stat_result, uid: int, *, directory: bool) -> bool:
    """是自己的、別人不能寫，而且是資料夾（或一般檔案）；連結不算。"""
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return kind(info.st_mode) and info.st_uid == uid and not info.st_mode & _GROUP_OTHER_WRITE


def _private_folders(ide_dir: Path, uid: int) -> bool:
    for folder in (ide_dir.parent, ide_dir):
        try:
            info = os.lstat(folder)
        except FileNotFoundError:
            return False
        except OSError as exc:
            # 例如別人先建了只有他能進的 gemini：略過，不讓搜尋 IDE 失敗
            logger.warning(f"略過 IDE 連線資訊資料夾 {folder}：{exc}")
            return False
        if not _is_private(info, uid, directory=True):
            logger.warning(f"略過 IDE 連線資訊資料夾 {folder}：是連結、不是自己的，或別人能寫")
            return False
    return True


def _read_discovery(path: Path, uid: int | None) -> Any:
    if uid is None:
        return json.loads(path.read_text(encoding="utf-8"))
    handle, info = open_read_no_follow(path)
    with handle:
        if not _is_private(info, uid, directory=False):
            logger.warning(f"略過 IDE 連線資訊檔 {path}：不是自己的，或別人能寫")
            return None
        return json.loads(handle.read().decode("utf-8"))


def _local_endpoint(value: Any, default: str) -> str | None:
    """只接受 loopback base URL 下的絕對 path，不接受外部 URL。"""
    endpoint = default if value is None else value
    if not isinstance(endpoint, str):
        return None
    parsed = urlsplit(endpoint)
    if (
        not endpoint.startswith("/")
        or endpoint.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        return None
    return endpoint


async def discover_ide_servers(
    ide_base_dir: Path | None = None,
) -> dict[str, Any]:
    """掃描 ToukaIDE discovery file，只回傳 modern endpoint。"""
    search_dir = Path(ide_base_dir) if ide_base_dir is not None else _default_ide_dir()
    uid = _current_uid()
    if uid is not None:
        if not _private_folders(search_dir, uid):
            return {}
    elif not search_dir.exists():
        return {}

    ide_clients: dict[str, Any] = {}
    for discovery_file in search_dir.glob("gemini-ide-server-*.json"):
        try:
            data = _read_discovery(discovery_file, uid)
        except Exception as exc:
            logger.warning(f"解析 IDE 發現檔案 {discovery_file} 失敗: {exc}")
            continue
        if data is None:
            continue

        server_name = data.get("ideInfo", {}).get("name", "")
        port = data.get("port")
        token = data.get("authToken")
        if (
            server_name != _TOUKA_UI_NAME
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or not isinstance(token, str)
            or not token
        ):
            continue

        transport = data.get("transport", {})
        if not isinstance(transport, dict):
            continue
        transport_type = transport.get("type", "streamable-http")
        protocol_mode = transport.get("protocolMode", _MODERN_PROTOCOL_MODE)
        if transport_type != "streamable-http" or protocol_mode != _MODERN_PROTOCOL_MODE:
            continue

        modern_endpoint = _local_endpoint(
            transport.get("endpoint"),
            _DEFAULT_MODERN_ENDPOINT,
        )
        if modern_endpoint is None:
            continue

        base_url = f"http://localhost:{port}"
        ide_clients[_TOUKA_UI_NAME] = {
            "url": f"{base_url}{modern_endpoint}",
            "token": token,
            "protocol_mode": protocol_mode,
        }
        logger.info(f"✅ 已發現 ToukaUI IDE Server 運行於通訊埠 {port}")

    return ide_clients

"""Gemini CLI Companion 相容的 ToukaIDE MCP discovery。"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_TOUKA_UI_NAME = "ToukaUI"
_DEFAULT_IDE_DIR = Path(tempfile.gettempdir()) / "gemini" / "ide"
_DEFAULT_MODERN_ENDPOINT = "/mcp"
_MODERN_PROTOCOL_MODE = "2026-07-28"


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
    search_dir = ide_base_dir if ide_base_dir is not None else _DEFAULT_IDE_DIR
    if not Path(search_dir).exists():
        return {}

    ide_clients: dict[str, Any] = {}
    for discovery_file in Path(search_dir).glob("gemini-ide-server-*.json"):
        try:
            data = json.loads(discovery_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"解析 IDE 發現檔案 {discovery_file} 失敗: {exc}")
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

"""封鎖埠 B 的組成（iso-c2 C2-2；monitor 計算、見 ``status.IsolationMonitor.block_ports``）.

- **核心 B**（一定算得出來、數量很小）：``profiles.blocked_ports`` 的固定埠與 Touka 自己的埠
  （SearXNG 只在 proxy 位置），加上 Settings 裡主機是非公網的上游 URL 的埠。
- **上游埠**：``connections.json`` 的全部條目（不看 enabled、不看探測結果）、MCP registry
  （``server_config.json``）全部條目的遠端 url，加上記憶體裡的來源（路由快照，接線時注入 monitor），
  monitor 只增不減地累積。registry 是額外來源：讀不到時沒有它的埠，不改層級（記憶體裡的 registry
  只有啟用的條目，所以讀檔）。
- 「非公網」：IP 用 ``not is_global``（也認 ``127.1`` 這類 inet_aton 寫法）；主機名是 localhost、
  ``*.localhost``、``*.internal``、``*.local``、``*.home.arpa`` 或單段名稱。不查 DNS。
- 80、443、53 不加（B 只看埠號，加了就不能上網）：由呼叫端記告警，列為已知限制。

``connections.json`` 含 API 金鑰：只在這裡讀、只取每條的 ``url`` 換成埠；不套 pydantic 模型、
例外只留型別名稱、不在 ``except`` 裡再丟例外，原文、dict 與 url 都不保留。
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from src.config import settings
from src.sandbox_kernel.spec import MAX_PORT
from src.services.isolation.profiles import blocked_ports
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from src.services.isolation.profiles import Location

CONNECTIONS_FILE = "connections.json"
MCP_REGISTRY_FILE = "server_config.json"
# Settings 裡指向上游的 URL（主機是非公網時取埠）；SEARXNG_BASE_URL（只進 proxy 的 B）、
# UI_DEFAULT_URL 與 HEARTBEAT_EXTERNAL_URL（Touka 自己的埠）由 ``profiles.blocked_ports`` 處理
SETTINGS_UPSTREAM_URLS = (
    "OPENAI_BASE_URL",
    "OLLAMA_BASE_URL",
    "LITELLM_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "GEMINI_BASE_URL",
    "TOOL_BASE_URL",
    "TOUKA_MCP_OAUTH_CALLBACK_BASE_URL",
)
UNBLOCKABLE_PORTS = frozenset({53, 80, 443})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_NON_PUBLIC_SUFFIXES = (".localhost", ".internal", ".local", ".home.arpa")
INVALID_STRUCTURE = "invalid_structure"


@dataclass(frozen=True)
class ConnectionsRead:
    """``connections.json`` 讀一次的結果：成功時只有埠；失敗時只有例外的型別名稱。"""

    ok: bool
    ports: frozenset[int] = frozenset()
    error: str | None = None


def _ip(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        pass
    try:  # 127.1、2130706433、0x7f.1：連線時也會被當成 IPv4
        return ipaddress.IPv4Address(socket.inet_aton(name))
    except OSError:
        return None


def is_non_public_host(host: str) -> bool:
    """主機是不是非公網（本機、私有網段、容器內部名稱）；只看字串，不查 DNS。"""
    name = host.strip().rstrip(".").lower()
    address = _ip(name)
    if address is not None:
        return not address.is_global
    return "." not in name or name == "localhost" or name.endswith(_NON_PUBLIC_SUFFIXES)


def upstream_port(url: str) -> int | None:
    """URL 的主機是非公網時回它的埠（沒寫就用 scheme 的預設）；其他情況回 None。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or not is_non_public_host(parts.hostname):
        return None
    port = port or _DEFAULT_PORTS.get(parts.scheme.lower())
    return port if port is not None and 1 <= port <= MAX_PORT else None


def core_block_ports(
    location: Location, on_unblockable: Callable[[int], None] | None = None
) -> tuple[int, ...]:
    """核心 B（排序）：固定埠、Touka 自己的埠，加上 Settings 裡非公網上游的埠.

    80／443／53 不加，交給 ``on_unblockable`` 記告警。
    """
    ports = set(blocked_ports(location))
    for name in SETTINGS_UPSTREAM_URLS:
        port = upstream_port(str(getattr(settings, name) or ""))
        if port in UNBLOCKABLE_PORTS and on_unblockable is not None:
            on_unblockable(port)
        elif port is not None and port not in UNBLOCKABLE_PORTS:
            ports.add(port)
    return tuple(sorted(ports))


def _connection_ports(document: object) -> frozenset[int] | None:
    """結構：頂層 dict、``connections`` 是 list、每條是 dict 而且 url 是非空字串；不對回 None。"""
    items = document.get("connections") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return None
    ports: set[int] = set()
    for item in items:
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or not url.strip():
            return None
        port = upstream_port(url)
        if port is not None:
            ports.add(port)
    return frozenset(ports)


def _registry_ports(document: object) -> frozenset[int] | None:
    """結構：頂層 dict、``mcpServers``（沒有就是空的）是 dict、每條是 dict；只取字串的 ``url``。"""
    servers = document.get("mcpServers", {}) if isinstance(document, dict) else None
    if not isinstance(servers, dict) or not all(isinstance(s, dict) for s in servers.values()):
        return None
    urls = (server.get("url") for server in servers.values())
    ports = (upstream_port(url) for url in urls if isinstance(url, str))
    return frozenset(port for port in ports if port is not None)


def _read_ports(
    path: Path, extract: Callable[[object], frozenset[int] | None]
) -> ConnectionsRead:
    """讀設定檔（會碰磁碟：在執行緒裡呼叫）；不拋例外.

    只有 ``exists`` 正常回 False 才算「沒有」；``exists``、讀檔、解析的任何例外與結構不對都是
    讀不到。
    """
    try:
        if not resource_manager.exists(path):
            return ConnectionsRead(ok=True)
        document = json.loads(resource_manager.read_file_text(path))
    except Exception as exc:  # 只留型別名稱：JSONDecodeError.doc 是整份原文（含金鑰）
        return ConnectionsRead(ok=False, error=type(exc).__name__)
    ports = extract(document)
    if ports is None:
        return ConnectionsRead(ok=False, error=INVALID_STRUCTURE)
    return ConnectionsRead(ok=True, ports=ports)


def read_connection_ports(path: Path) -> ConnectionsRead:
    """``connections.json`` 全部條目的上游埠（讀不到時 monitor 判 ``block_ports_incomplete``）."""
    return _read_ports(path, _connection_ports)


def read_registry_ports(path: Path) -> ConnectionsRead:
    """MCP registry（``server_config.json``）全部條目（不看 enabled）的遠端 url 埠."""
    return _read_ports(path, _registry_ports)

"""自動化測試的外部網路封鎖（ADR-0214 第二層保險）；純標準庫、不依賴 ``src``。

pytest conftest 與 standalone smoke sandbox 共用。產品程式常把例外吞成文字，所以除了
raise 也會記錄違規，由呼叫端 ``drain_violations()`` 取出後讓測試失敗。
殘留：子程序不受 audit hook 約束；Windows Proactor 上以 IP 字面值發起的 async 連線
不觸發任何 socket 稽核事件。
"""

from __future__ import annotations

import ipaddress
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

# CPython 的 gethostbyname_ex 也發 "socket.gethostbyname" 事件。
_HOST_EVENTS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"})
_ADDRESS_EVENTS = frozenset({"socket.connect", "socket.sendto"})
_LOCAL_SUFFIX = ".localhost"

_state_lock = threading.Lock()
_violations: list[str] = []
_installed = False


class ExternalNetworkBlocked(RuntimeError):
    """自動化測試企圖解析或連線到本機以外的位址。"""

    def __init__(self, event: str, destination: object) -> None:
        super().__init__(f"automated tests must not reach the network: {event} -> {destination!r}")


def is_local_destination(host: object) -> bool:
    """本機＝``None``／空字串、localhost、``*.localhost``、loopback、unspecified；其他皆外部。"""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    if not host:
        return True
    name = host.casefold().removesuffix(".")
    if name == "localhost" or (name.endswith(_LOCAL_SUFFIX) and len(name) > len(_LOCAL_SUFFIX)):
        return True
    try:
        address = ipaddress.ip_address(name.removeprefix("[").removesuffix("]").split("%")[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def is_local_socket_address(address: object) -> bool:
    """connect／sendto 位址：IP tuple 看 host；``str``／``bytes`` 是 AF_UNIX 路徑（本機 IPC）。"""
    if isinstance(address, tuple):
        return bool(address) and is_local_destination(address[0])
    return isinstance(address, (str, bytes, bytearray))


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not event.startswith("socket."):
        return
    if event in _HOST_EVENTS:
        destination = args[0] if args else None
        allowed = is_local_destination(destination)
    elif event in _ADDRESS_EVENTS:
        destination = args[1] if len(args) > 1 else None
        allowed = is_local_socket_address(destination)
    else:
        return
    if not allowed:
        entry = f"{event} -> {destination!r} (thread={threading.current_thread().name})"
        with _state_lock:
            _violations.append(entry)
        raise ExternalNetworkBlocked(event, destination)


def install() -> bool:
    """掛上 audit hook；可重複呼叫，只有第一次真的掛（回傳本次是否新掛）。hook 無法移除。"""
    global _installed
    with _state_lock:
        if _installed:
            return False
        sys.addaudithook(_audit_hook)
        _installed = True
        return True


def is_installed() -> bool:
    """回傳本行程是否已掛上封鎖。"""
    return _installed


def clear_proxy_environment(environ: MutableMapping[str, str]) -> None:
    """清掉大小寫代理變數並設 NO_PROXY=*：經本機轉發代理的連線只連 127.0.0.1，會繞過 hook。

    NO_PROXY 非空時 urllib 的 getproxies() 不再讀 Windows 登錄檔的系統代理（httpx 沿用）。
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environ.pop(name, None)
        environ.pop(name.lower(), None)
    environ["NO_PROXY"] = environ["no_proxy"] = "*"


def drain_violations() -> list[str]:
    """取出並清空已記錄的違規（含其他執行緒裡發生的）。"""
    with _state_lock:
        drained = list(_violations)
        _violations.clear()
    return drained


def format_violations(violations: list[str]) -> str:
    """把違規排成測試失敗訊息，列出每個被擋的目的地。"""
    lines = "\n".join(f"  - {item}" for item in violations)
    return f"自動化測試企圖連到外部網路，已在送出前擋下（請改用 mock）：\n{lines}"

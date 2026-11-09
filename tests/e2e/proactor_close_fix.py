"""讓 Windows 上被對方重置的連線照樣收尾，實際驗證的後端專用（ADR-0295）.

Python 3.12 的 Windows 事件迴圈關連線時先呼叫 ``sock.shutdown()``。對方已經重置時它會丟例外，
排在後面的「關 socket」與「告訴 ``asyncio.Server`` 這條連線結束了」都被跳過，``Server`` 就一直
以為連線還在。uvicorn 關機時要等 ``Server.wait_closed()``，而且沒有上限，後端永遠停不下來，
連應用程式的關閉流程都走不到。

這裡只讓 ``shutdown`` 的網路錯誤不再中斷收尾，後面的步驟仍由標準函式庫自己做。
沒關的連線照舊要等，真的卡住照舊由測試工具的逾時判失敗。Linux 不會用到這個類別。
"""

from __future__ import annotations

import asyncio.proactor_events
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)
_Transport = asyncio.proactor_events._ProactorBasePipeTransport


class _KeepClosingSocket:
    """只攔 ``shutdown`` 的網路錯誤，其他都交給真的 socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)

    def shutdown(self, how: int) -> None:
        try:
            self._sock.shutdown(how)
        except OSError as error:
            logger.warning("finished closing a connection the peer had already reset: %r", error)


def install() -> None:
    """把連線收尾換成 ``shutdown`` 失敗也會做完的版本；每個行程呼叫一次."""
    original = _Transport._call_connection_lost

    def call_connection_lost(self: Any, exc: BaseException | None) -> None:
        # 第二次呼叫時已經是 None；子行程的管線沒有 shutdown，標準函式庫靠 hasattr 分辨
        if isinstance(self._sock, socket.socket):
            self._sock = _KeepClosingSocket(self._sock)
        original(self, exc)

    _Transport._call_connection_lost = call_connection_lost

"""送完就刪的暫存檔回應（下載專案的 zip，ADR-0332 PR B）."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import FileResponse

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)


class TempFileResponse(FileResponse):
    """送出暫存檔，之後一律刪掉：送到一半斷線、Range 不合格的回應也一樣.

    Starlette 的 ``FileResponse`` 只在正常送完後跑 ``background``；送到一半斷線
    （``ClientDisconnect``）或 Range 不合格時直接結束，暫存檔就留在磁碟上。刪檔函式由呼叫端給
    （ResourceManager）。
    """

    def __init__(
        self, path: str | Path, *, remove: Callable[[str | Path], None], **kwargs: Any
    ) -> None:
        """``remove`` 收暫存檔路徑；其餘參數同 ``FileResponse``."""
        super().__init__(path, **kwargs)
        self._remove = remove

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """送出回應；不論怎麼結束都刪掉暫存檔."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self._remove(self.path)
            except OSError:
                logger.warning("Temporary download file was not removed", exc_info=True)

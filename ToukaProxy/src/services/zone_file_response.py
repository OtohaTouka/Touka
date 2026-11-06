"""``/v1/outputs`` 的回檔：先以不跟隨連結的方式開好或檢查好檔案，再交給 Starlette（ADR-0293 R7）.

- Linux：``ZoneFileResponse`` 持有開好的檔案，路徑是 ``/proc/self/fd/N``。Starlette 依這個字串重開
  的是同一個 inode，換掉連結也換不到別的檔案。建立時以 ``PROC_FD`` 登記（唯一的登記點），關檔時
  取消；回應物件沒被呼叫就被丟掉時，``weakref.finalize`` 也會關檔並取消登記。
- 沒有 ``/proc`` 的系統（Windows 本機）：``PathZoneFileResponse`` 在 ``__call__`` 裡才進入
  ``zone_handoff``，回應送完才離開；Starlette 依路徑重新開檔，檢查與開檔之間有時間差（R7 唯一依字串
  重新開檔的例外，寫在已知界線）。
"""

from __future__ import annotations

import errno
import sys
import weakref
from mimetypes import guess_type
from typing import IO, TYPE_CHECKING

from memory_server.utils.zone_paths import HandoffKind, _register_handoff, _release_handoff
from starlette.responses import FileResponse

from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    import os

    from memory_server.utils.zone_paths import ZonePath, _HandoffToken
    from starlette.types import Receive, Scope, Send

_PROC_FD = sys.platform.startswith("linux")


def _close(handle: IO[bytes], token: _HandoffToken) -> None:
    try:
        handle.close()
    finally:
        _release_handoff(token)


class ZoneFileResponse(FileResponse):
    """Linux：送出已開好的檔案（``/proc/self/fd/N``）；送完、出錯、中斷或被丟掉時關檔."""

    def __init__(self, handle: IO[bytes], info: os.stat_result, media_type: str) -> None:
        """``info`` 是 ``handle`` 的 fstat；ETag、Last-Modified 與 Range 由 Starlette 照它算."""
        path = f"/proc/self/fd/{handle.fileno()}"
        super().__init__(path, stat_result=info, media_type=media_type)
        token = _register_handoff(path, HandoffKind.PROC_FD)
        # 檔案在回應結束前一直開著，fd 號碼不會被別的請求重用
        self._finalizer = weakref.finalize(self, _close, handle, token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """送出回應；400、416 與送到一半中斷都會關檔."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finalizer()


class PathZoneFileResponse(FileResponse):
    """沒有 ``/proc`` 的系統：送出時才把區內路徑交給 Starlette 依路徑開檔."""

    def __init__(self, zp: ZonePath, media_type: str) -> None:
        """路徑先放 ``display()``，``__call__`` 裡才換成交出去的字串."""
        super().__init__(zp.display(), media_type=media_type)
        self._zone_path = zp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """在 ``zone_handoff`` 裡送出（G1 明列）；送完才取消登記."""
        kind = HandoffKind.WINDOWS_FILE_RESPONSE
        with resource_manager.zone_handoff(self._zone_path, kind) as path:
            self.path = path
            await super().__call__(scope, receive, send)


def zone_file_response(zp: ZonePath, filename: str) -> FileResponse:
    """區內一般檔案的回應（media type 照原檔名算）；連結、不存在或不是檔案丟 ``OSError``."""
    media_type = guess_type(filename)[0] or "application/octet-stream"
    if not _PROC_FD:
        if resource_manager.zone_kind(zp) != "file":
            raise FileNotFoundError(errno.ENOENT, "not a regular file", zp.display())
        return PathZoneFileResponse(zp, media_type)
    handle, info = resource_manager.zone_open_read(zp)
    return ZoneFileResponse(handle, info, media_type)

"""開放名單回報給 Touka 的時機（ADR-0330）。

規則只放在這裡，``service.py`` 只負責接線。只在輪詢的執行緒用，不帶鎖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from touka_local.fsops import Clock

# 暫停中可能因為隨身碟拔掉而變，所以名單沒變也每隔這麼久送一次；送失敗也等這麼久再試
FOLDER_REPORT_SECONDS: Final = 30.0


class FolderReporter:
    """名單變了、這次連線還沒送過、或隔了一陣子才送；送不出去時隔一陣子再試，不是每一輪都送。"""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        # 這次連線最後送成功的（修改次數, 是否被看見）與時間；None 表示這次連線還沒送成功過
        self._sent: tuple[int, bool] | None = None
        self._sent_at = 0.0
        self._failed_at: float | None = None

    def due(self, revision: int, *, exposed: bool) -> bool:
        """這一輪要不要送。"""
        now = self._clock()
        if self._failed_at is not None and now - self._failed_at < FOLDER_REPORT_SECONDS:
            return False
        if self._sent != (revision, exposed):
            return True
        return now - self._sent_at >= FOLDER_REPORT_SECONDS

    def record(self, revision: int, *, exposed: bool, ok: bool) -> None:
        """記下這次送的結果。"""
        now = self._clock()
        if ok:
            self._sent = (revision, exposed)
            self._sent_at = now
            self._failed_at = None
        else:
            self._failed_at = now

    def connection_lost(self) -> None:
        """連線斷了或換了配對：重新連上時馬上再送。"""
        self._sent = None
        self._failed_at = None

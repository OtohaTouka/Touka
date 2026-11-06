"""這次執行有沒有被停止（ADR-0292）：hook 跑完之後、開始串流之前用.

hook 可能跑上幾十秒；這段時間使用者按了停止、接著又送出下一則，下一則會清掉對話的中斷旗標
（``MonitorLayer``、心跳每次開始時都會清）。所以這裡不只看旗標，也看這次執行自己的記號：

- ``ui_pending``：這一則 UI 請求還沒開始串流時被停止（停止端點標在這一則請求上）。
- ``stop_baseline``：這次執行開始時記下的停止次數（``rhythm_service.stop_count``）；之後次數
  變多就是這次執行期間被停止過。
- 都沒有記下時（例如除錯執行）hook 的檢查點只看對話旗標。

開始串流之前（dispatcher）只看這次執行自己的記號（``stopped_since_start``）：旗標還在時由
ReAct 迴圈自己在第一輪前結束（回「已中斷」），不在這裡改成 409。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from src.services.rhythm_service import rhythm_service
from src.services.stream_buffer_service import PendingRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

STOP_BASELINE_KEY: Final[str] = "stop_baseline"


def stop_baseline(session_id: str) -> dict[str, int]:
    """執行開始時放進 ctx 的起點（清掉中斷旗標之後記）。"""
    return {STOP_BASELINE_KEY: rhythm_service.stop_count(session_id)}


def _session_id(ctx: Mapping[str, Any]) -> str | None:
    session_id = ctx.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def stopped_since_start(ctx: Mapping[str, Any]) -> bool:
    """這一則請求被停止，或這次執行開始之後這個對話被停止過（不看對話旗標）。"""
    pending = ctx.get("ui_pending")
    if isinstance(pending, PendingRequest) and pending.stopped:
        return True
    session_id = _session_id(ctx)
    baseline = ctx.get(STOP_BASELINE_KEY)
    return (
        session_id is not None
        and isinstance(baseline, int)
        and rhythm_service.stop_count(session_id) > baseline
    )


def request_stopped(ctx: Mapping[str, Any]) -> bool:
    """``stopped_since_start``，或對話旗標已設（hook 跑完時用）。"""
    if stopped_since_start(ctx):
        return True
    session_id = _session_id(ctx)
    return session_id is not None and rhythm_service.is_interrupted(session_id)

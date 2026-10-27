"""最後一次重置的結果（舊審查報告丙-5，ADR-0289）.

請求斷線或逾時，畫面只知道「結果不明」；伺服器把最後一次重置的結果留在行程記憶體，
`GET /admin/agents/reset/last` 讀得到。只存在記憶體：伺服器重啟後回 idle。

每次寫入整筆換掉參照（和還原狀態同一種做法），清除工作在工作執行緒裡寫也不需要鎖。
寫入都在破壞性操作鎖或清除旗標放開之前，下一次重置開始時，上一次晚到的結果蓋不掉它。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src.core.exceptions import ErrorCode, ToukaException
from src.models.agent_reset import AgentResetResponse, LastResetStatus
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from src.models.agent_reset import ResetState
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_last = LastResetStatus(state="idle")


def last_reset() -> LastResetStatus:
    """目前記著的最後一次重置."""
    return _last


def _record(status: LastResetStatus) -> None:
    global _last  # 工作執行緒寫、事件迴圈讀；整筆替換參照，不需鎖
    _last = status


@dataclass(frozen=True)
class ResetRun:
    """一次重置：開始時記 running，結束時由記結果的那一方記最終狀態."""

    target: AgentIdentity
    started_at: float

    def _finish(
        self,
        state: ResetState,
        *,
        error_code: str | None = None,
        report: AgentResetResponse | None = None,
    ) -> None:
        _record(
            LastResetStatus(
                state=state,
                agent_id=self.target.agent_id,
                started_at=self.started_at,
                finished_at=get_current_epoch(),
                error_code=error_code,
                report=report,
            )
        )

    def done(self, report: dict[str, Any]) -> None:
        """重置結束（清除也結束）；報告不合回應格式時記成失敗（端點回應同樣會失敗）."""
        try:
            parsed = AgentResetResponse.model_validate(report)
        except ValidationError:  # 端點回應同樣會失敗：照實記成沒有完成
            logger.exception("[reset] last result does not fit the response model")
            self._finish("failed", error_code=ErrorCode.SYSTEM_ERROR.value)
            return
        self._finish("done", report=parsed)

    def failed(self, exc: Exception) -> None:
        """重置出錯：只記錯誤碼（不是 Touka 錯誤的一律 SYSTEM_ERROR）."""
        code = exc.code if isinstance(exc, ToukaException) else ErrorCode.SYSTEM_ERROR
        self._finish("failed", error_code=code.value)

    def interrupted(self) -> None:
        """請求中途取消、還沒交給清除工作：可能只清了一部分."""
        self._finish("interrupted")


def start(target: AgentIdentity) -> ResetRun:
    """拿到破壞性操作鎖之後呼叫：記 running（被 409 擋下的請求不算一次重置）."""
    run = ResetRun(target=target, started_at=get_current_epoch())
    _record(LastResetStatus(state="running", agent_id=target.agent_id, started_at=run.started_at))
    return run


@contextmanager
def recording(run: ResetRun, handed_over: Callable[[], bool]) -> Iterator[None]:
    """重置本體出錯或被取消時記結果；已經交給清除工作的話，結果由清除工作記."""
    try:
        yield
    except asyncio.CancelledError:
        if not handed_over():
            run.interrupted()
        raise
    except Exception as exc:
        if not handed_over():
            run.failed(exc)
        raise

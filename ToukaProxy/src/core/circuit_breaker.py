"""系統連線斷路器實作 (Circuit Breaker).

用於保護對外部 API 或 MCP 伺服器的連線，防止單點故障導致請求堆積與系統崩潰。
"""

from collections.abc import Callable
from enum import Enum
from typing import Any

from src.utils.time_utils import get_current_epoch


class CircuitState(Enum):
    """斷路器狀態枚舉.

    包含 CLOSED (正常運作), OPEN (故障拒絕), HALF_OPEN (嘗試恢復) 三個狀態。
    """
    CLOSED = "CLOSED"  # 正常運作，請求放行
    OPEN = "OPEN"  # 故障次數過多，拒絕請求
    HALF_OPEN = "HALF_OPEN"  # 嘗試恢復，放行部分請求


class CircuitBreakerException(Exception):
    """斷路器處於開啟狀態時拒絕請求所拋出的異常."""
    pass


class BreakerNeutralError(Exception):
    """沒探到上游就結束的結果（例如呼叫前的本機拒絕）：不記成功也不記失敗，原樣往上丟."""


class CircuitBreaker:
    """斷路器實作，用於保護 MCP Server 連線

    當錯誤次數大於 failure_threshold 時，狀態轉為 OPEN，
    經過 recovery_timeout 秒後，轉為 HALF_OPEN 測試連線。
    如果測試成功，恢復 CLOSED；否則退回 OPEN。
    """

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        """初始化斷路器.

        Args:
            failure_threshold (int, optional): 開啟斷路器前的連續故障次數閾值。預設為 3。
            recovery_timeout (float, optional): 斷路器開啟後的冷卻時間（秒）。預設為 30.0。
        """
        self.state = CircuitState.CLOSED
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.last_failure_time = 0.0

    def _update_state(self):
        if self.state == CircuitState.OPEN:
            from src.utils.time_utils import get_current_epoch
            # 檢查是否過了恢復冷卻期
            if get_current_epoch() - self.last_failure_time > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN

    def record_success(self):
        """記錄一次成功呼叫.

        如果目前為 HALF_OPEN 狀態，則重置並恢復 CLOSED 狀態，同時將故障計數歸零。
        """
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
        self.failure_count = 0

    def record_failure(self):
        """記錄一次故障呼叫.

        遞增故障計數並更新最後故障時間。若連續故障次數達到閾值，則將狀態變更為 OPEN。
        """
        self.failure_count += 1
        self.last_failure_time = get_current_epoch()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

    def check_request_allowed(self) -> bool:
        """檢查目前是否允許執行請求.

        會先觸發內部狀態更新（例如檢查冷卻期是否已過），然後依狀態決定放行與否。

        Returns:
            bool: 若允許請求放行則回傳 True，若正處於 OPEN 故障狀態則回傳 False。
        """
        self._update_state()
        if self.state == CircuitState.OPEN:
            return False
        return True

    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """包裝異步呼叫"""
        if not self.check_request_allowed():
            raise CircuitBreakerException("Circuit breaker is OPEN. Request denied.")

        try:
            result = await func(*args, **kwargs)
            self.record_success()
            return result
        except BreakerNeutralError:
            raise
        except Exception as e:
            self.record_failure()
            raise e

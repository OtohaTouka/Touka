"""非同步重試機制與指數退避裝飾器 (Async Retry with Exponential Backoff).

提供系統在面對網路不穩定或臨時性 API 失敗時的自動重試功能。
"""

import asyncio
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions_to_catch: tuple[type[Exception], ...] = (Exception,),
):
    """帶有指數退避機制的非同步重試裝飾器工廠
    """

    def decorator(func: Callable) -> Callable:
        """接收目標非同步函數並封裝重試邏輯的裝飾器.

        Args:
            func (Callable): 欲重試的非同步函數。

        Returns:
            Callable: 封裝了指數退避重試邏輯的包裝函數。
        """

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            retries = 0
            delay = base_delay

            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions_to_catch as e:
                    retries += 1
                    if retries > max_retries:
                        logger.error(f"達到最大重試次數 ({max_retries})，拋出例外: {e}")
                        raise e

                    logger.warning(
                        f"執行 {func.__name__} 發生錯誤: {e}。預計 {delay} 秒後進行第 {retries} 次重試..."
                    )
                    await asyncio.sleep(delay)

                    # 指數退避，計算下次延遲
                    delay = min(delay * 2, max_delay)

        return wrapper

    return decorator

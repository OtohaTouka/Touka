"""主迴圈重試的錯誤分類（ADR-0248 3b-U5a；DEV-A 附註 3、29；R-3b ② M1、L3、L5）。

adapter 只把「這次失敗可以重試」標在 RoundError 上（``retry_note``＝重試狀態文字的前半、
``retry_after``＝廠商要求的等待秒數），真正的退避與重送在 react_core。可重試的有：

- 狀態碼 408／409／429 與 5xx（含 529）；400 等其他狀態一律直接顯示；
- 串流還沒收到 200 就斷（標頭在生成前就回，計費風險低）；非串流只重試連不上（``ConnectError``／
  ``ConnectTimeout``／``PoolTimeout``）——讀逾時、讀到一半斷線表示請求已送出、廠商可能還在生成；
- 200 之後才收到的錯誤本文，型別或代碼等同 429／5xx（``overloaded_error``、``UNAVAILABLE``…）；
- 還沒有任何輸出時的首事件逾時與閒置逾時。整輪上限、已經收到輸出之後的閒置逾時不重試
  （避免卡好幾個整輪、輸出重複計費）。

串流開了之後的斷線、半截串流、壞資料不重試。complete_once 不看這些標記（R3：只送 1 次）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx
import httpx2

from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from .base import RoundError
    from .stream_timeout import RoundClock

RETRYABLE_STATUS = frozenset({408, 409, 429})
_SERVER_ERRORS = range(500, 600)  # 5xx（含 529）也可重試
MAX_RETRY_AFTER_S = 30.0  # 廠商要求等更久時照這個上限（禁令 18）
# 200 之後的錯誤本文：這些型別／代碼等同 429／5xx（Anthropic、OpenAI、Gemini 的寫法）
_BUSY_ERROR_TYPES = frozenset({"overloaded_error", "api_error", "rate_limit_error",
                               "server_error"})
# 連不上（請求還沒送出）：非串流路徑只有這些能重試（R-3b ② N1）
_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                     httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)
_BUSY_ERROR_CODES = frozenset({"UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL",
                               "server_error", "rate_limit_exceeded"})
_MAX_CAUSE_DEPTH = 8  # 例外鏈最多往下找幾層（禁令 18；循環的鏈也會停）
_RETRY_DELAY = re.compile(r"(\d+(?:\.\d+)?)s")  # RetryInfo 的 retryDelay，如 "27s"


def retryable_status(status: int) -> bool:
    """408／409／429 與 5xx 可重試。"""
    return status in RETRYABLE_STATUS or status in _SERVER_ERRORS


def retry_after_s(headers: Any) -> float | None:
    """``retry-after-ms``／``retry-after``（秒或 HTTP 日期）→ 秒數；看不懂回 None、太大截到上限。"""
    if not headers:
        return None
    candidates = [(headers.get("retry-after-ms"), 1000.0), (headers.get("retry-after"), 1.0)]
    for value, scale in candidates:
        if not value:
            continue
        try:
            seconds = float(value) / scale
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - get_current_epoch()
            except (TypeError, ValueError):
                continue
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)
    return None


def retry_info_s(body: Any) -> float | None:
    """Google 錯誤本文的 RetryInfo（``error.details[].retryDelay``）→ 秒數；看不懂回 None。"""
    try:
        details = (json.loads(body) if isinstance(body, str | bytes) else body)["error"]["details"]
    except (ValueError, TypeError, KeyError):
        return None
    for detail in details if isinstance(details, list) else []:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        match = _RETRY_DELAY.fullmatch(delay) if isinstance(delay, str) else None
        if match:
            return min(float(match.group(1)), MAX_RETRY_AFTER_S)
    return None


def on_status(error: RoundError, status: int, headers: Any, note: str,
              body: Any = None) -> RoundError:
    """可重試的狀態碼：標上重試文字與等待秒數（標頭優先，再看 RetryInfo）；其餘原樣。"""
    if not retryable_status(status):
        return error
    after = retry_after_s(headers)
    return replace(error, retry_note=note,
                   retry_after=retry_info_s(body) if after is None and body is not None else after)


def on_connect(error: RoundError, opened: bool, note: str) -> RoundError:
    """串流還沒收到 200 就斷可重試；串流開了之後才斷（半截）不重試。"""
    return error if opened else replace(error, retry_note=note)


def on_post_failure(error: RoundError, cause: BaseException, note: str) -> RoundError:
    """非串流送出失敗：只有連不上可重試；SDK 例外沿 ``__cause__`` 找原本的傳輸錯誤。"""
    seen: BaseException | None = cause
    for _ in range(_MAX_CAUSE_DEPTH):
        if isinstance(seen, _CONNECT_FAILURES):
            return replace(error, retry_note=note)
        if seen is None:
            break
        seen = seen.__cause__
    return error


def on_error_body(error: RoundError, body: Any, label: str) -> RoundError:
    """200 之後才收到的錯誤本文：型別或代碼等同 429／5xx 就比照狀態碼可重試。"""
    if not isinstance(body, dict):
        return error
    code = body.get("code")
    number = int(code) if isinstance(code, str) and code.isdigit() else code
    busy = (body.get("type") in _BUSY_ERROR_TYPES or body.get("status") in _BUSY_ERROR_CODES
            or code in _BUSY_ERROR_CODES
            or (isinstance(number, int) and retryable_status(number)))
    kind = body.get("status") or code or body.get("type")
    return replace(error, retry_note=f"{label} busy ({kind})") if busy else error


def on_timeout(error: RoundError, clock: RoundClock) -> RoundError:
    """還沒有輸出時的首事件／閒置逾時可重試；整輪上限、已有輸出之後的閒置不重試。"""
    if clock.phase == "round" or clock.saw_output:
        return error
    return replace(error, retry_note=clock.timeout_text)

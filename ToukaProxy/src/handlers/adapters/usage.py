"""用量正規化 SSOT（ADR-0244；model-p1b U1）。

三家 adapter 的 ``extract_usage`` 一律經 :func:`normalized_usage` 回傳同一組四個欄位：
未快取輸入、快取讀、快取寫、輸出。鍵名沿用 ``token_usage.json`` 既有鍵（P20：不加欄位），
累計與稽核照舊讀取。

各家回報方式：

- Anthropic：``input_tokens`` 本來就不含快取，快取讀、寫另列。
- OpenAI 相容：``prompt_tokens`` 含 ``prompt_tokens_details.cached_tokens``，要扣掉；沒有快取寫。
- Gemini：``promptTokenCount`` 含 ``cachedContentTokenCount``（Google 自動快取），要扣掉；
  Touka 不建顯式快取（ADR-0243），所以沒有快取寫。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.utils.token_estimator import estimate_tokens


def usage_count(value: Any) -> int:
    """廠商回報的 token 數 → 非負整數；None、缺欄或非整數一律 0。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def split_included_cache(prompt: Any, cached: Any) -> tuple[int, int]:
    """廠商的 prompt 數含快取時拆成 ``(未快取, 快取讀)``；快取量夾在 prompt 以內。"""
    total = usage_count(prompt)
    cache_read = min(usage_count(cached), total)
    return total - cache_read, cache_read


def normalized_usage(
    *, uncached_input: int, cache_read: int, cache_write: int, output: int
) -> tuple[dict[str, int], int, int]:
    """組出 ``extract_usage`` 的共同回傳：``(四欄 dict, 本輪總輸入, 輸出)``。

    本輪總輸入＝未快取＋快取讀＋快取寫，也就是這一輪佔用的 context（context 分解的權威總數）。
    """
    usage = {
        "prompt_tokens": uncached_input,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "completion_tokens": output,
    }
    return usage, uncached_input + cache_read + cache_write, output


@dataclass(frozen=True)
class PartialUsage:
    """一輪沒完成（取消、逾時、半截串流）時要記的用量；``estimated``＝有欄位是估的。"""

    usage: dict[str, int]
    total_input: int
    estimated: bool


def estimate_partial_usage(
    *,
    request: Any,
    output_text: str,
    reported_input: tuple[int, int, int] | None,
    reported_output: int | None,
) -> PartialUsage:
    """取消、逾時、半截串流（R2、裁定 6）：廠商回報照記；沒回報的輸入用請求、輸出用收到的文字估。"""
    estimated = reported_input is None or reported_output is None
    if reported_input is None:  # 整份請求估（快取記 0；token_estimator 寧高估）
        request_text = json.dumps(request, ensure_ascii=False, default=str)
        reported_input = (estimate_tokens(request_text), 0, 0)
    uncached, cache_read, cache_write = reported_input
    output = estimate_tokens(output_text) if reported_output is None else reported_output
    usage, total_input, _ = normalized_usage(
        uncached_input=uncached, cache_read=cache_read, cache_write=cache_write, output=output
    )
    return PartialUsage(usage, total_input, estimated)

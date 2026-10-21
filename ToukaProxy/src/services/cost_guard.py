"""送前最壞花費（r55）：送出前估這一次呼叫最多會花多少，給花費上限先擋。

最壞花費＝輸出上限全部用滿（含思考）× 輸出價＋估的輸入 × 輸入價與寫入快取價較貴者。
輸入估法：實際要送的內容 JSON 每個字元至少算 1 token（中文每字 ≥ 1、英數多估），另加
廠商端餘量（例如帶工具時廠商自己加的系統提示）。估計只會偏大＝偏貴、偏安全。

和人工實測工具 ``tests/memory_eval/live_bounds.py`` 同一套估法；產品不 import tests，
兩邊由 ``tests/unit/test_cost_guard.py`` 的 parity 測試釘住。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.services.providers.pricing import get_pricing

if TYPE_CHECKING:
    from src.services.providers.pricing import Rate

VENDOR_OVERHEAD_TOKENS = 1_024
_PER_MTOK = 1_000_000


def estimate_input_tokens(payload: object) -> int:
    """實際要送的內容（含工具 schema）每字元至少 1 token，加廠商端餘量。"""
    return len(json.dumps(payload, ensure_ascii=False, default=str)) + VENDOR_OVERHEAD_TOKENS


def worst_case_input_price(rate: Rate) -> float:
    """每百萬 token 的輸入價：一般輸入與寫入快取取較貴者（帶快取標記可能按寫入價計）。"""
    cache_write = rate.input if rate.cache_write is None else rate.cache_write
    return max(rate.input, cache_write)


def worst_case_usd(
    *, input_tokens: int, max_output_tokens: int, input_price: float, output_price: float
) -> float:
    """輸出上限全部用滿（含思考）＋估計輸入，都照較貴價。"""
    return (input_tokens * input_price + max_output_tokens * output_price) / _PER_MTOK


def worst_case_call_usd(model: str | None, payload: object, max_output_tokens: int) -> float:
    """一次呼叫的最壞花費。

    ``payload`` 是實際要送的內容，``max_output_tokens`` 是送出欄位上的輸出上限。按估計的提示
    長度選檔（Haiku 5.5 超過 10 萬用貴檔；估計偏大＝偏貴、偏安全）。
    """
    tokens = estimate_input_tokens(payload)
    rate = get_pricing(model).for_prompt(tokens)
    return worst_case_usd(
        input_tokens=tokens,
        max_output_tokens=max_output_tokens,
        input_price=worst_case_input_price(rate),
        output_price=rate.output,
    )

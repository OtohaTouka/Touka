"""人工實測的送前最壞花費與報告去敏（純函式；不載入產品程式）。

每個請求送出前：已花（結清的照實際、沒結清的照最壞）＋這個請求的最壞花費超過金額上限就不送。
最壞花費＝輸出上限全部用滿（含思考）× 輸出價＋這個請求實際要送的內容估的輸入 × 輸入價與寫入快取價
較貴者。輸入估法：請求 JSON 每個字元至少算 1 token（中文每字 ≥ 1；英數多估），另加廠商端餘量
（例如帶工具時廠商自己加的系統提示）。報告裡的回應文字先去敏再截斷。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tests.memory_eval.scenarios import canary_fragments

VENDOR_OVERHEAD_TOKENS = 1_024
EXCERPT_CHARS = 200
_PER_MTOK = 1_000_000
# 產品正規化後的四欄用量鍵（ADR-0244）→ 報告欄名；輸出已含思考。
USAGE_FIELDS = (
    ("prompt_tokens", "input_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
    ("completion_tokens", "output_tokens_incl_thinking"),
)
_REDACTIONS = (
    (re.compile(r"(?<![A-Za-z])Bearer\s+\S+", re.IGNORECASE), "[REDACTED_KEY]"),
    (re.compile(r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+", re.IGNORECASE),
     "[REDACTED_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:(?:sk|xai)-[-_A-Za-z0-9]{12,}|AIza[-_A-Za-z0-9]{20,})"),
     "[REDACTED_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}"), "[REDACTED_KEY]"),
    # 磁碟代號路徑與 UNC 路徑（含後面的路徑字元；中文資料夾名一起遮）
    (re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|，。、「」]*"), "[REDACTED_PATH]"),
)


class WorstCaseOverBudget(RuntimeError):
    """已花＋這個請求的最壞花費會超過本批金額上限：不送、整批停。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


def estimate_input_tokens(request: object) -> int:
    """這個請求實際要送的內容（含工具 schema）每字元至少 1 token，加廠商端餘量。"""
    return len(json.dumps(request, ensure_ascii=False, default=str)) + VENDOR_OVERHEAD_TOKENS


def worst_case_input_price(rate: Any) -> float:
    """每百萬 token 的輸入價：一般輸入與寫入快取取較貴者（帶快取標記可能按寫入價計）。"""
    cache_write = rate.input if rate.cache_write is None else rate.cache_write
    return max(rate.input, cache_write)


def worst_case_usd(
    *, input_tokens: int, max_output_tokens: int, input_price: float, output_price: float
) -> float:
    """輸出上限全部用滿（含思考）＋估計輸入，都照較貴價。"""
    return (input_tokens * input_price + max_output_tokens * output_price) / _PER_MTOK


@dataclass(slots=True)
class RequestRecord:
    """一個已放行的請求：送前估計，與廠商回報後的實際用量。"""

    max_output_tokens: int
    input_tokens_estimate: int
    worst_case_usd: float
    usage: dict[str, int] | None = None
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        usage = self.usage or {}
        return {
            "max_output_tokens": self.max_output_tokens,
            "input_tokens_estimate": self.input_tokens_estimate,
            "worst_case_usd": round(self.worst_case_usd, 6),
            **{name: usage.get(key) for key, name in USAGE_FIELDS},
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
        }


@dataclass(slots=True)
class SpendLedger:
    """送前最壞花費帳本；價格是每百萬 token 的 USD。"""

    budget_usd: float
    input_price: float
    output_price: float
    records: list[RequestRecord] = field(default_factory=list)
    refused: str = ""

    @property
    def spent_usd(self) -> float:
        """已花：結清的照實際，沒結清的（沒回報用量、中途失敗）照最壞（寧高勿低）。"""
        return sum(r.worst_case_usd if r.cost_usd is None else r.cost_usd for r in self.records)

    def reserve(self, request: object, max_output_tokens: int) -> RequestRecord:
        """放行前預留這個請求的最壞花費；超過上限就記下原因並拒絕（不記帳）。"""
        tokens = estimate_input_tokens(request)
        worst = worst_case_usd(input_tokens=tokens, max_output_tokens=max_output_tokens,
                               input_price=self.input_price, output_price=self.output_price)
        spent = self.spent_usd
        if spent + worst > self.budget_usd:
            self.refused = (f"pre-send worst case over budget: spent {spent:.6f} + worst "
                            f"{worst:.6f} > budget {self.budget_usd:.6f}")
            raise WorstCaseOverBudget(self.refused)
        record = RequestRecord(max_output_tokens, tokens, worst)
        self.records.append(record)
        return record

    def settle(self, usage: dict[str, int], cost_usd: float) -> None:
        """最近放行的請求改記廠商回報的用量與實際金額；沒有用量就保留最壞值。"""
        if usage and self.records and self.records[-1].cost_usd is None:
            self.records[-1].usage = dict(usage)
            self.records[-1].cost_usd = cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": [r.to_dict() for r in self.records],
            "usage_totals": {name: sum((r.usage or {}).get(key, 0) for r in self.records)
                             for key, name in USAGE_FIELDS},
            "ledger_spent_usd": round(self.spent_usd, 6),
            "send_refused": self.refused,
        }


def redact_excerpt(text: str, canary: str) -> str:
    """報告用的回應摘要：遮這次的口令（整組與每段）、像金鑰的字串、本機路徑，再截到 200 字。"""
    for part in sorted({canary, *canary_fragments(canary)} - {""}, key=len, reverse=True):
        text = re.sub(re.escape(part), "[REDACTED_CANARY]", text, flags=re.IGNORECASE)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS] + "…"

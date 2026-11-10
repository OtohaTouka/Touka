"""模型費率規格表 (Pricing Registry).

提供 ``get_pricing(model_id) -> Rate``：回傳模型每百萬 token 的費率（USD），
作為成本估算（``telemetry.estimate_cost_usd``）與預算上限判定的 SSOT。

來源優先序（與 ``context_window_registry`` 同形）：
  1. ``config/model_pricing.json`` 的 ``pricing`` 覆蓋（人改／API 文件更新；**每次呼叫
     惰性讀檔 → 改檔即生效、零停機**，無需重啟或 apply_runtime_env）。
  2. 內建硬編規格表 ``_BUILTIN_PRICING``（code SSOT，離線可用）。
  3. 保守 fallback（未知模型）。

比對採「精確 → 最長子字串」兩層寬鬆匹配，容忍 provider 前綴與版本尾綴差異。只比對到家族鍵
（沒有具體價格）時用該家已知最高價，來源標 ``family``（ADR-0311）。
讀檔失敗一律降級至內建表／預設，**永不拋例外**。本模組為**純字串→費率**函式，
**不 import PROVIDER_SPECS**（避免 telemetry→providers→… 服務層耦合）。

安全（對抗複審 security-1）：未知模型的全域 fallback **不可為 0**——否則成本恆 0 →
``autonomy.is_over_budget`` 恆 False → 硬預算上限失效、可無限燒錢。真正合法的 0 只屬於
本地免費供應商（ollama/litellm 自架），須於內建表／pricing **顯式**標 0。走 fallback 時
記一次 WARN，提示成本為粗估。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from src.utils import config_utils
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

_CONFIG_FILE_NAME = "model_pricing.json"


@dataclass(frozen=True)
class Rate:
    """模型費率（USD / 1M tokens）。

    ``cache_read`` / ``cache_write`` 未設（None）時沿用 ``input`` 價：三家 adapter 已把快取
    從 ``prompt_tokens`` 拆出（ADR-0244），廠商快取折扣沒填就照 input 價保守估（寧高勿低，
    預算閘才不會少算）。

    ``long_prompt``＝（門檻, 超過門檻的費率）：按提示長度分檔的型號（r55：Haiku 5.5）。提示
    長度＝未快取輸入＋快取讀＋快取寫，每個請求各自判斷（廠商每請求獨立計價）。
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long_prompt: tuple[int, Rate] | None = None

    def for_prompt(self, prompt_tokens: int) -> Rate:
        """這個請求的提示長度適用的費率：超過門檻用長檔，否則就是自己。"""
        if self.long_prompt is not None and prompt_tokens > self.long_prompt[0]:
            return self.long_prompt[1]
        return self

    def cost_usd(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """估算一個請求的成本（USD，四捨五入至 6 位）；先依這個請求的提示長度選檔。

        輸入一律是正規化後的用量（ADR-0244）：``prompt_tokens`` 只含未快取輸入，快取讀、寫
        另列，三者不重疊。快取費率為 None 時照 ``input`` 價計，設了費率就用設定值。
        """
        rate = self.for_prompt(prompt_tokens + cache_read_tokens + cache_write_tokens)
        return rate.flat_cost_usd(
            prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens
        )

    def flat_cost_usd(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """照這一檔費率算、不選檔（多輪加總由呼叫端先按檔分好，見 ``telemetry.run_cost_usd``）。"""
        cr = self.input if self.cache_read is None else self.cache_read
        cw = self.input if self.cache_write is None else self.cache_write
        cost = (
            prompt_tokens / 1_000_000.0 * self.input
            + completion_tokens / 1_000_000.0 * self.output
            + cache_read_tokens / 1_000_000.0 * cr
            + cache_write_tokens / 1_000_000.0 * cw
        )
        return round(cost, 6)


# 內建表最後一次逐筆對官方價格頁的日期（畫面會顯示）；model_pricing.json 的覆蓋不算在內。
PRICING_VERIFIED_AT = "2026-11-10"

# Haiku 5.5 提示長度（含快取讀寫）超過這個數就整個請求改用長檔費率。
HAIKU_5_5_LONG_PROMPT_TOKENS = 100_000
_OPENAI_LONG_PROMPT_TOKENS = 272_000
_GOOGLE_LONG_PROMPT_TOKENS = 200_000
# xAI 官方是「達到 20 萬」就整個請求用高價；for_prompt 比的是超過。
_XAI_LONG_PROMPT_TOKENS = 199_999

# security-1：未知模型保守高估 fallback（不可為 0）。沿用重構前 Anthropic $3/$15；
# 可由 model_pricing.json 的 _default 覆蓋。
_DEFAULT = Rate(input=3.0, output=15.0)


def _tiered(
    threshold: int,
    short: tuple[float, float, float | None],
    long_tier: tuple[float, float, float | None],
) -> Rate:
    """官方按提示長度分兩檔的型號；兩邊都是（輸入, 輸出, 快取讀）。"""
    return Rate(
        short[0], short[1], cache_read=short[2],
        long_prompt=(threshold, Rate(long_tier[0], long_tier[1], cache_read=long_tier[2])),
    )


# 內建硬編規格表（code SSOT，離線可用）。鍵為小寫子字串，最長匹配優先（具體勝過泛化）。
# 費率為 USD / 1M tokens，2026-11-10 逐筆對官方價格頁（ADR-0311）：
#   Anthropic platform.claude.com/docs/en/about-claude/pricing
#   OpenAI developers.openai.com/api/docs/pricing（gpt-5.6-sol 是優惠價，到 11/30，官方沒寫之後多少）
#   Google aistudio.google.com/docs/pricing
#   xAI docs.x.ai/docs/models
#   DeepSeek api-docs.deepseek.com/quick_start/pricing（尖峰價，寧高勿低）
# 官方已不列的舊型號留著舊價。官方沒列快取價的留 None＝照 input 價計（保守；adapter 已把快取
# 從 prompt 拆出，不會重複，ADR-0244）；浮動時由 config/model_pricing.json 覆蓋，免改碼。
# 本地免費供應商（自架 ollama 等）模型名難以僅憑字串辨識，故不在此猜測——保守 fallback
# （非 0）確保預算護欄不失效；使用者可於 model_pricing.json 將自架模型顯式標 0。
_BUILTIN_PRICING: dict[str, Rate] = {
    # ── Anthropic Claude（cache_read / cache_write 為獨立計費項；只建 5 分鐘快取寫入價）──
    "claude-3-haiku": Rate(0.25, 1.25, cache_read=0.03, cache_write=0.30),
    "claude-3-5-haiku": Rate(0.80, 4.0, cache_read=0.08, cache_write=1.0),
    "claude-haiku-4": Rate(1.0, 5.0, cache_read=0.10, cache_write=1.25),
    "claude-3-opus": Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-opus-4": Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-3-5-sonnet": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-3-7-sonnet": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-opus-4-5": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-6": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-7": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-5": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-5-5": Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0),
    "claude-sonnet-5": Rate(2.0, 10.0, cache_read=0.20, cache_write=2.50),
    "claude-sonnet-5-5": Rate(2.0, 10.0, cache_read=0.10, cache_write=2.50),
    "claude-haiku-5-5": Rate(
        0.10, 0.50, cache_read=0.01, cache_write=0.125,
        long_prompt=(HAIKU_5_5_LONG_PROMPT_TOKENS,
                     Rate(0.50, 2.50, cache_read=0.05, cache_write=0.625)),
    ),
    "claude-fable": Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50),  # 家族最高價
    "claude-mythos": Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50),  # 家族最高價
    "claude-fable-5": Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50),
    "claude-mythos-5": Rate(10.0, 50.0, cache_read=1.0, cache_write=12.50),
    "claude-fable-5-1": Rate(10.0, 50.0, cache_read=0.25, cache_write=12.50),
    "claude-mythos-5-1": Rate(10.0, 50.0, cache_read=0.25, cache_write=12.50),
    "claude-fable-5-2": Rate(8.0, 40.0, cache_read=0.20, cache_write=10.0),
    "claude-mythos-5-2": Rate(8.0, 40.0, cache_read=0.20, cache_write=10.0),
    "claude": Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75),  # 家族最高價
    # ── DeepSeek（deepseek-chat、reasoner 官方已不列）──
    "deepseek-flash": Rate(0.30, 1.20, cache_read=0.006),
    "deepseek-v4-flash": Rate(0.30, 1.20, cache_read=0.006),  # 舊名，官方照 Flash 計價
    "deepseek-v4-pro": Rate(1.32, 3.96, cache_read=0.044),
    "deepseek-reasoner": Rate(0.55, 2.19),
    "deepseek-chat": Rate(0.27, 1.10),
    "deepseek": Rate(1.32, 3.96),  # 家族最高價
    # ── OpenAI ──
    "gpt-3.5": Rate(0.50, 1.50),
    "gpt-4o-mini": Rate(0.15, 0.60, cache_read=0.075),
    "gpt-4o": Rate(2.50, 10.0, cache_read=1.25),
    "gpt-4-turbo": Rate(10.0, 30.0),
    "gpt-4.1-mini": Rate(0.40, 1.60, cache_read=0.10),
    "gpt-4.1": Rate(2.0, 8.0, cache_read=0.50),
    "gpt-4": Rate(30.0, 60.0),
    "o1-mini": Rate(1.10, 4.40),
    "o1": Rate(15.0, 60.0),
    "o3-mini": Rate(1.10, 4.40),
    "o3-pro": Rate(20.0, 80.0),
    "o3": Rate(2.0, 8.0, cache_read=0.50),
    "chat-latest": Rate(5.0, 30.0, cache_read=0.50),
    "gpt-5": Rate(1.25, 10.0, cache_read=0.125),  # 只比完整名稱，見 _EXACT_ONLY_KEYS
    "gpt-5-mini": Rate(0.25, 2.0, cache_read=0.025),
    "gpt-5-nano": Rate(0.05, 0.40, cache_read=0.005),
    "gpt-5-pro": Rate(15.0, 120.0),
    "gpt-5.1": Rate(1.25, 10.0, cache_read=0.125),
    "gpt-5.2": Rate(1.75, 14.0, cache_read=0.175),
    "gpt-5.2-pro": Rate(21.0, 168.0),
    "gpt-5.3-codex": Rate(1.75, 14.0, cache_read=0.175),
    "gpt-5.4": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (2.50, 15.0, 0.25), (5.0, 22.50, 0.50)),
    "gpt-5.4-mini": Rate(0.75, 4.50, cache_read=0.075),
    "gpt-5.4-nano": Rate(0.20, 1.25, cache_read=0.02),
    "gpt-5.4-pro": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (30.0, 180.0, None), (60.0, 270.0, None)),
    "gpt-5.5": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (5.0, 30.0, 0.50), (10.0, 45.0, 1.0)),
    "gpt-5.5-pro": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (30.0, 180.0, None), (60.0, 270.0, None)),
    "gpt-5.6-sol": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (4.0, 20.0, 0.40), (8.0, 30.0, 0.80)),
    "gpt-5.6-terra": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (2.0, 12.0, 0.20), (4.0, 18.0, 0.40)),
    "gpt-5.6-luna": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (0.20, 1.20, 0.02), (0.40, 1.80, 0.04)),
    "gpt-6-astra": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (10.0, 50.0, 1.0), (20.0, 75.0, 2.0)),
    "gpt-6-sol": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (2.0, 10.0, 0.20), (4.0, 15.0, 0.40)),
    "gpt-6-luna": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (0.10, 0.50, 0.01), (0.20, 0.75, 0.02)),
    "gpt-6.1-sol": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (2.0, 10.0, 0.10), (4.0, 15.0, 0.20)),
    "gpt-6.1-terra": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (1.0, 5.0, 0.05), (2.0, 7.50, 0.10)),
    "gpt": _tiered(_OPENAI_LONG_PROMPT_TOKENS, (30.0, 180.0, None), (60.0, 270.0, None)),  # 家族最高價
    # ── xAI Grok ──
    "grok-2": Rate(2.0, 10.0),
    "grok-3": Rate(3.0, 15.0),
    "grok-4.20": _tiered(_XAI_LONG_PROMPT_TOKENS, (1.25, 2.50, 0.20), (2.50, 5.0, 0.40)),
    "grok-4.3": _tiered(_XAI_LONG_PROMPT_TOKENS, (1.25, 2.50, 0.20), (2.50, 5.0, 0.40)),
    "grok-4.5": _tiered(_XAI_LONG_PROMPT_TOKENS, (2.0, 6.0, 0.30), (4.0, 12.0, 0.60)),
    "grok-4.6": _tiered(_XAI_LONG_PROMPT_TOKENS, (2.0, 6.0, 0.50), (4.0, 12.0, 1.0)),
    "grok-4.7": _tiered(_XAI_LONG_PROMPT_TOKENS, (2.0, 6.0, 0.50), (4.0, 12.0, 1.0)),
    "grok-build-0.1": _tiered(_XAI_LONG_PROMPT_TOKENS, (1.0, 2.0, 0.20), (2.0, 4.0, 0.40)),
    "grok": Rate(4.0, 15.0),  # 家族最高價
    # ── Google Gemini ──
    "gemini-1.5-flash": Rate(0.075, 0.30),
    "gemini-1.5-pro": Rate(1.25, 5.0),
    "gemini-2.0-flash": Rate(0.10, 0.40),
    "gemini-2.5-flash": Rate(0.30, 2.50, cache_read=0.03),
    "gemini-2.5-flash-lite": Rate(0.10, 0.40, cache_read=0.01),
    "gemini-2.5-pro": _tiered(_GOOGLE_LONG_PROMPT_TOKENS, (1.25, 10.0, 0.125), (2.50, 15.0, 0.25)),
    "gemini-3-flash-preview": Rate(0.50, 3.0, cache_read=0.05),
    "gemini-3.1-flash-lite": Rate(0.25, 1.50, cache_read=0.025),
    "gemini-3.1-pro-preview": _tiered(_GOOGLE_LONG_PROMPT_TOKENS, (2.0, 12.0, 0.20), (4.0, 18.0, 0.40)),
    "gemini-3.5-flash-lite": Rate(0.30, 2.50, cache_read=0.03),
    "gemini-3.6-flash": Rate(0.75, 3.75, cache_read=0.075),
    "gemini-3.8-flash": Rate(0.75, 3.75, cache_read=0.075),
    "gemini-4-argon": Rate(2.0, 10.0, cache_read=0.10),
    "gemini": Rate(4.0, 20.0),  # 家族最高價
}

# 沒有具體價格時落到的家族鍵（該家已知最高價，寧高勿低）；resolve 標成 family，畫面會列出來。
_FAMILY_KEYS = frozenset(
    {"claude", "claude-fable", "claude-mythos", "deepseek", "gemini", "gpt", "grok"}
)
# 只比完整名稱：用子字串比，gpt-5.7 這類沒列的型號會被算成 gpt-5 的便宜價。
_EXACT_ONLY_KEYS = frozenset({"gpt-5"})

# 官方已公告的漲價，到日子才換（官方沒寫時區，取 UTC 當天 0 點）。
_EPOCH_2026_12_01 = 1_796_083_200
_EPOCH_2027_01_01 = 1_798_761_600
_SCHEDULED_PRICING: dict[str, tuple[int, Rate]] = {
    "gemini-4-argon": (_EPOCH_2026_12_01, Rate(4.0, 20.0, cache_read=0.20)),
    "gemini-3.6-flash": (_EPOCH_2027_01_01, Rate(1.50, 7.50, cache_read=0.15)),
    "gemini-3.8-flash": (_EPOCH_2027_01_01, Rate(1.50, 7.50, cache_read=0.15)),
}

# fallback 警告去重：每個未知 model id 只 WARN 一次，避免逐回合洗版。
_warned_unknown: set[str] = set()


def _parse_rate(raw: object) -> Rate | None:
    """防禦式解析單筆費率；缺欄／型別錯誤回 None（略過該項，不致命）。覆寫只有單一費率、不分檔。"""
    if not isinstance(raw, dict):
        return None
    try:
        cr = raw.get("cache_read")
        cw = raw.get("cache_write")
        return Rate(
            input=float(raw["input"]),
            output=float(raw["output"]),
            cache_read=None if cr is None else float(cr),
            cache_write=None if cw is None else float(cw),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_config() -> tuple[dict[str, Rate], Rate]:
    """回傳 ``(pricing_override, default_rate)``；讀檔失敗回 ``({}, _DEFAULT)``。永不拋例外。"""
    default = _DEFAULT
    overrides: dict[str, Rate] = {}
    try:
        data = config_utils.read_config_json(_CONFIG_FILE_NAME)
        if isinstance(data, dict):
            parsed_default = _parse_rate(data.get("_default"))
            if parsed_default is not None:
                default = parsed_default
            raw_pricing = data.get("pricing")
            if isinstance(raw_pricing, dict):
                for key, value in raw_pricing.items():
                    rate = _parse_rate(value)
                    if rate is not None:
                        overrides[str(key).lower()] = rate
    except Exception:
        logger.debug(
            "model_pricing.json not loaded; using builtin table", exc_info=True
        )
    return overrides, default


def _longest_substring_key(mid: str, keys: Iterable[str]) -> str | None:
    """回傳 ``keys`` 中作為 ``mid`` 子字串、且最長者（無則 None）。"""
    best_key: str | None = None
    for key in keys:
        if key in mid and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return best_key


def _builtin(key: str) -> tuple[Rate, str]:
    """內建表的一筆：已公告的漲價到日子就換，家族鍵標 family。"""
    scheduled = _SCHEDULED_PRICING.get(key)
    if scheduled is not None and get_current_epoch() >= scheduled[0]:
        rate = scheduled[1]
    else:
        rate = _BUILTIN_PRICING[key]
    return rate, "family" if key in _FAMILY_KEYS else "builtin"


def resolve(model: str | None) -> tuple[Rate, str]:
    """回傳 ``(Rate, source)``；``source`` ∈ {override, builtin, family, fallback}。永不拋例外。"""
    overrides, default = _load_config()
    if not isinstance(model, str) or not model.strip():
        return default, "fallback"
    mid = model.strip().lower()

    # 1. 精確匹配（override 先於 builtin）。
    if mid in overrides:
        return overrides[mid], "override"
    if mid in _BUILTIN_PRICING:
        return _builtin(mid)

    # 2. 最長子字串匹配（override 先於 builtin；具體勝過泛化）。
    key = _longest_substring_key(mid, overrides)
    if key is not None:
        return overrides[key], "override"
    key = _longest_substring_key(mid, _BUILTIN_PRICING.keys() - _EXACT_ONLY_KEYS)
    if key is not None:
        return _builtin(key)

    # 3. 未知 → 保守 fallback（記一次 WARN：成本為粗估、預算判定不精確）。
    if mid not in _warned_unknown:
        _warned_unknown.add(mid)
        logger.warning(
            "未知模型 '%s' 無費率表項，採保守 fallback 估算（input=$%.4g/M、output=$%.4g/M）；"
            "成本與預算判定僅為粗估，請於 config/model_pricing.json 補上實際費率。",
            mid,
            default.input,
            default.output,
        )
    return default, "fallback"


def get_pricing(model: str | None) -> Rate:
    """回傳模型費率（USD / 1M tokens），未知回保守 fallback。永不拋例外。"""
    return resolve(model)[0]


def is_estimated(model: str | None) -> bool:
    """這個模型的價格是不是估的（沒有具體價格，用家族最高價或保守預設）。"""
    return resolve(model)[1] in ("family", "fallback")


def has_overrides() -> bool:
    """model_pricing.json 有沒有有效的覆蓋（查證日期不含這些）。"""
    return bool(_load_config()[0])

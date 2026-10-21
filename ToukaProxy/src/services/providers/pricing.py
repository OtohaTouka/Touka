"""模型費率規格表 (Pricing Registry).

提供 ``get_pricing(model_id) -> Rate``：回傳模型每百萬 token 的費率（USD），
作為成本估算（``telemetry.estimate_cost_usd``）與預算上限判定的 SSOT。

來源優先序（與 ``context_window_registry`` 同形）：
  1. ``config/model_pricing.json`` 的 ``pricing`` 覆蓋（人改／API 文件更新；**每次呼叫
     惰性讀檔 → 改檔即生效、零停機**，無需重啟或 apply_runtime_env）。
  2. 內建硬編規格表 ``_BUILTIN_PRICING``（code SSOT，離線可用）。
  3. 保守 fallback（未知模型）。

比對採「精確 → 最長子字串」兩層寬鬆匹配，容忍 provider 前綴與版本尾綴差異。
讀檔失敗一律降級至內建表／預設，**永不拋例外**。本模組為**純字串→費率**函式，
**不 import PROVIDER_SPECS**（避免 telemetry→providers→… 服務層耦合）。

安全（對抗複審 security-1）：未知模型的全域 fallback **不可為 0**——否則成本恆 0 →
``autonomy.is_over_budget`` 恆 False → 硬預算上限失效、可無限燒錢。真正合法的 0 只屬於
本地免費供應商（ollama/litellm 自架），須於內建表／pricing **顯式**標 0。走 fallback 時
記一次 WARN，提示成本為粗估。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.utils import config_utils

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


# Haiku 5.5 提示長度（含快取讀寫）超過這個數就整個請求改用長檔費率。
HAIKU_5_5_LONG_PROMPT_TOKENS = 100_000

# security-1：未知模型保守高估 fallback（不可為 0）。沿用重構前 Anthropic $3/$15；
# 可由 model_pricing.json 的 _default 覆蓋。
_DEFAULT = Rate(input=3.0, output=15.0)

# 內建硬編規格表（code SSOT，離線可用）。鍵為小寫子字串，最長匹配優先（具體勝過泛化）。
# 費率為 USD / 1M tokens，依各供應商公告價填入（截至知識截止日的估算；浮動時由
# config/model_pricing.json 覆蓋，免改碼）。Anthropic 的 cache 為獨立計費項（cache_read≈
# 0.1×input、cache_write≈1.25×input）故顯式填入；Gemini/OpenAI 的快取折扣未填，留 None
# ＝照 input 價計（保守；adapter 已把快取從 prompt 拆出，不會重複，ADR-0244）。
# 本地免費供應商（自架 ollama 等）模型名難以僅憑字串辨識，故不在此猜測——保守 fallback
# （非 0）確保預算護欄不失效；使用者可於 model_pricing.json 將自架模型顯式標 0。
_BUILTIN_PRICING: dict[str, Rate] = {
    # ── Anthropic Claude（cache_read / cache_write 為獨立計費項）──
    "claude-3-haiku": Rate(0.25, 1.25, cache_read=0.03, cache_write=0.30),
    "claude-3-5-haiku": Rate(0.80, 4.0, cache_read=0.08, cache_write=1.0),
    "claude-haiku-4": Rate(1.0, 5.0, cache_read=0.10, cache_write=1.25),
    "claude-3-opus": Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-opus-4": Rate(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-3-5-sonnet": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-3-7-sonnet": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    # 規則表新型號（ADR-0247 P24）：來源 claude-api skill 主文 Current Models 表、shared/models.md、
    # model-migration.md（2026-10-06 快取）；快取讀寫廠商有寫照寫，否則讀 0.1×、寫 1.25× input。
    # Opus 4.5 查不到，照舊落到 claude-opus-4（偏高）；查不到的 Fable／Mythos（如 Mythos Preview）
    # 落到家族鍵，套同家族已知最高價（寧高勿低）。
    "claude-opus-4-6": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-7": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-5": Rate(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-5-5": Rate(4.0, 20.0, cache_read=0.20, cache_write=5.0),
    # r55：官方 https://platform.claude.com/docs/en/about-claude/pricing（2026-10-16）。Sonnet 5.5
    # 快取命中 0.10（Sonnet 5 仍 0.20）；Haiku 5.5 依提示長度兩檔。只建 5 分鐘快取寫入價。
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
    "claude": Rate(3.0, 15.0, cache_read=0.30, cache_write=3.75),  # 泛用（sonnet 級）
    # ── DeepSeek（2026 新名 v4-flash / v4-pro；舊名 2026-07-24 停用，暫留相容）──
    "deepseek-v4-flash": Rate(0.27, 1.10),
    "deepseek-v4-pro": Rate(0.55, 2.19),
    "deepseek-reasoner": Rate(0.55, 2.19),
    "deepseek-chat": Rate(0.27, 1.10),
    "deepseek": Rate(0.27, 1.10),
    # ── OpenAI ──
    "gpt-3.5": Rate(0.50, 1.50),
    "gpt-4o-mini": Rate(0.15, 0.60),
    "gpt-4o": Rate(2.50, 10.0),
    "gpt-4-turbo": Rate(10.0, 30.0),
    "gpt-4.1": Rate(2.0, 8.0),
    "gpt-4": Rate(30.0, 60.0),
    "o1-mini": Rate(1.10, 4.40),
    "o1": Rate(15.0, 60.0),
    "o3-mini": Rate(1.10, 4.40),
    "o3": Rate(2.0, 8.0),
    # ── xAI Grok ──
    "grok-2": Rate(2.0, 10.0),
    "grok-3": Rate(3.0, 15.0),
    "grok-4": Rate(3.0, 15.0),
    "grok": Rate(2.0, 10.0),
    # ── Google Gemini（快取折扣未填＝照 input 價）──
    "gemini-1.5-flash": Rate(0.075, 0.30),
    "gemini-1.5-pro": Rate(1.25, 5.0),
    "gemini-2.0-flash": Rate(0.10, 0.40),
    "gemini-2.5-flash": Rate(0.30, 2.50),
    "gemini-2.5-pro": Rate(1.25, 10.0),
    "gemini": Rate(1.25, 5.0),
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


def _longest_substring_match(mid: str, table: dict[str, Rate]) -> Rate | None:
    """回傳 ``table`` 中作為 ``mid`` 子字串、且鍵最長者的值（無則 None）。"""
    best_key: str | None = None
    for key in table:
        if key in mid and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return table[best_key] if best_key is not None else None


def resolve(model: str | None) -> tuple[Rate, str]:
    """回傳 ``(Rate, source)``；``source`` ∈ {override, builtin, fallback}。永不拋例外。"""
    overrides, default = _load_config()
    if not isinstance(model, str) or not model.strip():
        return default, "fallback"
    mid = model.strip().lower()

    # 1. 精確匹配（override 先於 builtin）。
    if mid in overrides:
        return overrides[mid], "override"
    if mid in _BUILTIN_PRICING:
        return _BUILTIN_PRICING[mid], "builtin"

    # 2. 最長子字串匹配（override 先於 builtin；具體勝過泛化）。
    match = _longest_substring_match(mid, overrides)
    if match is not None:
        return match, "override"
    match = _longest_substring_match(mid, _BUILTIN_PRICING)
    if match is not None:
        return match, "builtin"

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

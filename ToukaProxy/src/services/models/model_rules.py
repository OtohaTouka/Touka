"""每模型規則表（ADR-0247；model-p3a U1）：送出請求時各模型廠商規則的單一事實來源。

以家族＋版本樣式比對，不逐一列模型 ID；認不出版本的 Claude 走保守預設，認不出家族的走通用
預設。**只用於整備送出內容，永遠不拿來過濾模型發現**（禁令 39：發現路徑不得讀本表）。
輸入是已解析的 API 模型 ID（Claude 已去掉 Touka 的 ``-thinking`` 尾碼）。廠商規則來源：
claude-api skill（2026-10-06 快取）；GPT-6 兩列取自模型層研究 [S36]，尚未實測。

欄位：
- ``forbidden_params``：送了會 400 的參數（取樣參數另由 ``SAMPLING_PARAM_NAMES`` 一律不送）。
- ``forced_tool_choice``：可否強制 tool_choice（any／tool）；Claude 開思考時另外不可。
- ``thinking``：budget＝只收 enabled＋budget_tokens；adaptive＝可要 adaptive、不帶就不思考；
  default_on＝不帶參數也會思考；None＝Touka 不調整此家族的思考參數。
- ``required_params_with_tools``：帶工具時必送的參數（3b 接上）。
- ``uses_responses_api``：OpenAI 家族要走 Responses（3b 依此選 adapter）。
- ``default_max_output``：請求沒給 max_tokens 時用的上限；None＝不補，用廠商預設。
- ``nonstreaming_max_tokens``：非串流時 SDK 收得下的最大 max_tokens（Claude；見下方常數）。
- ``cache_min_tokens``：快取最低門檻；None＝不適用或未確認。
- ``mid_conversation_system``：messages 中途可否放 role=system（4a 選插話格式用）。
- ``stream``：主迴圈走上游串流（3b，ADR-0248 D6）；要關＝改本表＋重建映像，不是執行期開關。
  相容 Chat 另外只限 ``COMPAT_STREAM_PROVIDERS``，自訂連線一律不串流（裁定 5）。
- ``effort_levels``：收的 ``output_config.effort``，低到高；空＝一律不送（r55，只列 5.5 三型號）。
- ``thinking_off_mode``：怎麼關思考——disabled（Haiku 5.5）、between_tools（Sonnet 5.5，只收這個）、
  unsupported（Opus 5.5 關不掉）；None＝Touka 不調整。關思考都要 effort ≤ high（官方 2026-10-16）。

context window 不在本表：沿用 ``context_window_registry``（含使用者覆蓋），避免兩個來源。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Literal

logger = logging.getLogger(__name__)

ThinkingMode = Literal["budget", "adaptive", "default_on"]
ThinkingOffMode = Literal["disabled", "between_tools", "unsupported"]

# r55：5.5 三型號都收這五級 effort（低到高）；關思考只在 effort ≤ high 有效。廠商預設（不送
# effort）Opus／Haiku 5.5 是 medium、Sonnet 5.5 是 high，都 ≤ high。
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
THINKING_OFF_MAX_EFFORT = "high"
_TURNS_OFF = frozenset({"disabled", "between_tools"})  # 真的關得掉的方式

# ── Claude 非串流上限（只在主迴圈關掉串流時壓；ADR-0248 附註 4）──────────────────
# anthropic 1.11（與 0.83 相同）`messages.create` 非串流且 client 用預設逾時時，
# `_calculate_nonstreaming_timeout` 估 3600 秒 × max_tokens ÷ 128,000，超過 600 秒就丟
# ValueError（要求改串流）：floor(600 × 128,000 ÷ 3600)＝21,333。主迴圈的 client（main.py）用 SDK
# 預設逾時，關掉串流時檢查照樣觸發；complete_once 有傳 timeout，SDK 不擋。
CLAUDE_NONSTREAMING_MAX_TOKENS = 21_333
# 同一處另查 `MODEL_NONSTREAMING_TOKENS`：Opus 4（第一方 -0／-20250514 與雲平台版）為 8,192；
# Opus 4.1 在 1.11 只剩 Bedrock／Vertex 版本。本表對整個 Opus 4.0／4.1 系列套用（第一方 4.1
# 偏保守）。
CLAUDE_OPUS_4_NONSTREAMING_MAX_TOKENS = 8_192
# 只收預算的思考：max_tokens 必須大於 budget_tokens；至少留這麼多給可見輸出（＝廠商預算下限）。
THINKING_MIN_VISIBLE_TOKENS = 1_024
# default_on 思考（不帶參數也思考）：max_tokens 太小時思考會吃光額度、回空白或截斷（例：ToukaIDE
# 行內補全送 128）。至少給 3a 之前一律送出的 8,192；未實測，見 ADR-0247 剩餘風險。
DEFAULT_ON_THINKING_MIN_MAX_TOKENS = 8_192


@dataclass(frozen=True)
class ModelRules:
    """一個模型家族／版本段的送出規則（欄位說明見模組說明）。"""

    forbidden_params: frozenset[str] = frozenset()
    forced_tool_choice: bool = True
    thinking: ThinkingMode | None = None
    required_params_with_tools: tuple[tuple[str, str], ...] = ()
    uses_responses_api: bool = False
    default_max_output: int | None = None
    nonstreaming_max_tokens: int | None = None
    cache_min_tokens: int | None = None
    mid_conversation_system: bool = False
    stream: bool = False
    effort_levels: tuple[str, ...] = ()
    thinking_off_mode: ThinkingOffMode | None = None


@dataclass(frozen=True)
class ClaudeThinkingPlan:
    """這次要送的思考設定（None＝不送該欄位）；``thinking_off``＝思考真的關掉了。"""

    thinking: dict | None
    effort: str | None
    thinking_off: bool


_NO_BUDGET = frozenset({"budget_tokens"})
# Claude 共同底：4.5 以前的樣子（只收 budget_tokens、可強制工具、預設輸出 8,192、
# 非串流上限 21,333）。
_CLAUDE = ModelRules(
    thinking="budget", default_max_output=8_192,
    nonstreaming_max_tokens=CLAUDE_NONSTREAMING_MAX_TOKENS, cache_min_tokens=1_024, stream=True,
)
_CLAUDE_5 = replace(  # 5.x、Fable、Mythos：不帶參數也思考，故不能強制工具
    _CLAUDE,
    forbidden_params=_NO_BUDGET,
    forced_tool_choice=False,
    thinking="default_on",
    default_max_output=16_000,
    cache_min_tokens=512,
    mid_conversation_system=True,
)
_CLAUDE_ADAPTIVE = replace(_CLAUDE, thinking="adaptive", default_max_output=16_000)  # 4.6
_CLAUDE_5_5 = replace(_CLAUDE_5, effort_levels=CLAUDE_EFFORT_LEVELS)

# (版本下限, 系列〔None＝任一〕, 規則)：由上而下取第一個符合的列。
_CLAUDE_ROWS: tuple[tuple[tuple[int, int], str | None, ModelRules], ...] = (
    ((5, 6), None, _CLAUDE_5),  # 5.6 以後等查過官方再加，先照舊（不送 effort、不改寫 disabled）
    ((5, 5), "opus", replace(_CLAUDE_5_5, thinking_off_mode="unsupported")),
    ((5, 5), "sonnet", replace(_CLAUDE_5_5, thinking_off_mode="between_tools")),
    ((5, 5), "haiku", replace(_CLAUDE_5_5, thinking_off_mode="disabled")),
    ((5, 5), None, _CLAUDE_5),  # 5.5 但認不出系列：不送 effort（保守）
    ((5, 0), "sonnet", replace(_CLAUDE_5, cache_min_tokens=1_024, mid_conversation_system=False)),
    ((5, 0), None, _CLAUDE_5),  # Opus 5
    ((4, 8), None, replace(_CLAUDE_ADAPTIVE, forbidden_params=_NO_BUDGET,
                           mid_conversation_system=True)),  # Opus 4.8
    ((4, 7), None, replace(_CLAUDE_ADAPTIVE, forbidden_params=_NO_BUDGET,
                           cache_min_tokens=2_048)),  # Opus 4.7
    ((4, 6), "opus", replace(_CLAUDE_ADAPTIVE, cache_min_tokens=4_096)),
    ((4, 6), None, _CLAUDE_ADAPTIVE),  # Sonnet 4.6（budget_tokens 已棄用但仍收）
    ((4, 5), "sonnet", replace(_CLAUDE, default_max_output=16_000)),
    ((4, 5), None, replace(_CLAUDE, default_max_output=16_000, cache_min_tokens=4_096)),
    ((4, 0), "opus", replace(_CLAUDE,  # Opus 4／4.1
                             nonstreaming_max_tokens=CLAUDE_OPUS_4_NONSTREAMING_MAX_TOKENS)),
    ((0, 0), "haiku", replace(_CLAUDE, cache_min_tokens=2_048)),  # Haiku 3／3.5
    ((0, 0), None, _CLAUDE),  # Sonnet 4、3.x
)
# 認不出版本：保守（不送 budget_tokens、不強制工具、門檻未知、不放中途 system）。
_CLAUDE_UNKNOWN = replace(
    _CLAUDE, forbidden_params=_NO_BUDGET, forced_tool_choice=False, thinking="adaptive",
    cache_min_tokens=None,
)
_ALWAYS_THINKING_LINES = ("fable", "mythos")
# prompt-caching 門檻 2,048；不在官方中途 system 支援名單（M9-R1a，見 ADR-0249）
_MYTHOS_PREVIEW = replace(_CLAUDE_5, cache_min_tokens=2_048, mid_conversation_system=False)
_CLAUDE_LINES = ("opus", "sonnet", "haiku")
# claude-{系列}-M[-m][-日期]：次版號限 1–2 位且其後不接數字，8 位日期段不當版本。
_FAMILY_FIRST = re.compile(r"claude-(?:opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2})(?!\d))?(?!\d)")
# 舊命名 claude-M[-m]-系列（如 claude-3-5-sonnet-20241022）。
_VERSION_FIRST = re.compile(r"claude-(\d+)(?:[-.](\d{1,2})(?!\d))?(?!\d)")

_GEMINI = ModelRules(stream=True)  # 送出時不補輸出上限、不調思考；門檻未確認；主迴圈串流
# 相容 Chat 走串流的已知廠商（route.provider＝Provider Registry 的 label；其餘照非串流）。
COMPAT_STREAM_PROVIDERS = frozenset({"OpenAI", "DeepSeek", "Grok"})
# OpenAI 家族（樣式，容許日期等尾碼）：[S36] 這兩組帶工具時的規則。
_OPENAI_ROWS: tuple[tuple[re.Pattern[str], ModelRules], ...] = (
    (re.compile(r"gpt-6-astra|gpt-6\.1-sol"), ModelRules(uses_responses_api=True, stream=True)),
    (re.compile(r"gpt-6-(?:sol|luna)"),
     ModelRules(required_params_with_tools=(("reasoning_effort", "none"),), stream=True)),
)
_GENERIC = ModelRules(stream=True)  # 相容 Chat 預設串流（仍受廠商清單與自訂連線限制）


def _parse_claude_version(name: str) -> tuple[int, int] | None:
    for pattern in (_FAMILY_FIRST, _VERSION_FIRST):
        match = pattern.search(name)
        if match:
            return int(match.group(1)), int(match.group(2) or 0)
    return None


def claude_rules(model: str) -> ModelRules:
    """一律以 Claude 家族解析（Claude adapter 與轉換器用；認不出版本＝保守預設）。"""
    name = model.lower()
    if any(line in name for line in _ALWAYS_THINKING_LINES):
        return _MYTHOS_PREVIEW if "mythos-preview" in name else _CLAUDE_5
    version = _parse_claude_version(name)
    if version is None:
        return _CLAUDE_UNKNOWN
    line = next((candidate for candidate in _CLAUDE_LINES if candidate in name), None)
    for floor, row_line, rules in _CLAUDE_ROWS:
        if version >= floor and row_line in (None, line):
            return rules
    return _CLAUDE  # 不會走到：最後一列下限 (0, 0) 不限系列


def rules_for(model: str) -> ModelRules:
    """模型名 → 規則（家族判斷與 ``route_family.native_family`` 相同：含 claude／gemini）。"""
    name = model.lower()
    if "claude" in name:
        return claude_rules(name)
    if "gemini" in name:
        return _GEMINI
    return next((rules for pattern, rules in _OPENAI_ROWS if pattern.search(name)), _GENERIC)


def allows_forced_tool_choice(model: str, has_thinking: bool) -> bool:
    """Claude 可否強制 ``tool_choice``：規則允許且這次沒開思考。"""
    return not has_thinking and claude_rules(model).forced_tool_choice


def normalize_thinking(model: str, thinking: dict) -> dict:
    """拒收 budget_tokens 的 Claude 把 enabled 改 adaptive；非 enabled 不帶 budget_tokens。"""
    if thinking.get("type") != "enabled":
        return {key: value for key, value in thinking.items() if key != "budget_tokens"}
    if "budget_tokens" in claude_rules(model).forbidden_params:
        return {"type": "adaptive"}
    return thinking


def _turns_off_at(effort: str | None) -> bool:
    """這個 effort 下關得掉思考（沒送＝廠商預設，三型號都 ≤ high）。"""
    return effort is None or (
        CLAUDE_EFFORT_LEVELS.index(effort) <= CLAUDE_EFFORT_LEVELS.index(THINKING_OFF_MAX_EFFORT))


def _client_thinking_off(model: str, mode: ThinkingOffMode,
                         effort: str | None) -> tuple[dict | None, bool]:
    """客戶端送 disabled：改成這個型號收得下的寫法，收不下就不送並記 warning（只記型號與等級）。"""
    if mode == "unsupported":
        logger.warning("Claude %s cannot turn thinking off; thinking.disabled is not sent "
                       "(it still thinks and bills output)", model)
        return None, False
    if not _turns_off_at(effort):
        logger.warning("Claude %s turns thinking off only at effort %s or below; "
                       "thinking.disabled is not sent at effort %r",
                       model, THINKING_OFF_MAX_EFFORT, effort)
        return None, False
    if mode == "between_tools":
        logger.info("Claude %s: thinking.disabled sent as between_tools", model)
    return {"type": mode}, True


def claude_thinking_plan(
    model: str, *, thinking: dict | None, effort: str | None, minimal: bool
) -> ClaudeThinkingPlan:
    """這次的 thinking 與 effort（r55）；優先序：客戶端明確值＞用途少想（minimal）＞廠商預設。

    effort 只送給規則表列了它的型號（未知等級、舊型號丟掉並記 warning）；客戶端的 disabled 依
    ``thinking_off_mode`` 改寫或不送；minimal 補最低 effort，能關思考且客戶端沒指定思考時就關。
    舊型號（``thinking_off_mode`` 與 ``effort_levels`` 皆空）＝原本的 ``normalize_thinking``。
    """
    rules = claude_rules(model)
    sent = normalize_thinking(model, thinking) if thinking is not None else None
    if effort is not None and effort not in rules.effort_levels:
        logger.warning("Claude effort %r is not sent to %s (the model does not take it)",
                       effort, model)
        effort = None
    if minimal and effort is None and rules.effort_levels:
        effort = rules.effort_levels[0]
    mode = rules.thinking_off_mode
    if sent is not None and sent.get("type") == "disabled" and mode is not None:
        sent, thinking_off = _client_thinking_off(model, mode, effort)
        return ClaudeThinkingPlan(sent, effort, thinking_off)
    if sent is None and minimal and mode in _TURNS_OFF and _turns_off_at(effort):
        return ClaudeThinkingPlan({"type": mode}, effort, True)
    return ClaudeThinkingPlan(sent, effort, False)


def claude_send_max_tokens(
    model: str, requested: int | None, thinking_budget: int | None, *, thinking_off: bool = False
) -> int:
    """送出的 max_tokens：請求值，否則規則表預設（非串流上限在送出當下才壓）。

    帶 budget 思考時至少 budget＋``THINKING_MIN_VISIBLE_TOKENS``（請求值不夠就提高 max_tokens，
    budget 不動；預算本身也不低於廠商下限 1,024）。default_on 思考的型號至少
    ``DEFAULT_ON_THINKING_MIN_MAX_TOKENS``：小額度會被思考吃光；只有思考真的關掉
    （``thinking_off``，且型號關得掉：Haiku／Sonnet 5.5）才免除，Opus 5.5 永遠有下限。
    """
    rules = claude_rules(model)
    max_tokens = requested if requested is not None else rules.default_max_output or 0
    if thinking_budget is not None:
        max_tokens = max(max_tokens, thinking_budget + THINKING_MIN_VISIBLE_TOKENS)
    turned_off = thinking_off and rules.thinking_off_mode in _TURNS_OFF
    if rules.thinking == "default_on" and not turned_off:
        max_tokens = max(max_tokens, DEFAULT_ON_THINKING_MIN_MAX_TOKENS)
    return max_tokens


def claude_nonstream_cap(model: str, max_tokens: int) -> int:
    """主迴圈非串流送出時壓到 SDK 收得下的上限（最小 8,192＞budget 4,000）；壓低時記 info。"""
    cap = claude_rules(model).nonstreaming_max_tokens or CLAUDE_NONSTREAMING_MAX_TOKENS
    if max_tokens > cap:
        logger.info(
            "Claude max_tokens %d lowered to non-streaming cap %d for %s", max_tokens, cap, model
        )
        return cap
    return max_tokens

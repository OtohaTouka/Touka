"""Provider Adapter 合約（react-core-extraction；`typing.Protocol`＝DIP 鐵則）。

核心（react_core）唯一消費本合約；adapter 之間互不相識。設計不變式：

- ``Turn.raw_assistant`` 對核心**不透明**——歷史寫回一律把 provider 原形交回
  adapter（claude thinking/tool_use blocks、gemini parts 的 id 配對不得被核心
  以正規化資料重建）。
- ``send_round`` 失敗一律回 ``RoundError(user_message)``（不拋例外）——訊息文字
  屬 wire 方言（各供應商錯誤格式不同），核心只負責 yield＋收流。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from src.models.interjection import Interjection

    from .usage import PartialUsage

# 正規化停止原因（ADR-0244）：end＝模型自己正常收尾（含工具輪）；max_tokens＝輸出被截斷；
# refusal＝模型拒答；safety＝廠商安全過濾；error＝其他一切（未知值、缺欄、形狀異常）。
StopReason = Literal["end", "max_tokens", "refusal", "safety", "error"]

# 思考強度（r55）：只讓 Claude 5.5 少想；不影響路由、工具、權限、花費上限、身分。主迴圈由
# routers/proxy.py、complete_once 由用途表放進 adapter context 的同一個鍵，只有 ClaudeAdapter 讀。
ThinkingProfile = Literal["minimal"]
THINKING_PROFILE_KEY = "thinking_profile"

# 插話範本（ADR-0249 第 4a 段）：事實陳述，四家共用；多則依序各成一段。
INTERJECTION_TEMPLATE = "使用者在工具執行期間補充：{text}"
# 第 4b 段：Ctrl+Enter 打斷回答（或一輪開始前就搶先）時用這句（4a 裁定 6：事實陳述）
INTERRUPT_TEMPLATE = "使用者打斷了正在寫的回答並補充：{text}"


def interjection_text(items: Iterable[Interjection], *, interrupted: bool = False) -> str:
    """把取出的插話照範本組成一段文字（各家共用）；``interrupted`` 用打斷的範本。"""
    template = INTERRUPT_TEMPLATE if interrupted else INTERJECTION_TEMPLATE
    return "\n\n".join(template.format(text=item.text) for item in items)


def hold_key(holder: dict[str, Any], key: str) -> Callable[[], None]:
    """記下 ``holder[key]`` 的現值（或不存在），回傳還原函式（hold_tool_choice 用）。"""
    had, old = key in holder, copy.deepcopy(holder.get(key))

    def restore() -> None:
        if had:
            holder[key] = old
        else:
            holder.pop(key, None)

    return restore


def hold_tool_setting(
    holder: dict[str, Any], key: str, apply: Callable[[], None]
) -> Callable[[], None]:
    """暫時套用 ``apply``（設 tool_choice）並回傳還原函式；這一輪沒有 tools 時什麼都不改。

    沒有 tools 卻送 tool_choice 會被廠商拒絕（M9-R1a）；四家的 tools 和 tool_choice 在同一個 dict。
    """
    if not holder.get("tools"):
        return lambda: None
    restore = hold_key(holder, key)
    apply()
    return restore


def only_keys(messages: list[Any], keys: frozenset[str]) -> list[Any]:
    """外送前逐則白名單：每則 dict 訊息只留 ``keys`` 的欄位（新 dict；非 dict 原樣）。"""
    return [{k: v for k, v in m.items() if k in keys} if isinstance(m, dict) else m
            for m in messages]


@dataclass
class ToolCall:
    """正規化的工具呼叫（fan-out 用；歷史寫回不用它——見 raw_assistant 不變式）。"""

    id: str | None
    name: str
    args: dict


@dataclass
class Turn:
    """一輪模型回合的正規化視圖。"""

    reasoning_text: str = ""  # 全部 reasoning（joined；資訊性視圖）
    # 逐段 reasoning（核心逐段 yield＝保真 claude 多 thinking block 的 chunk 邊界；
    # openai 單段、gemini 無〔拍板 K3〕）。空 list＝本輪無 reasoning。
    reasoning_parts: list[str] = field(default_factory=list)
    visible_text: str = ""  # 全部可見文字（joined；guard/累積用）
    # 逐段可見文字（核心逐段 yield＝保真 gemini 多 text part 的 chunk 邊界；
    # openai/claude 單段）。空 list＝本輪無文字。
    text_parts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_assistant: Any = None  # provider 原形 assistant 訊息（歷史寫回專用、不透明）
    empty: bool = False  # 無 choices/candidates（上游異常形狀）→ 核心以 raw 作結
    refusal_text: str = ""  # 模型自己給的拒答文字（OpenAI／Responses；核心收尾時顯示）


@dataclass
class MidLoopAction:
    """mid_loop_hook 的指示：先 yield deltas、再重入迴圈（不 break）。"""

    deltas: list[str] = field(default_factory=list)


@dataclass
class RoundError:
    """上游回合失敗（傳輸/非 200）：user_message 為 wire 方言的使用者可read訊息。

    可重試時 adapter 另標 ``retry_note``（重試狀態文字的前半）與 ``retry_after``（秒；
    廠商要求的等待），由核心退避重送（3b-U5a，見 retry.py）；兩欄不參與比較。
    """

    user_message: str
    retry_note: str | None = field(default=None, compare=False)
    retry_after: float | None = field(default=None, compare=False)


class ProviderAdapter(Protocol):
    """供應商 wire 職責合約（契約測試＝tests/unit/test_adapter_contract.py）。"""

    # token_usage 遙測事件是否附 context_breakdown（分解 v1 僅 Claude 有值——
    # ADR-0092 Phase B；餘家 False＝事件形狀凍結不變）。
    emits_context_breakdown: bool

    async def prepare_request(self, request: Any, context: dict[str, Any]) -> Any:
        """整備 provider payload → 回傳 state（core 只透傳、不窺探其內部）。"""
        ...

    def send_round(self, state: Any) -> Any:
        """送出一輪（async generator）。

        可先 yield SSE 事件字串（如 503 重試的 touka_status，維持即時性），
        終端 yield 一個非 str 項＝raw response 或 RoundError。核心逐項轉發
        字串、以最後的非 str 項為本輪結果。
        """
        ...

    def parse_round(self, state: Any, raw: Any) -> Turn:
        """解析 raw → 正規化 Turn。

        reasoning 分頻方言、合成 call_id、跨輪文字累積等每請求狀態歸 state。
        """
        ...

    def append_assistant_turn(self, state: Any, turn: Turn) -> None:
        """把本輪 assistant 原形寫回歷史（raw 透傳不變式）。"""
        ...

    async def append_tool_results(
        self, state: Any, results: list[tuple[str | None, str, str]]
    ) -> None:
        """把工具結果（call_id, name, text）寫回歷史（wire 方言形狀）。

        async 介面保留給需要在此做 I/O 的方言；gemini 於此處做條件式
        tool_config AUTO 重置；assistant 原形若該方言慣例是「與結果同時寫回」
        （gemini），append_assistant_turn 可為 no-op、由本方法一併寫回。
        """
        ...

    def build_denied_turn(self, state: Any, turn: Turn, reason: str) -> None:
        """耗盡輪：assistant 原形＋denied 佔位結果＋WRAP_UP_NOTE 寫回歷史。"""
        ...

    def set_tool_choice(self, state: Any, mode: str) -> None:
        """設定下一輪工具許可（"auto"/"none"；gemini 另有 "any"）。"""
        ...

    def append_interjection(self, state: Any, items: list[Interjection], *,
                            interrupted: bool = False) -> None:
        """把使用者插話接在最後一則使用者側訊息之後（ADR-0249；形狀見各家方言）。

        第 4a 段是這一輪的工具結果；第 4b 段被 Ctrl+Enter 打斷時也可能是原本的提問，
        這時 ``interrupted=True``，文字改用 ``INTERRUPT_TEMPLATE``。
        """
        ...

    def hold_tool_choice(self, state: Any, mode: str) -> Callable[[], None]:
        """這一輪暫時改成 ``mode``，回傳還原原本值的函式（插話 urgent，ADR-0249）。

        這一輪沒有 tools 時什麼都不改（M9-R1a）。
        """
        ...

    def extract_usage(self, state: Any, raw: Any) -> tuple[dict, int, int]:
        """擷取 raw → ``(四欄 usage dict, 本輪總輸入, 輸出)``（ADR-0244 用量正規化）。

        四欄一律經 ``usage.normalized_usage`` 組出，鍵名＝token_usage.json 既有鍵：
        ``prompt_tokens``＝未快取輸入、``cache_read_input_tokens``＝快取讀、
        ``cache_creation_input_tokens``＝快取寫、``completion_tokens``＝輸出。
        本輪總輸入＝三種輸入相加（context 佔用）。

        核心每輪 parse 後就以此入帳（ADR-0244）。空 usage dict＝上游沒回報用量，該輪不入帳。
        """
        ...

    def partial_usage(self, state: Any) -> PartialUsage | None:
        """一輪沒完成（RoundError、取消）時要記的用量（3b 裁定 6）；串流沒開始或非串流＝None。"""
        ...

    def emits_usage_event(self, state: Any) -> bool:
        """是否送 Touka 的 token_usage SSE 事件（ADR-0244）。

        claude 的 native Anthropic egress 回 False：照常入帳，但串流不多出 Touka 事件
        （原生出口 wire 不變）；其餘回 True。
        """
        ...

    def stop_reason(self, state: Any, raw: Any) -> StopReason:
        """把 raw 正規化成停止原因（ADR-0244；complete_once 據此判斷成敗）。

        拒答與截斷**不得**回 ``end``；廠商回報的值不在對照表內、缺欄或形狀異常，
        一律回 ``error``（fail-closed）。
        """
        ...

    def telemetry_model(self, state: Any, request: Any) -> str:
        """遙測/計費用模型名（accumulate/audit/estimate_cost/context_breakdown）。

        claude＝ingress 轉換**後**的 kwargs.model（轉小寫、去 -thinking 的實名）；
        openai/gemini＝request.model（凍結舊制）。
        """
        ...

    def extract_final_text(self, state: Any, raw: Any) -> str:
        """防幻覺檢查的輸入文字（openai＝終端回應；gemini＝跨輪累積文字，方言各異）。"""
        ...

    def mid_loop_hook(self, state: Any, turn: Turn, actually_used_tools: set[str],
                      allow_react: bool) -> MidLoopAction | None:
        """純文字輪收尾前的方言介入點（gemini Honesty Guard 專用，拍板 K2）。

        回 MidLoopAction＝核心先 yield 其 deltas、然後 continue（不 break）；
        回 None＝照常收尾。預設實作應回 None。
        """
        ...

    def finalize(self, state: Any, raw: Any, request_id: str, latency_info: dict) -> dict:
        """包裝 raw → 終端 SSE chunk dict（id/telemetry/finish_reason 重整形）。"""
        ...

    def context_breakdown_args(self, state: Any) -> tuple:
        """compute_context_breakdown 的前三參數（分解 v1 僅 Claude，餘家回 None 三元組）。"""
        ...

    async def close(self, state: Any) -> None:
        """釋放傳輸資源（httpx client 等）；核心以 finally 保證呼叫。"""
        ...

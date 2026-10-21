"""Anthropic Claude 供應商 adapter（react-core-extraction E3）。

自 claude_handler 逐段移植：僅 wire 職責——雙 ingress（native Anthropic 形狀
直通／OpenAI 形狀經 convert_openai_to_anthropic，模型名轉小寫＋去 -thinking）／
kwargs 整備（取樣參數一律不送；thinking 依每模型規則表）／degraded 拼接／四斷點
prompt 快取（ADR-0127 工具斷點＋ADR-0128/0129/0130 system 重排與切分＋迴圈內
tool_result ephemeral 輪替）／上游串流（ADR-0248）／native thinking → reasoning 分頻（ADR-0152；
逐 block＝Turn.reasoning_parts）／Honesty Guard（ADR-0157 K2 推廣三家；thinking
啟用或 5.x 以後不強制 tool_choice＝Anthropic 400）／雙 egress（native＝Anthropic 終端
形狀，照常入帳但**不送 token_usage 事件**〔ADR-0244〕；否則 convert_anthropic_to_openai
重整形）。5s SSE keepalive 已升核心單一實作（ADR-0157 K1＝react_core.pump_send_round）。
行為凍結由特徵化 golden 把關（tests/proxy/characterization/test_golden_claude.py）。

已知刻意微註（文件化）：
- 舊 handler 把 thinking 文字以 ``<thinking>`` 包裝累積進 all_thinking_and_tool_logs
  ——該變數為 write-only（無任何讀取點）＝零可觀測行為，不再複製。
- 舊 handler 以 content block 順序交錯發射 thinking/text；核心改「reasoning 全部
  先於 text」。Anthropic 現行 wire 形狀 thinking 恆在 text 之前（未啟用
  interleaved-thinking beta）＝生產順序不變。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anthropic import APIConnectionError, APIStatusError

from src.adapters import tool_schema
from src.adapters.claude_to_openai import convert_anthropic_to_openai
from src.adapters.openai_to_claude import claude_stop_sequences, convert_openai_to_anthropic
from src.services.guard_service import guard_service
from src.services.models.model_rules import (
    allows_forced_tool_choice,
    claude_nonstream_cap,
    claude_rules,
    claude_send_max_tokens,
    claude_thinking_plan,
)
from src.services.prompt_blocks import VOLATILE_SYSTEM_TAGS
from src.services.rhythm_service import WRAP_UP_NOTE, build_denied_tool_result

from . import retry
from .base import (
    THINKING_PROFILE_KEY,
    MidLoopAction,
    RoundError,
    ToolCall,
    Turn,
    hold_tool_setting,
    interjection_text,
    only_keys,
)
from .stream_timeout import RoundClock
from .usage import estimate_partial_usage, normalized_usage, usage_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.interjection import Interjection

    from .base import StopReason
    from .usage import PartialUsage

logger = logging.getLogger(__name__)

_API_ERROR = "❌ **API 發生錯誤**\n\n`{}`"  # 下游錯誤文字格式（golden 釘住）
# 每則訊息只送 Anthropic Messages 規格欄位（＝AnthropicMessage，ADR-0260）；原生入口
# model_dump 直送，程式內插入的 dict 多帶的鍵在這裡擋掉（OpenAI 入口的轉換器本來就只取這兩個）。
_MESSAGE_KEYS = frozenset({"role", "content"})

# ADR-0244：Anthropic stop_reason → 正規化停止原因；表外（pause_turn、缺欄、未知）＝error。
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "end",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}


def _strip_breakpoints(content: Any) -> None:
    """就地清掉訊息內容區塊（遞迴含 tool_result 內層）的 ``cache_control``。"""
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            block.pop("cache_control", None)
            _strip_breakpoints(block.get("content"))


def _apply_thinking_plan(kwargs: dict, request: Any, api_params: dict, *, native: bool,
                         profile: Any) -> None:
    """thinking／effort／max_tokens 照規則表的計畫（r55）；profile 只認 minimal，不讀標頭。

    原生入口的 output_config 只轉送 effort（其他鍵只記鍵名）。max_tokens：原生入口一律、OpenAI
    入口只在思考真的關掉時，以請求原值重算（轉換器已把小值拉到 default_on 下限）。
    """
    output_config = api_params.get("output_config") or {}
    ignored = sorted(set(output_config) - {"effort"})
    if ignored:
        logger.warning("Native output_config keys are not forwarded: %r", ignored)
    plan = claude_thinking_plan(
        kwargs["model"], thinking=api_params.get("thinking"),
        effort=output_config.get("effort"), minimal=profile == "minimal",
    )
    if plan.thinking is not None:
        kwargs["thinking"] = plan.thinking
    if plan.effort is not None:
        kwargs["output_config"] = {"effort": plan.effort}
    if native or plan.thinking_off:
        requested = api_params["max_tokens"] if native else request.max_tokens
        budget = (plan.thinking or {}).get("budget_tokens")
        kwargs["max_tokens"] = claude_send_max_tokens(
            kwargs["model"], requested, budget, thinking_off=plan.thinking_off
        )


# 有內容的事件：之後才照閒置計時（message_start、ping 是空殼，R-3b ② M2）
_CONTENT_EVENTS = frozenset({"content_block_delta", "message_delta", "message_stop"})


@dataclass
class _StreamProgress:
    """主迴圈串流這一輪收到了什麼；沒完成時據此記用量（3b 裁定 6）。"""

    opened: bool = False  # 已收到 HTTP 200；之前失敗（狀態錯誤、連不上）沒有可記的用量
    complete: bool = False  # 收到 message_stop＝完整（ADR-0248 附註 7）
    reported_input: tuple[int, int, int] | None = None  # message_start：未快取／快取讀／快取寫
    reported_output: int | None = None  # message_delta 的累計輸出
    received: list[str] = field(default_factory=list)  # 已收到的文字、思考、工具參數片段

    def observe(self, event: Any) -> None:
        """記下一個 SDK 串流事件（其他事件型別不影響用量）。"""
        if event.type == "message_start":
            u = event.message.usage
            self.reported_input = (usage_count(u.input_tokens),
                                   usage_count(u.cache_read_input_tokens),
                                   usage_count(u.cache_creation_input_tokens))
        elif event.type == "content_block_delta":
            pieces = (getattr(event.delta, a, None) for a in ("text", "thinking", "partial_json"))
            self.received += [piece for piece in pieces if isinstance(piece, str)]
        elif event.type == "message_delta":
            self.reported_output = usage_count(event.usage.output_tokens)
        elif event.type == "message_stop":
            self.complete = True


@dataclass
class _State:
    """Claude adapter 的回合狀態（core 不窺探內部）。"""

    kwargs: dict  # anthropic_client.messages.create 的參數（payload＝SSOT）
    client: Any  # AsyncAnthropic——factory/context 持有共用連線池，本層不建不關
    is_native_anthropic: bool  # 雙 ingress/egress 分流旗標
    honesty_corrected: bool = False  # ADR-0157：誠實矯正一次性閂鎖（防矯正迴圈）
    honesty_forced: bool = False  # ADR-0157：矯正輪曾強制 tool_choice（工具輪後解除）
    stream: bool = False  # ADR-0248 D2：送出當下才選 stream()／create()，不寫進 kwargs
    progress: _StreamProgress | None = None  # 目前這一輪串流（非串流路徑恆為 None）

    @property
    def chunk_model(self) -> str:
        """SSE chunk 的 model 欄位（沿用 kwargs 的 model，與移植前一致）。"""
        return self.kwargs.get("model", "claude-3-5-sonnet")


class ClaudeAdapter:
    """Anthropic Claude wire adapter（契約：tests/unit/test_adapter_contract.py）。"""

    emits_context_breakdown = True  # 分解 v1 僅 Claude（ADR-0092 Phase B）

    def __init__(self, *, stream: bool = False) -> None:
        self._stream = stream  # ADR-0248 D1：預設非串流（complete_once）；主迴圈明確打開

    async def prepare_request(self, request: Any, context: dict[str, Any]) -> _State:
        """整備 Anthropic kwargs（雙 ingress/參數夾制/degraded/四斷點快取）。"""
        is_native_anthropic = "anthropic" in str(type(request)).lower()
        anthropic_req = request if is_native_anthropic else convert_openai_to_anthropic(request)

        api_params = anthropic_req.model_dump(exclude_none=True)
        # 移除 Claude 不支援的 Grok/Gemini 性安全參數
        api_params.pop("safety_mode", None)
        api_params.pop("safety_settings", None)

        # model-p0（Owner 決定 A）：temperature／top_p／top_k 一律不送（廠商預設；
        # 4.7 以後送了即 400）。請求欄位保留，舊設定照常載入、值被忽略。
        kwargs = {
            "model": api_params["model"],
            "max_tokens": api_params["max_tokens"],
            "messages": only_keys(api_params["messages"], _MESSAGE_KEYS),
        }
        if "system" in api_params:
            kwargs["system"] = api_params["system"]

        # 注入生存級警告至 System Prompt
        if context.get("system_degraded", False):
            warning = (
                f"\n\n⚠️ SYSTEM WARNING: The proxy is running in DEGRADED MODE. "
                f"{context.get('degraded_reason', '')} Some tools may be offline. "
                f"Please inform the user."
            )
            if "system" in kwargs:
                if isinstance(kwargs["system"], list):
                    kwargs["system"].append({"type": "text", "text": warning})
                else:
                    kwargs["system"] += warning
            else:
                kwargs["system"] = warning

        # 軌C：request.tools 為 canonical OpenAI → 出場轉成 Anthropic（冪等：已是
        # Anthropic 則直通）。無有效工具則省略 tools（不送 tools=[]）。
        _anthropic_tools = tool_schema.openai_to_anthropic_tools(api_params.get("tools"))
        if _anthropic_tools:
            # P4（ADR-0127 快取前綴優化）：對最後一個工具加 Anthropic 快取斷點——
            # 快取穩定的工具 schema 前綴（ReAct 迴圈重複命中）。
            kwargs["tools"] = tool_schema.apply_tool_cache_breakpoint(_anthropic_tools)
        # P4b/P4c/P4d（ADR-0128/0129/0130）：先把波動區塊搬到尾端（穩定工具區塊連成
        # 前綴），再於最早波動區塊切分成帶快取斷點的 block list——穩定前綴跨回合命中、
        # 波動後綴迴圈內命中（context_breakdown._system_to_text 相容 list，遙測不受影響）。
        if kwargs.get("system"):
            _reordered_system = tool_schema.reorder_system_stable_first(
                kwargs["system"], volatile_tags=VOLATILE_SYSTEM_TAGS
            )
            kwargs["system"] = tool_schema.to_cached_system(
                _reordered_system, volatile_tags=VOLATILE_SYSTEM_TAGS
            )
        # model-p0：budget_tokens 依模型改 adaptive 或拿掉（每模型規則表，ADR-0247）；r55：思考強度
        _apply_thinking_plan(kwargs, request, api_params, native=is_native_anthropic,
                             profile=context.get(THINKING_PROFILE_KEY))
        if not is_native_anthropic:  # r55：OpenAI 入口的 stop 照轉（原生入口 schema 沒有這欄）
            stop_sequences = claude_stop_sequences(getattr(request, "stop", None))
            if stop_sequences:
                kwargs["stop_sequences"] = stop_sequences

        return _State(
            kwargs=kwargs,
            client=context.get("anthropic_client"),
            is_native_anthropic=is_native_anthropic,
            stream=self._stream and claude_rules(kwargs["model"]).stream,  # D6：規則表開關
        )

    async def send_round(self, state: _State) -> Any:
        """送出一輪：非串流＝create；串流＝stream() 彙整成同形 Message（逾時區塊不跨 yield）。"""
        if not state.stream:
            kwargs = dict(state.kwargs)  # 主迴圈關掉串流時，送出當下才壓上限（附註 4）
            if self._stream:
                kwargs["max_tokens"] = claude_nonstream_cap(kwargs["model"], kwargs["max_tokens"])
            try:
                yield await state.client.messages.create(**kwargs)
            except Exception as e:
                yield _sdk_error(e, stream=False)
            return
        clock = RoundClock(wait_for_content=True)  # message_start 不算首事件（R-3b ② M2）
        try:
            async with clock.deadline():
                result = await self._stream_once(state, clock)
        except TimeoutError:
            result = retry.on_timeout(RoundError(_API_ERROR.format(clock.timeout_text)), clock)
        except Exception as e:
            result = _sdk_error(e, opened=bool(state.progress and state.progress.opened))
        yield result

    async def _stream_once(self, state: _State, clock: RoundClock) -> Any:
        """彙整一輪串流；沒收到 message_stop 就結束＝RoundError，不回半輪（附註 7）。"""
        progress = state.progress = _StreamProgress()
        async with state.client.messages.stream(**state.kwargs) as stream:
            progress.opened = True
            async for event in stream:
                progress.observe(event)
                if event.type in _CONTENT_EVENTS:
                    clock.content()
            if not progress.complete:
                return RoundError(_API_ERROR.format("上游串流沒送完就中斷（缺 message_stop）"))
            return await stream.get_final_message()

    def partial_usage(self, state: _State) -> PartialUsage | None:
        """串流沒完成時要記的用量（裁定 6）；完整的一輪核心已入帳，回 None 免得取消時重記。"""
        progress = state.progress
        if progress is None or not progress.opened or progress.complete:
            return None
        return estimate_partial_usage(
            request={key: state.kwargs.get(key) for key in ("system", "tools", "messages")},
            output_text="".join(progress.received),
            reported_input=progress.reported_input,
            reported_output=progress.reported_output,
        )

    def parse_round(self, _state: _State, raw: Any) -> Turn:
        """解析 content blocks → Turn（thinking 逐 block 分頻＝ADR-0152）。"""
        reasoning_parts: list[str] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.content:
            if block.type == "thinking":
                reasoning_parts.append(block.thinking)
            elif block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=block.input))
        return Turn(
            reasoning_text="\n".join(reasoning_parts),
            reasoning_parts=reasoning_parts,
            visible_text="".join(text_parts),
            text_parts=text_parts,
            tool_calls=tool_calls,
            raw_assistant=raw.content,  # SDK block 物件原形（歷史寫回透傳不變式）
        )

    def append_assistant_turn(self, state: _State, turn: Turn) -> None:
        """把 assistant 原形 blocks 寫回歷史（SDK 物件透傳，resend 相容）。"""
        state.kwargs["messages"].append({"role": "assistant", "content": turn.raw_assistant})

    async def append_tool_results(
        self, state: _State, results: list[tuple[str | None, str, str]]
    ) -> None:
        """工具結果以 tool_result user message 寫回＋ephemeral 快取斷點輪替。"""
        user_msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": [{"type": "text", "text": text}],
                }
                for call_id, _name, text in results
            ],
        }

        # [安全修正] 防止超過 Anthropic 4 個快取斷點的限制（tools 1＋system ≤2＋訊息 1）：
        # 1. 清掉先前所有 user 訊息的斷點——含呼叫端放在非最後一塊或 tool_result 內層的
        #    （ADR-0247；只清最後一塊會讓原生入口出現第 5 個而被 400）。system／tools 不動。
        for prev_msg in state.kwargs["messages"]:
            if prev_msg.get("role") == "user":
                _strip_breakpoints(prev_msg.get("content"))

        # 2. 標記最新的 tool result 塊（新的第 4 個斷點；新對話的首個工具輪也標，ADR-0247）
        user_msg["content"][-1]["cache_control"] = {"type": "ephemeral"}

        state.kwargs["messages"].append(user_msg)

        # ADR-0157 K2：誠實矯正輪的強制 tool_choice 用畢即解除（只解除自己設的，
        # 不碰呼叫端原生 tool_choice；不解除＝之後每輪都被迫調工具直到額度耗盡）。
        if state.honesty_forced:
            state.kwargs["tool_choice"] = {"type": "auto"}
            state.honesty_forced = False

    def build_denied_turn(self, state: _State, turn: Turn, reason: str) -> None:
        """耗盡輪：assistant 原形＋denied tool_result＋WRAP_UP text（同一 user 訊息）。"""
        state.kwargs["messages"].append({"role": "assistant", "content": turn.raw_assistant})
        denied_results: list[dict] = [
            {
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": [{"type": "text", "text": build_denied_tool_result(reason)}],
            }
            for tc in turn.tool_calls
        ]
        denied_results.append({"type": "text", "text": WRAP_UP_NOTE})
        state.kwargs["messages"].append({"role": "user", "content": denied_results})

    def set_tool_choice(self, state: _State, mode: str) -> None:
        """Anthropic 方言：tool_choice 為 dict 形狀 {"type": mode}。"""
        state.kwargs["tool_choice"] = {"type": mode}

    def append_interjection(self, state: _State, items: list[Interjection], *,
                            interrupted: bool = False) -> None:
        """插話（ADR-0249）：照規則表放 system 訊息或 tool_result 後的 text block。

        ``mid_conversation_system`` 為 True 時工具結果後放 system 訊息（最後一則已是 system 就
        併進那一則，不連兩則）；否則接在同一則 user 訊息
        最後一個 tool_result 之後（第 4b 段打斷時是原本的提問之後）。不產生連續兩則 user、
        不加 cache_control。
        """
        text = interjection_text(items, interrupted=interrupted)
        messages = state.kwargs["messages"]
        if claude_rules(state.kwargs["model"]).mid_conversation_system:
            last = messages[-1]
            if last.get("role") == "system" and isinstance(last.get("content"), str):
                last["content"] += "\n\n" + text  # 中途 system 不能連兩則（M9-R1a）
            else:
                messages.append({"role": "system", "content": text})
            return
        if messages[-1].get("role") != "user":  # 最後一則一定是使用者側，正常走不到
            messages.append({"role": "user", "content": []})
        last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": text})

    def hold_tool_choice(self, state: _State, mode: str) -> Callable[[], None]:
        """這一輪暫時改成 mode，回傳還原函式（原本沒設就拿掉）。"""
        return hold_tool_setting(state.kwargs, "tool_choice",
                                 lambda: self.set_tool_choice(state, mode))

    def mid_loop_hook(self, state: _State, turn: Turn, actually_used_tools: set[str],
                      allow_react: bool) -> MidLoopAction | None:
        """Honesty Guard（ADR-0157 K2＝三家推廣）：沒查資料就瞎掰 → 注入矯正輪。

        claude 方言：assistant 原形 blocks＋矯正 user 訊息寫回、`{"type": "any"}`
        強制下一輪查證——**thinking 啟用或 5.x 以後不強制**（強制即 400；
        每模型規則表），僅靠矯正提示引導；一次性閂鎖、工具輪後強制解除
        （見 append_tool_results）。
        """
        if state.honesty_corrected or not (
            allow_react
            and guard_service.check_fact_without_tools(turn.visible_text, actually_used_tools)
        ):
            return None
        state.honesty_corrected = True
        logger.warning("Honesty Guard triggered (Anthropic): fact mentioned without tool call.")
        state.kwargs["messages"].append({"role": "assistant", "content": turn.raw_assistant})
        state.kwargs["messages"].append(
            {"role": "user", "content": guard_service.get_honesty_correction_prompt()}
        )
        if allows_forced_tool_choice(state.kwargs["model"], "thinking" in state.kwargs):
            state.kwargs["tool_choice"] = {"type": "any"}
            state.honesty_forced = True
        return MidLoopAction(deltas=[guard_service.get_honesty_warning_message()])

    def extract_usage(self, _state: _State, raw: Any) -> tuple[dict, int, int]:
        """SDK usage → 四欄正規化（input_tokens 本來就不含快取；SDK 的 None 一律 0）。

        native egress 也照常入帳（ADR-0244）；不送畫面事件由 emits_usage_event 決定。
        """
        usage = getattr(raw, "usage", None)
        if not usage:
            return ({}, 0, 0)
        usage_m = usage.model_dump()
        return normalized_usage(
            uncached_input=usage_count(usage_m.get("input_tokens")),
            cache_read=usage_count(usage_m.get("cache_read_input_tokens")),
            cache_write=usage_count(usage_m.get("cache_creation_input_tokens")),
            output=usage_count(usage_m.get("output_tokens")),
        )

    def stop_reason(self, _state: _State, raw: Any) -> StopReason:
        """Anthropic ``stop_reason`` → 正規化（ADR-0244；表外一律 error）。"""
        value = getattr(raw, "stop_reason", None)
        return _STOP_REASONS.get(value, "error") if isinstance(value, str) else "error"

    def emits_usage_event(self, state: _State) -> bool:
        """原生 Anthropic 出口（native egress）不送 token_usage 事件（wire 不變，ADR-0244）。"""
        return not state.is_native_anthropic

    def extract_final_text(self, _state: _State, raw: Any) -> str:
        """終端回應全部可見文字（防幻覺檢查輸入；claude＝讀終端 text blocks）。"""
        return "".join(b.text for b in (getattr(raw, "content", None) or []) if b.type == "text")

    def finalize(self, state: _State, raw: Any, request_id: str,
                 latency_info: dict) -> dict:
        """雙 egress：native＝Anthropic 形狀（正文清空防重複）；否則 OpenAI 重整形。"""
        if state.is_native_anthropic:
            anth_resp = raw.model_dump()
            anth_resp["telemetry"] = latency_info
            # 清空 content 以避免重複顯示已 yield 的部分
            for item in anth_resp.get("content", []):
                if item.get("type") == "text":
                    item["text"] = ""
            return anth_resp

        openai_resp = convert_anthropic_to_openai(raw.model_dump())
        openai_resp["id"] = f"chatcmpl-{request_id}"
        openai_resp["telemetry"] = latency_info
        openai_resp["object"] = "chat.completion.chunk"
        if "choices" in openai_resp and len(openai_resp["choices"]) > 0:
            choice = openai_resp["choices"][0]
            choice["delta"] = {"content": ""}
            choice["finish_reason"] = "stop"
            if "message" in choice:
                del choice["message"]
        return openai_resp

    def context_breakdown_args(self, state: _State) -> tuple:
        """Context 分解參數（ADR-0092 Phase B；分解 v1＝Claude 供實值）。"""
        return (
            state.kwargs.get("system"),
            state.kwargs.get("tools"),
            state.kwargs.get("messages"),
        )

    def telemetry_model(self, state: _State, _request: Any) -> str:
        """遙測/計費用模型名＝ingress 轉換後的 kwargs.model（轉小寫、去 -thinking 後的實名）。"""
        return state.kwargs.get("model", "")

    def resolve_provider_label(self, _state: _State) -> str:
        """遙測用供應商標籤（claude 方言＝固定字面）。"""
        return "Anthropic"

    async def close(self, _state: _State) -> None:
        """No-op：Anthropic client 由 factory/context 持有共用連線池，本層不得關閉。"""


def _sdk_error(error: Exception, *, opened: bool = False, stream: bool = True) -> RoundError:
    """SDK 例外 → 下游錯誤文字（``str(e)``，golden 釘住）；可重試的另標記（3b-U5a、R-3b ② M1）。"""
    result = RoundError(_API_ERROR.format(str(error)))
    if isinstance(error, APIConnectionError) and not stream:  # 非串流只重試連不上（R-3b ② N1）
        return retry.on_post_failure(result, error, "Claude connection error")
    if isinstance(error, APIConnectionError):  # 含 APITimeoutError：串流沒收到 200 才可重試
        return retry.on_connect(result, opened, "Claude connection error")
    if not isinstance(error, APIStatusError):
        return result
    if opened:  # 200 之後的 error 事件（SDK 包成狀態 200）：看本文的錯誤型別
        body = error.body.get("error", error.body) if isinstance(error.body, dict) else None
        return retry.on_error_body(result, body, "Claude")
    return retry.on_status(result, error.status_code, error.response.headers,
                           f"Claude busy ({error.status_code})")

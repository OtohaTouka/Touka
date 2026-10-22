"""OpenAI 相容供應商 adapter（Grok/DeepSeek/OpenAI/自架相容端點）。

自 openai_handler 逐段移植（react-core-extraction E1）：僅 wire 職責——
request 整備／httpx 傳輸與錯誤文字／choices 解析與 reasoning 分頻方言／
role:"tool" 歷史形狀／終端 chunk 重整形。行為與移植前逐位元一致
（特徵化 golden 把關）。主迴圈對已知廠商走自寫 SSE 串流（ADR-0248；``[DONE]``＝完整），
彙整成同形回應；自訂連線與 complete_once 照舊非串流、只送 1 次。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from src.adapters.content_normalize import normalize_message_content
from src.services.guard_service import guard_service
from src.services.models.model_rules import COMPAT_STREAM_PROVIDERS, rules_for
from src.services.rhythm_service import WRAP_UP_NOTE, build_denied_tool_result
from src.utils.reasoning import split_reasoning

from . import retry
from .base import (
    MidLoopAction,
    RoundError,
    ToolCall,
    Turn,
    hold_tool_setting,
    interjection_text,
    only_keys,
)
from .sampling import SAMPLING_PARAM_NAMES
from .sse_stream import ChatStreamAggregator, StreamShapeError, sse_data
from .stream_timeout import FIRST_EVENT_TIMEOUT_S, RoundClock
from .usage import estimate_partial_usage, normalized_usage, split_included_cache, usage_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.interjection import Interjection

    from .base import StopReason
    from .usage import PartialUsage

logger = logging.getLogger(__name__)

# ADR-0244：OpenAI 相容 finish_reason → 正規化停止原因；表外（缺欄、廠商自訂值）＝error。
_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "end",
    "function_call": "end",
    "length": "max_tokens",
    "content_filter": "safety",
}
# KF-8（ADR-0242）：Touka 內部欄位與 MCP 工具的非標準鍵不外送；工具全被過濾時不送 tools。
_TOUKA_FIELDS = ("incognito", "deep_research", "mcp_dispatcher", "session_id", "project_id",
                 "auth_level", "ide_context")
_MCP_TOOL_KEYS = ("server", "tool", "category")
# 請求開頭的每則訊息只送這 4 個欄位（ADR-0260）：KF-8 只濾最外層，家對話等程式內插入的 dict
# 會帶 UI 樹欄位。OpenAIMessage 的 name 也濾掉：沒有路徑需要送，留著只會外送說話者標記。
_MESSAGE_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id"})
_STREAM_FIELDS = {"stream": True, "stream_options": {"include_usage": True}}  # 只在送出當下合併


@dataclass
class _Progress:
    """主迴圈串流這一輪收到了什麼（沒完成或沒回報用量時據此記帳，裁定 5／6）。"""

    opened: bool = False  # 已收到 HTTP 200
    complete: bool = False  # 收到 [DONE]
    chunks: ChatStreamAggregator = field(default_factory=ChatStreamAggregator)


@dataclass
class _State:
    """OpenAI adapter 的回合狀態（core 不窺探內部）。"""

    payload: dict
    headers: dict
    url: str | None
    provider: str | None
    api_key: str | None
    honesty_corrected: bool = False  # ADR-0157：誠實矯正一次性閂鎖（防矯正迴圈）
    honesty_forced: bool = False  # ADR-0157：矯正輪曾強制 tool_choice（工具輪後解除）
    stream: bool = False  # 送出當下才合併 stream／stream_options，不寫進 payload（D2）
    forced_tool_choice: bool = True  # 規則表：可否強制 tool_choice（誠實矯正輪用）
    progress: _Progress | None = None  # 目前這一輪串流（非串流路徑恆為 None）
    client: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(follow_redirects=False)  # 禁令 22
    )

    @property
    def chunk_model(self) -> str:
        """SSE chunk 的 model 欄位（沿用 payload 的 model，與移植前一致）。"""
        return self.payload.get("model", "unknown-model")


class OpenAIAdapter:
    """OpenAI 相容 wire adapter（契約：tests/unit/test_adapter_contract.py）。"""

    provider_fallback = "OpenAI"
    emits_context_breakdown = False  # 分解 v1 僅 Claude（事件形狀凍結）

    def __init__(self, *, stream: bool = False) -> None:
        self._stream = stream  # ADR-0248 D1：預設非串流、只送 1 次；主迴圈明確打開

    async def prepare_request(self, request: Any, context: dict[str, Any]) -> _State:
        """整備 OpenAI 相容 payload（多模態還原/參數過濾/degraded 拼接/headers）。"""
        payload = request.model_dump(exclude_none=True)
        if "messages" in payload:
            payload["messages"] = only_keys(payload["messages"], _MESSAGE_KEYS)
        # ToukaChat 夾帶的多模態 JSON 字串 content → 還原為 list（SSOT），否則 OpenAI 相容
        # 供應商會把整串 JSON（含 base64 圖片）當文字、視覺輸入失效。**排除 role=="tool"**：
        # OpenAI Chat 規格要求 tool 訊息 content 為字串，還原成 list 會違反契約。
        for _m in payload.get("messages", []):
            if isinstance(_m, dict) and "content" in _m and _m.get("role") != "tool":
                _m["content"] = normalize_message_content(_m["content"])
        # model-p0（ADR-0241 決定 A）：取樣參數一律不送（Ollama/Grok/DeepSeek/LiteLLM/
        # OpenRouter/自訂連線皆走此處；用廠商預設）。
        for name in SAMPLING_PARAM_NAMES:
            payload.pop(name, None)
        payload.pop("stream", None)
        for name in _TOUKA_FIELDS:  # KF-8a
            payload.pop(name, None)
        if payload.get("tools"):  # KF-8b：另建新 dict，不改到 ToolLayer 的物件
            payload["tools"] = [{k: v for k, v in tool.items() if k not in _MCP_TOOL_KEYS}
                                for tool in payload["tools"]]
        else:  # KF-8c
            payload.pop("tools", None)
        rules = rules_for(payload.get("model", ""))  # 每模型規則表（ADR-0247；3b 接上相容 Chat）
        for name in rules.forbidden_params:
            payload.pop(name, None)
        if "max_tokens" not in payload and rules.default_max_output:
            payload["max_tokens"] = rules.default_max_output
        if payload.get("tools"):
            payload.update(rules.required_params_with_tools)

        provider = context.get("provider")
        # 過濾安全性參數：僅 Grok (xAI) 支援
        if provider != "Grok":
            payload.pop("safety_mode", None)
            payload.pop("safety_settings", None)

        # P5b：DEGRADED 警告注入 system message（與 claude 一致）
        if context.get("system_degraded", False):
            warning = (
                f"\n\n⚠️ SYSTEM WARNING: The proxy is running in DEGRADED MODE. "
                f"{context.get('degraded_reason', '')} Some tools may be offline. "
                f"Please inform the user."
            )
            msgs = payload.get("messages", [])
            sys_msg = next((m for m in msgs if m.get("role") == "system"), None)
            if sys_msg is not None and isinstance(sys_msg.get("content"), str):
                sys_msg["content"] += warning
            else:
                msgs.insert(0, {"role": "system", "content": warning.strip()})
                payload["messages"] = msgs

        api_key = context.get("api_key")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 裁定 5：自訂連線（路由層判定）不串流；已知廠商才串流並要求最後送用量
        stream = (self._stream and context.get("custom_connection") is False  # 缺值＝自訂
                  and provider in COMPAT_STREAM_PROVIDERS and rules.stream)
        return _State(
            payload=payload, headers=headers, url=context.get("url"), provider=provider,
            api_key=api_key, stream=stream, forced_tool_choice=rules.forced_tool_choice,
        )

    async def send_round(self, state: _State) -> Any:
        """送出一輪：非串流＝單次 POST；串流＝自寫 SSE 彙整成同形回應（逾時區塊不跨 yield）。"""
        if not state.stream:
            yield await self._post_once(state)
            return
        clock = RoundClock(wait_for_content=True)  # 先送的 role chunk 不算首事件（R-3b ② M2）
        try:
            async with clock.deadline():
                result = await self._stream_once(state, clock)
        except TimeoutError:
            result = retry.on_timeout(_connection_error(state, clock.timeout_text), clock)
        except (httpx.HTTPError, ValueError, StreamShapeError) as e:  # JSON 壞掉＝ValueError
            result = _connection_error(state, f"{type(e).__name__}: {str(e)[:200]}")
            if isinstance(e, httpx.HTTPError):  # 還沒收到 200 就斷才可重試（R-3b ② M1）
                result = retry.on_connect(result, state.progress.opened,
                                          f"{state.provider} connection error")
        yield result

    async def _stream_once(self, state: _State, clock: RoundClock) -> Any:
        """彙整一輪串流；沒收到 [DONE] 就結束＝RoundError，不回半輪（附註 7）。"""
        progress = state.progress = _Progress()
        async with state.client.stream(
            "POST", state.url, headers=state.headers, json={**state.payload, **_STREAM_FIELDS},
            timeout=FIRST_EVENT_TIMEOUT_S,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return self._status_error(state, response)
            progress.opened = True
            async for data in sse_data(clock.timed(response.aiter_bytes())):
                if data == "[DONE]":
                    progress.complete = True
                    break
                chunk = json.loads(data)
                if isinstance(chunk, dict) and "error" in chunk:  # 200 之後才出錯
                    return retry.on_error_body(
                        RoundError(f"{state.provider} API Error (200)\n\n`{data}`"),
                        chunk["error"], state.provider)
                progress.chunks.add(chunk)
                if _has_content(chunk):
                    clock.content()
        if not progress.complete:
            return _connection_error(state, "上游串流沒送完就中斷（缺 [DONE]）")
        return progress.chunks.result()

    async def _post_once(self, state: _State) -> Any:
        """單次 POST；傳輸/非 200 錯誤化為 RoundError（wire 方言訊息）。"""
        try:
            http_resp = await state.client.post(
                state.url, headers=state.headers, json=state.payload, timeout=120.0
            )
        except httpx.HTTPError as e:
            # P5b：網路/連線錯誤明確報給使用者；只有連不上可重試（R-3b ② M1、N1）
            return retry.on_post_failure(
                _connection_error(state, f"{type(e).__name__}: {str(e)[:200]}"), e,
                f"{state.provider} connection error")
        if http_resp.status_code != 200:
            return self._status_error(state, http_resp)
        return http_resp.json()

    def _status_error(self, state: _State, response: httpx.Response) -> RoundError:
        """非 200 的使用者訊息（OpenAI 缺金鑰的 401 另給提示；429／5xx 標可重試）。"""
        status, error_msg = response.status_code, response.text
        if status == 401 and state.provider == "OpenAI" and not state.api_key:
            return RoundError(
                f"{state.provider} Access Denied (401)\n\n"
                "Missing `OPENAI_API_KEY` in `.env`. "
                f"Raw error:\n`{error_msg}`"
            )
        error = RoundError(f"{state.provider} API Error ({status})\n\n`{error_msg}`")
        if retry.retryable_status(status):  # 只有可重試的才讀 retry-after 標頭
            note = f"{state.provider} busy ({status})"
            error = retry.on_status(error, status, response.headers, note)
        return error

    def parse_round(self, _state: _State, raw: Any) -> Turn:
        """解析 choices → Turn（含 ADR-0152 reasoning 分頻的 OpenAI 方言）。"""
        choices = raw.get("choices", [])
        if not choices:
            return Turn(empty=True)
        message = choices[0].get("message", {})

        # --- 思考分頻（ADR-0152 T1）---
        # ①上游獨立欄位 message.reasoning_content——pop 掉＝歷史 append 絕不回送
        # （DeepSeek 回送即 400）；②content 內嵌 <think>/<thinking> 前綴——剝離後把
        # 乾淨正文寫回 message["content"]（同一 dict 進歷史/final/防幻覺，全下游一致）。
        field_reasoning = message.pop("reasoning_content", None) or ""
        embedded_reasoning, turn_text = split_reasoning(message.get("content", ""))
        if embedded_reasoning:
            message["content"] = turn_text
        reasoning_text = "\n".join(p for p in (field_reasoning, embedded_reasoning) if p)

        tool_calls = [
            ToolCall(
                id=tc.get("id"),
                name=tc.get("function", {}).get("name"),
                args=_safe_json_args(tc.get("function", {}).get("arguments", "{}")),
            )
            for tc in message.get("tool_calls", [])
        ]
        return Turn(
            reasoning_text=reasoning_text,
            reasoning_parts=[reasoning_text] if reasoning_text else [],
            visible_text=turn_text,
            text_parts=[turn_text] if turn_text else [],
            tool_calls=tool_calls,
            raw_assistant=message,
            refusal_text=message.get("refusal") or "",
        )

    def append_assistant_turn(self, state: _State, turn: Turn) -> None:
        """把 assistant 原形訊息寫回歷史（raw 透傳不變式）。"""
        state.payload["messages"].append(turn.raw_assistant)

    async def append_tool_results(
        self, state: _State, results: list[tuple[str | None, str, str]]
    ) -> None:
        """工具結果以 role:"tool" 訊息寫回歷史。"""
        for call_id, name, text in results:
            state.payload["messages"].append(
                {"role": "tool", "tool_call_id": call_id, "name": name, "content": text}
            )
        # ADR-0157 K2：誠實矯正輪的強制 tool_choice 用畢即解除（只解除自己設的，
        # 不碰呼叫端原生 tool_choice；不解除＝之後每輪都被迫調工具直到額度耗盡）。
        if state.honesty_forced:
            state.payload["tool_choice"] = "auto"
            state.honesty_forced = False

    def build_denied_turn(self, state: _State, turn: Turn, reason: str) -> None:
        """耗盡輪：assistant＋denied tool 佔位＋WRAP_UP_NOTE（openai 方言形狀）。"""
        state.payload["messages"].append(turn.raw_assistant)
        for tc in turn.tool_calls:
            state.payload["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": build_denied_tool_result(reason),
                }
            )
        state.payload["messages"].append({"role": "user", "content": WRAP_UP_NOTE})

    def set_tool_choice(self, state: _State, mode: str) -> None:
        """設定 tool_choice（openai 方言＝字串 "none"/"auto"）。"""
        state.payload["tool_choice"] = mode

    def append_interjection(self, state: _State, items: list[Interjection], *,
                            interrupted: bool = False) -> None:
        """插話（ADR-0249）：tool 訊息之後加一則 user 訊息（未實測，只有模擬契約）。"""
        text = interjection_text(items, interrupted=interrupted)
        state.payload["messages"].append({"role": "user", "content": text})

    def hold_tool_choice(self, state: _State, mode: str) -> Callable[[], None]:
        """這一輪暫時改成 mode，回傳還原函式（原本沒設就拿掉）。"""
        return hold_tool_setting(state.payload, "tool_choice",
                                 lambda: self.set_tool_choice(state, mode))

    def extract_usage(self, _state: _State, raw: Any) -> tuple[dict, int, int]:
        """Usage → 四欄正規化（prompt_tokens 含 cached_tokens，扣掉才是未快取輸入）。

        快取讀取 ``prompt_tokens_details.cached_tokens``，沒有才用 DeepSeek 頂層的
        ``prompt_cache_hit_tokens``，兩者都在不相加（ADR-0250 A7）。
        回傳新 dict、不改 raw（終端 chunk 原樣帶出 raw["usage"]）。
        """
        usage = raw.get("usage") or {}
        if not usage:
            return ({}, 0, 0)
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens")
        uncached, cache_read = split_included_cache(usage.get("prompt_tokens"), cached)
        return normalized_usage(
            uncached_input=uncached,
            cache_read=cache_read,
            cache_write=0,
            output=usage_count(usage.get("completion_tokens")),
        )

    def partial_usage(self, state: _State) -> PartialUsage | None:
        """串流沒完成、或完成了卻沒回報用量時要記的用量（裁定 5／6）；其餘回 None。"""
        progress = state.progress
        if progress is None or not progress.opened:
            return None
        raw = progress.chunks.result()
        if progress.complete and raw.get("usage"):
            return None  # 已由 extract_usage 照實入帳
        usage = self.extract_usage(state, raw)[0] if raw.get("usage") else None
        return estimate_partial_usage(
            request=state.payload, output_text=_received_text(raw),
            reported_input=None if usage is None else (
                usage["prompt_tokens"], usage["cache_read_input_tokens"], 0),
            reported_output=None if usage is None else usage["completion_tokens"],
        )

    def stop_reason(self, _state: _State, raw: Any) -> StopReason:
        """choices[0] → 正規化（ADR-0244）：``message.refusal`` 有值＝refusal；表外一律 error。"""
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not choices or not isinstance(choices[0], dict):
            return "error"
        message = choices[0].get("message")
        if isinstance(message, dict) and message.get("refusal"):
            return "refusal"
        finish = choices[0].get("finish_reason")
        return _FINISH_REASONS.get(finish, "error") if isinstance(finish, str) else "error"

    def emits_usage_event(self, _state: _State) -> bool:
        """一律送 token_usage 事件（只有 claude native egress 不送，ADR-0244）。"""
        return True

    def extract_final_text(self, _state: _State, raw: Any) -> str:
        """終端回應全部可見文字（防幻覺檢查輸入；openai＝讀終端回應）。"""
        # OpenAI 規格中 assistant 帶 tool_calls 時 content 為 null——`or ""` 歸一化。
        return "".join(
            (c.get("message", {}).get("content") or "")
            for c in raw.get("choices", [])
        )

    def finalize(self, _state: _State, raw: Any, request_id: str,
                 latency_info: dict) -> dict:
        """終端 chunk 重整形（id/telemetry/object/finish_reason；就地變異沿舊制）。"""
        raw["id"] = f"chatcmpl-{request_id}"
        raw["telemetry"] = latency_info
        raw["object"] = "chat.completion.chunk"
        if "choices" in raw and len(raw["choices"]) > 0:
            choice = raw["choices"][0]
            choice["delta"] = {"content": ""}
            choice["finish_reason"] = "stop"
            if "message" in choice:
                del choice["message"]
        return raw

    def context_breakdown_args(self, _state: _State) -> tuple:
        """Context 分解參數（ADR-0092 Phase B；分解 v1 僅 Claude，本家留空）。"""
        return (None, None, None)

    def mid_loop_hook(self, state: _State, turn: Turn, actually_used_tools: set[str],
                      allow_react: bool) -> MidLoopAction | None:
        """Honesty Guard（ADR-0157 K2＝三家推廣）：沒查資料就瞎掰 → 注入矯正輪。

        openai 方言：assistant 原形＋矯正 user 訊息寫回、`tool_choice="required"`
        （OpenAI Chat 標準值；Grok/DeepSeek 皆支援）強制下一輪查證；一次性閂鎖
        （每 run 至多矯正一次）、工具輪後強制解除（見 append_tool_results）。
        """
        if state.honesty_corrected or not (
            allow_react
            and guard_service.check_fact_without_tools(turn.visible_text, actually_used_tools)
        ):
            return None
        state.honesty_corrected = True
        logger.warning(
            "Honesty Guard triggered (%s): fact mentioned without tool call.",
            state.provider,
        )
        state.payload["messages"].append(turn.raw_assistant)
        state.payload["messages"].append(
            {"role": "user", "content": guard_service.get_honesty_correction_prompt()}
        )
        if state.forced_tool_choice:  # 規則表不允許強制就只靠矯正提示
            state.payload["tool_choice"] = "required"
            state.honesty_forced = True
        return MidLoopAction(deltas=[guard_service.get_honesty_warning_message()])

    def telemetry_model(self, _state: _State, request: Any) -> str:
        """遙測/計費用模型名（openai＝request.model，凍結舊制）。"""
        return getattr(request, "model", "")

    def resolve_provider_label(self, state: _State) -> str | None:
        """遙測用供應商標籤（ctx.provider 透傳）。"""
        return state.provider

    async def close(self, state: _State) -> None:
        """釋放 httpx client。"""
        await state.client.aclose()


def _has_content(chunk: Any) -> bool:
    """這個 chunk 帶了輸出（文字、思考、工具、結束原因或用量）；只有 role 的空殼不算。"""
    if not isinstance(chunk, dict) or chunk.get("usage"):
        return True
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict) or choice.get("finish_reason") or any(
                value for key, value in delta.items() if key != "role"):
            return True
    return False


def _connection_error(state: _State, detail: str) -> RoundError:
    """連線類失敗（含串流逾時與半截）的使用者訊息。"""
    return RoundError(f"❌ **{state.provider} 連線錯誤**\n\n`{detail}`")


def _received_text(raw: dict) -> str:
    """已收到的文字、思考、拒答與工具參數（估算輸出用）。"""
    parts: list[str] = []
    for choice in raw.get("choices") or []:
        message = choice.get("message") or {}
        parts += [v for k, v in message.items() if isinstance(v, str) and k != "role"]
        parts += [c["function"]["arguments"] for c in message.get("tool_calls") or []]
    return "".join(parts)


def _safe_json_args(args_str: str) -> dict:
    try:
        return json.loads(args_str)
    except Exception:
        return {}

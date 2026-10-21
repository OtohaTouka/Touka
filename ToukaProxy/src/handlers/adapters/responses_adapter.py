"""OpenAI Responses API adapter（ADR-0248；3b-U4a 請求側、3b-U4b-1 送出與解析）。

只給「非自訂連線、provider＝官方 OpenAI」的路由（DEV-A 裁定 2；路由在 U4b-2 接上）。請求：
Chat 形狀的請求轉成 ``instructions``＋input items、扁平工具、``max_output_tokens``；工具結果寫成
``function_call_output``（用 ``call_id``）；模型回的 output items（含加密 reasoning）下一輪原樣
回送。

- 送出（D1／D5）：預設非串流、單次 ``responses.create``；主迴圈打開串流時用
  ``create(stream=True)`` 的原始事件（不用 ``stream()`` 輔助，它遇到 incomplete 會丟例外）。
  ``response.completed``／``response.incomplete`` 才算完整；沒收到就斷線、error 事件、
  ``response.failed`` 都轉 RoundError，不回半輪。completed 沒帶 output 時依 output_index 補回。
- 兩段逾時沿用 ``stream_timeout``，閒置計時看 httpx2 層位元組（附註 27；ping 也算）。
- 用量扣掉 ``cached_tokens``；沒完成或沒回報用量的一輪照 U1b 規則估算。

- D4：SDK 一律顯式傳 ``api_key``、``base_url``、``http_client``（不跟隨轉址），值都來自路由
  context，缺一個就不建 client（不讓 SDK 改讀環境變數）；建構前清掉會改寫標頭／日誌的
  ``OPENAI_*`` 環境變數。``max_retries=0`` 跟重試一起在 U5a。
- D5：一律 ``store=false``、``include`` 加密 reasoning；``prompt_cache_key`` 用 agent＋session
  的雜湊，不送原文。串流旗標在送出當下合併，不寫進 ``kwargs``（D2）。取樣參數與 ``stop`` 不在
  白名單裡，不送（Responses 沒有 stop）。規則表接上 ``stream``（D6）與 ``forced_tool_choice``。
- GPT 登入接縫（附註 24）：context 可給 ``token_provider``（每次請求取最新權杖）、額外標頭與
  另一個 ``base_url``；本段不實作登入。
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)

from src.adapters.content_normalize import normalize_message_content
from src.config import strip_openai_sdk_env_overrides
from src.services.guard_service import guard_service
from src.services.models.model_rules import rules_for
from src.services.rhythm_service import WRAP_UP_NOTE, build_denied_tool_result

from . import retry
from .base import MidLoopAction, RoundError, ToolCall, Turn, hold_tool_setting, interjection_text
from .stream_timeout import RoundClock, liveness_event_hooks
from .usage import estimate_partial_usage, normalized_usage, split_included_cache, usage_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.interjection import Interjection

    from .base import StopReason
    from .usage import PartialUsage

logger = logging.getLogger(__name__)

_INCLUDE = ("reasoning.encrypted_content",)  # store=false 時下一輪要原樣回送加密 reasoning
_CACHE_KEY_PREFIX = "touka-"
_TOOL_FIELDS = ("name", "description", "parameters")  # 其餘（MCP 的 server/tool/category）不外送
_PART_TYPES = {"text": "input_text", "image_url": "input_image"}
_MISSING_KEY = "{} Access Denied (401)\n\nMissing `OPENAI_API_KEY` in `.env`."
_DONE_EVENTS = frozenset({"response.completed", "response.incomplete"})
# 先送的空殼事件：不結束首事件階段（R-3b ② M2）
_SHELL_EVENTS = frozenset({"response.created", "response.in_progress", "response.queued",
                           "response.output_item.added", "response.content_part.added",
                           "response.reasoning_summary_part.added"})
_DELTA_EVENTS = frozenset({  # 估算沒完成那一輪的輸出用
    "response.output_text.delta", "response.refusal.delta", "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta", "response.function_call_arguments.delta",
})
# ADR-0244：incomplete 的原因 → 正規化停止原因；表外（缺欄、未知）＝error。
_INCOMPLETE_REASONS: dict[str, StopReason] = {
    "max_output_tokens": "max_tokens",
    "content_filter": "safety",
}


@dataclass
class _Progress:
    """主迴圈串流這一輪收到了什麼（沒完成或沒回報用量時據此記帳，裁定 5／6）。"""

    opened: bool = False  # 已收到 HTTP 200
    final: dict | None = None  # completed／incomplete 帶的 response
    items: dict[int, dict] = field(default_factory=dict)  # output_item.done，依 output_index
    received: list[str] = field(default_factory=list)  # 收到的 delta


@dataclass
class _State:
    """Responses adapter 的回合狀態（core 不窺探內部）。"""

    kwargs: dict  # responses.create 的參數（不含 stream，D2）
    base_url: str | None
    provider: str | None
    client: AsyncOpenAI | None = None  # 缺憑證或 base URL 時不建
    setup_error: RoundError | None = None  # 沒建 client 時這一輪要回的錯誤
    stream: bool = False
    progress: _Progress | None = None  # 目前這一輪串流（非串流路徑恆為 None）
    honesty_corrected: bool = False  # ADR-0157：誠實矯正一次性閂鎖
    honesty_forced: bool = False  # 矯正輪強制了 tool_choice（工具輪後解除）
    forced_tool_choice: bool = True  # 規則表：可否強制 tool_choice（誠實矯正輪用）

    @property
    def label(self) -> str:
        return self.provider or ResponsesAdapter.provider_fallback

    @property
    def chunk_model(self) -> str:
        """下游 SSE chunk 的 model 欄位（與其他三家同源：送出的 model）。"""
        return self.kwargs.get("model", "unknown-model")


class ResponsesAdapter:
    """OpenAI Responses wire adapter（請求側；送出與解析在 U4b-1）。"""

    provider_fallback = "OpenAI"
    emits_context_breakdown = False

    def __init__(self, *, stream: bool = False) -> None:
        self._stream = stream  # ADR-0248 D1：預設非串流、只送 1 次；主迴圈明確打開

    async def prepare_request(self, request: Any, context: dict[str, Any]) -> _State:
        """Chat 形狀請求 → Responses 參數；建好不跟隨轉址的 SDK client。"""
        body = request.model_dump(exclude_none=True)
        instructions, items = _input_items(body.get("messages", []))
        if context.get("system_degraded", False):  # P5b：與其他三家同一段警告
            warning = (
                f"\n\n⚠️ SYSTEM WARNING: The proxy is running in DEGRADED MODE. "
                f"{context.get('degraded_reason', '')} Some tools may be offline. "
                f"Please inform the user."
            )
            instructions = instructions + warning if instructions else warning.strip()
        kwargs: dict[str, Any] = {"model": body.get("model", ""), "input": items, "store": False,
                                  "include": list(_INCLUDE),
                                  "prompt_cache_key": _prompt_cache_key(context)}
        if instructions:
            kwargs["instructions"] = instructions
        if tools := _flat_tools(body.get("tools") or []):
            kwargs["tools"] = tools
        if max_tokens := body.get("max_tokens"):
            kwargs["max_output_tokens"] = max_tokens  # incomplete 依此判成截斷（U4b-1）
        rules = rules_for(kwargs["model"])  # D6：規則表的 stream 開關；誠實矯正可否強制
        state = _State(kwargs=kwargs, base_url=context.get("base_url"),
                       provider=context.get("provider"), stream=self._stream and rules.stream,
                       forced_tool_choice=rules.forced_tool_choice)
        credential = context.get("token_provider") or context.get("api_key")
        if not credential:
            state.setup_error = RoundError(_MISSING_KEY.format(state.label))
        elif not state.base_url:
            state.setup_error = _connection_error(state, "缺少 base URL")
        else:
            strip_openai_sdk_env_overrides()  # 環境不得改寫請求（D4）
            state.client = AsyncOpenAI(
                api_key=credential,
                base_url=state.base_url,
                max_retries=0,  # 重試由 react_core 控制（3b-U5a）；complete_once 只送一次
                default_headers=context.get("extra_headers") or None,
                http_client=DefaultAsyncHttpxClient(  # 禁令 22（SDK 預設會跟隨）
                    follow_redirects=False,
                    event_hooks=liveness_event_hooks(),  # 閒置計時看位元組（附註 27）
                ),
            )
        return state

    async def send_round(self, state: _State) -> Any:
        """送出一輪：非串流＝單次 create；串流＝原始事件彙整成同形回應（逾時區塊不跨 yield）。"""
        if state.client is None:
            yield state.setup_error
            return
        clock = RoundClock(wait_for_content=True)
        try:
            async with clock.deadline() if state.stream else nullcontext():
                result = await (self._stream_once(state, clock) if state.stream
                                else self._create_once(state))
        except TimeoutError:
            result = retry.on_timeout(_connection_error(state, clock.timeout_text), clock)
        except APIStatusError as e:  # 錯誤本文原樣附上（與相容 Chat 同格式；SDK 已讀完本文）
            result = retry.on_status(
                RoundError(f"{state.label} API Error ({e.status_code})\n\n`{e.response.text}`"),
                e.status_code, e.response.headers, f"{state.label} busy ({e.status_code})")
        except APIConnectionError as e:  # 串流沒收到 200、非串流連不上才可重試（R-3b ② M1、N1）
            result = _connection_error(state, f"{type(e).__name__}: {str(e)[:200]}")
            note = f"{state.label} connection error"
            result = (retry.on_connect(result, bool(state.progress and state.progress.opened), note)
                      if state.stream else retry.on_post_failure(result, e, note))
        except APIError as e:  # 200 之後資料裡帶 error（SDK 直接丟例外）
            result = retry.on_error_body(
                RoundError(f"{state.label} API Error (200)\n\n`{e.message}`"), e.body, state.label)
        except ValueError as e:  # 壞掉的 JSON（R-3b ② L4）
            result = _connection_error(state, f"{type(e).__name__}: {str(e)[:200]}")
        yield result

    async def _create_once(self, state: _State) -> dict:
        response = await state.client.responses.create(**state.kwargs)
        return response.to_dict(mode="json")

    async def _stream_once(self, state: _State, clock: RoundClock) -> Any:
        """彙整一輪原始事件；沒收到 completed／incomplete 就結束＝RoundError（附註 7）。"""
        progress = state.progress = _Progress()
        stream = await state.client.responses.create(**state.kwargs, stream=True)
        progress.opened = True
        async with stream:
            async for event in stream:
                data = event.to_dict(mode="json")
                kind = data.get("type")
                if kind not in _SHELL_EVENTS:
                    clock.content()
                if kind in ("error", "response.failed"):
                    response = data.get("response")
                    detail = data if kind == "error" else (
                        response.get("error") if isinstance(response, dict) else None)
                    return retry.on_error_body(RoundError(
                        f"{state.label} API Error (200)\n\n"
                        f"`{json.dumps(detail, ensure_ascii=False)}`"), detail, state.label)
                if kind == "response.output_item.done" and isinstance(data.get("item"), dict):
                    progress.items[data.get("output_index", len(progress.items))] = data["item"]
                elif kind in _DELTA_EVENTS:
                    progress.received.append(str(data.get("delta") or ""))
                elif kind in _DONE_EVENTS:
                    progress.final = data.get("response") or {}
                    break
        if progress.final is None:
            return _connection_error(state, "上游串流沒送完就中斷（缺 response.completed）")
        raw = dict(progress.final)
        if not raw.get("output") and progress.items:  # Q4：completed 沒帶 output 時補回
            raw["output"] = [progress.items[i] for i in sorted(progress.items)]
        return raw

    def parse_round(self, _state: _State, raw: Any) -> Turn:
        """解析 output items：reasoning 摘要走思考頻道、function_call 用 call_id。"""
        items = raw.get("output") or []
        if not items:
            return Turn(empty=True)
        reasoning = [part.get("text", "") for item in _of(items, "reasoning")
                     for part in item.get("summary") or []]
        texts = _output_texts(items)
        return Turn(
            reasoning_text="\n".join(reasoning),
            reasoning_parts=reasoning,
            visible_text="".join(texts),
            text_parts=texts,
            tool_calls=[ToolCall(id=item.get("call_id"), name=item.get("name"),
                                 args=_safe_json_args(item.get("arguments") or "{}"))
                        for item in _of(items, "function_call")],
            raw_assistant=list(items),
            refusal_text="".join(part.get("refusal", "") for item in _of(items, "message")
                                 for part in item.get("content") or []
                                 if part.get("type") == "refusal"),
        )

    def append_assistant_turn(self, state: _State, turn: Turn) -> None:
        """模型回的 output items 原樣接回 input（加密 reasoning、function_call 都不改）。"""
        state.kwargs["input"].extend(turn.raw_assistant)

    async def append_tool_results(
        self, state: _State, results: list[tuple[str | None, str, str]]
    ) -> None:
        """工具結果寫成 function_call_output（對應 function_call 的 call_id，不是 item id）。"""
        state.kwargs["input"].extend(
            {"type": "function_call_output", "call_id": call_id, "output": text}
            for call_id, _name, text in results
        )
        if state.honesty_forced:  # ADR-0157 K2：矯正輪的強制 tool_choice 用畢即解除
            state.kwargs["tool_choice"] = "auto"
            state.honesty_forced = False

    def build_denied_turn(self, state: _State, turn: Turn, reason: str) -> None:
        """耗盡輪：output items＋拒絕的 function_call_output＋WRAP_UP_NOTE。"""
        state.kwargs["input"].extend(turn.raw_assistant)
        state.kwargs["input"].extend(
            {"type": "function_call_output", "call_id": tc.id,
             "output": build_denied_tool_result(reason)}
            for tc in turn.tool_calls
        )
        state.kwargs["input"].append({"role": "user", "content": WRAP_UP_NOTE})

    def set_tool_choice(self, state: _State, mode: str) -> None:
        """設定 tool_choice（Responses 同樣收 "none"／"auto"／"required" 字串）。"""
        state.kwargs["tool_choice"] = mode

    def append_interjection(self, state: _State, items: list[Interjection], *,
                            interrupted: bool = False) -> None:
        """插話（ADR-0249、R8）：function_call_output 之後加 ``{"role": "user"}`` input item。"""
        text = interjection_text(items, interrupted=interrupted)
        state.kwargs["input"].append({"role": "user", "content": text})

    def hold_tool_choice(self, state: _State, mode: str) -> Callable[[], None]:
        """這一輪暫時改成 mode，回傳還原函式（原本沒設就拿掉）。"""
        return hold_tool_setting(state.kwargs, "tool_choice",
                                 lambda: self.set_tool_choice(state, mode))

    def extract_usage(self, _state: _State, raw: Any) -> tuple[dict, int, int]:
        """用量四欄正規化（input_tokens 含 cached_tokens，扣掉才是未快取輸入）。"""
        usage = raw.get("usage") or {}
        if not usage:
            return ({}, 0, 0)
        details = usage.get("input_tokens_details") or {}
        uncached, cache_read = split_included_cache(usage.get("input_tokens"),
                                                    details.get("cached_tokens"))
        return normalized_usage(uncached_input=uncached, cache_read=cache_read, cache_write=0,
                                output=usage_count(usage.get("output_tokens")))

    def partial_usage(self, state: _State) -> PartialUsage | None:
        """串流沒完成、或完成了卻沒回報用量時要記的估算（裁定 5／6）；其餘回 None。"""
        progress = state.progress
        if progress is None or not progress.opened or (progress.final or {}).get("usage"):
            return None
        received = "".join(progress.received)
        return estimate_partial_usage(
            request=state.kwargs,
            output_text=received or "".join(_output_texts((progress.final or {}).get("output"))),
            reported_input=None, reported_output=None,  # Responses 只在結尾事件帶用量
        )

    def stop_reason(self, _state: _State, raw: Any) -> StopReason:
        """停止原因正規化（ADR-0244）：incomplete 看原因；completed 有拒答＝refusal。"""
        status = raw.get("status") if isinstance(raw, dict) else None
        if status == "incomplete":
            reason = (raw.get("incomplete_details") or {}).get("reason")
            return _INCOMPLETE_REASONS.get(reason, "error") if isinstance(reason, str) else "error"
        if status != "completed":
            return "error"
        refused = any(part.get("type") == "refusal" for item in _of(raw.get("output"), "message")
                      for part in item.get("content") or [])
        return "refusal" if refused else "end"

    def emits_usage_event(self, _state: _State) -> bool:
        """一律送 token_usage 事件（只有 claude native egress 不送，ADR-0244）。"""
        return True

    def extract_final_text(self, _state: _State, raw: Any) -> str:
        """終端回應的可見文字（防幻覺檢查輸入）。"""
        return "".join(_output_texts(raw.get("output")))

    def finalize(self, _state: _State, raw: Any, request_id: str,
                 latency_info: dict) -> dict:
        """終端 chunk：和其他三家同形（finish_reason 一律 stop，U5a 才改實際值）。"""
        return {
            "id": f"chatcmpl-{request_id}", "object": "chat.completion.chunk",
            "created": raw.get("created_at"), "model": raw.get("model"),
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
            "telemetry": latency_info,
        }

    def context_breakdown_args(self, _state: _State) -> tuple:
        """Context 分解參數（分解 v1 僅 Claude，本家留空）。"""
        return (None, None, None)

    def mid_loop_hook(self, state: _State, turn: Turn, actually_used_tools: set[str],
                      allow_react: bool) -> MidLoopAction | None:
        """Honesty Guard（ADR-0157 K2）：沒查資料就瞎掰 → output items＋矯正提示寫回。"""
        if state.honesty_corrected or not (
            allow_react
            and guard_service.check_fact_without_tools(turn.visible_text, actually_used_tools)
        ):
            return None
        state.honesty_corrected = True
        logger.warning("Honesty Guard triggered (%s): fact mentioned without tool call.",
                       state.label)
        state.kwargs["input"].extend(turn.raw_assistant)
        state.kwargs["input"].append(
            {"role": "user", "content": guard_service.get_honesty_correction_prompt()})
        if state.forced_tool_choice:
            state.kwargs["tool_choice"] = "required"
            state.honesty_forced = True
        return MidLoopAction(deltas=[guard_service.get_honesty_warning_message()])

    def telemetry_model(self, _state: _State, request: Any) -> str:
        """遙測／計費用模型名（request.model）。"""
        return getattr(request, "model", "")

    def resolve_provider_label(self, state: _State) -> str | None:
        """遙測用供應商標籤（ctx.provider 透傳）。"""
        return state.provider

    async def close(self, state: _State) -> None:
        """釋放 SDK client（連帶關掉它的 http client）。"""
        if state.client is not None:
            await state.client.close()


def _connection_error(state: _State, detail: str) -> RoundError:
    """連線類失敗（含串流逾時與半截）的使用者訊息（與相容 Chat 同格式）。"""
    return RoundError(f"❌ **{state.label} 連線錯誤**\n\n`{detail}`")


def _of(items: Any, kind: str) -> list[dict]:
    return [item for item in items or [] if isinstance(item, dict) and item.get("type") == kind]


def _output_texts(items: Any) -> list[str]:
    """取 message items 的 output_text（拒答 part 不算可見文字，見 stop_reason）。"""
    return [part.get("text", "") for item in _of(items, "message")
            for part in item.get("content") or [] if part.get("type") == "output_text"]


def _safe_json_args(args: str) -> dict:
    try:
        parsed = json.loads(args)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _prompt_cache_key(context: dict[str, Any]) -> str:
    """agent＋session 的 SHA-256 前 32 碼；同一個靈魂同一段對話每輪相同。"""
    seed = f"{context.get('agent_id') or ''}\x00{context.get('session_id') or ''}"
    return _CACHE_KEY_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _input_items(messages: list[dict]) -> tuple[str, list[dict]]:
    """Chat 訊息 → (instructions, input items)；system 合併成 instructions。"""
    system: list[str] = []
    items: list[dict] = []
    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if role == "tool":  # 工具結果照原文字送（不做多模態還原）
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            items.append({"type": "function_call_output", "call_id": msg.get("tool_call_id"),
                          "output": text})
            continue
        content = normalize_message_content(content)
        if role == "system":
            system.append(_plain_text(content))
            continue
        converted = _content(role, content) if content else None
        if converted:  # 區塊全被略過的訊息不送空殼
            items.append({"role": role, "content": converted})
        for call in msg.get("tool_calls") or []:
            function = call.get("function") or {}
            items.append({"type": "function_call", "call_id": call.get("id"),
                          "name": function.get("name"),
                          "arguments": function.get("arguments") or "{}"})
    return "\n".join(text for text in system if text), items


def _content(role: str, content: Any) -> Any:
    """字串照送；多模態 list：使用者轉 input_text／input_image，其他角色只留文字。"""
    if not isinstance(content, list):
        return content
    if role != "user":
        return _plain_text(content)
    parts = []
    for block in content:
        kind = _PART_TYPES.get(block.get("type")) if isinstance(block, dict) else None
        if kind == "input_text":
            parts.append({"type": kind, "text": block.get("text", "")})
        elif kind == "input_image":
            image = block.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": kind, "image_url": url})
        else:  # 失效可見化：不支援的區塊不靜默丟棄
            kind = block.get("type") if isinstance(block, dict) else type(block).__name__
            logger.warning("[responses] 不支援的內容區塊，已略過：%s", kind)
    return parts


def _plain_text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def _flat_tools(tools: list[dict]) -> list[dict]:
    """Chat 巢狀工具 → Responses 扁平工具；strict 照工具自己的值，沒寫就 False（預設是 True）。"""
    flat = []
    for tool in tools:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict):
            logger.warning("[responses] 不支援的工具型別，已略過：%s", tool.get("type"))
            continue
        fields = {k: function[k] for k in _TOOL_FIELDS if k in function}
        flat.append({"type": "function", **fields, "strict": bool(function.get("strict", False))})
    return flat

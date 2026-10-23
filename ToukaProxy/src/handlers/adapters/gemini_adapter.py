"""Gemini 供應商 adapter（react-core-extraction E2）。

自 gemini_handler 逐段移植：僅 wire 職責——模型名原樣送出（ADR-0247）／convert 轉換與
degraded 拼接／503 指數退避（send_round 於重試間 yield touka_status＝即時性保留）／
parts 解析與 gcall 合成 id（C5）＋thought parts 分頻（ADR-0157 K3）／functionResponse
歷史形狀＋條件式 AUTO 重置／Honesty Guard（mid_loop_hook；ADR-0157 K2 推廣三家，
本家強制 ANY）／usageMetadata 對映／convert_gemini_to_openai 終端包裝。
不建 Gemini 顯式快取（ADR-0243）：每輪照常送 system_instruction、tools、tool_config。
主迴圈走自寫 SSE 串流（ADR-0248：同一份 payload 送 ``:streamGenerateContent?alt=sse``，
彙整規則與完整訊號見 ADR-0248）；complete_once 照舊非串流、只送 1 次。

已知刻意微差（文件化）：舊 handler 以「收到 log 事件」置 has_tool_call 才寫回
工具輪歷史（無 log＝執行了卻不記史、直接收尾）——生產 ToolExecutor 每工具必發
finish log＝該邊永不觸發；核心改為「執行即寫回」（審計標記的脆弱點，不再複製）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from src.adapters.gemini_to_openai import convert_gemini_to_openai
from src.adapters.openai_to_gemini import convert_openai_to_gemini
from src.config import GEMINI_BASE_URL
from src.core.exceptions import ErrorCode, ToukaException
from src.services.guard_service import guard_service
from src.services.models.model_rules import rules_for
from src.services.rhythm_service import WRAP_UP_NOTE, build_denied_tool_result
from src.utils.sse import sse_line

from . import retry
from .base import MidLoopAction, RoundError, ToolCall, Turn, hold_tool_setting, interjection_text
from .sse_stream import GeminiStreamAggregator, StreamShapeError, sse_data
from .stream_timeout import FIRST_EVENT_TIMEOUT_S, RoundClock
from .usage import estimate_partial_usage, normalized_usage, split_included_cache, usage_count

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.models.interjection import Interjection

    from .base import StopReason
    from .usage import PartialUsage

logger = logging.getLogger(__name__)

# 送出的模型名至少要像「家族-版本」，且只含 URL 路徑安全字元（ADR-0247；KF-7b）。
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._]*(?:-[A-Za-z0-9._]+)+")

# ADR-0244：finishReason → 正規化停止原因；表外（OTHER、LANGUAGE、MALFORMED_FUNCTION_CALL、
# 缺欄、未知）＝error。Gemini 沒有獨立的拒答值，內容政策類一律歸 safety。
_FINISH_REASONS: dict[str, StopReason] = {
    "STOP": "end",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "safety",
    "RECITATION": "safety",
    "BLOCKLIST": "safety",
    "PROHIBITED_CONTENT": "safety",
    "SPII": "safety",
    "IMAGE_SAFETY": "safety",
}


@dataclass
class _Progress:
    """主迴圈串流這一次嘗試收到了什麼（沒完成或沒回報用量時據此記帳，裁定 5／6）。"""

    opened: bool = False  # 已收到 HTTP 200
    chunks: GeminiStreamAggregator = field(default_factory=GeminiStreamAggregator)


@dataclass
class _State:
    """Gemini adapter 的回合狀態（core 不窺探內部）。"""

    payload: dict
    url: str
    api_key: str | None
    resolved_model: str  # 送出的 API 模型 ID（wire 字串、非身分——IDENTITY_SSOT）
    agent_label: str | None  # 日誌顯示用 agent 名（非身分物件）
    call_seq: int = 0  # C5（D7）：run 內合成 call_id 序號
    cumulative_text: str = ""  # gemini 方言：guard 檢查輸入＝跨輪累積文字
    last_parts: Any = None  # 本輪 model parts（gemini 慣例＝與工具結果同時寫回）
    honesty_corrected: bool = False  # ADR-0157：誠實矯正一次性閂鎖（防矯正迴圈）
    # 主迴圈外（只有 complete_once）的送出次數上限：1＝不重試（ADR-0244）；主迴圈只送一次。
    max_attempts: int = 1
    main_loop: bool = False  # 主迴圈：只送一次，重試交給 react_core（3b-U5a）
    stream_url: str = ""  # 同一份 payload 的串流端點（主迴圈打開串流時用）
    stream: bool = False  # 送出當下才選串流端點，不寫進 payload（D2）
    progress: _Progress | None = None  # 目前這次串流嘗試（非串流路徑恆為 None）
    error_headers: Any = None  # 最後一個非 200 回應的標頭（retry-after，R-3b ② L5）
    client: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(follow_redirects=False)  # 禁令 22
    )

    @property
    def chunk_model(self) -> str:
        """SSE chunk 的 model 欄位（gemini 方言＝送出的 model_id）。"""
        return self.resolved_model


def _wire_model_id(req_model_raw: str) -> str:
    """模型名只去 ``models/`` 前綴並轉小寫（同型號正規化）。

    空名或非法名回可讀錯誤（ADR-0247 決定 A13）。
    """
    model_id = req_model_raw.lower().removeprefix("models/")
    if not _MODEL_ID.fullmatch(model_id):
        raise ToukaException(
            ErrorCode.MODEL_NOT_FOUND,
            detail_override=(
                f"Invalid Gemini model name: '{req_model_raw}'. "
                "Use the full model ID from the model list, e.g. gemini-2.5-flash."
            ),
        )
    return model_id


class GeminiAdapter:
    """Gemini wire adapter（契約：tests/unit/test_adapter_contract.py）。"""

    emits_context_breakdown = False  # 分解 v1 僅 Claude（事件形狀凍結）

    def __init__(self, *, stream: bool = False) -> None:
        self._stream = stream  # ADR-0248 D1：預設非串流（complete_once）；主迴圈明確打開

    async def prepare_request(self, request: Any, context: dict[str, Any]) -> _State:
        """整備 gemini payload（convert/degraded 拼接/模型名檢查）。"""
        gemini_req = convert_openai_to_gemini(request)

        # P5b：DEGRADED 警告注入 system_instruction
        if context.get("system_degraded", False):
            warning = (
                f"\n\n⚠️ SYSTEM WARNING: The proxy is running in DEGRADED MODE. "
                f"{context.get('degraded_reason', '')} Some tools may be offline. "
                f"Please inform the user."
            )
            si = gemini_req.get("system_instruction")
            if isinstance(si, dict) and isinstance(si.get("parts"), list) and si["parts"]:
                first = si["parts"][0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    first["text"] += warning
                else:
                    si["parts"].append({"text": warning})
            else:
                gemini_req["system_instruction"] = {"parts": [{"text": warning.strip()}]}

        model_id = _wire_model_id(request.model)
        # 禁令 23：金鑰走 x-goog-api-key header（非 URL query），避免洩漏。
        base = f"{GEMINI_BASE_URL}/models/{model_id}"
        return _State(
            payload=gemini_req,
            url=f"{base}:generateContent",
            api_key=context.get("gemini_api_key"),
            resolved_model=model_id,
            agent_label=context.get("agent_id"),
            stream_url=f"{base}:streamGenerateContent?alt=sse",
            stream=self._stream and rules_for(model_id).stream,  # D6：規則表開關
            main_loop=self._stream,
        )

    async def send_round(self, state: _State) -> Any:
        """送出一輪（主迴圈只送一次，重試交給 react_core）。

        可重試的錯誤標在 RoundError 上；其餘路徑（complete_once）照舊：503 先發狀態、連線錯誤
        後先等再放棄，次數看 ``max_attempts``。
        """
        last: tuple[Any, str] | None = None  # 最後一個非 200 回應的（狀態碼, 內容）
        opened, failure = False, None  # 連線錯誤：串流開了沒、原本的例外（非串流只重試連不上）
        attempts = 1 if state.main_loop else state.max_attempts
        for attempt in range(attempts):
            wait_time = (attempt + 1) * 2
            try:
                status, body = await (self._stream_attempt(state) if state.stream
                                      else self._post_attempt(state))
            except httpx.HTTPError as e:
                logger.exception("HTTP Connection Error (Attempt %d)", attempt + 1)
                opened, failure = bool(state.progress and state.progress.opened), e
                if not state.main_loop:
                    await asyncio.sleep(wait_time)
                continue
            if status == 200:
                yield body
                return
            last = (status, body)
            if status != 503 or state.main_loop:
                break
            yield sse_line({
                "type": "touka_status",
                "status": f"Gemini busy (503), retrying... (attempt {attempt + 1})",
            })
            logger.warning(
                "Gemini 503 encountered. Retry %d/%d in %ds...", attempt + 1, attempts, wait_time,
            )
            await asyncio.sleep(wait_time)
        code, text = last or ("Timeout", "No Response")
        error = RoundError(f"API Error ({code})\n\n`{text}`")
        if state.main_loop and isinstance(code, int):
            error = retry.on_status(error, code, state.error_headers, f"Gemini busy ({code})",
                                    body=text)
        elif state.main_loop and state.stream:  # 還沒收到 200 就斷才可重試（R-3b ② M1）
            error = retry.on_connect(error, opened, "Gemini connection error")
        elif state.main_loop and failure is not None:  # 非串流：只有連不上可重試（N1）
            error = retry.on_post_failure(error, failure, "Gemini connection error")
        yield error

    async def _post_attempt(self, state: _State) -> tuple[int, Any]:
        """非串流單次 POST：（狀態碼, 200 的 JSON 或錯誤內容）。"""
        http_resp = await state.client.post(
            state.url, headers={"x-goog-api-key": state.api_key}, json=state.payload,
            timeout=120.0,
        )
        state.error_headers = http_resp.headers
        return http_resp.status_code, (
            http_resp.json() if http_resp.status_code == 200 else http_resp.text)

    async def _stream_attempt(self, state: _State) -> tuple[int, Any]:
        """串流單次嘗試；逾時、壞 JSON／形狀、半截都回 200＋RoundError（逾時另標可否重試）。"""
        clock = RoundClock()
        try:
            async with clock.deadline():
                return await self._stream_once(state, clock)
        except TimeoutError:
            return 200, retry.on_timeout(
                RoundError(f"API Error (Timeout)\n\n`{clock.timeout_text}`"), clock)
        except (ValueError, StreamShapeError) as e:  # JSON 壞掉＝ValueError
            return 200, RoundError(f"API Error (200)\n\n`{type(e).__name__}: {str(e)[:200]}`")

    async def _stream_once(self, state: _State, clock: RoundClock) -> tuple[int, Any]:
        progress = state.progress = _Progress()
        async with state.client.stream(
            "POST", state.stream_url, headers={"x-goog-api-key": state.api_key},
            json=state.payload, timeout=FIRST_EVENT_TIMEOUT_S,
        ) as response:
            if response.status_code != 200:
                await response.aread()
                state.error_headers = response.headers
                return response.status_code, response.text
            progress.opened = True
            async for data in sse_data(clock.timed(response.aiter_bytes())):
                chunk = json.loads(data)
                if isinstance(chunk, dict) and "error" in chunk:  # 200 之後才出錯
                    return 200, retry.on_error_body(
                        RoundError(f"API Error (200)\n\n`{data}`"), chunk["error"], "Gemini")
                progress.chunks.add(chunk)
        if not progress.chunks.complete:  # 沒有 finishReason／blockReason＝不回半輪
            return 200, RoundError("API Error (200)\n\n`上游串流沒送完就中斷（缺 finishReason）`")
        return 200, progress.chunks.result()

    def parse_round(self, state: _State, raw: Any) -> Turn:
        """解析 candidates/parts → Turn（gcall 合成 id＝C5；thought 分頻＝K3）。"""
        candidates = raw.get("candidates", [])
        if not candidates:
            return Turn(empty=True)
        parts = candidates[0].get("content", {}).get("parts", [])

        # K3（ADR-0157）：`thought: true` 的思考摘要 part 走 reasoning 頻道
        # （ADR-0152 同款分頻）——不進正文、不進 guard 累積、不回送歷史
        # （Google 標記 thought 為 output-only；對齊 openai reasoning pop 語意）。
        reasoning_parts = [p["text"] for p in parts if "text" in p and p.get("thought")]
        # 逐段可見文字（舊 handler 逐 part yield＝chunk 邊界保真；含空字串照舊）；
        # 跨輪累積（gemini 方言：終端 guard 檢查讀累積文字、每輪都加——含工具輪）。
        text_parts = [p["text"] for p in parts if "text" in p and not p.get("thought")]
        turn_text = "".join(text_parts)
        state.cumulative_text += turn_text

        tool_calls = []
        for p in parts:
            if "functionCall" in p:
                state.call_seq += 1  # C5（D7）：合成穩定 call_id
                tool_calls.append(ToolCall(
                    id=f"gcall_{state.call_seq}",
                    name=p["functionCall"]["name"],
                    args=p["functionCall"].get("args", {}),
                ))

        return Turn(
            reasoning_text="\n".join(reasoning_parts),
            reasoning_parts=reasoning_parts,
            visible_text=turn_text,
            text_parts=text_parts,
            tool_calls=tool_calls,
            raw_assistant=[p for p in parts if not p.get("thought")],
        )

    def append_assistant_turn(self, state: _State, turn: Turn) -> None:
        """Gemini 方言：assistant parts 與工具結果同時寫回（見 append_tool_results）。"""
        state.last_parts = turn.raw_assistant  # type: ignore[attr-defined]

    async def append_tool_results(
        self, state: _State, results: list[tuple[str | None, str, str]]
    ) -> None:
        """Model parts＋functionResponse 寫回＋條件式 AUTO 重置。"""
        parts = getattr(state, "last_parts", [])
        state.payload["contents"].append({"role": "model", "parts": parts})
        state.payload["contents"].append({
            "role": "user",
            "parts": [
                {"functionResponse": {"name": name, "response": {"result": text}}}
                for _cid, name, text in results
            ],
        })
        # 條件式 AUTO 重置（僅曾設 tool_config 才重置——強制工具語意保持）
        if "tool_config" in state.payload:
            state.payload["tool_config"] = {"function_calling_config": {"mode": "AUTO"}}

    def build_denied_turn(self, state: _State, turn: Turn, reason: str) -> None:
        """耗盡輪：model parts＋denied functionResponse＋WRAP_UP text part。"""
        denied_parts: list[dict] = [
            {
                "functionResponse": {
                    "name": tc.name,
                    "response": {"result": build_denied_tool_result(reason)},
                }
            }
            for tc in turn.tool_calls
        ]
        denied_parts.append({"text": WRAP_UP_NOTE})
        state.payload["contents"].append({"role": "model", "parts": turn.raw_assistant})
        state.payload["contents"].append({"role": "user", "parts": denied_parts})

    def set_tool_choice(self, state: _State, mode: str) -> None:
        """Gemini 方言：tool_config.function_calling_config.mode（NONE/ANY/AUTO）。"""
        state.payload["tool_config"] = {
            "function_calling_config": {"mode": mode.upper()}
        }

    def append_interjection(self, state: _State, items: list[Interjection], *,
                            interrupted: bool = False) -> None:
        """插話（ADR-0249）：functionResponse 同一個 content 加 text part（只有模擬契約）。"""
        contents = state.payload["contents"]
        if not contents or contents[-1].get("role") != "user":  # 正常走不到
            contents.append({"role": "user", "parts": []})
        contents[-1]["parts"].append({"text": interjection_text(items, interrupted=interrupted)})

    def hold_tool_choice(self, state: _State, mode: str) -> Callable[[], None]:
        """這一輪暫時改成 mode，回傳還原函式（原本沒設就拿掉）。"""
        return hold_tool_setting(state.payload, "tool_config",
                                 lambda: self.set_tool_choice(state, mode))

    def mid_loop_hook(self, state: _State, turn: Turn, actually_used_tools: set[str],
                      allow_react: bool) -> MidLoopAction | None:
        """Honesty Guard（ADR-0157 K2＝三家推廣）：沒查資料就瞎掰 → 注入矯正輪。

        矯正文案讀 guard_service SSOT（舊內嵌版教已退役工具名）；一次性閂鎖
        （每 run 至多矯正一次，防弱強制方言下的矯正迴圈燒到額度耗盡）。
        """
        if state.honesty_corrected or not (
            allow_react
            and guard_service.check_fact_without_tools(turn.visible_text, actually_used_tools)
        ):
            return None
        state.honesty_corrected = True
        logger.warning(
            f"Honesty Guard triggered for {state.agent_label}: "
            f"Fact mentioned without tool call."
        )
        state.payload["contents"].append({"role": "model", "parts": turn.raw_assistant})
        state.payload["contents"].append({
            "role": "user",
            "parts": [{"text": guard_service.get_honesty_correction_prompt()}],
        })
        # 強制切換為 ANY 模式以利修正
        state.payload["tool_config"] = {"function_calling_config": {"mode": "ANY"}}
        return MidLoopAction(deltas=[guard_service.get_honesty_warning_message()])

    def extract_usage(self, _state: _State, raw: Any) -> tuple[dict, int, int]:
        """UsageMetadata → 四欄正規化（promptTokenCount 含 Google 自動快取，扣掉才是未快取）。

        Touka 不建顯式快取（ADR-0243），快取寫恆為 0。思考 token（thoughtsTokenCount）Google
        當輸出計費，算進輸出（ADR-0250 A6）。
        """
        usage_metadata = raw.get("usageMetadata") or {}
        if not usage_metadata:
            return ({}, 0, 0)
        uncached, cache_read = split_included_cache(
            usage_metadata.get("promptTokenCount"),
            usage_metadata.get("cachedContentTokenCount"),
        )
        return normalized_usage(
            uncached_input=uncached,
            cache_read=cache_read,
            cache_write=0,
            output=usage_count(usage_metadata.get("candidatesTokenCount"))
            + usage_count(usage_metadata.get("thoughtsTokenCount")),
        )

    def partial_usage(self, state: _State) -> PartialUsage | None:
        """串流沒完成、或完成了卻沒回報用量時要記的用量（裁定 5／6）；其餘回 None。"""
        progress = state.progress
        if progress is None or not progress.opened:
            return None
        raw = progress.chunks.result()
        if progress.chunks.complete and raw.get("usageMetadata"):
            return None  # 已由 extract_usage 照實入帳
        usage = self.extract_usage(state, raw)[0] if raw.get("usageMetadata") else None
        return estimate_partial_usage(
            request=state.payload, output_text=_received_text(raw),
            reported_input=None if usage is None else (
                usage["prompt_tokens"], usage["cache_read_input_tokens"], 0),
            reported_output=None if usage is None else usage["completion_tokens"],
        )

    def stop_reason(self, _state: _State, raw: Any) -> StopReason:
        """candidates[0].finishReason → 正規化（ADR-0244）。

        沒有 candidates 而 ``promptFeedback.blockReason`` 有值＝提示整段被擋＝safety；
        表外一律 error。
        """
        candidates = raw.get("candidates") if isinstance(raw, dict) else None
        if not candidates:
            feedback = raw.get("promptFeedback") if isinstance(raw, dict) else None
            blocked = isinstance(feedback, dict) and feedback.get("blockReason")
            return "safety" if blocked else "error"
        first = candidates[0]
        finish = first.get("finishReason") if isinstance(first, dict) else None
        return _FINISH_REASONS.get(finish, "error") if isinstance(finish, str) else "error"

    def emits_usage_event(self, _state: _State) -> bool:
        """一律送 token_usage 事件（只有 claude native egress 不送，ADR-0244）。"""
        return True

    def extract_final_text(self, state: _State, _raw: Any) -> str:
        """Guard 檢查輸入＝跨輪累積文字（gemini 方言，與 openai 的終端回應不同）。"""
        return state.cumulative_text

    def finalize(self, state: _State, raw: Any, request_id: str,
                 latency_info: dict) -> dict:
        """convert_gemini_to_openai ＋ id/telemetry/object/finish_reason 重整形。"""
        openai_resp = convert_gemini_to_openai(raw, model_name=state.resolved_model)
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

    def context_breakdown_args(self, _state: _State) -> tuple:
        """Context 分解參數（ADR-0092 Phase B；分解 v1 僅 Claude，本家留空）。"""
        return (None, None, None)

    def telemetry_model(self, _state: _State, request: Any) -> str:
        """遙測/計費用模型名（gemini＝request.model，凍結舊制）。"""
        return getattr(request, "model", "")

    def resolve_provider_label(self, _state: _State) -> str:
        """遙測用供應商標籤（gemini 方言＝固定字面）。"""
        return "Gemini"

    async def close(self, state: _State) -> None:
        """釋放 httpx client。"""
        await state.client.aclose()


def _received_text(raw: dict) -> str:
    """已收到的文字、思考與工具參數（估算輸出用）。"""
    contents = [c.get("content") or {} for c in raw.get("candidates") or []]
    return "".join(p["text"] if isinstance(p.get("text"), str)
                   else json.dumps(p.get("functionCall") or {}, ensure_ascii=False)
                   for content in contents for p in content.get("parts", []))

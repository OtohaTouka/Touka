"""一次性模型呼叫 ``complete_once``（ADR-0244；model-p1b U3）。

旁路呼叫（摘要、互審 critic、創建評分、Auto 審查員）共用：單次、不帶工具、不串流。

- 路由與 dispatcher factory 共用 ``resolve_model_route``；只走該模型自己那一家，不跨廠商。
- 先過成本閘（``autonomy_service.is_over_budget``），超預算就不送出；整備完、送出前再以「輸出上限
  全用滿」估最壞花費（``cost_guard``），已花＋最壞會超過上限也不送（r55，``would_exceed``）。
- 廠商回了用量就記帳（``token_usage.json``＋稽核 CSV），provider 標籤＝用途；拒答、截斷也照記
  （已計費）。只記用量，不記提示內容或回覆內容（無痕對話因此與主迴圈一致）。
- 不重試：Claude SDK ``max_retries=0``；Gemini 的 send_round 上限設 1 次，adapter 若仍送出
  重試通知也立即中止。
- ``timeout_s``＝整次呼叫的總時間（整備＋送出＋解析）。
- 拒答、截斷、安全過濾、未知停止原因、逾時、超預算、廠商錯誤一律回結構化非成功：``error``
  有值、``text`` 為空，``stop_reason`` 不會被改成 ``end``。
- ``response_schema``：1b 只對 Claude 外送（``output_config.format``）。其他家族不外送、原文照回；
  成功也不代表符合 schema，呼叫端一律嚴格解析，解析失敗＝不放行。

底層沿用三個 adapter 的 ``prepare_request``／``send_round``／``parse_round``／``extract_usage``／
``stop_reason``。輸出上限隨請求進 adapter（3a 起 Claude 轉換器照請求值，3b-U3 起 Gemini 轉換器
也是）；``_SHAPERS`` 只留 Claude 的斷點／schema 與 Gemini 的不重試（以 adapter 類別為鍵）。
主迴圈的退避重試不得放進這一層。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.config import ANTHROPIC_API_BASE_URL, strip_anthropic_sdk_env_overrides
from src.core.exceptions import ToukaException
from src.handlers.adapters.base import THINKING_PROFILE_KEY, RoundError
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.handlers.adapters.responses_adapter import ResponsesAdapter
from src.services.autonomy import autonomy_service
from src.services.cost_guard import worst_case_call_usd
from src.services.dispatchers.route_family import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    OPENAI_ROUTE,
    RESPONSES_ROUTE,
    resolve_model_route,
)
from src.services.dispatchers.utils import get_provider_info
from src.services.model_manager import model_manager
from src.services.telemetry import accumulate_token_usage, estimate_cost_usd, log_usage_audit
from src.services.tool_errors import (
    INTERNAL_ERROR,
    PERMISSION_DENIED,
    RESOURCE_NOT_FOUND,
    TIMEOUT_EXCEEDED,
    UPSTREAM_ERROR,
)
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.handlers.adapters.base import ProviderAdapter, StopReason, ThinkingProfile
    from src.models.identity import AgentIdentity
    from src.services.dispatchers.route_family import ModelRoute

logger = logging.getLogger(__name__)

# 細分原因碼（CompletionError.reason）；max_tokens／refusal／safety 直接沿用停止原因。
REASON_OVER_BUDGET = "over_budget"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_TIMEOUT = "timeout"
REASON_UPSTREAM = "upstream"
REASON_UNKNOWN_STOP = "unknown_stop"
REASON_INTERNAL = "internal"

# 原因碼 → (禁令 59 的錯誤類別, 給呼叫端的英文一句話；不含提示內容或上游原文)
_FAILURES: dict[str, tuple[str, str]] = {
    REASON_OVER_BUDGET: (PERMISSION_DENIED, "Cost cap reached; the call was not sent."),
    REASON_MODEL_UNAVAILABLE: (RESOURCE_NOT_FOUND, "The model cannot be routed."),
    REASON_MISSING_CREDENTIALS: (RESOURCE_NOT_FOUND, "No API key is configured for the model."),
    REASON_TIMEOUT: (TIMEOUT_EXCEEDED, "The call exceeded its total time budget."),
    REASON_UPSTREAM: (UPSTREAM_ERROR, "The provider request failed."),
    "max_tokens": (UPSTREAM_ERROR, "The output was truncated at max_tokens."),
    "refusal": (UPSTREAM_ERROR, "The model refused to answer."),
    "safety": (UPSTREAM_ERROR, "The provider safety filter blocked the output."),
    REASON_UNKNOWN_STOP: (UPSTREAM_ERROR, "The provider returned an unrecognized stop reason."),
    REASON_INTERNAL: (INTERNAL_ERROR, "The response could not be processed."),
}
_LOG_DETAIL_LIMIT = 200  # 上游錯誤寫進 log 的長度上限（禁令 18：命名常數）
PURPOSE_INLINE_COMPLETION = "inline_completion"  # IDE 行內補全（經 routers/proxy.py 的用途標頭）
# 用途 → 思考強度（r55；只對 Claude 5.5 生效，由 ClaudeAdapter 照規則表套用）。審查員
# （auto_reviewer）與互審（critic）刻意不列＝照模型預設（ADR-0271）；互審是品質關卡。
_THINKING_PROFILES: dict[str, ThinkingProfile] = {
    "judge": "minimal",
    "summarizer": "minimal",
    PURPOSE_INLINE_COMPLETION: "minimal",
}
_NATIVE_KEY_REQUIRED = frozenset({ANTHROPIC_FAMILY, GEMINI_FAMILY, RESPONSES_ROUTE})
_ADAPTERS: dict[str, type] = {
    ANTHROPIC_FAMILY: ClaudeAdapter,
    GEMINI_FAMILY: GeminiAdapter,
    OPENAI_ROUTE: OpenAIAdapter,
    RESPONSES_ROUTE: ResponsesAdapter,  # 預設非串流（D1）；裁定 4
}


@dataclass(frozen=True)
class CompletionError:
    """結構化非成功：``error_class``＝禁令 59 六類之一；``reason``＝細分原因碼。"""

    error_class: str
    reason: str
    message: str


@dataclass(frozen=True)
class CompletionResult:
    """complete_once 的結果。成功＝``error is None``，此時 ``stop_reason`` 必為 ``end``。

    ``usage``＝四欄正規化用量（ADR-0244）；沒送出或廠商沒回用量時為空 dict、``cost_usd`` 為 0。
    """

    text: str
    usage: dict[str, int]
    cost_usd: float
    stop_reason: StopReason
    error: CompletionError | None

    @property
    def ok(self) -> bool:
        """是否成功（error 為 None）。"""
        return self.error is None


class Completer(Protocol):
    """complete_once 的呼叫合約：呼叫端依賴它，測試注入假的實作。"""

    async def __call__(
        self,
        identity: AgentIdentity,
        purpose: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        timeout_s: float,
        response_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """跑一次模型呼叫（語意同 complete_once）。"""
        ...


def _failure(
    reason: str,
    *,
    stop_reason: StopReason = "error",
    usage: dict[str, int] | None = None,
    cost_usd: float = 0.0,
) -> CompletionResult:
    error_class, message = _FAILURES[reason]
    return CompletionResult(
        text="",
        usage=usage or {},
        cost_usd=cost_usd,
        stop_reason=stop_reason,
        error=CompletionError(error_class=error_class, reason=reason, message=message),
    )


def _shape_claude(state: Any, _max_tokens: int, response_schema: dict[str, Any] | None) -> None:
    # max_tokens 已由轉換器依規則表帶入（ADR-0247）：照請求值，但會被調高（budget 思考至少預算
    # ＋1,024、default_on 思考至少 8,192）；不壓非串流上限（有傳 timeout，SDK 不擋；附註 4）。
    # 使用者那則每次都不同，快取斷點只會多付快取寫入費；system 的斷點保留（同用途可命中）。
    for block in state.kwargs["messages"][-1].get("content") or []:
        if isinstance(block, dict):
            block.pop("cache_control", None)
    if response_schema is not None:  # 併進 adapter 已放的 output_config（effort），不覆寫
        state.kwargs.setdefault("output_config", {})["format"] = {
            "type": "json_schema", "schema": response_schema
        }


def _shape_gemini(state: Any, _max_tokens: int, _response_schema: dict[str, Any] | None) -> None:
    # 輸出上限已由轉換器依請求帶入（3b-U3）；schema 不外送，這裡只保證不重試。
    state.max_attempts = 1  # 不重試：adapter 遇 503／連線錯誤原本會退避重送


# OpenAI 相容：max_tokens 已隨請求進 payload，schema 不外送，不需要調整。Responses 的 SDK client
# 建構時就是 max_retries=0（3b-U5a），也不需要。
_SHAPERS: dict[type, Callable[[Any, int, dict[str, Any] | None], None]] = {
    ClaudeAdapter: _shape_claude,
    GeminiAdapter: _shape_gemini,
}


def thinking_profile_for(purpose: str) -> ThinkingProfile | None:
    """用途的思考強度；不在表上（審查員、互審、未知用途）＝None，照模型預設。"""
    return _THINKING_PROFILES.get(purpose)


def _adapter_context(
    route: ModelRoute, identity: AgentIdentity, client: Any, purpose: str
) -> dict[str, Any]:
    # 每次新建：主迴圈 context 的任何鍵都不會流進旁路呼叫（審查員照舊）
    if route.kind == ANTHROPIC_FAMILY:
        context: dict[str, Any] = {"anthropic_client": client}
        profile = thinking_profile_for(purpose)
        if profile is not None:
            context[THINKING_PROFILE_KEY] = profile
        return context
    if route.kind == GEMINI_FAMILY:
        return {"gemini_api_key": route.api_key, "agent_id": identity.agent_id}
    if route.kind == RESPONSES_ROUTE:
        return {"api_key": route.api_key, "base_url": route.url, "provider": route.provider,
                "agent_id": identity.agent_id}
    return {"api_key": route.api_key, "url": route.url, "provider": route.provider}


_WIRE_OUTPUT_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _wire_payload(state: Any) -> dict[str, Any]:
    """實際要送的內容（Claude／Responses 在 kwargs，相容 Chat／Gemini 在 payload）。"""
    return {"kwargs": getattr(state, "kwargs", None), "payload": getattr(state, "payload", None)}


def _wire_max_output(state: Any, requested: int) -> int:
    """送出欄位上的輸出上限（含 Claude 的 8,192 下限或免除）；讀不到用請求值，取兩者較大。"""
    caps = [requested]
    for fields in _wire_payload(state).values():
        if isinstance(fields, dict):
            generation = fields.get("generationConfig")
            values = [fields.get(key) for key in _WIRE_OUTPUT_KEYS] + [
                generation.get("maxOutputTokens") if isinstance(generation, dict) else None]
            caps += [value for value in values if isinstance(value, int)]
    return max(caps)


async def _send_once(adapter: ProviderAdapter, state: Any) -> Any:
    """只取 send_round 的一輪結果；收到狀態事件（adapter 的重試通知）就中止＝不重試。"""
    rounds = adapter.send_round(state)
    try:
        async for item in rounds:
            if isinstance(item, str):
                return RoundError("retry suppressed by complete_once")
            return item
    finally:
        await rounds.aclose()
    return RoundError("no response")


def _record_usage(
    identity: AgentIdentity, purpose: str, usage: dict[str, int], model: str, started: float
) -> None:
    elapsed_ms = int((get_current_epoch() - started) * 1000)
    accumulate_token_usage(identity, purpose, usage, e_index=None, model=model)  # 不寫 E-Index
    log_usage_audit(
        identity,
        f"{purpose}-{uuid.uuid4().hex[:12]}",
        purpose,
        {"total_ms": elapsed_ms, "pure_gen_ms": elapsed_ms},
        usage,
        model=model,
    )


async def complete_once(
    identity: AgentIdentity,
    purpose: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout_s: float,
    response_schema: dict[str, Any] | None = None,
) -> CompletionResult:
    """單次、不帶工具的模型呼叫；任何失敗都回結構化非成功，不拋例外（見模組說明）。"""
    if autonomy_service.is_over_budget(identity):
        return _failure(REASON_OVER_BUDGET)
    try:
        route = resolve_model_route(model, manager=model_manager, provider_info=get_provider_info)
    except ToukaException:
        return _failure(REASON_MODEL_UNAVAILABLE)
    adapter_cls = _ADAPTERS.get(route.kind)
    if adapter_cls is None:
        return _failure(REASON_MODEL_UNAVAILABLE)
    if route.kind in _NATIVE_KEY_REQUIRED and not route.api_key:
        return _failure(REASON_MISSING_CREDENTIALS)

    adapter: ProviderAdapter = adapter_cls()
    request = OpenAIChatRequest(
        model=model,
        max_tokens=max_tokens,
        messages=[
            OpenAIMessage(role="system", content=system),
            OpenAIMessage(role="user", content=user),
        ],
    )
    client = None
    if route.kind == ANTHROPIC_FAMILY:
        strip_anthropic_sdk_env_overrides()  # 環境不得改寫請求（ADR-0247）
        client = AsyncAnthropic(
            api_key=route.api_key,
            base_url=ANTHROPIC_API_BASE_URL,  # 不看 ANTHROPIC_BASE_URL（ADR-0247）
            max_retries=0,
            timeout=timeout_s,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),  # 禁令 22
        )
    state = None
    started = get_current_epoch()
    try:
        async with asyncio.timeout(timeout_s):
            state = await adapter.prepare_request(
                request, _adapter_context(route, identity, client, purpose)
            )
            shaper = _SHAPERS.get(adapter_cls)
            if shaper is not None:
                shaper(state, max_tokens, response_schema)
            worst = worst_case_call_usd(  # r55：送前用「輸出上限全用滿」估最壞花費
                adapter.telemetry_model(state, request), _wire_payload(state),
                _wire_max_output(state, max_tokens),
            )
            if autonomy_service.would_exceed(identity, worst):
                return _failure(REASON_OVER_BUDGET)
            raw = await _send_once(adapter, state)
    except TimeoutError:
        return _failure(REASON_TIMEOUT)
    except ToukaException:
        return _failure(REASON_MODEL_UNAVAILABLE)
    except Exception:
        logger.exception("complete_once(%s, %s) failed before a response", purpose, model)
        return _failure(REASON_INTERNAL)
    finally:
        if state is not None:
            await adapter.close(state)
        if client is not None:
            await client.close()

    if isinstance(raw, RoundError):
        logger.warning(
            "complete_once(%s, %s) upstream error: %s",
            purpose,
            model,
            raw.user_message[:_LOG_DETAIL_LIMIT],
        )
        return _failure(REASON_UPSTREAM)
    try:
        usage, _total_input, _output = adapter.extract_usage(state, raw)
    except Exception:
        logger.exception("complete_once(%s, %s) could not read usage", purpose, model)
        return _failure(REASON_INTERNAL)

    cost = 0.0
    if usage:
        telemetry_model = adapter.telemetry_model(state, request)
        cost = estimate_cost_usd(
            usage["prompt_tokens"],
            usage["completion_tokens"],
            model=telemetry_model,
            cache_read_tokens=usage["cache_read_input_tokens"],
            cache_write_tokens=usage["cache_creation_input_tokens"],
        )
        _record_usage(identity, purpose, usage, telemetry_model, started)
    try:  # 記帳在解析之前：回應讀不懂也已計費
        stop = adapter.stop_reason(state, raw)
        text = adapter.parse_round(state, raw).visible_text if stop == "end" else ""
    except Exception:
        logger.exception("complete_once(%s, %s) could not read the response", purpose, model)
        return _failure(REASON_INTERNAL, usage=usage, cost_usd=cost)
    if stop != "end":
        reason = REASON_UNKNOWN_STOP if stop == "error" else stop
        return _failure(reason, stop_reason=stop, usage=usage, cost_usd=cost)
    return CompletionResult(text=text, usage=usage, cost_usd=cost, stop_reason="end", error=None)

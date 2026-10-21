"""MCP 2026-07-28 ephemeral multi-round-trip driver（P7）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from mcp import types
from src.protocols.mcp.canonical import digest_mcp_arguments as _digest_mcp_arguments

if TYPE_CHECKING:
    from src.protocols.mcp.canonical import McpRequestContext

_ResultT = TypeVar("_ResultT")

DEFAULT_MAX_ROUNDS = 8
DEFAULT_ROUND_TIMEOUT_SECONDS = 120.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_INPUT_REQUESTS = 8
DEFAULT_MAX_INPUT_BYTES = 128 * 1024
DEFAULT_MAX_STATE_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 128 * 1024
_STATE_ONLY_BACKOFF_SECONDS = 0.05


class McpMrtrError(RuntimeError):
    """MRTR 生命週期失敗的基底類別。"""


class McpMrtrRoundsExceededError(McpMrtrError):
    """上游 input_required 超過本機 round cap。"""

    def __init__(self) -> None:
        super().__init__("MCP input_required exceeded the local round limit.")


class McpMrtrLimitError(McpMrtrError):
    """上游 input/state 或本機 response 超過資源上限。"""

    def __init__(self) -> None:
        super().__init__("MCP input_required exceeded a local resource limit.")


class McpMrtrAuthorizationError(McpMrtrError):
    """等待期間 Gate 1／Gate 2 或 installation generation 已失效。"""

    def __init__(self) -> None:
        super().__init__("MCP continuation authorization was revoked.")


class McpInteractionTimeoutError(McpMrtrError):
    """MRTR interaction 或整體生命週期逾時。"""

    def __init__(self) -> None:
        super().__init__("MCP interaction timed out.")


class McpInteractionUnsupportedError(McpMrtrError):
    """Touka 無法安全履行的 input kind。"""

    def __init__(self) -> None:
        super().__init__("MCP input kind or schema is not supported by Touka.")


@dataclass(frozen=True)
class McpMrtrLimits:
    """單次 ephemeral MRTR 的明確資源與期限上限。"""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    round_timeout_seconds: float = DEFAULT_ROUND_TIMEOUT_SECONDS
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS
    max_input_requests: int = DEFAULT_MAX_INPUT_REQUESTS
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class McpMrtrBinding:
    """MRTR callback 的不可變安全綁定；不含 opaque requestState。"""

    request_context: McpRequestContext
    installation_generation: int
    operation_method: str
    operation_name: str
    arguments_digest: str


class McpInteractionBroker(Protocol):
    """MRTR input request → Touka 行程內 interaction 的抽象。"""

    async def respond(
        self,
        key: str,
        request: types.InputRequest,
        binding: McpMrtrBinding,
    ) -> types.InputResponse:
        """取得人類回應或 fail-loud。"""
        ...


McpMrtrAuthorizer = Callable[[McpMrtrBinding], Awaitable[None]]
McpMrtrRetry = Callable[
    [types.InputResponses | None, str | None],
    Awaitable[_ResultT | types.InputRequiredResult],
]


def digest_mcp_arguments(arguments: Any) -> str:
    """Compatibility export for the canonical authorization digest SSOT。"""
    return _digest_mcp_arguments(arguments)


def _encoded_size(value: Any) -> int:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=True)

    def _json_default(item: Any) -> Any:
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json", by_alias=True, exclude_none=True)
        raise TypeError

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    )


class McpMrtrDriver:
    """同 coroutine、非持久化 MRTR driver；每輪 retry 前重驗 Touka 授權。"""

    def __init__(
        self,
        *,
        broker: McpInteractionBroker,
        authorize: McpMrtrAuthorizer,
        limits: McpMrtrLimits | None = None,
    ) -> None:
        self._broker = broker
        self._authorize = authorize
        self._limits = limits or McpMrtrLimits()

    async def drive(
        self,
        first: _ResultT | types.InputRequiredResult,
        *,
        retry: McpMrtrRetry[_ResultT],
        binding: McpMrtrBinding,
    ) -> _ResultT:
        """完成 input_required loop；requestState 只存在此 stack frame。"""
        if not isinstance(first, types.InputRequiredResult):
            return first
        try:
            async with asyncio.timeout(self._limits.total_timeout_seconds):
                return await self._drive_rounds(
                    first,
                    retry=retry,
                    binding=binding,
                )
        except TimeoutError as exc:
            raise McpInteractionTimeoutError() from exc

    async def _drive_rounds(
        self,
        first: types.InputRequiredResult,
        *,
        retry: McpMrtrRetry[_ResultT],
        binding: McpMrtrBinding,
    ) -> _ResultT:
        current: _ResultT | types.InputRequiredResult = first
        rounds = 0
        while isinstance(current, types.InputRequiredResult):
            rounds += 1
            if rounds > self._limits.max_rounds:
                raise McpMrtrRoundsExceededError()
            self._validate_input_required(current)
            responses = await asyncio.wait_for(
                self._dispatch_round(current, binding),
                timeout=self._limits.round_timeout_seconds,
            )
            await asyncio.wait_for(
                self._authorize(binding),
                timeout=self._limits.round_timeout_seconds,
            )
            current = await asyncio.wait_for(
                retry(responses, current.request_state),
                timeout=self._limits.round_timeout_seconds,
            )
        return current

    def _validate_input_required(self, result: types.InputRequiredResult) -> None:
        requests = result.input_requests or {}
        if len(requests) > self._limits.max_input_requests:
            raise McpMrtrLimitError()
        if _encoded_size(requests) > self._limits.max_input_bytes:
            raise McpMrtrLimitError()
        state = result.request_state
        if state is not None and len(state.encode("utf-8")) > self._limits.max_state_bytes:
            raise McpMrtrLimitError()

    async def _dispatch_round(
        self,
        result: types.InputRequiredResult,
        binding: McpMrtrBinding,
    ) -> types.InputResponses | None:
        requests = result.input_requests or {}
        if not requests:
            await asyncio.sleep(_STATE_ONLY_BACKOFF_SECONDS)
            return None

        responses: types.InputResponses = {}

        async def _respond(key: str, request: types.InputRequest) -> None:
            response = await self._broker.respond(key, request, binding)
            if _encoded_size(response) > self._limits.max_response_bytes:
                raise McpMrtrLimitError()
            responses[key] = response

        tasks = [
            asyncio.create_task(_respond(key, request))
            for key, request in requests.items()
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if _encoded_size(responses) > self._limits.max_response_bytes:
            raise McpMrtrLimitError()
        return responses

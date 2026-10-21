"""單次、低額度、無 fallback 的人工 provider chat probe（ADR-0214）。

這不是自動 smoke。每次執行都必須重新取得人類對精確 provider/model/上限的同意：

    python tests/smoke/manual/live_chat_probe.py \
        --provider openai --model gpt-5.2 --max-tokens 64 \
        --human-approved-live-api [--env-file <金鑰 .env 路徑>]

probe 只送固定合成 prompt，不載入真實對話；所有本機 write root 先切到程序專屬 sandbox。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
DEFAULT_ENV_FILE = PROXY_DIR / ".env"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROXY_DIR))
sys.path.insert(0, str(MEMORY_SRC_DIR))

from tests.live_api_policy import (  # noqa: E402
    LIVE_API_APPROVAL_FLAG,
    LiveApiCallBudget,
    LiveApiScope,
    format_live_api_scope,
    issue_live_api_approval,
    preflight_live_api_cli,
    validate_live_api_approval,
)

if TYPE_CHECKING:
    from tests.live_api_policy import LiveApiApproval


class _EnvFileRejected(SystemExit):
    def __init__(self, given: str) -> None:
        super().__init__(f"--env-file 必須是既有的一般檔案：{given}")


def _resolve_env_file(argv: Sequence[str]) -> Path:
    """--env-file 給了就必須是既有一般檔案（回傳實際路徑）；沒給維持預設。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file")
    given = parser.parse_known_args(argv)[0].env_file
    if given is None:
        return DEFAULT_ENV_FILE
    if not Path(given).is_file():
        raise _EnvFileRejected(given)
    return Path(given).resolve(strict=True)


# 最早期人工閘：缺旗標時，連 dotenv 與 provider-capable 模組都不 import；
# --env-file 不合格也在任何 provider 模組 import 前停下。
_ENV_FILE = DEFAULT_ENV_FILE
if __name__ == "__main__":
    preflight_live_api_cli(sys.argv[1:])
    _ENV_FILE = _resolve_env_file(sys.argv[1:])

from dotenv import load_dotenv  # noqa: E402

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

_SANDBOX_ROOT = ""
if __name__ == "__main__":
    _SANDBOX_ROOT = enable_smoke_sandbox("manual_live_chat", allow_external_network=True)
    load_dotenv(_ENV_FILE)

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient  # noqa: E402
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage  # noqa: E402
from src.config import ANTHROPIC_API_BASE_URL  # noqa: E402
from src.handlers import react_core  # noqa: E402
from src.handlers.adapters.claude_adapter import ClaudeAdapter  # noqa: E402
from src.handlers.adapters.gemini_adapter import GeminiAdapter  # noqa: E402
from src.handlers.adapters.openai_adapter import OpenAIAdapter  # noqa: E402
from src.handlers.adapters.responses_adapter import ResponsesAdapter  # noqa: E402
from src.handlers.adapters.stream_timeout import liveness_event_hooks  # noqa: E402
from src.models.context import PipelineContext  # noqa: E402
from src.models.identity import AgentIdentity  # noqa: E402
from src.services.dispatcher import dispatch_request  # noqa: E402
from src.services.providers import registry as provider_registry  # noqa: E402
from src.utils.time_utils import get_current_epoch  # noqa: E402

MAX_LIVE_TOKENS = 128
DEFAULT_LIVE_TOKENS = 64
_FIXED_PROMPT = "請只回覆：LIVE_PROBE_OK"
_ERROR_MARKERS = (
    '"error"',
    "api error",
    "access denied",
    "dispatcher error",
    "missing `",
    "connection error",
    "api 發生錯誤",
    "連線錯誤",
)
_ORIGINAL_CLAUDE_SEND_ROUND = ClaudeAdapter.send_round
_ORIGINAL_OPENAI_SEND_ROUND = OpenAIAdapter.send_round
_ORIGINAL_GEMINI_SEND_ROUND = GeminiAdapter.send_round
_ORIGINAL_GEMINI_PREPARE_REQUEST = GeminiAdapter.prepare_request
_ORIGINAL_RESPONSES_SEND_ROUND = ResponsesAdapter.send_round
# 主迴圈的四家共用重試開關（react_core.MAX_SEND_ATTEMPTS 是總送出次數）：只送 1 次、不重試。
_PROBE_SEND_ATTEMPTS = 1
_PROVIDER_REQUEST_BUDGET: LiveApiCallBudget | None = None


class _TokenBoundsError(argparse.ArgumentTypeError):
    def __init__(self) -> None:
        super().__init__(f"max-tokens 必須介於 1 到 {MAX_LIVE_TOKENS}")


class _NonObjectProviderResponse(TypeError):
    def __init__(self) -> None:
        super().__init__("provider probe returned a non-object response")


class _ProviderErrorResponse(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider probe returned an error response")


class _UnexpectedProbeAcknowledgement(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider probe did not return expected acknowledgement")


class _ProviderBudgetNotInitialized(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider request budget is not initialized")


class _ProviderRouteMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("provider/model route mismatch or unsupported provider")


def _consume_provider_request() -> None:
    if _PROVIDER_REQUEST_BUDGET is None:
        raise _ProviderBudgetNotInitialized
    _PROVIDER_REQUEST_BUDGET.consume()


async def _bounded_claude_send_round(
    adapter: ClaudeAdapter,
    state: Any,
) -> AsyncGenerator[Any]:
    _consume_provider_request()
    async for item in _ORIGINAL_CLAUDE_SEND_ROUND(adapter, state):
        yield item


async def _bounded_openai_send_round(
    adapter: OpenAIAdapter,
    state: Any,
) -> AsyncGenerator[Any]:
    _consume_provider_request()
    async for item in _ORIGINAL_OPENAI_SEND_ROUND(adapter, state):
        yield item


async def _bounded_responses_send_round(
    adapter: ResponsesAdapter,
    state: Any,
) -> AsyncGenerator[Any]:
    _consume_provider_request()
    async for item in _ORIGINAL_RESPONSES_SEND_ROUND(adapter, state):
        yield item


async def _bounded_gemini_send_round(adapter: GeminiAdapter, state: Any) -> AsyncGenerator[Any]:
    _consume_provider_request()
    async for item in _ORIGINAL_GEMINI_SEND_ROUND(adapter, state):
        yield item


async def _capped_gemini_prepare_request(
    adapter: GeminiAdapter,
    request: Any,
    context: Any,
) -> Any:
    """probe 行程內先檢查輸出上限範圍，再明確覆寫成這次的上限（產品轉換器也依請求帶入）。"""
    if not 1 <= request.max_tokens <= MAX_LIVE_TOKENS:
        raise _TokenBoundsError
    state = await _ORIGINAL_GEMINI_PREPARE_REQUEST(adapter, request, context)
    state.payload["generationConfig"] = {"maxOutputTokens": request.max_tokens}
    return state


def _install_one_request_bound(provider: str) -> None:
    global _PROVIDER_REQUEST_BUDGET
    _PROVIDER_REQUEST_BUDGET = LiveApiCallBudget(max_requests=1)
    react_core.MAX_SEND_ATTEMPTS = _PROBE_SEND_ATTEMPTS  # retry=0（四家同一個開關）
    if provider == "anthropic":
        ClaudeAdapter.send_round = _bounded_claude_send_round
    elif provider == "gemini":
        GeminiAdapter.prepare_request = _capped_gemini_prepare_request
        GeminiAdapter.send_round = _bounded_gemini_send_round
    else:  # OpenAI 家族：相容 Chat 與官方 Responses 路由都包住
        OpenAIAdapter.send_round = _bounded_openai_send_round
        ResponsesAdapter.send_round = _bounded_responses_send_round


def _bounded_tokens(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_LIVE_TOKENS:
        raise _TokenBoundsError
    return parsed


def _provider_for_model(model: str) -> str | None:
    lowered = model.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    spec = provider_registry.resolve_provider(model)
    return spec.id if spec is not None else None


def _live_chat_scope(
    *, model: str, max_tokens: int, provider: str, env_file: Path = DEFAULT_ENV_FILE
) -> LiveApiScope:
    return LiveApiScope(
        operation_id="manual_live_chat_probe",
        provider=provider,
        model=model,
        limits=(
            "provider_requests=1",
            f"max_tokens={max_tokens}",
            "retry=0",
            "fallback=0",
            f"env_file={env_file}",
        ),
        data_scope="one fixed synthetic prompt; no real conversation data",
        write_scope="temporary sandbox only",
    )


def _decode_response(response: Any) -> dict[str, Any]:
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise _NonObjectProviderResponse
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    if any(marker in serialized for marker in _ERROR_MARKERS):
        raise _ProviderErrorResponse
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _UnexpectedProbeAcknowledgement
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or content.strip() != "LIVE_PROBE_OK":
        raise _UnexpectedProbeAcknowledgement
    return payload


async def run_live_chat_probe(
    *,
    model: str,
    max_tokens: int,
    provider: str,
    approval: LiveApiApproval,
    env_file: Path = DEFAULT_ENV_FILE,
) -> dict[str, Any]:
    """直接走單模型 dispatcher；無 tools，所以恰好一個模型 round。"""
    if isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_LIVE_TOKENS:
        raise _TokenBoundsError
    resolved_provider = _provider_for_model(model)
    normalized_provider = provider.casefold()
    if resolved_provider != normalized_provider:
        raise _ProviderRouteMismatch
    validate_live_api_approval(
        approval,
        expected_scope=_live_chat_scope(
            model=model,
            max_tokens=max_tokens,
            provider=normalized_provider,
            env_file=env_file,
        ),
    )
    _install_one_request_bound(normalized_provider)
    request = OpenAIChatRequest(
        model=model,
        max_tokens=max_tokens,
        messages=[OpenAIMessage(role="user", content=_FIXED_PROMPT)],
        tools=None,
        stream=False,
        incognito=True,
    )
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    ctx = PipelineContext(
        agent_id="DebugAgent",
        identity=identity,
        is_toukasoul=False,
        enable_autonomy=False,
        incognito=True,
        request_id="manual-live-chat-probe",
        t0=get_current_epoch(),
    )
    anthropic_client: AsyncAnthropic | None = None
    if normalized_provider == "anthropic":
        anthropic_client = AsyncAnthropic(
            api_key=provider_registry.get_key_by_id("anthropic"),
            base_url=ANTHROPIC_API_BASE_URL,  # 不看 ANTHROPIC_BASE_URL（ADR-0247）
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(  # 禁令 22；閒置計時和主迴圈同一條（R-3b ② L7）
                follow_redirects=False, event_hooks=liveness_event_hooks()),
        )
        ctx["anthropic_client"] = anthropic_client
    try:
        response = await dispatch_request(
            model,
            request,
            identity,
            "manual-live-chat-probe",
            get_current_epoch(),
            ctx,
        )
        return _decode_response(response)
    finally:
        if anthropic_client is not None:
            await anthropic_client.close()


def main(argv: Sequence[str] | None = None) -> int:
    """驗證核准範圍並執行一次人工 live request。"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="One-shot human-approved live chat probe")
    parser.add_argument("--provider", required=True, help="核准的 provider id")
    parser.add_argument("--model", required=True, help="核准的精確 model id")
    parser.add_argument(
        "--max-tokens",
        type=_bounded_tokens,
        default=DEFAULT_LIVE_TOKENS,
        help=f"單次輸出上限（1..{MAX_LIVE_TOKENS}；預設 {DEFAULT_LIVE_TOKENS}）",
    )
    parser.add_argument("--env-file", help=f"金鑰 .env 路徑（預設 {DEFAULT_ENV_FILE}）")
    parser.add_argument(
        LIVE_API_APPROVAL_FLAG,
        action="store_true",
        help="確認已取得本次明確人類同意（不得由自動化傳入）",
    )
    args = parser.parse_args(argv)

    resolved_provider = _provider_for_model(args.model)
    if resolved_provider is None:
        parser.error("無法由 provider registry 判定此 model；拒絕未知路由")
    if args.provider.casefold() != resolved_provider:
        parser.error(
            f"核准 provider={args.provider} 與 model 實際路由 provider={resolved_provider} 不符"
        )

    env_file = _resolve_env_file(sys.argv[1:] if argv is None else argv)
    scope = _live_chat_scope(
        model=args.model,
        max_tokens=args.max_tokens,
        provider=resolved_provider,
        env_file=env_file,
    )
    approval = issue_live_api_approval(
        approved=args.human_approved_live_api,
        scope=scope,
    )
    sys.stdout.write(format_live_api_scope(scope) + "\n")
    sys.stdout.write(f"sandbox={_SANDBOX_ROOT}\n")

    payload = asyncio.run(
        run_live_chat_probe(
            model=args.model,
            max_tokens=args.max_tokens,
            provider=resolved_provider,
            approval=approval,
            env_file=env_file,
        )
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

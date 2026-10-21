"""Automated chat-pipeline smoke with a deterministic provider boundary mock."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("chat")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
for _path in (PROXY_DIR, MEMORY_SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from src.adapters.schemas import AnthropicRequest  # noqa: E402
from src.models.context import PipelineContext  # noqa: E402
from src.models.identity import AgentIdentity  # noqa: E402
from src.services.proxy_service import process_proxy_request  # noqa: E402

_MOCK_TEXT = "deterministic-provider-ok"


class ChatSmokeFailure(RuntimeError):
    """The deterministic pipeline contract did not hold."""


async def run_smoke_test() -> None:
    """Exercise the real middleware pipeline without any provider/network request."""
    sys.stdout.write("[*] Starting deterministic chat pipeline smoke\n")
    request = AnthropicRequest(
        model="mock-provider-chat",
        max_tokens=32,
        messages=[{"role": "user", "content": "deterministic smoke prompt"}],
        stream=False,
    )
    ctx = PipelineContext(
        agent_id="DebugAgent",
        role="Debugger",
        identity=AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger"),
        is_toukasoul=False,
        incognito=True,
        request_id="smoke-chat-mock",
        t0=asyncio.get_running_loop().time(),
    )
    fake_response = JSONResponse(content={"content": [{"type": "text", "text": _MOCK_TEXT}]})

    with patch(
        "src.services.proxy_service.dispatch_request",
        new_callable=AsyncMock,
        return_value=fake_response,
    ) as dispatch_mock:
        response = await process_proxy_request(request, ctx, is_anthropic=True)

    dispatch_mock.assert_awaited_once()
    body = json.loads(response.body)
    if body != {"content": [{"type": "text", "text": _MOCK_TEXT}]}:
        message = f"Unexpected deterministic response: {body!r}"
        raise ChatSmokeFailure(message)
    if ctx.get("is_native") is not True:
        message = "Anthropic-compatible pipeline did not set is_native"
        raise ChatSmokeFailure(message)
    sys.stdout.write(
        "[SUCCESS] Middleware pipeline reached the mocked provider boundary exactly once.\n"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    asyncio.run(run_smoke_test())

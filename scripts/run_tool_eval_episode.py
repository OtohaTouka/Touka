"""手動腳本：跑「LIVE episode」——真實 in-process producer 子 loop（不進 CI、不排程）。

與 record_tool_eval_baseline.py（select-only：只選不執行）不同：本腳本跑**完整
episode**（ADR-0136）——真實 pipeline 組裝（persona/soul 依身分注入）、工具真的
執行、多輪 ReAct，回傳 tool calls＋最終文字＋Phase 2 回報塊＋artifact 存在檢查。
消耗真實 API token；工具執行受既有權限 Gate 1/2 約束。
所有 app data、workspace、config 與工具寫入都改指程序專屬暫存 sandbox；
`--artifact-root` 若提供，必須是該 sandbox 內的相對路徑。

用法（於倉庫根目錄、.venv 3.12）：
    python scripts/run_tool_eval_episode.py --list-cases
    python scripts/run_tool_eval_episode.py --case read-soul-owner --model gpt-5.2 \
        --human-approved-live-api
    python scripts/run_tool_eval_episode.py --case <id> --model <m> --artifact-root artifacts \
        --human-approved-live-api --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ToukaProxy"))
sys.path.insert(0, str(_ROOT / "ToukaMemory" / "src"))

from tests.live_api_policy import (  # noqa: E402
    LIVE_API_APPROVAL_FLAG,
    LiveApiScope,
    preflight_live_api_cli,
    require_live_api_approval,
)

if __name__ == "__main__":
    preflight_live_api_cli(sys.argv[1:], local_only_flags=("--list-cases",))

from dotenv import load_dotenv  # noqa: E402

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

_SANDBOX_ROOT = ""
if __name__ == "__main__":
    _SANDBOX_ROOT = enable_smoke_sandbox(
        "tool_eval_live_episode", allow_external_network=LIVE_API_APPROVAL_FLAG in sys.argv
    )
    load_dotenv(_ROOT / "ToukaProxy" / ".env")

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient  # noqa: E402
from src.config import ANTHROPIC_API_BASE_URL  # noqa: E402
from src.handlers import react_core  # noqa: E402
from src.services.providers import registry as provider_registry  # noqa: E402

from tests.tool_eval import live_episode  # noqa: E402
from tests.tool_eval.cases import SEED_CASES  # noqa: E402

_MAX_PROVIDER_REQUESTS = 16
_MAX_EPISODE_TOKENS = 512
_UNBOUNDED_EXTERNAL_CASES = frozenset({"research-auto"})
# 主迴圈的四家共用重試開關（react_core.MAX_SEND_ATTEMPTS 是總送出次數）：只送 1 次、不重試。
_LIVE_SEND_ATTEMPTS = 1


class _BoundedIntegerError(argparse.ArgumentTypeError):
    def __init__(self, label: str, maximum: int) -> None:
        super().__init__(f"{label} 必須介於 1 到 {maximum}")


class _TimeoutBoundsError(argparse.ArgumentTypeError):
    def __init__(self) -> None:
        super().__init__("timeout 必須介於 1 到 600 秒")


class _AbsoluteArtifactRootError(ValueError):
    def __init__(self) -> None:
        super().__init__("artifact_root must be relative to the live sandbox")


class _EscapedArtifactRootError(ValueError):
    def __init__(self) -> None:
        super().__init__("artifact_root escaped the live sandbox")


def _bounded_positive_int(value: str, *, maximum: int, label: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= maximum:
        raise _BoundedIntegerError(label, maximum)
    return parsed


def _bounded_request_count(value: str) -> int:
    return _bounded_positive_int(
        value,
        maximum=_MAX_PROVIDER_REQUESTS,
        label="max-provider-requests",
    )


def _bounded_tokens(value: str) -> int:
    return _bounded_positive_int(value, maximum=_MAX_EPISODE_TOKENS, label="max-tokens")


def _resolve_sandbox_artifact_root(raw: str | None, sandbox_root: str) -> Path | None:
    if raw is None:
        return None
    relative = Path(raw)
    if relative.is_absolute():
        raise _AbsoluteArtifactRootError
    root = Path(sandbox_root).resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise _EscapedArtifactRootError
    return resolved


async def _run_bounded_episode(
    *,
    case: Any,
    model: str,
    agent_id: str,
    artifact_root: Path | None,
    provider: str,
    max_provider_requests: int,
    max_tokens: int,
    timeout_seconds: float,
) -> Any:
    react_core.MAX_SEND_ATTEMPTS = _LIVE_SEND_ATTEMPTS  # provider_retry=0（429／5xx 不重送）
    anthropic_client: AsyncAnthropic | None = None
    if provider == "anthropic":
        anthropic_client = AsyncAnthropic(
            api_key=provider_registry.get_key_by_id("anthropic"),
            base_url=ANTHROPIC_API_BASE_URL,  # 不看 ANTHROPIC_BASE_URL（ADR-0247）
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),  # 禁令 22
        )
    limits = live_episode.LiveEpisodeLimits(
        provider=provider,
        max_tokens=max_tokens,
        max_provider_requests=max_provider_requests,
        anthropic_client=anthropic_client,
    )
    try:
        return await asyncio.wait_for(
            live_episode.run_case_live_episode(
                case,
                model=model,
                agent_id=agent_id,
                artifact_root=artifact_root,
                limits=limits,
            ),
            timeout=timeout_seconds,
        )
    finally:
        if anthropic_client is not None:
            await anthropic_client.close()


def _bounded_timeout(value: str) -> float:
    parsed = float(value)
    if not 1 <= parsed <= 600:
        raise _TimeoutBoundsError
    return parsed


def _provider_label(model: str) -> str:
    lowered = model.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    spec = provider_registry.resolve_provider(model)
    return spec.id if spec is not None else "custom-model-router"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Run one LIVE tool-eval episode")
    parser.add_argument("--case", default=None, help="eval case id（見 --list-cases）")
    parser.add_argument("--model", default=None, help="子 loop 底層模型（實跑必填）")
    parser.add_argument(
        "--agent",
        default=None,
        help="覆寫身分；本人工診斷只允許 DebugAgent",
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="sandbox 內的相對 artifact 檢查根（省略＝跳過檢查）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出完整觀測")
    parser.add_argument("--list-cases", action="store_true", help="列出可用 case id")
    parser.add_argument(
        "--timeout-seconds",
        type=_bounded_timeout,
        default=180.0,
        help="整個 episode wall-clock 上限（1..600；預設 180）",
    )
    parser.add_argument(
        "--max-provider-requests",
        type=_bounded_request_count,
        default=3,
        help="本次模型 provider request 硬上限（1..16；預設 3）",
    )
    parser.add_argument(
        "--max-tokens",
        type=_bounded_tokens,
        default=128,
        help="每個 model turn 的輸出上限（1..512；預設 128）",
    )
    parser.add_argument(
        LIVE_API_APPROVAL_FLAG,
        action="store_true",
        help="確認已取得本次明確人類同意（不得由自動化傳入）",
    )
    args = parser.parse_args(argv)

    if args.list_cases:
        for case in SEED_CASES:
            print(f"  - {case.id}: {case.title}")  # noqa: T201
        return 0

    if not args.case or not args.model:
        parser.error("--case 與 --model 為實跑必填（或用 --list-cases）")

    case = next((c for c in SEED_CASES if c.id == args.case), None)
    if case is None:
        print(f"❌ 找不到 case '{args.case}'（--list-cases 查看）")  # noqa: T201
        return 1
    if case.id in _UNBOUNDED_EXTERNAL_CASES:
        parser.error(
            "此 case 會使用另一個未具 request hard-limit 的外部搜尋 API；本 CLI fail-closed"
        )

    effective_agent = args.agent or case.context.get("agent_id", "DebugAgent")
    if effective_agent != "DebugAgent":
        parser.error("LIVE tool-eval 寫入邊界只允許 DebugAgent")

    provider = _provider_label(args.model)
    if provider == "gemini":
        parser.error("Gemini adapter 目前含內建 retry，無法證明精確 request 上限；fail-closed")
    scope = LiveApiScope(
        operation_id="tool_eval_live_episode",
        provider=provider,
        model=args.model,
        limits=(
            f"case={case.id}",
            "episodes=1",
            f"provider_requests<={args.max_provider_requests}",
            f"max_tokens_per_model_turn={args.max_tokens}",
            f"wall_timeout_seconds={args.timeout_seconds:g}",
            "provider_retry=0",
            "main_loop_send_attempts=1",  # 由 _run_bounded_episode 設定
            "fallback=0",
        ),
        data_scope="selected fixed eval prompt and temporary sandbox data only",
        write_scope="temporary sandbox only",
    )
    print(  # noqa: T201
        require_live_api_approval(
            approved=args.human_approved_live_api,
            scope=scope,
        )
    )
    print(f"sandbox={_SANDBOX_ROOT}")  # noqa: T201

    try:
        root = _resolve_sandbox_artifact_root(args.artifact_root, _SANDBOX_ROOT)
    except ValueError as exc:
        parser.error(str(exc))
    episode = asyncio.run(
        _run_bounded_episode(
            case=case,
            model=args.model,
            agent_id=effective_agent,
            artifact_root=root,
            provider=provider,
            max_provider_requests=args.max_provider_requests,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
        )
    )

    if args.json:
        payload = {
            "case_id": case.id,
            "tool_calls": [asdict(c) for c in episode.tool_calls],
            "final_text": episode.final_text,
            "report": episode.report.model_dump(),
            "artifacts_present": episode.artifacts_present,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))  # noqa: T201
        return 0

    print(f"📋 case={case.id} tool_calls={len(episode.tool_calls)}")  # noqa: T201
    for call in episode.tool_calls:
        print(f"  - {call.name} [{call.status}] {json.dumps(call.args, ensure_ascii=False)[:120]}")  # noqa: T201
    print(f"📝 report: status={episode.report.status} structured={episode.report.structured}")  # noqa: T201
    for claim in episode.report.claims:
        print(f"  • {claim}")  # noqa: T201
    if episode.artifacts_present:
        print(f"📦 artifacts: {episode.artifacts_present}")  # noqa: T201
    print("---")  # noqa: T201
    print(episode.final_text[:2000])  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

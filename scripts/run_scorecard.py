r"""手動執行模型計分卡（軌B）。

⚠️ 會驅動**真實模型**並花費 API 費用。手動觸發、不進 CI／guard_all／冒煙套件。
模型 prompt、工具資料與所有本機 write root 均落程序專屬暫存 sandbox；
`web_search` 與 `docgen_xlsx` 因額外外連／產物邊界不在本核准範圍而 fail-closed。

用法：
    python scripts/run_scorecard.py --model deepseek-chat --budget 0.50 \
        --human-approved-live-api
    python scripts/run_scorecard.py --model claude-3-5-sonnet --budget 0.20 \
        --human-approved-live-api \
        --scenarios chat_basic,chat_reasoning
"""
# ruff: noqa: E402, T201  路徑注入後才能 import 業務模組；CLI 以 print 輸出成績單
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

# 路徑設置（SSOT，比照冒煙測試）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROXY_DIR))
sys.path.insert(0, str(MEMORY_SRC_DIR))

from tests.live_api_policy import (
    LIVE_API_APPROVAL_FLAG,
    LiveApiCallBudget,
    LiveApiScope,
    preflight_live_api_cli,
    require_live_api_approval,
)

# 真正以 CLI 執行時，必須在 dotenv／任何 provider-capable 業務模組 import 前 fail-closed。
if __name__ == "__main__":
    preflight_live_api_cli(sys.argv[1:])

from dotenv import load_dotenv

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox

_SANDBOX_ROOT = ""
if __name__ == "__main__":
    _SANDBOX_ROOT = enable_smoke_sandbox("scorecard_live", allow_external_network=True)
    load_dotenv(PROXY_DIR / ".env")

from src.config import strip_anthropic_sdk_env_overrides
from src.handlers import react_core
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.main import build_base_context
from src.services import proxy_service
from src.services.identity_service import identity_service
from src.services.providers import registry as provider_registry
from src.services.scorecard import formatter, run_scorecard
from src.services.scorecard.runner import make_real_runner
from src.services.scorecard.scenarios import select_scenarios

_MAX_PROVIDER_REQUESTS = 64
_BLOCKED_SCENARIOS = frozenset({"docgen_xlsx", "web_search"})
_ORIGINAL_CLAUDE_SEND_ROUND = ClaudeAdapter.send_round
_ORIGINAL_OPENAI_SEND_ROUND = OpenAIAdapter.send_round
# 主迴圈的四家共用重試開關（react_core.MAX_SEND_ATTEMPTS 是總送出次數）：只送 1 次、不重試。
_LIVE_SEND_ATTEMPTS = 1
_PROVIDER_REQUEST_BUDGET: LiveApiCallBudget | None = None


class _ProviderRequestCountError(argparse.ArgumentTypeError):
    def __init__(self) -> None:
        super().__init__(
            f"max-provider-requests 必須介於 1 到 {_MAX_PROVIDER_REQUESTS}"
        )


class _PositiveBudgetError(argparse.ArgumentTypeError):
    def __init__(self) -> None:
        super().__init__("budget 必須大於 0")


class _ProviderBudgetNotInitialized(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider request budget is not initialized")


def _bounded_request_count(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= _MAX_PROVIDER_REQUESTS:
        raise _ProviderRequestCountError
    return parsed


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


def _install_provider_request_budget(max_requests: int) -> None:
    global _PROVIDER_REQUEST_BUDGET
    _PROVIDER_REQUEST_BUDGET = LiveApiCallBudget(max_requests=max_requests)
    react_core.MAX_SEND_ATTEMPTS = _LIVE_SEND_ATTEMPTS  # provider_retry=0（429／5xx 不重送）
    ClaudeAdapter.send_round = _bounded_claude_send_round
    OpenAIAdapter.send_round = _bounded_openai_send_round


def _positive_budget(value: str) -> float:
    budget = float(value)
    if budget <= 0:
        raise _PositiveBudgetError
    return budget


def _provider_label(model: str) -> str:
    lowered = model.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    spec = provider_registry.resolve_provider(model)
    return spec.id if spec is not None else "custom-model-router"


def _no_fallback(_identity: object) -> None:
    """本人工 CLI 只允許核准的 model，不讀取另一個 last-working brain。"""
    pass


async def _run(
    model: str,
    budget: float,
    scenario_ids: list[str] | None,
) -> None:
    scenarios = select_scenarios(scenario_ids)
    print(f"[*] 計分卡：model={model} budget=${budget} 情境={[s.id for s in scenarios]}")
    # 與生產同源（ADR-0101）：base context 取自 build_base_context（SSOT），身分用
    # identity_service 解析（DebugAgent→Debugger→Owner 工具面，資料仍歸 DebugAgent）。
    # ⚠️ CLI 未連線 MCP（未跑 initialize_all）→ 記憶／內建工具齊備；需 live MCP 覆蓋請用 UI 端點。
    base_context = build_base_context()
    anthropic_client = base_context.get("anthropic_client")
    if anthropic_client is not None:
        # Anthropic SDK 的隱式 retry 會繞過 request count；人工量測一律設為 0。
        strip_anthropic_sdk_env_overrides()  # 副本建構時 SDK 會重讀環境（ADR-0247）
        base_context["anthropic_client"] = anthropic_client.with_options(max_retries=0)
    runner = make_real_runner(
        base_context,
        identity_service.get_identity("DebugAgent"),
        clean_slate=True,
    )
    card = await run_scorecard(
        model, budget, scenarios, runner,
        on_progress=lambda ev: print(f"  → {ev}"),
    )
    print("\n" + formatter.to_markdown(card))
    print("\n--- JSON ---")
    print(json.dumps(formatter.to_json(card), ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    """解析人工核准範圍後才驅動真實 scorecard。"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="模型計分卡（軌B，手動觸發）")
    parser.add_argument("--model", required=True, help="要評測的模型 ID（如 deepseek-chat）")
    parser.add_argument(
        "--budget",
        type=_positive_budget,
        default=0.50,
        help="單輪累計預算上限（USD）",
    )
    parser.add_argument("--scenarios", default="", help="逗號分隔的情境 id（空＝全部核心情境）")
    parser.add_argument(
        "--max-provider-requests",
        type=_bounded_request_count,
        default=None,
        help="本次模型 provider request 硬上限（1..64；預設為情境數 x 2）",
    )
    parser.add_argument(
        LIVE_API_APPROVAL_FLAG,
        action="store_true",
        help="確認已取得本次明確人類同意（不得由自動化傳入）",
    )
    args = parser.parse_args(argv)
    ids = [s.strip() for s in args.scenarios.split(",") if s.strip()] or None
    scenarios = select_scenarios(ids)
    if not scenarios:
        parser.error("--scenarios 沒有任何已知情境")
    unsafe_scenarios = sorted(
        scenario.id for scenario in scenarios if scenario.id in _BLOCKED_SCENARIOS
    )
    if unsafe_scenarios:
        if ids is not None:
            parser.error(
                "下列情境的額外外連或非 DebugAgent artifact write 無法落入本次硬界，"
                f"本 CLI fail-closed：{', '.join(unsafe_scenarios)}"
            )
        scenarios = tuple(
            scenario
            for scenario in scenarios
            if scenario.id not in _BLOCKED_SCENARIOS
        )
        ids = [scenario.id for scenario in scenarios]

    max_provider_requests = args.max_provider_requests or max(1, len(scenarios) * 2)
    provider = _provider_label(args.model)
    if provider == "gemini":
        parser.error("Gemini adapter 目前含內建 retry，無法證明精確 request 上限；fail-closed")

    max_tokens = max(scenario.max_tokens for scenario in scenarios)
    scope = LiveApiScope(
        operation_id="scorecard_cli",
        provider=provider,
        model=args.model,
        limits=(
            f"scenarios={len(scenarios)}",
            f"budget_usd={args.budget:.4f}",
            f"provider_requests<={max_provider_requests}",
            f"max_tokens_per_model_turn={max_tokens}",
            "provider_retry=0",
            "main_loop_send_attempts=1",  # 由 _install_provider_request_budget 設定
            "fallback=0",
        ),
        data_scope="selected fixed scorecard prompts and temporary sandbox data only",
        write_scope="temporary sandbox only",
    )
    print(
        require_live_api_approval(
            approved=args.human_approved_live_api,
            scope=scope,
        )
    )
    print(f"sandbox={_SANDBOX_ROOT}")

    # scorecard pipeline 原本可讀 last-working brain；人工核准只涵蓋 args.model，故關閉 fallback。
    proxy_service.get_last_working_brain = _no_fallback
    _install_provider_request_budget(max_provider_requests)
    asyncio.run(_run(args.model, args.budget, ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

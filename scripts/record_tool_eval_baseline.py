"""手動腳本：錄「工具調用 select-only」LIVE 基準（不進 CI、不排程）。

以真實 Gate 1 組出的廣告工具 schema 對 LIVE 模型量測工具選擇品質，
工具永不執行（select-only）。結果只落程序專屬暫存目錄；提升為正式 baseline 是另一次人工動作。

用法（於倉庫根目錄、.venv 3.12）：
    python scripts/record_tool_eval_baseline.py --dry-run       # 只組工具，不打 API
    python scripts/record_tool_eval_baseline.py --list-models \
        --human-approved-live-api                               # 外部 API，通常不計 token
    python scripts/record_tool_eval_baseline.py --model gpt-5.2 \
        --max-tokens 128 --human-approved-live-api              # 實錄基準（消耗 token）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ToukaProxy"))
sys.path.insert(0, str(_ROOT / "ToukaMemory" / "src"))

from tests.live_api_policy import (  # noqa: E402
    LIVE_API_APPROVAL_FLAG,
    LiveApiCallBudget,
    LiveApiRequestBudgetRequired,
    LiveApiScope,
    preflight_live_api_cli,
    require_live_api_approval,
)

# --dry-run 是唯一免核准工作模式；--list-models 仍會帶憑證打 provider。
if __name__ == "__main__":
    preflight_live_api_cli(sys.argv[1:], local_only_flags=("--dry-run",))
    _config_sandbox = tempfile.TemporaryDirectory(prefix="touka_tool_eval_config_")
    _config_dir = Path(_config_sandbox.name) / "config"
    shutil.copytree(_ROOT / "ToukaProxy" / "config", _config_dir)
    _process_environment = os.environ  # noqa: ENV_SSOT_VIOLATION - manual CLI sandbox boundary
    _process_environment["TOUKA_CONFIG_DIR"] = str(_config_dir)
    _process_environment["TOUKA_BOOTSTRAP_ENABLED"] = "false"
    _process_environment["ENABLE_DYNAMIC_MODELS"] = "false"
    _process_environment["DISPATCH_AUTOSPAWN"] = "false"
    _process_environment["TOUKA_EVAL_JUDGE_ENABLED"] = "false"
    _process_environment["TAVILY_API_KEY"] = ""

import httpx  # noqa: E402
from src.utils.time_utils import get_current_taipei_iso  # noqa: E402

from tests.tool_eval import live_select_only as lso  # noqa: E402

_BASELINE_DIR = (
    Path(tempfile.mkdtemp(prefix="touka_tool_eval_result_"))
    if __name__ == "__main__"
    else _ROOT / "tests" / "tool_eval" / "baselines"
)
_MAX_REPETITIONS = 4
_MAX_OUTPUT_TOKENS = 512
_SUPPORTED_PROVIDERS = ("openai", "deepseek", "grok")


class _BoundedIntegerError(argparse.ArgumentTypeError):
    def __init__(self, label: str, maximum: int) -> None:
        super().__init__(f"{label} 必須介於 1 到 {maximum}")


def _bounded_int(value: str, *, label: str, maximum: int) -> int:
    parsed = int(value)
    if not 1 <= parsed <= maximum:
        raise _BoundedIntegerError(label, maximum)
    return parsed


def _bounded_repetition(value: str) -> int:
    return _bounded_int(value, label="samples/turns", maximum=_MAX_REPETITIONS)


def _bounded_tokens(value: str) -> int:
    return _bounded_int(value, label="max-tokens", maximum=_MAX_OUTPUT_TOKENS)


def _list_models(
    provider_id: str,
    request_budget: LiveApiCallBudget,
) -> None:
    if request_budget is None:
        raise LiveApiRequestBudgetRequired
    chat_url, api_key = lso.resolve_chat_endpoint(provider_id)
    models_url = chat_url.replace("/chat/completions", "/models")
    request_budget.consume()
    resp = httpx.get(models_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0)
    resp.raise_for_status()
    ids = sorted(m.get("id", "?") for m in resp.json().get("data", []))
    print(f"📋 {provider_id} 可用模型（{len(ids)}）:")  # noqa: T201
    for mid in ids:
        print(f"  - {mid}")  # noqa: T201


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Record tool-call select-only baseline")
    parser.add_argument(
        "--provider",
        choices=_SUPPORTED_PROVIDERS,
        default="openai",
        help="provider id（openai/deepseek/grok）",
    )
    parser.add_argument("--model", default=None, help="模型 id（實錄時必填）")
    parser.add_argument(
        "--samples",
        type=_bounded_repetition,
        default=1,
        help=f"每 case 取樣次數（1..{_MAX_REPETITIONS}；預設 1）",
    )
    parser.add_argument(
        "--turns",
        type=_bounded_repetition,
        default=1,
        help=(f"每 case 回合上限（1..{_MAX_REPETITIONS}；>1 為多回合且強制 samples=1）"),
    )
    parser.add_argument(
        "--max-tokens",
        type=_bounded_tokens,
        default=128,
        help=f"每 request 輸出 token 硬上限（1..{_MAX_OUTPUT_TOKENS}；預設 128）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只組廣告工具、不打 API")
    parser.add_argument("--list-models", action="store_true", help="列出供應商可用模型")
    parser.add_argument(
        LIVE_API_APPROVAL_FLAG,
        action="store_true",
        help="確認已取得本次明確人類同意（不得由自動化傳入）",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        info = lso.describe_assembly()
        print(json.dumps(info, ensure_ascii=False, indent=2))  # noqa: T201
        return 0

    if args.list_models:
        scope = LiveApiScope(
            operation_id="tool_eval_baseline_list_models",
            provider=args.provider,
            model="provider-model-catalog",
            limits=("provider_requests=1", "model_tokens=0", "retry=0", "fallback=0"),
            data_scope="provider model identifiers only",
            write_scope="none",
        )
        print(  # noqa: T201
            require_live_api_approval(
                approved=args.human_approved_live_api,
                scope=scope,
            )
        )
        _list_models(args.provider, LiveApiCallBudget(max_requests=1))
        return 0

    if not args.model:
        print("❌ 實錄需要 --model（先用 --list-models 挑一個）")  # noqa: T201
        return 1
    if args.turns > 1 and args.samples > 1:
        parser.error("--turns > 1 時 --samples 必須為 1")

    case_count = len(lso.SEED_CASES)
    max_requests = case_count * (args.turns if args.turns > 1 else args.samples)
    scope = LiveApiScope(
        operation_id="tool_eval_baseline_record",
        provider=args.provider,
        model=args.model,
        limits=(
            f"cases={case_count}",
            f"provider_requests<={max_requests}",
            f"samples={args.samples}",
            f"turns={args.turns}",
            f"max_tokens_per_request={args.max_tokens}",
            "retry=0",
            "fallback=0",
        ),
        data_scope="fixed tool-eval prompts and advertised tool schemas",
        write_scope="temporary result sandbox only; promotion is a separate human action",
    )
    print(  # noqa: T201
        require_live_api_approval(
            approved=args.human_approved_live_api,
            scope=scope,
        )
    )

    payload = lso.record_baseline(
        args.model,
        provider_id=args.provider,
        samples=args.samples,
        turns=args.turns,
        max_tokens=args.max_tokens,
        request_budget=LiveApiCallBudget(max_requests=max_requests),
    )

    _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = get_current_taipei_iso()[:10]  # YYYY-MM-DD（TIME_SSOT）
    model_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", args.model)
    suffix = f"-mt{args.turns}" if args.turns > 1 else ""
    out_path = _BASELINE_DIR / f"{date_str}-{args.provider}-{model_slug}-select_only{suffix}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(payload["report_text"])  # noqa: T201
    print(f"💾 基準已寫入：{out_path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())

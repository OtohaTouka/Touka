"""人工延遲實測（r55）：Claude 5.5 三型號「少想」與「模型預設」各多久回完、花多少。寫好不自動跑。

不是自動 smoke（pytest、CI、guard、run_all_smoke 都不跑）；每批都要重新取得人類同意（禁令 61）：

    python tests/smoke/manual/thinking_latency_probe.py --models claude-haiku-5-5,claude-sonnet-5-5,
        claude-opus-5-5 --profiles minimal,default --repeats 5 --max-provider-requests 30
        --max-tokens 1024 --budget 3.0 --human-approved-live-api [--env-file <金鑰 .env>]

呼叫產品的 complete_once：minimal＝用途 summarizer（用途表少想）、default＝critic（模型預設）。
缺核准旗標連 dotenv 與產品都不 import；先切沙盒才載入產品。send_round 外連前依序核對：實送
max_tokens ≤ 核准值與 8,192 → 已花＋最壞花費（該型號價，分檔取貴檔）≤ 上限 → 核銷請求預算。
任何請求失敗整批停、非零結束；報告只印字數不印原文，先印報告再刪沙盒（共用記憶評估 probe）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _path in (PROJECT_ROOT, PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    sys.path.insert(0, str(_path))

from tests.live_api_policy import (  # noqa: E402
    LIVE_API_APPROVAL_FLAG,
    LiveApiCallBudget,
    LiveApiScope,
    format_live_api_scope,
    issue_live_api_approval,
    preflight_live_api_cli,
    validate_live_api_approval,
)

DEFAULT_ENV_FILE = PROJECT_ROOT / "ToukaProxy" / ".env"


class EnvFileRejected(SystemExit):
    def __init__(self, given: str) -> None:
        super().__init__(f"--env-file 必須是既有的一般檔案：{given}")


def env_file_of(argv: list[str]) -> Path:
    given = argparse.ArgumentParser(add_help=False)
    given.add_argument("--env-file")
    path = given.parse_known_args(argv)[0].env_file
    if path is not None and not Path(path).is_file():
        raise EnvFileRejected(path)
    return DEFAULT_ENV_FILE if path is None else Path(path).resolve(strict=True)


if __name__ == "__main__":  # 最早期人工閘：缺旗標時下面的 import 都不會執行
    preflight_live_api_cli(sys.argv[1:])

from dotenv import load_dotenv  # noqa: E402

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

_SANDBOX_ROOT = ""
if __name__ == "__main__":  # 先切沙盒、載金鑰，才載入產品程式
    _SANDBOX_ROOT = enable_smoke_sandbox("manual_thinking_latency", allow_external_network=True)
    load_dotenv(env_file_of(sys.argv[1:]))

from src.handlers.adapters.claude_adapter import ClaudeAdapter  # noqa: E402
from src.models.identity import AgentIdentity  # noqa: E402
from src.services import completion, model_manager, telemetry  # noqa: E402
from src.services.dispatchers import route_family, utils  # noqa: E402
from src.services.models import model_rules  # noqa: E402
from src.utils.time_utils import get_current_epoch  # noqa: E402

from tests.memory_eval import live_bounds  # noqa: E402
from tests.smoke.manual import memory_eval_probe as shared  # noqa: E402  # 上限解析、沙盒釋放

PROBE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
# 強度 → (complete_once 用途, 用途表應給的 profile)；用途表是 SSOT，啟動時核對
PROFILES = {"minimal": ("summarizer", "minimal"), "default": ("critic", None)}
MAX_REPEATS, MAX_PROVIDER_REQUESTS, MAX_LIVE_TOKENS = 5, 30, 1_024
OUTPUT_TOKEN_CEILING = 8_192  # 工具自己的天花板：實送值超過就不送
MAX_BUDGET_USD = 3.0
REQUEST_TIMEOUT_S = 120.0  # 每次 complete_once 的總時間（和互審同值）
_IDENTITY = AgentIdentity(agent_id="DebugAgent")
_SYSTEM = "Summarize the user's text in exactly one plain sentence. No preamble."
_USER = 20 * (  # 固定合成段落，約 8,000 字元≈2,000 token
    "The fictional river town of Lanmere keeps a ledger of every ferry crossing, every lantern "
    "lit on the north quay, and every bale of wool weighed at the old customs house. Clerks copy "
    "the totals into a second book each evening, and two apprentices compare the books at dawn. "
)
_ORIGINAL_SEND_ROUND = ClaudeAdapter.send_round
_ORIGINAL_EXTRACT_USAGE = ClaudeAdapter.extract_usage
_BUDGET: LiveApiCallBudget | None = None
_LEDGER: live_bounds.SpendLedger | None = None
_CURRENT: dict[str, Any] = {}  # 這個請求：model、核准的實送上限、實際送出的思考設定


class ProbeRefused(RuntimeError):
    """核准範圍、上限或產品設定不符：不送。"""

    REASONS: dict[str, str] = {
        "bounds": "repeats, max-provider-requests, max-tokens or budget out of bounds",
        "profiles": "profiles must be minimal and/or default",
        "batch": "models x profiles x repeats exceeds --max-provider-requests",
        "purpose": "the purpose table no longer matches the probe profiles",
        "model": "not an approved Claude 5.5 model routed to Anthropic:",
        "wire": "wire max_tokens missing or above the approved cap:",
        "install": "probe bounds are not installed",
    }

    def __init__(self, code: str, detail: object = "") -> None:
        super().__init__(f"{self.REASONS[code]} {detail}".strip())


def wire_max_tokens(model: str, profile: str, requested: int) -> int:
    """Touka 實際要送的 max_tokens：思考真的關掉才照請求值，否則規則表下限 8,192。"""
    plan = model_rules.claude_thinking_plan(model, thinking=None, effort=None,
                                            minimal=profile == "minimal")
    return model_rules.claude_send_max_tokens(model, requested, None,
                                              thinking_off=plan.thinking_off)


def check_request(*, models: list[str], profiles: list[str], repeats: int, max_requests: int,
                  max_tokens: int, budget: float, **_other: Any) -> None:
    if not (1 <= repeats <= MAX_REPEATS and 1 <= max_requests <= MAX_PROVIDER_REQUESTS
            and 1 <= max_tokens <= MAX_LIVE_TOKENS and 0 < budget <= MAX_BUDGET_USD):
        raise ProbeRefused("bounds")
    if not models or not profiles or not set(profiles) <= set(PROFILES):
        raise ProbeRefused("profiles")
    if len(models) * len(profiles) * repeats > max_requests:
        raise ProbeRefused("batch")
    if any(completion.thinking_profile_for(p) != want for p, want in PROFILES.values()):
        raise ProbeRefused("purpose")
    for model in models:
        if model not in PROBE_MODELS or route_family.resolve_model_route(
                model, manager=model_manager.model_manager, provider_info=utils.get_provider_info,
        ).kind != route_family.ANTHROPIC_FAMILY:
            raise ProbeRefused("model", model)


def latency_scope(*, models: list[str], profiles: list[str], repeats: int, max_requests: int,
                  max_tokens: int, budget: float, env_file: Path) -> LiveApiScope:
    wire = ";".join(f"{m}/{p}={wire_max_tokens(m, p, max_tokens)}"
                    for m in models for p in profiles)
    rates = {m: shared._dearest_rate(m) for m in models}  # 分檔計價（Haiku 5.5）取貴檔
    prices = ";".join(f"{m}=in:{live_bounds.worst_case_input_price(r):g}/out:{r.output:g}"
                      for m, r in rates.items())
    return LiveApiScope(
        operation_id="manual_thinking_latency_probe", provider="anthropic", model=",".join(models),
        limits=(f"profiles={','.join(f'{p}->{PROFILES[p][0]}' for p in profiles)}",
                f"repeats={repeats}", f"expected_requests={len(models) * len(profiles) * repeats}",
                f"provider_requests<={max_requests}", f"max_tokens_requested={max_tokens}",
                f"wire_max_tokens={wire}", f"wire_max_tokens_ceiling={OUTPUT_TOKEN_CEILING}",
                f"budget_usd={budget:.4f}", "pre_send_worst_case=spent + wire cap x output + "
                f"(request chars + {live_bounds.VENDOR_OVERHEAD_TOKENS}) x max(input|cache_write)",
                f"worst_case_usd_per_mtok={prices}", f"request_timeout_s={REQUEST_TIMEOUT_S:g}",
                "provider_retry=0", "fallback=0", f"env_file={env_file}"),
        data_scope="one fixed synthetic paragraph and a one-line system instruction only",
        write_scope="temporary sandbox only",
    )


async def _bounded_send_round(adapter: Any, state: Any) -> Any:
    """輸出上限 → 最壞花費 → 核銷請求預算，三關都過才外連。"""
    if _LEDGER is None or _BUDGET is None or not _CURRENT:
        raise ProbeRefused("install")
    kwargs = state.kwargs
    found = kwargs.get("max_tokens")
    if not isinstance(found, int) or not 1 <= found <= min(_CURRENT["wire_cap"],
                                                           OUTPUT_TOKEN_CEILING):
        raise ProbeRefused("wire", repr(found))
    rate = shared._dearest_rate(_CURRENT["model"])  # 每個請求用該型號的價
    _LEDGER.input_price = live_bounds.worst_case_input_price(rate)
    _LEDGER.output_price = rate.output
    record = _LEDGER.reserve({"kwargs": kwargs}, found)
    try:
        _BUDGET.consume()
    except BaseException:
        _LEDGER.records.remove(record)  # 沒送出就不記帳
        raise
    _CURRENT["sent"] = {"max_tokens": found, "thinking": (kwargs.get("thinking") or {}).get("type"),
                        "effort": (kwargs.get("output_config") or {}).get("effort")}
    async for item in _ORIGINAL_SEND_ROUND(adapter, state):
        yield item


def _settling_extract_usage(adapter: Any, state: Any, raw: Any) -> Any:
    """廠商回報的用量（輸出含思考）結清剛放行的請求；金額用產品價目表。"""
    result = _ORIGINAL_EXTRACT_USAGE(adapter, state, raw)
    if _LEDGER is not None and _CURRENT:
        _LEDGER.settle(result[0], telemetry.usage_cost_usd(result[0], model=_CURRENT["model"]))
    return result


def install_bounds(max_requests: int, budget: float) -> None:
    global _BUDGET, _LEDGER
    _BUDGET = LiveApiCallBudget(max_requests=max_requests)
    _LEDGER = live_bounds.SpendLedger(budget_usd=budget, input_price=0.0, output_price=0.0)
    ClaudeAdapter.send_round = _bounded_send_round
    ClaudeAdapter.extract_usage = _settling_extract_usage


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每組 (model, profile)：成功請求的 [最小, 中位數, 最大] 耗時與合計金額。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["model"], row["profile"]), []).append(row)
    return [{"model": model, "profile": profile, "requests": len(items),
             "elapsed_ms": [min(ms), statistics.median(ms), max(ms)] if (
                 ms := [r["elapsed_ms"] for r in items if r["ok"]]) else None,
             "cost_usd_total": round(sum(r["cost_usd"] for r in items), 6)}
            for (model, profile), items in groups.items()]


async def run_latency_probe(*, approval: Any, **bounds: Any) -> dict[str, Any]:
    """核對範圍後跑一批；任何一個請求失敗就整批停（報告 stopped_reason 有值）。"""
    models, profiles, max_tokens = bounds["models"], bounds["profiles"], bounds["max_tokens"]
    check_request(**bounds)
    validate_live_api_approval(approval, expected_scope=latency_scope(**bounds))
    install_bounds(bounds["max_requests"], bounds["budget"])
    rows: list[dict[str, Any]] = []
    for repeat in range(1, bounds["repeats"] + 1):
        for model, profile in [(m, p) for m in models for p in profiles]:
            _CURRENT.clear()
            _CURRENT.update(model=model, wire_cap=wire_max_tokens(model, profile, max_tokens))
            started = get_current_epoch()
            result = await completion.complete_once(
                _IDENTITY, PROFILES[profile][0], model, _SYSTEM, _USER, max_tokens,
                REQUEST_TIMEOUT_S)
            usage = result.usage or {}
            rows.append({"model": model, "profile": profile, "repeat": repeat,
                         "sent": _CURRENT.get("sent"), "ok": result.ok,
                         "elapsed_ms": int((get_current_epoch() - started) * 1000),
                         "error": result.error.reason if result.error else None,
                         "stop_reason": result.stop_reason, "text_chars": len(result.text),
                         **{name: usage.get(key) for key, name in live_bounds.USAGE_FIELDS},
                         "cost_usd": round(result.cost_usd, 6)})
            if not result.ok:
                return _report(rows, f"{model}/{profile} #{repeat}: {rows[-1]['error']}")
    return _report(rows, "")


def _report(rows: list[dict[str, Any]], stopped: str) -> dict[str, Any]:
    return {"requests": rows, "groups": summarize(rows), "stopped_reason": stopped,
            "provider_requests_used": _BUDGET.used_requests if _BUDGET else 0,
            "ledger": _LEDGER.to_dict() if _LEDGER else None}  # 送前估算與結清（每個請求）


def _csv_items(text: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in text.split(",") if item.strip()))


def main(argv: list[str] | None = None) -> int:
    """驗證核准範圍後跑一批；整批停下或沙盒刪不掉時回非零。"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description="Human-approved thinking latency probe")
    parser.add_argument("--models", required=True, help=f"逗號分隔，只收 {','.join(PROBE_MODELS)}")
    parser.add_argument("--profiles", required=True, help="逗號分隔：minimal、default")
    for flag, high in (("--repeats", MAX_REPEATS), ("--max-tokens", MAX_LIVE_TOKENS),
                       ("--max-provider-requests", MAX_PROVIDER_REQUESTS)):
        parser.add_argument(flag, required=True, type=shared._bounded_int(flag[2:], high))
    parser.add_argument("--budget", required=True, type=float, help=f"USD（0..{MAX_BUDGET_USD:g}）")
    parser.add_argument("--env-file", help=f"金鑰 .env 路徑（預設 {DEFAULT_ENV_FILE}）")
    parser.add_argument(LIVE_API_APPROVAL_FLAG, action="store_true",
                        help="確認已取得本批明確人類同意（不得由自動化傳入）")
    args = parser.parse_args(argv)
    bounds = {"models": _csv_items(args.models), "profiles": _csv_items(args.profiles),
              "repeats": args.repeats, "max_requests": args.max_provider_requests,
              "max_tokens": args.max_tokens, "budget": args.budget,
              "env_file": env_file_of(sys.argv[1:] if argv is None else argv)}
    try:
        check_request(**bounds)
    except ProbeRefused as exc:
        parser.error(str(exc))
    scope = latency_scope(**bounds)
    approval = issue_live_api_approval(approved=args.human_approved_live_api, scope=scope)
    sys.stdout.write(f"{format_live_api_scope(scope)}\nsandbox={_SANDBOX_ROOT}\n")
    return shared._report_then_release(
        lambda: asyncio.run(run_latency_probe(approval=approval, **bounds)), _SANDBOX_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())

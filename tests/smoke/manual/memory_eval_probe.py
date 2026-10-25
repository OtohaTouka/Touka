"""人工記憶評估實測（ADR-0214）：合成靈魂、多輪、真模型。寫好不自動跑。

這不是自動 smoke。每一批都必須重新取得人類對精確 provider／model／上限的同意：

    python tests/smoke/manual/memory_eval_probe.py \
        --provider deepseek --model deepseek-v4-flash --suite smoke \
        --max-provider-requests 40 --max-tokens 512 --budget 0.20 \
        --human-approved-live-api [--env-file <金鑰 .env 路徑>]

只送合成資料（合成人格、合成事實與對話、注入句）。所有本機寫入（靈魂檔、session DB、記憶庫、
log）都先切到程序專屬暫存沙盒；結果只印到 stdout。主迴圈只送 1 次（retry=0）、四家 send_round
在外連前依序：核對送出欄位的輸出上限（Touka 實際要送的值，且不超過工具天花板 8,192）、核對
「已花＋這個請求最壞花費」不超過金額上限、核銷請求預算；不換模型、摘要員與審查員一律不呼叫；
任何一輪遇到 429／5xx 或其他錯誤，整批停下。報告先印出（含每個請求的用量），再放開紀錄檔、
記憶庫與向量庫的連線、刪掉沙盒；刪不掉照實回報並以非零結束。

嵌入一律用替身編碼器：產品讀到的嵌入模型名稱在建庫前固定成評估專用名稱（不看 .env），查詢下限
與查重門檻取產品對這個名稱的預設值；核准範圍與報告都印這一行。分數只代表替身，不代表真實嵌入
模型的召回。

``--suite continuity``（時間軸）另外打開活動記錄器，每個情境先在沙盒預放活動事件；核准範圍印
``activity_record=on``。smoke 不開。兩條命令算兩批，各自取得同意。
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
    from collections.abc import AsyncGenerator, Callable, Sequence
    from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
DEFAULT_ENV_FILE = PROXY_DIR / ".env"
IDENTITY_MAP_EXAMPLE = PROXY_DIR / "config" / "identity_map.json.example"
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


# 最早期人工閘：缺旗標時，連 dotenv、產品程式與評估框架都不 import。
_ENV_FILE = DEFAULT_ENV_FILE
if __name__ == "__main__":
    preflight_live_api_cli(sys.argv[1:])
    _ENV_FILE = _resolve_env_file(sys.argv[1:])

from dotenv import load_dotenv  # noqa: E402

from tests.smoke.lib.sandbox_env import (  # noqa: E402
    _cleanup_standalone_at_exit,
    _validated_temp_root,
    enable_smoke_sandbox,
)

_SANDBOX_ROOT = ""
if __name__ == "__main__":
    _SANDBOX_ROOT = enable_smoke_sandbox("manual_memory_eval", allow_external_network=True)
    load_dotenv(_ENV_FILE)

import atexit  # noqa: E402
from functools import partial  # noqa: E402

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient  # noqa: E402
from memory_server import config_utils  # noqa: E402
from memory_server import vector as memory_vector  # noqa: E402
from memory_server.factual import FactualStore  # noqa: E402
from memory_server.services.lifecycle import duplicate_similarity_threshold  # noqa: E402
from memory_server.services.manager import MemoryManager  # noqa: E402
from src.config import (  # noqa: E402
    ANTHROPIC_API_BASE_URL,
    apply_runtime_env,
    strip_anthropic_sdk_env_overrides,
)
from src.core.logging_config import close_sandbox_log_handlers  # noqa: E402
from src.handlers import react_core  # noqa: E402
from src.handlers.adapters.claude_adapter import ClaudeAdapter  # noqa: E402
from src.handlers.adapters.gemini_adapter import GeminiAdapter  # noqa: E402
from src.handlers.adapters.openai_adapter import OpenAIAdapter  # noqa: E402
from src.handlers.adapters.responses_adapter import ResponsesAdapter  # noqa: E402
from src.handlers.adapters.stream_timeout import liveness_event_hooks  # noqa: E402
from src.main import build_base_context  # noqa: E402
from src.services import dialogue_summarizer, proxy_service, telemetry  # noqa: E402
from src.services.agent_service import agent_service  # noqa: E402
from src.services.audit_service import PolicyAuditService  # noqa: E402
from src.services.completion import (  # noqa: E402
    REASON_OVER_BUDGET,
    CompletionError,
    CompletionResult,
)
from src.services.models.model_rules import claude_send_max_tokens  # noqa: E402
from src.services.permission_service import permission_service  # noqa: E402
from src.services.providers import pricing  # noqa: E402
from src.services.providers import registry as provider_registry  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402
from src.services.review import review_loop  # noqa: E402
from src.utils.async_db import init_db_async, session_service  # noqa: E402
from src.utils.brain_persistence import set_active_brain  # noqa: E402

from tests.memory_eval import live_bounds, runner, seeding  # noqa: E402
from tests.memory_eval.scenarios import (  # noqa: E402
    ACTIVITY_RECORD_SUITES,
    SUITES,
    UnknownScenario,
    expected_requests,
    select_scenarios,
)
from tests.sandbox_cleanup import remove_session_sandbox  # noqa: E402

if TYPE_CHECKING:
    from tests.memory_eval.scenarios import Scenario
    from tests.sandbox_cleanup import ReleaseStep

MAX_PROVIDER_REQUESTS = 40
MAX_LIVE_TOKENS = 1024  # 請求裡的 max_tokens；Touka 送出時可能拉高（見 _wire_max_tokens）
DEFAULT_LIVE_TOKENS = 512
# 工具自己的天花板：Touka 實際要送的輸出上限超過就不送（之後規則表改 32K／64K 也不會照送）。
OUTPUT_TOKEN_CEILING = 8_192
_WIRE_RULE = "anthropic follows Touka model rules; other providers send the requested value"
MAX_BUDGET_USD = 2.0
# 主迴圈的四家共用重試開關（react_core.MAX_SEND_ATTEMPTS 是總送出次數）：只送 1 次、不重試。
_PROBE_SEND_ATTEMPTS = 1
_AUDIT_DRAIN_TIMEOUT_S = 30
STUB_NOTE = "stub encoder; recall does not reflect a real embedding model"
ACTIVITY_RECORD_ON = "activity_record=on (seeded events written to the sandbox only)"
ACTIVITY_RECORD_OFF = "activity_record=off"
_ADAPTERS = (ClaudeAdapter, OpenAIAdapter, ResponsesAdapter, GeminiAdapter)
_ORIGINAL_SEND_ROUNDS = {adapter: adapter.send_round for adapter in _ADAPTERS}
_ORIGINAL_EXTRACT_USAGES = {adapter: adapter.extract_usage for adapter in _ADAPTERS}
_PROVIDER_REQUEST_BUDGET: LiveApiCallBudget | None = None
_OUTPUT_CAP: int | None = None
_LEDGER: live_bounds.SpendLedger | None = None
_AUXILIARY_CALLS: list[str] = []


class _BoundsError(argparse.ArgumentTypeError):
    def __init__(self, label: str, low: float, high: float) -> None:
        super().__init__(f"{label} 必須介於 {low:g} 到 {high:g}")


class _ProviderBudgetNotInitialized(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider request budget is not initialized")


class _OutputCapViolation(RuntimeError):
    def __init__(self, found: object) -> None:
        super().__init__(f"wire output cap missing or above the approved cap or the "
                         f"{OUTPUT_TOKEN_CEILING} ceiling: {found!r}")


class _OutputCeilingExceeded(RuntimeError):
    def __init__(self, wire: int) -> None:
        super().__init__(f"Touka would send max_tokens={wire}, above the probe ceiling "
                         f"{OUTPUT_TOKEN_CEILING}; nothing sent")


class _ProviderRouteMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("provider/model route mismatch or unsupported provider")


class _SoulNotOwner(RuntimeError):
    def __init__(self, role: str) -> None:
        super().__init__(f"synthetic soul resolved to role {role!r}, expected Owner")


class _StubEmbeddingNotPinned(RuntimeError):
    def __init__(self, found: str | None = None) -> None:
        reason = "the vector store already exists" if found is None else (
            f"the product reads {found!r}")
        super().__init__(f"stub embedding model not pinned ({reason}); nothing sent")


def _bounded_int(label: str, high: int) -> Callable[[str], int]:
    def _parse(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= high:
            raise _BoundsError(label, 1, high)
        return parsed

    return _parse


def _bounded_budget(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= MAX_BUDGET_USD:
        raise _BoundsError("budget", 0, MAX_BUDGET_USD)
    return parsed


def _provider_for_model(model: str) -> str | None:
    lowered = model.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    spec = provider_registry.resolve_provider(model)
    return spec.id if spec is not None else None


def _wire_output_cap(state: Any) -> object:
    """各家送出前的輸出上限欄位（Claude／Responses 在 kwargs，相容 Chat／Gemini 在 payload）。"""
    kwargs = getattr(state, "kwargs", None) or {}
    payload = getattr(state, "payload", None) or {}
    generation = payload.get("generationConfig") or {}
    for value in (kwargs.get("max_tokens"), kwargs.get("max_output_tokens"),
                  payload.get("max_tokens"), payload.get("max_completion_tokens"),
                  generation.get("maxOutputTokens")):
        if value is not None:
            return value
    return None


def _wire_max_tokens(provider: str, model: str, requested: int) -> int:
    """Touka 實際要送的輸出上限：Anthropic 照產品規則表（5.5 這類關不掉思考的型號拉到
    8,192），其他廠商照請求值。"""
    if provider.casefold() == "anthropic":
        return claude_send_max_tokens(model, requested, None)
    return requested


def _check_output_cap(state: Any) -> int:
    """送出欄位必須是 1..min(核准的實送值, 工具天花板)；回這個請求的輸出上限。"""
    found = _wire_output_cap(state)
    if (_OUTPUT_CAP is None or not isinstance(found, int)
            or not 1 <= found <= min(_OUTPUT_CAP, OUTPUT_TOKEN_CEILING)):
        raise _OutputCapViolation(found)
    return found


def _consume_provider_request() -> None:
    if _PROVIDER_REQUEST_BUDGET is None:
        raise _ProviderBudgetNotInitialized
    _PROVIDER_REQUEST_BUDGET.consume()


def _bounded_send_round(original: Any) -> Any:
    """依序核對輸出上限、送前最壞花費、核銷請求預算，最後才外連（任何一關不過都不送）。"""

    async def _send_round(adapter: Any, state: Any) -> AsyncGenerator[Any]:
        max_output = _check_output_cap(state)
        if _LEDGER is None:
            raise _ProviderBudgetNotInitialized
        # 這個請求實際要送的內容（訊息、system、工具 schema）都在 kwargs／payload 裡
        request = {"kwargs": getattr(state, "kwargs", None),
                   "payload": getattr(state, "payload", None)}
        record = _LEDGER.reserve(request, max_output)
        try:
            _consume_provider_request()
        except BaseException:
            _LEDGER.records.remove(record)  # 沒送出就不記帳
            raise
        async for item in original(adapter, state):
            yield item

    return _send_round


def _recording_extract_usage(original: Any, model: str) -> Any:
    """廠商回報的用量（輸出含思考）結清剛放行的請求；金額用產品同一套價目表。"""

    def _extract_usage(adapter: Any, state: Any, raw: Any) -> Any:
        result = original(adapter, state, raw)
        if _LEDGER is not None:
            _LEDGER.settle(result[0], telemetry.usage_cost_usd(result[0], model=model))
        return result

    return _extract_usage


def _dearest_rate(model: str) -> Any:
    """送前最壞估價用的費率：有分檔（r55：Haiku 5.5）就取貴檔，寧高勿低。"""
    rate = pricing.get_pricing(model)
    return rate.long_prompt[1] if rate.long_prompt is not None else rate


def _install_bounds(max_requests: int, wire_cap: int, *, model: str, budget: float) -> None:
    global _PROVIDER_REQUEST_BUDGET, _OUTPUT_CAP, _LEDGER
    _PROVIDER_REQUEST_BUDGET = LiveApiCallBudget(max_requests=max_requests)
    _OUTPUT_CAP = wire_cap
    rate = _dearest_rate(model)
    _LEDGER = live_bounds.SpendLedger(
        budget_usd=budget, input_price=live_bounds.worst_case_input_price(rate),
        output_price=rate.output,
    )
    react_core.MAX_SEND_ATTEMPTS = _PROBE_SEND_ATTEMPTS  # retry=0（四家同一個開關）
    ClaudeAdapter.send_round = _bounded_send_round(_ORIGINAL_SEND_ROUNDS[ClaudeAdapter])
    OpenAIAdapter.send_round = _bounded_send_round(_ORIGINAL_SEND_ROUNDS[OpenAIAdapter])
    ResponsesAdapter.send_round = _bounded_send_round(_ORIGINAL_SEND_ROUNDS[ResponsesAdapter])
    GeminiAdapter.send_round = _bounded_send_round(_ORIGINAL_SEND_ROUNDS[GeminiAdapter])
    for adapter in _ADAPTERS:
        adapter.extract_usage = _recording_extract_usage(_ORIGINAL_EXTRACT_USAGES[adapter], model)
    proxy_service.get_last_working_brain = _no_fallback
    dialogue_summarizer.complete_once = _refuse_auxiliary_call
    review_loop.complete_once = _refuse_auxiliary_call


def _no_fallback(_identity: object) -> None:
    """核准只涵蓋 args.model：不讀另一個 last-working brain。"""


async def _refuse_auxiliary_call(*args: Any, **kwargs: Any) -> CompletionResult:
    """摘要員／審查員不在核准範圍：不送出、回結構化非成功並記下用途。"""
    _AUXILIARY_CALLS.append(str(kwargs.get("purpose", args[1] if len(args) > 1 else "?")))
    return CompletionResult(
        text="", usage={}, cost_usd=0.0, stop_reason="error",
        error=CompletionError(error_class="permission_denied", reason=REASON_OVER_BUDGET,
                              message="memory eval probe: auxiliary model calls not approved"),
    )


def _stub_facts() -> dict[str, Any]:
    """替身的名稱與兩個門檻（經產品自己的查表，不看 .env）。"""
    name = seeding.STUB_EMBEDDING_MODEL
    return {"model": name, "query_floor": memory_vector.query_score_floor(name),
            "dedup_threshold": duplicate_similarity_threshold(name), "note": STUB_NOTE}


def _pin_stub_embedding() -> dict[str, Any]:
    """建庫前把產品讀到的嵌入模型名稱換成替身名稱（之後讀 .env 也蓋不掉）。

    向量庫已經建立（名稱已定）或產品讀回的名稱不符就停下，不送任何請求。
    """
    if MemoryManager._vector_store_singleton is not None:
        raise _StubEmbeddingNotPinned
    apply_runtime_env({"EMBEDDING_MODEL": seeding.STUB_EMBEDDING_MODEL})
    found = config_utils.get_embedding_model_name()
    if found != seeding.STUB_EMBEDDING_MODEL:
        raise _StubEmbeddingNotPinned(found)
    return _stub_facts()


def _memory_eval_scope(
    *,
    provider: str,
    model: str,
    suite: str,
    scenario_ids: Sequence[str],
    max_requests: int,
    max_tokens: int,
    budget: float,
    env_file: Path = DEFAULT_ENV_FILE,
) -> LiveApiScope:
    rate = _dearest_rate(model)
    stub = _stub_facts()
    return LiveApiScope(
        operation_id="manual_memory_eval_probe",
        provider=provider,
        model=model,
        limits=(
            f"suite={suite}",
            f"scenarios={','.join(scenario_ids)}",
            f"provider_requests<={max_requests}",
            f"max_tokens_requested={max_tokens}",
            f"wire_max_tokens_per_request={_wire_max_tokens(provider, model, max_tokens)}",
            f"wire_max_tokens_ceiling={OUTPUT_TOKEN_CEILING}",
            f"wire_rule={_WIRE_RULE}",
            f"budget_usd={budget:.4f}",
            "pre_send_worst_case=spent + wire cap x output + (request chars + "
            f"{live_bounds.VENDOR_OVERHEAD_TOKENS}) x max(input|cache_write) <= budget",
            f"worst_case_usd_per_mtok=input={live_bounds.worst_case_input_price(rate):g};"
            f"output={rate.output:g}",
            "provider_retry=0",
            "main_loop_send_attempts=1",
            "fallback=0",
            "auxiliary_model_calls=0",
            f"embedding_stub={stub['model']};query_floor={stub['query_floor']:g};"
            f"dedup_threshold={stub['dedup_threshold']:g} ({stub['note']})",
            ACTIVITY_RECORD_ON if suite in ACTIVITY_RECORD_SUITES else ACTIVITY_RECORD_OFF,
            f"env_file={env_file}",
        ),
        data_scope="synthetic persona, facts, conversations and injection text only",
        write_scope="temporary sandbox only",
    )


def _seed_sandbox(sandbox_root: Path) -> None:
    """身分對映（.example）、替身編碼器：全部只在沙盒；合成靈魂每個情境另建（_prepare_soul）。"""
    memory_vector.SentenceTransformer = seeding.DeterministicEncoder  # 零下載
    seeding.seed_identity_map(
        permission_service.identity_path, IDENTITY_MAP_EXAMPLE, sandbox_root=sandbox_root
    )
    permission_service.reload_all()


def _prepare_soul(soul_id: str, *, sandbox_root: Path, model: str) -> None:
    """這個情境的合成靈魂：寫進沙盒、核對解析成 Owner、設主要模型（核准的那一個）。"""
    seeding.write_synthetic_soul(
        Path(config_utils.get_app_data_dir()), soul_id, sandbox_root=sandbox_root
    )
    role, _caps = agent_service.resolve_role_and_caps(soul_id)
    if role != "Owner":
        raise _SoulNotOwner(role)
    set_active_brain(seeding.soul_identity(soul_id), model)


async def run_memory_eval_probe(
    *,
    provider: str,
    model: str,
    suite: str,
    scenarios: tuple[Scenario, ...],
    max_requests: int,
    max_tokens: int,
    budget: float,
    approval: LiveApiApproval,
    sandbox_root: Path,
    env_file: Path = DEFAULT_ENV_FILE,
) -> dict[str, Any]:
    """驗證核准範圍與上限後跑一批；回彙總（含實際請求數與被拒的輔助呼叫）。"""
    if not 1 <= max_tokens <= MAX_LIVE_TOKENS:
        raise _BoundsError("max-tokens", 1, MAX_LIVE_TOKENS)
    if not 1 <= max_requests <= MAX_PROVIDER_REQUESTS:
        raise _BoundsError("max-provider-requests", 1, MAX_PROVIDER_REQUESTS)
    wire_cap = _wire_max_tokens(provider, model, max_tokens)
    if wire_cap > OUTPUT_TOKEN_CEILING:
        raise _OutputCeilingExceeded(wire_cap)
    if not sandbox_root.is_absolute():
        raise seeding.SeedOutsideSandbox
    if _provider_for_model(model) != provider.casefold():
        raise _ProviderRouteMismatch
    validate_live_api_approval(approval, expected_scope=_memory_eval_scope(
        provider=provider.casefold(), model=model, suite=suite,
        scenario_ids=[s.id for s in scenarios], max_requests=max_requests,
        max_tokens=max_tokens, budget=budget, env_file=env_file,
    ))
    _install_bounds(max_requests, wire_cap, model=model, budget=budget)
    stub = _pin_stub_embedding()
    await init_db_async()
    # 查舊對話（memory_tools conversations）要讀 UI 對話：和 Proxy 啟動時一樣裝上沙盒的 session
    resource_manager.bind_ui_conversation_reader(session_service)
    _seed_sandbox(sandbox_root)
    base_ctx = build_base_context()
    anthropic_client: AsyncAnthropic | None = None
    if provider.casefold() == "anthropic":
        strip_anthropic_sdk_env_overrides()  # 環境不得改寫請求（ADR-0247）
        anthropic_client = AsyncAnthropic(
            api_key=provider_registry.get_key_by_id("anthropic"),
            base_url=ANTHROPIC_API_BASE_URL,
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(  # 禁令 22；閒置計時和主迴圈同一條
                follow_redirects=False, event_hooks=liveness_event_hooks()),
        )
    base_ctx["anthropic_client"] = anthropic_client
    driver = runner.MemoryEvalDriver(
        base_ctx=base_ctx, send_turn=runner.pipeline_send_turn,
        save_fact=runner.memory_save_fact, max_tokens=max_tokens, budget_usd=budget,
        activity_record=suite in ACTIVITY_RECORD_SUITES,
        prepare_soul=partial(_prepare_soul, sandbox_root=sandbox_root, model=model),
        read_facts=runner.memory_active_facts,
        archive_history=runner.legacy_archiver(sandbox_root),
    )
    try:
        report = (await driver.run_batch(scenarios)).to_dict()
    finally:
        resource_manager.bind_ui_conversation_reader(None)
        if anthropic_client is not None:
            await anthropic_client.close()
    used = _PROVIDER_REQUEST_BUDGET.used_requests if _PROVIDER_REQUEST_BUDGET else 0
    return {
        **report, "provider_requests_used": used, "auxiliary_calls_refused": _AUXILIARY_CALLS,
        "embedding_stub": stub,
        "wire_max_tokens": {"per_request": wire_cap, "ceiling": OUTPUT_TOKEN_CEILING,
                            "rule": _WIRE_RULE},
        **(_LEDGER.to_dict() if _LEDGER else {}),
    }


def _drain_policy_audit_writes() -> None:
    """稽核紀錄由背景執行緒寫進沙盒 stats/：等它寫完，免得刪掉後又被建回來。"""
    PolicyAuditService._executor.submit(int).result(timeout=_AUDIT_DRAIN_TIMEOUT_S)


def _sandbox_release_steps(root: Path) -> list[ReleaseStep]:
    """刪沙盒前依序放開握住它的東西：稽核寫入、紀錄檔 handler、記憶庫 SQLite、向量庫 Qdrant。"""
    return [
        ("drain_policy_audit_writes", _drain_policy_audit_writes),
        ("close_log_handlers", partial(close_sandbox_log_handlers, str(root))),
        ("close_sqlite_connections", FactualStore.close_all_connections),
        ("close_qdrant_clients", memory_vector.VectorStore.close_all_clients),
    ]


def _release_sandbox(root: Path) -> Path | None:
    """放開 handle 後刪掉本程序的暫存沙盒（只收 OS 暫存底下）；回刪不掉的路徑。"""
    owned = _validated_temp_root(root)
    residue = remove_session_sandbox(owned, _sandbox_release_steps(owned))
    atexit.unregister(_cleanup_standalone_at_exit)  # 已在這裡刪過；結果由呼叫端回報
    return residue


def _report_then_release(run: Callable[[], dict[str, Any]], sandbox_root: str) -> int:
    """先跑完並印出報告（含每個請求的用量），再刪沙盒；刪不掉照實回報並回非零。"""
    residue: Path | None = None
    try:
        report = run()
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        if sandbox_root:
            residue = _release_sandbox(Path(sandbox_root))
        if residue is not None:
            sys.stderr.write(f"sandbox_cleanup_failed={residue}\n")
    return 1 if residue is not None or report["stopped_reason"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    """驗證核准範圍後跑一批記憶評估；整批停下時回非零。"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="Human-approved memory eval probe")
    parser.add_argument("--provider", required=True, help="核准的 provider id")
    parser.add_argument("--model", required=True, help="核准的精確 model id")
    parser.add_argument("--suite", choices=SUITES, required=True, help="情境套件")
    parser.add_argument("--scenarios", default="", help="逗號分隔的情境 id（空＝整個套件）")
    parser.add_argument("--max-provider-requests", required=True,
                        type=_bounded_int("max-provider-requests", MAX_PROVIDER_REQUESTS),
                        help=f"本批 provider request 硬上限（1..{MAX_PROVIDER_REQUESTS}）")
    parser.add_argument("--max-tokens", type=_bounded_int("max-tokens", MAX_LIVE_TOKENS),
                        default=DEFAULT_LIVE_TOKENS,
                        help=f"請求裡的輸出上限（1..{MAX_LIVE_TOKENS}）；Touka 實際送出值"
                             f"另印在核准範圍，超過 {OUTPUT_TOKEN_CEILING} 不送")
    parser.add_argument("--budget", type=_bounded_budget, required=True,
                        help=f"USD 軟上限（0..{MAX_BUDGET_USD:g}；依內建價目表累計）")
    parser.add_argument("--env-file", help=f"金鑰 .env 路徑（預設 {DEFAULT_ENV_FILE}）")
    parser.add_argument(LIVE_API_APPROVAL_FLAG, action="store_true",
                        help="確認已取得本批明確人類同意（不得由自動化傳入）")
    args = parser.parse_args(argv)

    provider = _provider_for_model(args.model)
    if provider is None or args.provider.casefold() != provider:
        parser.error(f"核准 provider={args.provider} 與 model 實際路由 provider={provider} 不符")
    ids = [s.strip() for s in args.scenarios.split(",") if s.strip()] or None
    try:
        scenarios = select_scenarios(args.suite, ids)
    except UnknownScenario as exc:
        parser.error(str(exc))
    env_file = _resolve_env_file(sys.argv[1:] if argv is None else argv)
    scope = _memory_eval_scope(
        provider=provider, model=args.model, suite=args.suite,
        scenario_ids=[s.id for s in scenarios], max_requests=args.max_provider_requests,
        max_tokens=args.max_tokens, budget=args.budget, env_file=env_file,
    )
    approval = issue_live_api_approval(approved=args.human_approved_live_api, scope=scope)
    sys.stdout.write(format_live_api_scope(scope) + "\n")
    sys.stdout.write(f"expected_requests={expected_requests(scenarios)}\n")
    sys.stdout.write(f"sandbox={_SANDBOX_ROOT}\n")

    return _report_then_release(lambda: asyncio.run(run_memory_eval_probe(
        provider=provider, model=args.model, suite=args.suite, scenarios=scenarios,
        max_requests=args.max_provider_requests, max_tokens=args.max_tokens,
        budget=args.budget, approval=approval, sandbox_root=Path(_SANDBOX_ROOT),
        env_file=env_file,
    )), _SANDBOX_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())

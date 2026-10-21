"""LIVE select-only 執行器：量測真實模型的工具「選擇」，工具永不執行。

流程：以真實 Gate 1（ToolLayer）組出指定身分的廣告工具 schema → 把 eval case 的
prompt 連同工具送給模型一次 → 收集模型選了哪些工具、帶了哪些參數。工具**不會**
被執行（select-only，零風險觸碰 data/）。

v1 走 OpenAI 相容供應商（openai / deepseek / grok；chat 端點由 provider registry
SSOT 正規化）。Anthropic 原生協定是文件化的後續項。

已知限制（記錄於基準檔 meta）：builtin-only（不掛 MCP、無 memory_tools schema）、
system prompt 非完整 pipeline（無 persona/soul 注入）——量測的是「schema 驅動的
工具選擇」，正是 Tier 2/3 重設計要改善的對象。

手動執行入口：``python scripts/record_tool_eval_baseline.py``（不進 CI、不排程）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx

# 需要 ToukaProxy 於 sys.path（pytest 由 conftest 設定；手動腳本由
# scripts/record_tool_eval_baseline.py 先行 bootstrap 再匯入本模組）。
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.permission_service import permission_service
from src.services.providers import registry as provider_registry

from tests.live_api_policy import (
    InvalidLiveApiTokenLimit,
    LiveApiRequestBudgetRequired,
)

from . import metrics
from .cases import SEED_CASES, ToolCall
from .harness import known_builtin_tool_names

if TYPE_CHECKING:
    from tests.live_api_policy import LiveApiCallBudget

    from .cases import EvalCase

_ERR_NO_KEY = RuntimeError("provider API key is not configured in .env")
_ERR_NO_URL = RuntimeError("provider has no chat completions URL configured")
_ERR_MULTITURN_SAMPLES = ValueError(
    "multi-turn mode (turns > 1) requires samples=1 (one episode per case)"
)

# registry 的 route_url_attr 若在 config 中為空時的官方端點後備。
_FALLBACK_BASE_URLS = {"openai": "https://api.openai.com/v1"}


class ProviderHTTPError(RuntimeError):
    """供應商回非 200 時攜帶狀態碼與截斷回應內文。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"provider returned HTTP {status}: {body[:300]}")


def build_identity(agent_id: str = "DebugAgent", role: str | None = None) -> Any:
    """建 DebugAgent 身分（角色可依 case 收窄；不觸碰其他 agent 的資料）。"""
    resolved = role or permission_service.get_role_key(agent_id)
    caps = permission_service.get_capabilities_by_id(agent_id)
    return AgentIdentity.for_role(agent_id=agent_id, role=resolved, capabilities=caps or [])


async def assemble_advertised_tools(identity: Any) -> list[dict[str, Any]]:
    """跑真實 ToolLayer（Gate 1）組廣告工具（builtin-only；不掛 MCP）。

    request 帶 project_id 讓 PROJECT_TOOLS_SCHEMA 照常廣告（僅影響 schema 注入，
    不觸任何專案資料）；resumable_stream=True 模擬互動會話（ask_user 照常廣告）。
    """
    request = SimpleNamespace(tools=None, project_id="tool-eval-baseline")
    ctx = PipelineContext(identity=identity)
    ctx["agent_id"] = identity.agent_id
    ctx["resumable_stream"] = True
    await ToolLayer().process(request, ctx)
    return list(request.tools or [])


def resolve_chat_endpoint(provider_id: str) -> tuple[str, str]:
    """回傳 (chat_completions_url, api_key)；金鑰或端點缺失時 raise。"""
    spec = provider_registry.spec_by_id(provider_id)
    api_key = provider_registry.get_key_by_id(provider_id)
    if not api_key:
        raise _ERR_NO_KEY
    base = provider_registry.get_route_url(spec) or _FALLBACK_BASE_URLS.get(provider_id, "")
    chat_url = provider_registry.normalize_chat_url(base)
    if not chat_url:
        raise _ERR_NO_URL
    return chat_url, api_key


def _post_json(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    request_budget: LiveApiCallBudget,
) -> dict[str, Any]:
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise InvalidLiveApiTokenLimit
    if request_budget is None:
        raise LiveApiRequestBudgetRequired
    headers = {"Authorization": f"Bearer {api_key}"}
    request_budget.consume()
    resp = httpx.post(url, headers=headers, json=payload, timeout=120.0)
    if resp.status_code != 200:
        raise ProviderHTTPError(resp.status_code, resp.text)
    return resp.json()


def _parse_calls(message: dict[str, Any]) -> tuple[list[ToolCall], list[dict[str, Any]]]:
    """message → (ToolCall 清單, 原始 tool_calls dicts——多回合續跑需要 id)。"""
    raw_calls = message.get("tool_calls") or []
    calls: list[ToolCall] = []
    for tc in raw_calls:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {"_unparsed_arguments": raw_args}
        if not isinstance(args, dict):
            args = {"_non_object_arguments": args}
        calls.append(ToolCall(name=fn.get("name") or "<missing>", args=args, status="ok"))
    return calls, raw_calls


def _build_payload(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_tokens: int,
) -> dict[str, Any]:
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise InvalidLiveApiTokenLimit
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    # Gate 1 可能過濾到零工具（如 Base 角色）——OpenAI 相容 API 拒收空 tools 陣列，
    # 此時直接不送 tools（模型物理上無法發出結構化 tool_call，正是真實情境）。
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def select_tools_for_case(
    chat_url: str,
    api_key: str,
    model: str,
    case: EvalCase,
    tools: list[dict[str, Any]],
    *,
    max_tokens: int,
    request_budget: LiveApiCallBudget,
) -> list[ToolCall]:
    """單一 case 打一次模型，收集 tool_calls（不執行）。"""
    payload = _build_payload(
        model,
        [{"role": "user", "content": case.prompt}],
        tools,
        max_tokens=max_tokens,
    )
    data = _post_json(chat_url, api_key, payload, request_budget=request_budget)
    message = (data.get("choices") or [{}])[0].get("message") or {}
    calls, _raw = _parse_calls(message)
    return calls


def _simulate_result(case: EvalCase, tool_name: str) -> str:
    """回合間餵給模型的工具結果（工具永不執行）：案例腳本優先、通用後備次之。"""
    scripted = case.simulated_results.get(tool_name)
    if scripted is not None:
        return scripted
    return f"[simulated] {tool_name} completed successfully."


def run_episode_for_case(
    chat_url: str,
    api_key: str,
    model: str,
    case: EvalCase,
    tools: list[dict[str, Any]],
    max_turns: int = 1,
    *,
    max_tokens: int,
    request_budget: LiveApiCallBudget,
) -> list[list[ToolCall]]:
    """多回合 select-only：非目標呼叫→餵模擬結果續跑；達標／無呼叫／上限即停。

    工具**永不執行**——「結果」是案例腳本化（simulated_results）或通用後備字串。
    無 expected_tools 的案例只跑一回合（無達標判準，與單回合語義一致）。
    """
    expected = set(case.expected_tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": case.prompt}]
    turns: list[list[ToolCall]] = []

    for _ in range(max(1, max_turns)):
        payload = _build_payload(model, messages, tools, max_tokens=max_tokens)
        data = _post_json(chat_url, api_key, payload, request_budget=request_budget)
        message = (data.get("choices") or [{}])[0].get("message") or {}
        calls, raw_calls = _parse_calls(message)
        turns.append(calls)

        if not calls:
            break  # 模型改用文字作答（放棄工具）→ 誠實入列後結束
        if not expected:
            break  # 無目標案例＝單回合
        if any(c.name in expected for c in calls):
            break  # 達標即停（真實情境該呼叫會實際執行）

        # 續跑：assistant(tool_calls) ＋ 每個呼叫一則 role=tool 模擬結果
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": raw_calls,
            }
        )
        for tc in raw_calls:
            fn = tc.get("function") or {}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": _simulate_result(case, fn.get("name") or "<missing>"),
                }
            )
    return turns


def describe_assembly(cases: list[EvalCase] | None = None) -> dict[str, Any]:
    """dry-run：逐（agent, role）組廣告工具並回報名稱（不打任何 API）。"""
    per_role: dict[str, list[str]] = {}
    for case in cases or SEED_CASES:
        key = _identity_key(case)
        if key in per_role:
            continue
        identity = build_identity(
            case.context.get("agent_id", "DebugAgent"), case.context.get("role")
        )
        tools = asyncio.run(assemble_advertised_tools(identity))
        per_role[key] = sorted(_tool_name(t) for t in tools)
    return {
        "provider_key_status": provider_registry.provider_key_status(),
        "advertised_by_identity": per_role,
    }


def _identity_key(case: EvalCase) -> str:
    return f"{case.context.get('agent_id', 'DebugAgent')}:{case.context.get('role') or 'auto'}"


def _tool_name(schema: dict[str, Any]) -> str:
    return schema.get("function", {}).get("name") or schema.get("name") or "<unnamed>"


def record_baseline(
    model: str,
    provider_id: str = "openai",
    samples: int = 1,
    turns: int = 1,
    cases: list[EvalCase] | None = None,
    *,
    max_tokens: int | None = None,
    request_budget: LiveApiCallBudget | None = None,
) -> dict[str, Any]:
    """對所有 case 跑 select-only 並回傳可序列化的基準 payload（由呼叫端落檔）。

    ``turns > 1`` 進多回合模式（每案一個 episode、強制 samples=1）：非目標呼叫
    之後餵入模擬結果續跑——量測「多步規劃者最終是否達標」而非只看第一步。
    """
    if turns > 1 and samples > 1:
        raise _ERR_MULTITURN_SAMPLES
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise InvalidLiveApiTokenLimit
    if request_budget is None:
        raise LiveApiRequestBudgetRequired
    chat_url, api_key = resolve_chat_endpoint(provider_id)
    case_list = cases or SEED_CASES

    tools_by_identity: dict[str, list[dict[str, Any]]] = {}
    results = []
    observations: list[dict[str, Any]] = []
    n_trials = max(1, samples)
    # pass^k 可靠度：每個「有 expected」的案例，記 n_trials 次中 selection_ok 的次數。
    reliability_successes: list[int] = []

    for case in case_list:
        key = _identity_key(case)
        if key not in tools_by_identity:
            identity = build_identity(
                case.context.get("agent_id", "DebugAgent"), case.context.get("role")
            )
            tools_by_identity[key] = asyncio.run(assemble_advertised_tools(identity))
        tools = tools_by_identity[key]
        advertised = frozenset(_tool_name(t) for t in tools)
        known = known_builtin_tool_names() | advertised

        if turns > 1:
            # 多回合模式（強制 samples=1）：一個 episode。
            episodes = [
                run_episode_for_case(
                    chat_url,
                    api_key,
                    model,
                    case,
                    tools,
                    max_turns=turns,
                    max_tokens=max_tokens,
                    request_budget=request_budget,
                )
            ]
        else:
            # 單回合：每個 trial 各自一個單回合 episode（**逐試分開評分**，供 pass^k）。
            episodes = [
                [
                    select_tools_for_case(
                        chat_url,
                        api_key,
                        model,
                        case,
                        tools,
                        max_tokens=max_tokens,
                        request_budget=request_budget,
                    )
                ]
                for _ in range(n_trials)
            ]

        trial_results = [metrics.score_episode(case, ep, known) for ep in episodes]
        head = trial_results[0]  # headline＝第 1 試（與 samples=1 可比）
        results.append(head)
        if turns == 1 and n_trials > 1 and head.evaluated_selection:
            reliability_successes.append(sum(1 for r in trial_results if r.selection_ok))

        head_episode = episodes[0]
        flat = [call for turn in head_episode for call in turn]
        observations.append(
            {
                "case_id": case.id,
                "identity": key,
                "advertised_count": len(tools),
                "turns": [
                    [{"name": c.name, "args": c.args, "status": c.status} for c in turn]
                    for turn in head_episode
                ],
                "calls": [{"name": c.name, "args": c.args, "status": c.status} for c in flat],
            }
        )

    limitations = [
        "builtin-only (no MCP servers mounted, no memory_tools schema)",
        "minimal system prompt (no persona/soul pipeline)",
        "select-only: tools are never executed; status is always 'ok'",
    ]
    if turns > 1:
        limitations.append(
            "multi-turn: intermediate tool results are SIMULATED "
            "(per-case script or generic stub), not real executions"
        )

    reliability = (
        metrics.mean_pass_hat_k(reliability_successes, n_trials) if reliability_successes else None
    )
    report = metrics.aggregate(results, reliability=reliability)
    return {
        "mode": "live-select-only",
        "provider": provider_id,
        "model": model,
        "samples": max(1, samples),
        "turns": max(1, turns),
        "max_tokens": max_tokens,
        "limitations": limitations,
        "advertised_by_identity": {
            k: sorted(_tool_name(t) for t in v) for k, v in tools_by_identity.items()
        },
        "report": asdict(report),
        "report_text": metrics.format_report(report),
        "observations": observations,
    }

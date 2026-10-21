"""LIVE episode 執行器（ADR-0136，互審設計文件 §7.1-A）：真跑 producer 子 loop.

與 ``live_select_only``（只選不執行、無 persona/soul）的差異：
- **完整 pipeline 組裝**（重用 Phase 1 `subagent_tools` 的組裝＝真實
  ProxyPipeline）——persona/soul 依身分注入，隔離對照（C-same vs C-diff）才有效；
- **工具真的執行**（真實 builtin registry；權限照 Gate 1/2）＝會產生 artifact；
- **多輪 ReAct**（handler while-loop 原生）；
- 產出：SSE 抽出的 ``tool_calls``（tool_start/tool_result join）＋最終文字＋
  Phase 2 回報塊解析（claims/artifact_refs/confidence）＋artifact 存在檢查。

真實 LIVE 跑仍為**擁有者手動**（帶 API 金鑰、顯式 ``model``；不進 CI、不排程）：
``python scripts/run_tool_eval_episode.py --case <id> --model <model>``。
單元測試以 mock 串流／組裝縫離線驗證接線（見 test_live_episode.py）。
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.advertised_tools import AdvertisedTools
from src.models.identity import AgentIdentity
from src.services.dispatchers.factory import get_stream_generator
from src.services.permission_service import permission_service
from src.services.providers import registry as provider_registry
from src.services.subagent_report import parse_subagent_report
from src.tools.builtin import tool_registry

# 重用 Phase 1 組裝（真實 pipeline：soul/工具面/系統塊依身分注入；ADR-0134）。
from src.tools.subagent_tools import _assemble_subagent_request
from src.utils.time_utils import get_current_epoch
from src.utils.tool_events import TOOL_RESULT, TOOL_START

from tests.live_api_policy import LiveApiCallBudget

from .cases import ToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from src.services.subagent_report import SubagentReport

    from .cases import EvalCase


@dataclass(frozen=True)
class LiveEpisode:
    """一次 LIVE episode 的完整觀測：工具調用＋最終文字＋回報＋產出存在性。"""

    tool_calls: list[ToolCall]
    final_text: str
    report: SubagentReport
    artifacts_present: dict[str, bool]


class UnsupportedLiveEpisodeProvider(ValueError):
    """provider 的內建 retry 無法由此人工 seam 證明為零。"""

    def __init__(self) -> None:
        super().__init__("Gemini adapter has internal retries; bounded LIVE episode is blocked")


class InvalidLiveEpisodeLimits(ValueError):
    """人工 live episode 的 request/token 上限不合法。"""

    def __init__(self) -> None:
        super().__init__("max_tokens and max_provider_requests must be positive")


class LiveEpisodeLimitsRequired(RuntimeError):
    """真實或注入式 episode 均必須明列 request/token 硬界。"""

    def __init__(self) -> None:
        super().__init__("bounded LiveEpisodeLimits are required")


class LiveEpisodeProviderMismatch(RuntimeError):
    """宣告的 budget provider 與 model 真實路由不一致。"""

    def __init__(self) -> None:
        super().__init__("declared provider does not match the model route")


class LiveEpisodeNoRetryClientRequired(RuntimeError):
    """Anthropic 真 live 必須注入明確 max_retries=0 的 client。"""

    def __init__(self) -> None:
        super().__init__("Anthropic LIVE requires an injected no-retry client")


@dataclass(frozen=True, slots=True)
class LiveEpisodeLimits:
    """人工 episode 的公開 request/token 硬界。"""

    provider: str
    max_tokens: int
    max_provider_requests: int
    anthropic_client: Any | None = None

    def __post_init__(self) -> None:
        if self.max_tokens < 1 or self.max_provider_requests < 1:
            raise InvalidLiveEpisodeLimits
        if self.provider.casefold() == "gemini":
            raise UnsupportedLiveEpisodeProvider


def _limited_send_round(original: Any, budget: LiveApiCallBudget) -> Any:
    """把公開 adapter send_round 包成「核銷後才可外連」的 async generator。"""

    async def _send_round(adapter: Any, state: Any) -> AsyncGenerator[Any]:
        budget.consume()
        async for item in original(adapter, state):
            yield item

    return _send_round


def _provider_request_limit_stack(limits: LiveEpisodeLimits) -> ExitStack:
    """只 patch 本次已核准 provider 的公開 adapter seam。"""
    budget = LiveApiCallBudget(max_requests=limits.max_provider_requests)
    stack = ExitStack()
    if limits.provider.casefold() == "anthropic":
        stack.enter_context(
            patch.object(
                ClaudeAdapter,
                "send_round",
                _limited_send_round(ClaudeAdapter.send_round, budget),
            )
        )
    else:
        stack.enter_context(
            patch.object(
                OpenAIAdapter,
                "send_round",
                _limited_send_round(OpenAIAdapter.send_round, budget),
            )
        )
    return stack


def _provider_for_model(model: str) -> str:
    lowered = model.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    spec = provider_registry.resolve_provider(model)
    return spec.id if spec is not None else "custom-model-router"


def _validate_true_live_transport(model: str, limits: LiveEpisodeLimits) -> None:
    if _provider_for_model(model) != limits.provider.casefold():
        raise LiveEpisodeProviderMismatch
    if limits.provider.casefold() == "anthropic" and limits.anthropic_client is None:
        raise LiveEpisodeNoRetryClientRequired


def _build_identity(agent_id: str, role: str | None = None) -> AgentIdentity:
    """建案例身分（live_select_only 同款：角色可依 case 收窄）。"""
    resolved = role or permission_service.get_role_key(agent_id)
    caps = permission_service.get_capabilities_by_id(agent_id)
    return AgentIdentity.for_role(agent_id=agent_id, role=resolved, capabilities=caps or [])


def _episode_ctx() -> dict[str, Any]:
    """子 loop 執行所需最小 ctx：真實 builtin registry（工具真的執行）。

    v1 邊界：不掛 MCP（protocol_manager=None）、無 memory/plugin registry；
    Claude 原生 brain 需另備 anthropic_client——v1 走 OpenAI 相容供應商
    （與 live_select_only 相同的文件化限制）。
    """
    return {
        "tool_registry": tool_registry,
        "memory_tool_registry": {},
        "plugin_registry": {},
        "protocol_manager": None,
        "is_toukasoul": False,
        "enable_autonomy": False,
    }


async def _assemble_episode_request(
    *,
    case: EvalCase,
    identity: AgentIdentity,
    model: str,
    base_ctx: dict[str, Any],
) -> Any:
    """以 case.prompt 為 goal 跑真實 pipeline 組裝（persona/soul 依身分注入）。"""
    return await _assemble_subagent_request(
        goal=case.prompt, sub_identity=identity, base_ctx=base_ctx, model=model
    )


def _to_call_status(wire_status: str, error_class: str | None) -> str:
    """wire status（success/error＋error_class）→ eval ToolCall status（ok/denied/error）。"""
    if wire_status == "success":
        return "ok"
    if error_class == "permission_denied":
        return "denied"
    return "error"


async def _drain_episode(stream: AsyncGenerator[str]) -> tuple[str, list[ToolCall]]:
    """抽乾子 loop SSE：文字 delta（雙形）＋ tool_start/tool_result join 成 ToolCall。"""
    texts: list[str] = []
    pending: dict[str, tuple[str, dict]] = {}
    calls: list[ToolCall] = []

    async for chunk in stream:
        if not isinstance(chunk, str) or not chunk.startswith("data: "):
            continue
        data_str = chunk[6:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        event_type = data.get("type")
        if event_type == TOOL_START:
            call_id = str(data.get("id", ""))
            pending[call_id] = (str(data.get("tool", "<unknown>")), data.get("args") or {})
            continue
        if event_type == TOOL_RESULT:
            call_id = str(data.get("id", ""))
            name, args = pending.pop(call_id, ("<unknown>", {}))
            calls.append(
                ToolCall(
                    name,
                    args,
                    status=_to_call_status(str(data.get("status", "")), data.get("error_class")),
                )
            )
            continue

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            delta = (choices[0].get("delta") or {}).get("content") or ""
            if delta:
                texts.append(delta)
        elif event_type == "content_block_delta":
            delta = (data.get("delta") or {}).get("text") or ""
            if delta:
                texts.append(delta)

    return "".join(texts).strip(), calls


async def run_case_live_episode(
    case: EvalCase,
    *,
    model: str,
    agent_id: str | None = None,
    artifact_root: Path | None = None,
    limits: LiveEpisodeLimits,
    stream_factory: Any | None = None,
) -> LiveEpisode:
    """跑一個真實 in-process producer episode，回完整觀測（LiveEpisode）。

    Args:
        case: eval 案例（prompt＝producer 的自包含任務；context 帶 agent/role）。
        model: 子 loop 底層模型（**顯式必填**——真實跑＝擁有者手動帶金鑰）。
        agent_id: 覆寫身分（預設取 ``case.context["agent_id"]``，再預設 DebugAgent）。
        artifact_root: artifact 存在檢查根目錄（None＝跳過檢查，回空 dict）。
        limits: 必填的 request/token/client 硬界。
        stream_factory: 僅供離線測試顯式注入 fake；None 才使用真 provider factory。
    """
    if not isinstance(limits, LiveEpisodeLimits):
        raise LiveEpisodeLimitsRequired
    if stream_factory is None or stream_factory is get_stream_generator:
        _validate_true_live_transport(model, limits)
    resolved_agent = agent_id or case.context.get("agent_id", "DebugAgent")
    identity = _build_identity(resolved_agent, case.context.get("role"))
    base_ctx = _episode_ctx()
    if limits.anthropic_client is not None:
        base_ctx["anthropic_client"] = limits.anthropic_client

    request = await _assemble_episode_request(
        case=case, identity=identity, model=model, base_ctx=base_ctx
    )
    request.max_tokens = limits.max_tokens
    base_ctx["advertised_tools"] = AdvertisedTools.from_schemas(request.tools)

    request_id = f"tool-eval-episode:{uuid.uuid4().hex[:8]}"
    resolved_stream_factory = stream_factory or get_stream_generator
    with _provider_request_limit_stack(limits):
        stream = await resolved_stream_factory(
            request.model,
            request,
            identity,
            request_id,
            get_current_epoch(),
            dict(base_ctx),
        )
        final_text, tool_calls = await _drain_episode(stream)
    report = parse_subagent_report(final_text)

    artifacts_present: dict[str, bool] = {}
    if artifact_root is not None:
        must_exist = (case.expected_artifact_check or {}).get("must_exist", []) or []
        refs = list(dict.fromkeys([*report.artifact_refs, *must_exist]))
        artifacts_present = {ref: (artifact_root / ref).is_file() for ref in refs}

    return LiveEpisode(
        tool_calls=tool_calls,
        final_text=final_text,
        report=report,
        artifacts_present=artifacts_present,
    )

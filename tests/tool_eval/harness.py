"""Run eval cases: OFFLINE (replay) or LIVE (in-process real episode).

- OFFLINE: returns each case's ``recorded_tool_calls`` (deterministic, CI-safe).
- LIVE（ADR-0136 已接通）: 跑真實 in-process producer episode（完整 pipeline
  組裝＋工具真的執行；見 ``live_episode.py``）。需**顯式** ``live_model``——
  真實跑＝擁有者手動帶金鑰（scripts/run_tool_eval_episode.py），不進 CI。
"""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING

from . import live_episode, metrics

if TYPE_CHECKING:
    from .cases import EvalCase, ToolCall
    from .metrics import Report

# Static snapshot of builtin tool names (tool_registry keys as of the audit).
# Used as a fallback when the proxy package can't be imported (e.g. outside the
# ToukaProxy environment). LIVE mode should pass the FULL advertised set
# (builtin + MCP) via the ``known`` argument instead.
_FALLBACK_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "update_my_soul",
        "agent_control",
        "research",
        "ask_user",
        "vault_tools",
        "project_tools",
        "memory_tools",
        "mission_tools",
        "bash",
        "ide_open_diff",
        "sns_control",
        "connect_mcp_server",
        "activate_skill",
        "search_skill_market",
        "request_skill_install",
        "propose_new_capability",
        "write_mcp_implementation",
        "verify_mcp_locally",
        "request_mcp_creation",
        "create_skill",
        "create_mcp",
        "scaffold_mcp",
        # Tier 3 統一動詞＋專屬寫入／發送（舊名保留於上＝雙軌 shim 仍可執行）
        "read",
        "list",
        "find",
        "grep",
        "write",
        "edit",
        "insert",
        "undo",
        "delete",
        "edit_soul",
        "send_message",
        # 非同步指令執行（bash → execute_command）
        "execute_command",
        "get_command_result",
        # 演化管線 4→2
        "evolution_define",
        "evolution_implement_and_verify",
    }
)

_ERR_NO_RECORDED = ValueError("offline mode needs recorded_tool_calls (see README)")
_ERR_UNKNOWN_MODE = ValueError("mode must be 'offline' or 'live'")
_ERR_LIVE_MODEL_REQUIRED = RuntimeError(
    "live mode requires an explicit live_model "
    "(owner-manual run with real keys — see tests/tool_eval/README.md)",
)


def known_builtin_tool_names() -> frozenset[str]:
    """Real builtin tool names from the proxy, or the static fallback snapshot."""
    try:
        module = importlib.import_module("src.tools.builtin")
        registry = module.tool_registry
    except (ImportError, AttributeError):
        return _FALLBACK_BUILTIN_TOOLS
    return frozenset(registry.keys())


def run_case(
    case: EvalCase,
    *,
    mode: str = "offline",
    known: frozenset[str] | None = None,
    live_model: str | None = None,
    live_limits: live_episode.LiveEpisodeLimits | None = None,
) -> list[ToolCall]:
    """Return the observed tool calls for one case (offline=replay, live=episode)."""
    if mode == "offline":
        if case.recorded_tool_calls is None:
            raise _ERR_NO_RECORDED
        return list(case.recorded_tool_calls)
    if mode == "live":
        return _run_case_live(
            case,
            known=known,
            live_model=live_model,
            live_limits=live_limits,
        )
    raise _ERR_UNKNOWN_MODE


def run_episode(
    case: EvalCase,
    *,
    mode: str = "offline",
    known: frozenset[str] | None = None,
    live_model: str | None = None,
    live_limits: live_episode.LiveEpisodeLimits | None = None,
) -> list[list[ToolCall]]:
    """Return the observed multi-turn episode（單回合錄製自動視為 1 回合）."""
    if mode == "offline":
        if case.recorded_turns is not None:
            return [list(turn) for turn in case.recorded_turns]
        return [run_case(case, mode=mode, known=known)]
    if mode == "live":
        return [
            _run_case_live(
                case,
                known=known,
                live_model=live_model,
                live_limits=live_limits,
            )
        ]
    raise _ERR_UNKNOWN_MODE


def _run_case_live(
    case: EvalCase,
    *,
    known: frozenset[str] | None = None,  # noqa: ARG001 — 介面對稱保留（MCP 名單由 caller 聚合）
    live_model: str | None = None,
    live_limits: live_episode.LiveEpisodeLimits | None = None,
) -> list[ToolCall]:
    """LIVE（ADR-0136 已接通）：跑真實 in-process producer episode、回工具調用序列.

    完整觀測（final_text／report／artifacts_present）走 ``live_episode.
    run_case_live_episode``；本函式維持 harness 舊契約（list[ToolCall]）。
    ``live_model`` 顯式必填：不讀環境、不猜預設——真實跑＝擁有者手動帶金鑰。
    """
    if not live_model:
        raise _ERR_LIVE_MODEL_REQUIRED
    if not isinstance(live_limits, live_episode.LiveEpisodeLimits):
        raise live_episode.LiveEpisodeLimitsRequired
    episode = asyncio.run(
        live_episode.run_case_live_episode(
            case,
            model=live_model,
            limits=live_limits,
        )
    )
    return list(episode.tool_calls)


def run_suite(
    cases: list[EvalCase],
    *,
    mode: str = "offline",
    known: frozenset[str] | None = None,
    live_model: str | None = None,
    live_limits: live_episode.LiveEpisodeLimits | None = None,
) -> Report:
    """Score a batch of cases and return the aggregate Report."""
    resolved = known or known_builtin_tool_names()
    results = [
        metrics.score_episode(
            c,
            run_episode(
                c,
                mode=mode,
                known=resolved,
                live_model=live_model,
                live_limits=live_limits,
            ),
            resolved,
        )
        for c in cases
    ]
    return metrics.aggregate(results)

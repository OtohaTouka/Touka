"""H1 — Gate-2 CONFIRM fail-closed 防退化錨（hitl-unification 提案 H1；實作前＝紅）。

診斷（2026-07-11）：tool_executor.py:250-251 對「參數無 ``command``」的工具把 CONFIRM
**只記一行 log 就照常執行**（fail-open）——Manual 下 memory／檔案動詞／send_message／
dispatch_subagent／skill/MCP 全家零審批直接跑，是「HITL 選了 Manual 卻沒問我」的主因。

H1 語意：非終端工具的 CONFIRM 一律 fail-closed——回結構化 ``permission_denied``
（reason=approval_required）、**工具函式零呼叫**。H2 已接上統一審批 broker：**互動
會話**（resumable_stream）改為「審批卡＋阻塞等待人類決定」（見
test_hitl_broker_executor）；本檔 ctx 無 resumable_stream＝**headless 形態**，
fail-closed 語意永久成立（「未獲決定前不得執行」的錨）。終端（``command`` in args）
headless 維持 terminal.py 內部遺留通道、互動會話同走 broker。

本檔直接對 ToolExecutor.execute_to_queue 打樁（monkeypatch permission_service 單例的
evaluate_policy），不碰磁碟設定，隔離驗證執行層對三種決策效果的處置。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.models.safety import (
    AuthPoliciesConfig,
    SafetyRules,
    ToolRiskConfig,
    ToolRiskEntry,
)
from src.services import tool_errors
from src.services import tool_executor as executor_module
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor

_IDENTITY = AgentIdentity.for_role(agent_id="FailClosedProbe", role="Owner", is_human=False)


class _Recorder:
    """工具替身：記錄是否被呼叫（防退化錨的核心觀測點）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True, "echo": "ran"})


def _trusted_fixture_executor(monkeypatch, registry):
    """Explicit synthetic registration for recorder-only approval routing tests.

    Real callable identity and replacement denial are tested in F01 separately.
    """
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    for name, handler in registry.items():
        bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    return _executor(registry)


def _executor(registry: dict[str, Any]) -> ToolExecutor:
    return ToolExecutor(
        protocol_manager=None,
        tool_registry=registry,
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )


def _patch_decision(monkeypatch: pytest.MonkeyPatch, effect: str) -> None:
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect=effect, reason=f"test-{effect.lower()}"),
    )


async def _run(
    executor: ToolExecutor, name: str, args: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    queue: asyncio.Queue = asyncio.Queue()
    ctx = {"identity": _IDENTITY, "auth_level": "Manual", "session_id": None}
    result = await executor.execute_to_queue(name, args, "req-hitl", ctx, queue)
    events: list[dict[str, Any]] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


# ── 防退化錨：CONFIRM（非終端）→ 零執行＋結構化拒絕 ─────────────────────────────


@pytest.mark.asyncio
async def test_confirm_never_executes_non_terminal_tool(monkeypatch):
    """CONFIRM 未獲人類決定前，工具函式必須零呼叫（fail-open 主因的直接對釘）。"""
    recorder = _Recorder()
    _patch_decision(monkeypatch, "CONFIRM")
    result, _events = await _run(_executor({"write": recorder}), "write", {"scope": "vault"})

    assert recorder.calls == [], "CONFIRM 下工具被執行＝fail-open 回歸"
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED
    payload = json.loads(result)
    assert payload.get("reason") == "approval_required"


@pytest.mark.asyncio
async def test_confirm_result_event_is_error_status(monkeypatch):
    """CONFIRM fail-closed 的 SSE 事件走 error 態（前端 P3 卡片黃/紅牌可辨識）。"""
    _patch_decision(monkeypatch, "CONFIRM")
    _result, events = await _run(_executor({"write": _Recorder()}), "write", {"scope": "vault"})

    serialized = json.dumps(events, ensure_ascii=False, default=str)
    assert "permission_denied" in serialized
    assert "tool_start" not in serialized, "fail-closed 不得先發 tool_start 再拒絕"


@pytest.mark.asyncio
async def test_confirm_terminal_command_still_reaches_tool(monkeypatch):
    """終端（command in args）H1 維持既有內部審批流：CONFIRM 仍進入 execute_command，
    由 terminal.py 自行處理 approval/ui_unavailable（H2 收斂統一 broker 後另立新錨）。"""
    recorder = _Recorder()
    _patch_decision(monkeypatch, "CONFIRM")
    _result, _events = await _run(
        _trusted_fixture_executor(monkeypatch, {"execute_command": recorder}),
        "execute_command",
        {"command": "ls"},
    )
    assert len(recorder.calls) == 1


# ── 回歸鎖：DENY／ALLOW 行為不變 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deny_blocks_and_never_executes(monkeypatch):
    recorder = _Recorder()
    _patch_decision(monkeypatch, "DENY")
    result, _events = await _run(_executor({"write": recorder}), "write", {"scope": "vault"})

    assert recorder.calls == []
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_allow_executes_tool(monkeypatch):
    recorder = _Recorder()
    _patch_decision(monkeypatch, "ALLOW")
    result, _events = await _run(_executor({"write": recorder}), "write", {"scope": "vault"})

    assert len(recorder.calls) == 1
    assert '"ok": true' in result.lower()


# ── 聚合工具動詞抽取：審計環境只帶動詞（tool_action），不帶整包 args ─────────────


@pytest.mark.asyncio
async def test_policy_context_carries_tool_action_verb(monkeypatch):
    """執行層對聚合工具把動詞抽進 PolicyContext.environment["tool_action"]，
    供引擎按 action 細分（且審計不記整包 args——write 的 content 等大載荷不進審計）。

    動詞參數名來自分類表（action_param）：此處直接打樁 policies，不依賴磁碟設定。
    """
    captured: list[Any] = []

    def _capture(policy_ctx):
        captured.append(policy_ctx)
        return PolicyDecision(effect="ALLOW", reason="capture")

    monkeypatch.setattr(
        permission_service,
        "policies",
        AuthPoliciesConfig(
            policies=SafetyRules(),
            tool_risk=ToolRiskConfig(
                tools={
                    "memory_tools": ToolRiskEntry(
                        action_param="action", actions={"search": "read_only"}
                    )
                }
            ),
        ),
    )
    monkeypatch.setattr(permission_service, "evaluate_policy", _capture)
    recorder = _Recorder()
    await _run(
        _executor({"memory_tools": recorder}),
        "memory_tools",
        {"action": "search", "query": "hi"},
    )

    assert captured, "evaluate_policy 未被呼叫"
    env = captured[0].environment
    assert env.get("tool_action") == "search"
    assert "query" not in env, "整包 args 不得塞進審計環境"

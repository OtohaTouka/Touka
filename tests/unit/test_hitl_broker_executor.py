"""H2 — Gate-2 × 統一審批 broker 接線（hitl-unification）。

互動會話（resumable_stream）的 CONFIRM：審批卡（tool_approval_request）推進聊天
SSE 佇列 → 人類解算 → approved 才執行；rejected/timeout/queue_full＝結構化
permission_denied、**零執行**（H1 錨「未決不執行」在 H2 依然成立）。headless 維持
H1 fail-closed；終端在互動會話同走 broker、以 ``_hitl_preapproved=True`` 跳過
terminal.py 自帶審批（headless 則維持遺留通道 fall-through）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.routers import terminal as terminal_router
from src.services import approval_service as approval_module
from src.services import tool_errors
from src.services import tool_executor as executor_module
from src.services.approval_flow import approval_args_preview
from src.services.approval_service import approval_service
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools import terminal as terminal_tool

_IDENTITY = AgentIdentity.for_role(agent_id="BrokerProbe", role="Owner", is_human=False)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


@pytest.fixture(autouse=True)
def _clean_broker():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


def _trusted_fixture_executor(monkeypatch, registry):
    """Explicit synthetic registration for recorder-only approval routing tests.

    Real callable identity and replacement denial are tested in F01 separately.
    """
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    for name, handler in registry.items():
        bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    return _executor(registry)


def _executor(
    registry: dict[str, Any] | None = None,
    memory_registry: dict[str, Any] | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        protocol_manager=None,
        tool_registry=registry or {},
        memory_tool_registry=memory_registry or {},
        plugin_registry={},
    )


def _ctx(*, interactive: bool) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "identity": _IDENTITY,
        "auth_level": "Manual",
        "session_id": "sess-broker",
    }
    if interactive:
        ctx["resumable_stream"] = True
    return ctx


def _patch_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="CONFIRM", reason="test-confirm"),
    )


async def _run_with_resolution(
    executor: ToolExecutor,
    name: str,
    args: dict[str, Any],
    *,
    approved: bool,
) -> tuple[str, list[dict[str, Any]]]:
    """跑 execute_to_queue（互動 ctx），待審批出現後以指定決定解算。"""
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        executor.execute_to_queue(name, args, "req-broker", _ctx(interactive=True), queue)
    )
    exec_id = None
    for _ in range(200):  # 等待登記出現（避免 sleep 競態）
        pending_ids = list(approval_service._pending)
        if pending_ids:
            exec_id = pending_ids[0]
            break
        await asyncio.sleep(0.005)
    assert exec_id, "審批從未登記"
    await approval_service.resolve(exec_id, approved=approved, resolver="tester")
    result = await task
    events: list[dict[str, Any]] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


# ── 互動會話：approve → 執行；reject/timeout/queue_full → 零執行 ─────────────────


@pytest.mark.asyncio
async def test_interactive_confirm_approved_executes(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    result, events = await _run_with_resolution(
        _executor({"write": recorder}), "write", {"scope": "vault"}, approved=True
    )
    assert len(recorder.calls) == 1
    assert '"ok": true' in result.lower()

    types = [e.get("type") for e in events]
    assert "tool_approval_request" in types
    assert "tool_approval_resolved" in types
    assert types.index("tool_approval_request") < types.index("tool_start"), (
        "審批卡必須先於 tool_start（未核准前不得開始執行）"
    )
    resolved = next(e for e in events if e.get("type") == "tool_approval_resolved")
    assert resolved["decision"] == "approved"


@pytest.mark.asyncio
async def test_interactive_confirm_rejected_never_executes(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    result, events = await _run_with_resolution(
        _executor({"write": recorder}), "write", {"scope": "vault"}, approved=False
    )
    assert recorder.calls == []
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED
    assert json.loads(result)["reason"] == "approval_rejected"
    assert "tool_start" not in [e.get("type") for e in events]


@pytest.mark.asyncio
async def test_interactive_confirm_timeout_fail_closed(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    monkeypatch.setattr(approval_module, "DEFAULT_TIMEOUT_S", 0.05)
    queue: asyncio.Queue = asyncio.Queue()
    result = await _executor({"write": recorder}).execute_to_queue(
        "write", {"scope": "vault"}, "req-t", _ctx(interactive=True), queue
    )
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_timeout"


@pytest.mark.asyncio
async def test_interactive_queue_full_fail_closed(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    monkeypatch.setattr(approval_module, "MAX_PENDING", 0)
    queue: asyncio.Queue = asyncio.Queue()
    result = await _executor({"write": recorder}).execute_to_queue(
        "write", {"scope": "vault"}, "req-q", _ctx(interactive=True), queue
    )
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_queue_full"


@pytest.mark.asyncio
async def test_memory_family_also_gated_and_executes_on_approval(monkeypatch):
    """四家族收斂：memory registry 工具同走 broker（approved → 執行）。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    _result, events = await _run_with_resolution(
        _executor(memory_registry={"memory_tools": recorder}),
        "memory_tools",
        {"action": "save", "content": "hi"},
        approved=True,
    )
    assert len(recorder.calls) == 1
    assert "tool_approval_request" in [e.get("type") for e in events]


# ── headless：H1 錨不變 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_headless_confirm_still_fail_closed(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    result = await _executor({"write": recorder}).execute_to_queue(
        "write", {"scope": "vault"}, "req-h", _ctx(interactive=False), queue
    )
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_required"
    assert approval_service.pending_count() == 0  # headless 不進 broker


# ── 終端：互動走 broker＋預核准透傳；headless 維持遺留 fall-through ──────────────


@pytest.mark.asyncio
async def test_terminal_interactive_broker_preapproves(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    _result, events = await _run_with_resolution(
        _trusted_fixture_executor(monkeypatch, {"execute_command": recorder}),
        "execute_command",
        {"command": "mkdir x"},
        approved=True,
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_hitl_preapproved") is True
    assert "tool_approval_request" in [e.get("type") for e in events]


@pytest.mark.asyncio
async def test_terminal_interactive_rejected_never_reaches_tool(monkeypatch):
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    result, _events = await _run_with_resolution(
        _trusted_fixture_executor(monkeypatch, {"execute_command": recorder}),
        "execute_command",
        {"command": "mkdir x"},
        approved=False,
    )
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_rejected"


@pytest.mark.asyncio
async def test_terminal_headless_falls_through_to_legacy(monkeypatch):
    """headless 終端維持遺留通道：CONFIRM 仍進 execute_command（其內部
    ToukaUI/ui_unavailable 邏輯自理），且未帶預核准。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    await _trusted_fixture_executor(monkeypatch, {"execute_command": recorder}).execute_to_queue(
        "execute_command", {"command": "ls"}, "req-th", _ctx(interactive=False), queue
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_hitl_preapproved") is False


# ── ADR-0148：自帶審批工具（request_skill_install）跳過 broker，不雙重審批 ────────


@pytest.mark.asyncio
async def test_self_approving_tool_skips_broker_interactive(monkeypatch):
    """request_skill_install 在互動 CONFIRM 下**不**彈 broker 卡（自帶 EvolutionModal
    才是唯一審批）；工具直接執行、收到 _interactive=True 自行 gate。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    await _trusted_fixture_executor(
        monkeypatch, {"request_skill_install": recorder}
    ).execute_to_queue(
        "request_skill_install", {"skill_id": "x"}, "req-self-i", _ctx(interactive=True), queue
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_interactive") is True
    assert recorder.calls[0].get("_hitl_preapproved") is False  # broker 未預核准
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    assert "tool_approval_request" not in [e.get("type") for e in events], (
        "自帶審批工具不得再彈 broker 卡（雙重審批）"
    )
    assert approval_service.pending_count() == 0


@pytest.mark.asyncio
async def test_self_approving_tool_headless_passes_interactive_false(monkeypatch):
    """headless CONFIRM 下 request_skill_install 同樣跳過 broker（不在 executor
    fail-closed），改由工具本體據 _interactive=False 自行 fail-closed。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    await _trusted_fixture_executor(
        monkeypatch, {"request_skill_install": recorder}
    ).execute_to_queue(
        "request_skill_install", {"skill_id": "x"}, "req-self-h", _ctx(interactive=False), queue
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_interactive") is False


# ── terminal.execute_command：預核准跳過自帶審批 ────────────────────────────────


@pytest.mark.asyncio
async def test_execute_command_preapproved_skips_internal_approval(monkeypatch):
    async def _fake_core(command, env, drive_path, session_id, emit=None, call_id=None, **_kwargs):
        return 0, "done", 1.0

    monkeypatch.setattr(terminal_tool, "_execute_core", _fake_core)
    result = await terminal_tool.execute_command(
        command="mkdir dangerous-thing",
        identity=_IDENTITY,
        auth_level="Manual",
        _hitl_preapproved=True,
    )
    assert "EXIT_CODE: 0" in result
    assert "approval_pending" not in result
    assert "ui_unavailable" not in result


@pytest.mark.asyncio
async def test_execute_command_without_preapproval_keeps_legacy_gate():
    result = await terminal_tool.execute_command(
        command="mkdir dangerous-thing",
        identity=_IDENTITY,
        auth_level="Manual",
    )
    payload = json.loads(result)
    assert payload["error_class"] == "permission_denied"
    assert payload["reason"] == "ui_unavailable"  # 無 ToukaUI 訂閱者＝遺留 fail-closed


# ── request_approval：聊天佇列 emit ＋ 無通道快速拒絕 ───────────────────────────


@pytest.mark.asyncio
async def test_request_approval_fast_rejects_without_any_channel():
    approved, payload = await terminal_router.request_approval(
        "fastfail-1", _IDENTITY, "Install Skill: x", type="skill_install_approval_request"
    )
    assert approved is False
    assert payload == {}
    assert "fastfail-1" not in terminal_router.pending_approvals


@pytest.mark.asyncio
async def test_request_approval_emits_to_chat_stream_and_resolves():
    received: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        received.append(event)

    task = asyncio.create_task(
        terminal_router.request_approval(
            "emit-1",
            _IDENTITY,
            "Install Skill: y",
            type="skill_install_approval_request",
            extra_data={"skill_id": "y"},
            emit=_emit,
        )
    )
    for _ in range(200):
        if "emit-1" in terminal_router.pending_approvals:
            break
        await asyncio.sleep(0.005)
    assert received and received[0]["type"] == "skill_install_approval_request"
    assert received[0]["skill_id"] == "y"

    terminal_router.approval_results["emit-1"] = "approved"
    terminal_router.pending_approvals["emit-1"].set()
    approved, _payload = await task
    assert approved is True


def test_approval_args_preview_truncates_large_values():
    preview = approval_args_preview(
        {"scope": "vault", "content": "x" * 5000, "count": 3, "flag": True}
    )
    assert preview["scope"] == "vault"
    assert preview["count"] == 3 and preview["flag"] is True
    assert len(preview["content"]) < 300
    assert "chars" in preview["content"]  # 截斷標記

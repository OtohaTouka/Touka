"""ADR-0149（ADR-0148 對抗式審查揪出的既有缺口，非 0148 引入）— 互動演化部署審批雙修。

症狀：互動聊天會話中 evolution_implement_and_verify(submit_for_deploy=True) 沙盒
驗證通過後，部署 **雙重審批＋靜默失敗**——
  1. evolution_implement_and_verify 分類 dangerous → tool_executor 先彈統一 broker
     卡（卡一），人類核准；
  2. 執行中 request_mcp_creation 又無條件呼 request_approval
     (skill_creation_approval_request)（卡二，帶 source_code），未納入
     _SELF_APPROVING_TOOLS、也不看 _interactive；
  3. 卡二型別過去不在 ToukaChat 白名單 → 前端丟棄 → 180s 逾時 auto-reject。

修法（比照 ADR-0148 request_skill_install）：
  (a) evolution_implement_and_verify ＋ request_mcp_creation 進 _SELF_APPROVING_TOOLS
      （跳過 broker 卡一，不雙重審批）；
  (b) request_mcp_creation 自帶 _interactive headless fail-closed（不空等 180s）。

本檔覆蓋 (a)（executor 跳過 broker）＋(b)（request_mcp_creation self-gate）。前端
白名單／渲染見 ToukaChat 對應 vitest。

iso-c1 缺口 1 推翻 (a) 的一半：evolution_implement_and_verify 不再自帶審批——建置（寫碼、
venv、pip、import 代理人程式）前先過一般審批卡，無人在場由 executor 拒絕；部署子步
request_mcp_creation 仍自帶審批（完整證據見 test_evolution_manual_approval_security.py）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.permission_service import permission_service
from src.services.tool_executor import _SELF_APPROVING_TOOLS, ToolExecutor
from src.tools.builtin import evolution
from src.tools.builtin.evolution import request_mcp_creation

_IDENTITY = AgentIdentity.for_role(agent_id="Evolver", role="Owner", is_human=False)


# ═══════════════════════════════════════════════════════════════════════════════
# (b) request_mcp_creation 本體 self-gate（比照 request_skill_install）
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_headless_fail_closed_no_approval_call(mock_approval):
    """headless（_interactive=False）→ 立即 fail-closed，**從不**呼叫 request_approval
    （不空等 180s；H1 headless 語意保持）。"""
    mock_approval.return_value = (True, {})
    result = await request_mcp_creation("some-skill", identity=_IDENTITY, _interactive=False)
    assert "interactive session" in result.lower()
    mock_approval.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_missing_interactive_flag_treated_as_headless(mock_approval):
    """未帶 _interactive kwarg（防禦性）→ 視同 headless fail-closed。"""
    mock_approval.return_value = (True, {})
    result = await request_mcp_creation("some-skill", identity=_IDENTITY)
    assert "interactive session" in result.lower()
    mock_approval.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tools.builtin.evolution.request_approval")
async def test_interactive_calls_own_creation_approval(mock_approval, zone_data_root):
    """互動（_interactive=True）→ 呼叫自帶 request_approval，型別
    skill_creation_approval_request 且帶 source_code（前端據此渲染審批卡）。

    使用者拒絕 → 回 Rejected、不部署（此處只驗『有走自帶審批卡＋型別/載荷』這一環）。
    檔案用真的技能資料夾（ADR-0293 PR 2：演化的檔案經 RM 區方法讀寫）。
    """
    guard = evolution.EvolutionPathGuard
    guard.mkdir_tool("some-skill")
    guard.write_tool_file("some-skill", "server.py", "print('hi')")
    guard.write_tool_file("some-skill", "requirements.txt", "")
    guard.write_tool_file("some-skill", "spec.json", '{"name": "X", "description": "d"}')
    mock_approval.return_value = (False, {})  # 拒絕 → 不部署

    result = await request_mcp_creation(
        "some-skill", identity=_IDENTITY, _interactive=True, _emit_event=AsyncMock()
    )

    mock_approval.assert_awaited_once()
    call_kwargs = mock_approval.call_args.kwargs
    assert call_kwargs.get("type") == "skill_creation_approval_request"
    assert call_kwargs.get("extra_data", {}).get("source_code") == "print('hi')"
    # H2：審批同步推進當前聊天 SSE（emit 透傳），否則前端收不到 → 空等逾時
    assert call_kwargs.get("emit") is not None
    assert "rejected" in result.lower() or "denied" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# (a) tool_executor：演化部署工具自帶審批 → 跳過 broker（不雙重審批）
# ═══════════════════════════════════════════════════════════════════════════════


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ok"


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


def _executor(registry: dict[str, Any]) -> ToolExecutor:
    return ToolExecutor(
        protocol_manager=None,
        tool_registry=registry,
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )


def _ctx(*, interactive: bool) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "identity": _IDENTITY,
        "auth_level": "Manual",
        "session_id": "sess-evo",
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


def _valid_tool_args(tool_name: str) -> dict[str, Any]:
    args: dict[str, Any] = {"skill_id": "x"}
    if tool_name == "evolution_implement_and_verify":
        args.update(code="pass", test_tool_name="probe", test_arguments={})
    return args


async def _drain(queue: asyncio.Queue) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return events


def test_evolution_deploy_tools_are_self_approving():
    """名單釘死：只有部署子步 request_mcp_creation 自帶審批；演化建置走一般審批（缺口 1）。"""
    assert "evolution_implement_and_verify" not in _SELF_APPROVING_TOOLS
    assert "request_mcp_creation" in _SELF_APPROVING_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["request_mcp_creation"])
async def test_evolution_deploy_skips_broker_interactive(monkeypatch, tool_name):
    """互動 CONFIRM 下**不**彈 broker 卡（部署子步自帶 skill_creation_approval_request
    才是唯一審批）；工具直接執行、收 _interactive=True 自行 gate、未帶 broker 預核准。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    await _trusted_fixture_executor(monkeypatch, {tool_name: recorder}).execute_to_queue(
        tool_name, _valid_tool_args(tool_name), "req-evo-i", _ctx(interactive=True), queue
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_interactive") is True
    assert recorder.calls[0].get("_hitl_preapproved") is False  # broker 未預核准
    events = await _drain(queue)
    assert "tool_approval_request" not in [e.get("type") for e in events], (
        "自帶審批工具不得再彈 broker 卡（雙重審批）"
    )
    assert approval_service.pending_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["request_mcp_creation"])
async def test_evolution_deploy_headless_passes_interactive_false(monkeypatch, tool_name):
    """headless CONFIRM 下同跳過 broker（不在 executor fail-closed），改由工具本體據
    _interactive=False 自行 fail-closed（request_mcp_creation：部署 fail-closed）。"""
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    await _trusted_fixture_executor(monkeypatch, {tool_name: recorder}).execute_to_queue(
        tool_name, _valid_tool_args(tool_name), "req-evo-h", _ctx(interactive=False), queue
    )
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_interactive") is False
    events = await _drain(queue)
    assert "tool_approval_request" not in [e.get("type") for e in events]


@pytest.mark.asyncio
async def test_evolution_build_asks_broker_first_interactive(monkeypatch):
    """iso-c1 缺口 1：互動 CONFIRM 下演化建置先跳一般審批卡，核准前不執行。"""
    tool_name = "evolution_implement_and_verify"
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _executor({tool_name: recorder}).execute_to_queue(
            tool_name, _valid_tool_args(tool_name), "req-evo-i", _ctx(interactive=True), queue
        )
    )
    try:
        async with asyncio.timeout(5):
            while (event := await queue.get()) is not None:
                if event.get("type") == "tool_approval_request":
                    break
        assert event is not None, "演化建置必須先跳一般審批卡"
        assert recorder.calls == []
        pending = next(iter(approval_service._pending))
        await approval_service.resolve(pending, approved=True, resolver="synthetic-owner")
        async with asyncio.timeout(5):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("_hitl_preapproved") is True


@pytest.mark.asyncio
async def test_evolution_build_headless_refused_by_executor(monkeypatch):
    """iso-c1 缺口 1：headless CONFIRM 下 executor 直接拒絕（approval_required），工具不執行。"""
    tool_name = "evolution_implement_and_verify"
    recorder = _Recorder()
    _patch_confirm(monkeypatch)
    queue: asyncio.Queue = asyncio.Queue()
    result = await _executor({tool_name: recorder}).execute_to_queue(
        tool_name, _valid_tool_args(tool_name), "req-evo-h", _ctx(interactive=False), queue
    )
    assert recorder.calls == []
    assert '"approval_required"' in result
    events = await _drain(queue)
    assert "tool_approval_request" not in [e.get("type") for e in events]

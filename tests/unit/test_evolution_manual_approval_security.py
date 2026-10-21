"""缺口 1：演化工具不再算「自帶審批」，Manual 下先過一般審批卡（iso-c1 U2）.

修補前：``evolution_implement_and_verify`` 在 ``BUILTIN_EXECUTION_BINDINGS`` 綁成
SELF_APPROVING，Manual／CONFIRM 時 executor 不跳卡，工具只在最後「部署」才問人——寫碼、
建 venv、``pip install``、import 代理人的 ``server.py`` 都不問人就跑，無人在場也跑（Auto 下它屬
F5 只有人能核准，所以 Manual 反而比 Auto 鬆）。

修補後：拿掉這個綁定（工具回到一般分類）。
- Manual、有人在場：先跳一般審批卡，核准才建 venv／pip／驗證；拒絕就什麼都不跑。
- Manual、無人在場：executor 直接拒絕（approval_required），什麼都不跑。
- 部署前那張 ``skill_creation_approval_request`` 卡照舊（工具靠 ``_interactive`` 判斷）。
- Auto（F5 只有人能核准）、YOLO 行為不變；``request_skill_install``、``request_mcp_creation``
  仍是自帶審批。

全部 sandbox：真政策檔取 tracked ``auth_policies.json.example``；沙盒子程序、寫檔與部署卡全換成
記錄器，不建 venv、不連網、不寫 ``data/``。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import types
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.permission_service import permission_service
from src.services.tool_executor import _SELF_APPROVING_TOOLS, ToolExecutor
from src.tools.builtin import BUILTIN_EXECUTION_BINDINGS, tool_registry
from src.tools.builtin import evolution as evolution_module
from src.tools.execution_policy import ToolExecutionClass

_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY = AgentIdentity.for_role(agent_id="EvolutionGateProbe", role="Owner")
_TOOL = "evolution_implement_and_verify"
_ARGS: dict[str, Any] = {
    "skill_id": "synthetic_evo",
    "code": "pass",
    "test_tool_name": "probe",
    "test_arguments": {},
    "dependencies": ["example-package"],
    "submit_for_deploy": True,
}
_WAIT_S = 5.0


class _Sandbox:
    """記錄演化管線的每個副作用（寫檔、子程序）；驗證子程序回傳成功標記。"""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.commands: list[list[str]] = []

    def write(self, _skill_id: str, filename: str, _content: str) -> None:
        self.writes.append(filename)

    def run(self, _skill_id: str, cmd_list: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.commands.append(list(cmd_list))
        return SimpleNamespace(
            returncode=0, stdout="", stderr=""
        )

    @property
    def touched(self) -> bool:
        return bool(self.writes or self.commands)


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> _Sandbox:
    """真政策＋真 handler；只把沙盒 I/O、部署卡與部署設定換成記錄器。"""
    policy = AuthPoliciesConfig.model_validate_json(
        (_ROOT / "ToukaProxy/config/auth_policies.json.example").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(permission_service, "policies", policy)
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)
    monkeypatch.setattr(executor_module.soul_service, "is_toukasoul", lambda _: False)
    recorder = _Sandbox()
    guard = evolution_module.EvolutionPathGuard
    monkeypatch.setattr(
        guard, "tool_file_exists", lambda _skill, name: name in {"spec.json", "server.py"}
    )
    monkeypatch.setattr(guard, "write_tool_file", recorder.write)
    monkeypatch.setattr(guard, "delete_tool_file", lambda *_a: True)
    monkeypatch.setattr(guard, "read_tool_file", lambda *_a: "{}")
    monkeypatch.setattr(evolution_module.resource_manager, "read_file_binary", lambda *_a: b"{}")
    monkeypatch.setattr(guard, "run_sandbox_command", recorder.run)
    monkeypatch.setattr(evolution_module, "config_utils", MagicMock())
    session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(content=[])))
    monkeypatch.setattr(evolution_module, "connect_mcp_server", AsyncMock(
        return_value=(SimpleNamespace(session=session), AsyncExitStack()),
    ))

    @asynccontextmanager
    async def windows(*_args):
        yield SimpleNamespace(session=session)

    monkeypatch.setattr(evolution_module, "_windows_client", windows)
    approval_service.reset_for_test()
    yield recorder
    approval_service.reset_for_test()


@pytest.fixture
def deploy_card(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """部署前那張自帶卡（skill_creation_approval_request）；一律回「拒絕」，不熱部署。"""
    card = AsyncMock(return_value=(False, {}))
    monkeypatch.setattr(evolution_module, "request_approval", card)
    return card


def _ctx(level: str, *, present: bool) -> dict[str, Any]:
    return {"identity": _IDENTITY, "auth_level": level, "resumable_stream": present}


async def _first_card(queue: asyncio.Queue) -> dict[str, Any] | None:
    """讀到第一張一般審批卡；呼叫先結束（None）就回 None。"""
    async with asyncio.timeout(_WAIT_S):
        while True:
            event = await queue.get()
            if event is None:
                return None
            if event.get("type") == "tool_approval_request":
                return event


async def _run_with_card(level: str, *, approve: bool) -> tuple[str, dict[str, Any] | None]:
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, tool_registry, {}, {}).execute_to_queue(
            _TOOL, dict(_ARGS), "evo-gate", _ctx(level, present=True), queue
        )
    )
    try:
        card = await _first_card(queue)
        if card is not None:
            pending = next(iter(approval_service._pending))
            await approval_service.resolve(pending, approved=approve, resolver="synthetic-owner")
        async with asyncio.timeout(_WAIT_S):
            result = await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return result, card


async def _run_headless(level: str) -> str:
    async with asyncio.timeout(_WAIT_S):
        return await ToolExecutor(None, tool_registry, {}, {}).execute_to_queue(
            _TOOL, dict(_ARGS), "evo-headless", _ctx(level, present=False), asyncio.Queue()
        )


# ── 分類：演化不再自帶審批；安裝技能與部署仍自帶 ────────────────────────────────


def test_evolution_is_no_longer_self_approving():
    assert _TOOL not in BUILTIN_EXECUTION_BINDINGS
    assert _TOOL not in _SELF_APPROVING_TOOLS


@pytest.mark.parametrize("name", ["request_skill_install", "request_mcp_creation"])
def test_install_and_deploy_keep_their_own_approval(name: str):
    assert BUILTIN_EXECUTION_BINDINGS[name].kind == ToolExecutionClass.SELF_APPROVING
    assert name in _SELF_APPROVING_TOOLS


# ── Manual：先跳一般審批卡，核准才建置 ─────────────────────────────────────────


async def test_manual_with_human_asks_before_any_build_and_rejection_runs_nothing(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        ToolExecutor(None, tool_registry, {}, {}).execute_to_queue(
            _TOOL, dict(_ARGS), "evo-reject", _ctx("Manual", present=True), queue
        )
    )
    try:
        card = await _first_card(queue)
        assert card is not None, "Manual 有人在場時，建置前必須先跳一般審批卡"
        assert not sandbox.touched, "審批卡未決前不得寫碼、建 venv 或 pip install"
        pending = next(iter(approval_service._pending))
        await approval_service.resolve(pending, approved=False, resolver="synthetic-owner")
        async with asyncio.timeout(_WAIT_S):
            result = await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert json.loads(result)["reason"] == "approval_rejected"
    assert not sandbox.touched
    deploy_card.assert_not_awaited()


async def test_manual_with_human_approved_builds_then_deploy_card_still_asks(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result, card = await _run_with_card("Manual", approve=True)
    assert card is not None
    assert "human_only" not in card
    assert sandbox.writes[:2] == ["server.py", "requirements.txt"]
    assert any("pip" in command for command in sandbox.commands)
    deploy_card.assert_awaited_once()
    assert deploy_card.await_args.kwargs["type"] == "skill_creation_approval_request"
    assert "Rejected" in result


async def test_manual_without_human_is_refused_by_executor_and_runs_nothing(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result = await _run_headless("Manual")
    assert json.loads(result)["reason"] == "approval_required"
    assert not sandbox.touched
    deploy_card.assert_not_awaited()


# ── 釘住：Auto 仍是 HUMAN_ONLY（兩張卡）、YOLO 行為不變 ───────────────────────────


async def test_auto_is_still_human_only_card_then_deploy_card(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result, card = await _run_with_card("Auto", approve=True)
    assert card is not None
    assert card.get("human_only") is True
    assert sandbox.commands
    deploy_card.assert_awaited_once()
    assert "Rejected" in result


async def test_auto_without_human_is_refused_and_runs_nothing(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result = await _run_headless("Auto")
    assert json.loads(result)["reason"] == "human_only"
    assert not sandbox.touched
    deploy_card.assert_not_awaited()


async def test_yolo_with_human_builds_without_executor_card_and_deploy_card_still_asks(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result, card = await _run_with_card("YOLO", approve=True)
    assert card is None
    assert sandbox.commands
    deploy_card.assert_awaited_once()
    assert "Rejected" in result


async def test_yolo_without_human_builds_but_deploy_fails_closed(
    sandbox: _Sandbox, deploy_card: AsyncMock
):
    result = await _run_headless("YOLO")
    assert sandbox.commands
    deploy_card.assert_not_awaited()
    assert "interactive session" in result

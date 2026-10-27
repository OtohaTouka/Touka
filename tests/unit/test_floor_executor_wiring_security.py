"""R-2c 修正（ADR-0246）：executor 與底線、審查員、IDE 標記的接線。

- F5 動詞不受設定檔左右：``agent_control`` 的動詞由 executor 直接從 ``args["action"]``
  取，``tool_risk`` 少了該工具或 ``action_param`` 改名，Auto 下 ``switch_brain`` 仍只有人
  能核准（無人在場拒絕、審查員沒被呼叫）。
- 告訴審查員的 shell：和 ``_execute_core`` 共用 ``effective_shell``（非 Windows 一律 bash）。
- IDE「由人核准」標記只在審批卡沒有截斷任何參數時產生；截斷時 IDE 照常自己審。

全部 sandbox；審查員一律假的，不打 API（禁令 61）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.protocols.mcp.canonical import McpCompleteOutcome
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services import approval_flow
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.auto_reviewer import ReviewOutcome
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools import terminal as terminal_tool
from src.utils.user_intent import chat_intent

if TYPE_CHECKING:
    from src.protocols.mcp.canonical import McpRequestContext
    from src.services.auto_reviewer import ReviewRequest

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "auth_policies.json.example"
)
_SOUL = AgentIdentity.for_role(agent_id="FloorWiringProbe", role="Owner", is_human=False)
_WAIT_S = 5.0


def _policy(mutate=None) -> AuthPoliciesConfig:
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(data)
    return AuthPoliciesConfig.model_validate(data)


@pytest.fixture(autouse=True)
def _clean():
    approval_service.reset_for_test()
    yield
    approval_service.reset_for_test()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


class _FakeReviewer:
    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    async def review(self, _identity: AgentIdentity, request: ReviewRequest) -> ReviewOutcome:
        self.requests.append(request)
        return ReviewOutcome("deny", "test reviewer refuses", "test")


def _native_executor(
    monkeypatch: pytest.MonkeyPatch, policy: AuthPoliciesConfig, name: str, handler: Any
) -> ToolExecutor:
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    if name in bindings:  # 只有特殊執行類別（終端、自帶審批）有封存的 binding
        bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    monkeypatch.setattr(permission_service, "policies", policy)
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)
    monkeypatch.setattr(
        permission_service,
        "resolve_grants",
        lambda _role: SimpleNamespace(tools=SimpleNamespace(allows=lambda _name: True)),
    )
    return ToolExecutor(
        protocol_manager=None,
        tool_registry={name: handler},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )


# ── 3. switch_brain 的 F5 不受設定檔左右 ─────────────────────────────────────────


def _drop_agent_control(data: dict[str, Any]) -> None:
    del data["tool_risk"]["tools"]["agent_control"]


def _rename_action_param(data: dict[str, Any]) -> None:
    data["tool_risk"]["tools"]["agent_control"]["action_param"] = "mode"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [_drop_agent_control, _rename_action_param])
async def test_switch_brain_is_f5_even_without_config_action_param(monkeypatch, mutate):
    policy = _policy(mutate)
    assert policy.tool_risk.action_param_for("agent_control") != "action"
    recorder = _Recorder()
    reviewer = _FakeReviewer()
    monkeypatch.setattr(executor_module, "auto_reviewer", reviewer)
    executor = _native_executor(monkeypatch, policy, "agent_control", recorder)
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(_WAIT_S):
        result = await executor.execute_to_queue(
            "agent_control",
            {"action": "switch_brain", "mode": "list_brains", "model": "x"},
            "req-f5",
            {"identity": _SOUL, "auth_level": "Auto", "session_id": "s-f5"},
            queue,
        )
    assert json.loads(result)["reason"] == "human_only"
    assert recorder.calls == []
    assert reviewer.requests == []


# ── 4. 告訴審查員的 shell 和實際執行共用同一個判斷 ─────────────────────────────


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", None, "bash"),
        ("linux", "powershell", "bash"),
        ("win32", None, "powershell"),
        ("win32", "wsl", "wsl"),
        ("win32", "bash", "unsupported:bash"),
    ],
)
def test_effective_shell_follows_platform(monkeypatch, platform, env, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert terminal_tool.effective_shell(env) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "args", "expected"),
    [
        ("linux", {"command": "ls"}, "bash"),
        ("win32", {"command": "ls"}, "powershell"),
        ("win32", {"command": "ls", "env": "wsl"}, "wsl"),
    ],
)
async def test_reviewer_is_told_the_shell_that_actually_runs(monkeypatch, platform, args, expected):
    recorder = _Recorder()
    reviewer = _FakeReviewer()
    monkeypatch.setattr(executor_module, "auto_reviewer", reviewer)
    executor = _native_executor(monkeypatch, _policy(), "execute_command", recorder)
    queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(sys, "platform", platform)
    async with asyncio.timeout(_WAIT_S):
        await executor.execute_to_queue(
            "execute_command",
            args,
            "req-shell",
            {"identity": _SOUL, "auth_level": "Auto", "session_id": "s-shell"},
            queue,
        )
    monkeypatch.undo()
    assert [r.shell_kind for r in reviewer.requests] == [expected]
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_execute_core_uses_the_shared_shell_decision(monkeypatch, tmp_path):
    spawned: list[tuple[Any, ...]] = []

    async def _spawn(*argv: Any, **_kwargs: Any):
        spawned.append(argv)
        raise RuntimeError

    monkeypatch.setattr(terminal_tool, "spawn_exec", _spawn)
    monkeypatch.setattr(terminal_tool, "effective_shell", lambda _env: "no-such-shell")
    exit_code, output, _ms = await terminal_tool._execute_core("ls", "powershell", tmp_path, None)
    assert exit_code == -1
    assert "Unknown environment" in output
    assert spawned == []


# ── 6. IDE 標記只在審批卡沒有截斷任何參數時產生 ─────────────────────────────────


@pytest.mark.parametrize(
    ("args", "truncated"),
    [
        ({"filePath": "a.py", "newContent": "x" * 200}, False),
        ({"filePath": "a.py", "newContent": "x" * 201}, True),
        ({"command": "c" * 8192}, False),
        ({"command": "c" * 8193}, True),
        ({"edits": ["y" * 300]}, True),
        ({"flag": True, "count": 3, "none": None}, False),
    ],
)
def test_preview_truncation_detection(args, truncated):
    assert approval_flow.approval_preview_truncated(args) is truncated


class _FakeProtocolManager:
    def __init__(self) -> None:
        self.registry = MagicMock()
        self.registry.get_server.return_value = MagicMock(vault_jail_arg=None)
        self.contexts: list[McpRequestContext] = []

    def resolve_tool(self, name: str) -> tuple[str, str]:
        return "ToukaUI", name

    async def call_tool_on_server(self, _sid, _tool, _args, *, request_context):
        self.contexts.append(request_context)
        return McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": "ok"}],
        )


async def _approve_ide_call(monkeypatch, args: dict[str, Any]) -> _FakeProtocolManager:
    monkeypatch.setattr(permission_service, "evaluate_policy", _policy().evaluate)
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: True)
    pm = _FakeProtocolManager()
    executor = ToolExecutor(pm, {}, {}, {}, enforce_advertised=False)
    ctx = {
        "identity": _SOUL,
        "auth_level": "Manual",
        "session_id": "s-ide-trunc",
        "resumable_stream": True,
        "user_intent": chat_intent([{"role": "user", "content": "open the diff"}]),
    }
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(_WAIT_S):
        task = asyncio.create_task(
            executor.execute_to_queue("openDiff", args, "req-ide-trunc", ctx, queue)
        )
        while not approval_service._pending:
            assert not task.done(), "應該跳出審批卡"
            await asyncio.sleep(0.005)
        exec_id = next(iter(approval_service._pending))
        await approval_service.resolve(exec_id, approved=True, resolver="tester")
        await task
    return pm


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "marked"),
    [
        ({"filePath": "a.py", "newContent": "x" * 200}, True),
        ({"filePath": "a.py", "newContent": "x" * 201}, False),
    ],
)
async def test_ide_marker_only_when_the_card_showed_every_argument(monkeypatch, args, marked):
    pm = await _approve_ide_call(monkeypatch, args)
    assert [c.human_approved for c in pm.contexts] == [marked]

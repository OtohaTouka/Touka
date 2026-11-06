"""ADR-0246 U2c-5：心跳的模式對應與 KF-4（心跳第一次拿到工具）。

從 ``/v1/heartbeat/awaken`` 入口走一遍：入口決定的模式 → 脈衝在 ``dispatch_request`` 前跑
ToolLayer（不跑 MonitorLayer）→ 交給 dispatch 的請求帶工具、情境保留設定時決定的模式 →
同一個情境交給真的 ToolExecutor。

- safe→Auto：safe 類照跑；終端與高風險工具直接拒絕，審查員（假 Completer）沒被呼叫。
- full→YOLO：非地板指令會跑（陽性對照），地板指令仍只有人能核准，無人在場拒絕。
- 看不懂的值→Manual：要人核准的動作無人在場拒絕。
- session 存的模式（家對話共用人類的 AuthSwitcher）不套到心跳；靈魂的 enabled_tools 照樣收斂。

全部 sandbox／mock；審查員只用假的 Completer，不打 API（禁令 61）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.middleware.layers.monitor import MonitorLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.safety import AuthPoliciesConfig
from src.models.souls import SoulConfig
from src.models.user import User
from src.routers import heartbeat as hb
from src.services import auto_reviewer as reviewer_module
from src.services import tool_executor as executor_module
from src.services.approval_service import approval_service
from src.services.auth_service import AuthService
from src.services.auto_reviewer import AutoReviewer
from src.services.completion import CompletionResult
from src.services.permission_service import permission_service
from src.services.review.budget import _reset_review_ledger
from src.services.soul_service import SoulService
from src.services.tool_executor import ToolExecutor
from src.utils.async_db import SessionSnapshot
from src.utils.permission_grants import NameGrant

_SOUL = AgentIdentity.for_role(
    agent_id="Alice", role="Owner", is_human=False, capabilities=["heartbeat_awakening"]
)
_HB_VALUE = "test_heartbeat_token_for_ci"
_SESSION = "hb-mode-session"
_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "auth_policies.json.example"
)
_POLICY = AuthPoliciesConfig.model_validate(json.loads(_EXAMPLE.read_text(encoding="utf-8")))
_REVIEWER_CONFIG = {"model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0}
_WAIT_S = 5.0


class _FakeCompleter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(
        self, identity, purpose, model, system, user, max_tokens, timeout_s, response_schema=None
    ) -> CompletionResult:
        self.calls.append(user)
        return CompletionResult(
            text='{"verdict": "allow", "reason": "ok"}',
            usage={},
            cost_usd=0.0,
            stop_reason="end",
            error=None,
        )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"ok": True})


async def _no_chunks():
    for _ in ():
        yield ""


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    approval_service.reset_for_test()
    _reset_review_ledger()
    monkeypatch.setattr(reviewer_module.audit_service, "log_decision", lambda *_a: None)
    monkeypatch.setattr(permission_service, "evaluate_policy", _POLICY.evaluate)
    monkeypatch.setattr(
        permission_service, "get_allowed_tools", lambda _i: NameGrant.from_lists(["*"])
    )
    yield
    approval_service.reset_for_test()
    _reset_review_ledger()


def _awaken(auth_client, monkeypatch, security_level: str) -> Any:
    """真的打 /awaken，回傳交給背景迴圈的情境。"""
    monkeypatch.setattr(hb, "HEARTBEAT_TOKEN", _HB_VALUE)
    loop_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(hb, "_process_heartbeat_loop", loop_spy)
    monkeypatch.setattr(hb.identity_service, "get_identity", lambda _aid: _SOUL)
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _x: True)
    owner = User(
        id="owner_id",
        email="owner@test.com",
        hashed_password="...",
        owned_agents=["Alice"],
        refresh_token_exp=9999999999,
    )
    monkeypatch.setattr(AuthService, "get_all_users", lambda _self: [owner])
    monkeypatch.setattr(
        hb.session_service, "get_session_by_id", AsyncMock(return_value={"messages": {}})
    )
    monkeypatch.setattr(hb.session_service, "append_message_to_session", AsyncMock())
    resp = auth_client.post(
        "/v1/heartbeat/awaken",
        json={
            "token": _HB_VALUE,
            "agent_id": "Alice",
            "session_id": _SESSION,
            "security_level": security_level,
            "model_override": "fake-brain",
        },
    )
    assert resp.status_code == 202
    return loop_spy.await_args.args[3]


async def _pulse(
    monkeypatch, ctx: Any, *, stored_mode: str, enabled_tools: list[str] | None = None
) -> dict[str, Any]:
    """跑一次真的脈衝（ToolLayer 照跑），回傳 dispatch_request 收到的引數。"""
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    config = SoulConfig(enabled_tools=enabled_tools or []).model_dump()
    soul = object.__new__(SoulService)  # 不污染 singleton
    soul.get_soul_data = lambda _identity: ("靈魂", config)
    session = {
        "id": _SESSION,
        "title": "t",
        "messages": [{"role": "user", "content": "hi"}],
        "auth_level": stored_mode,  # 人類在家對話選的模式，不得套到心跳
        "active_mcp_servers": [],
    }
    monkeypatch.setattr(hb.session_service, "get_session_by_id", AsyncMock(return_value=session))
    monkeypatch.setattr(
        hb.session_service,
        "read_execution_snapshot",
        AsyncMock(return_value=SessionSnapshot(session=session, revision="hb-mode-revision")),
    )
    monkeypatch.setattr(hb, "dispatch_request", _dispatch)
    monkeypatch.setattr(hb, "autonomy_service", MagicMock())
    monkeypatch.setattr(hb, "rhythm_service", MagicMock())
    monkeypatch.setattr(hb, "soul_service", soul)
    await hb._execute_single_pulse(_SESSION, _SOUL, "fake-brain", ctx, 1, 0.0, "")
    return captured


def _tool_names(request: Any) -> set[str]:
    return {(t.get("function") or {}).get("name") for t in request.tools or []}


def _executor(monkeypatch, registry: dict[str, Any]) -> tuple[ToolExecutor, _FakeCompleter]:
    completer = _FakeCompleter()
    reviewer = AutoReviewer(completer, config_loader=lambda: _REVIEWER_CONFIG)
    monkeypatch.setattr(executor_module, "auto_reviewer", reviewer)
    bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
    for name, handler in registry.items():
        if name in bindings:
            bindings[name] = replace(bindings[name], handler=handler)
    monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    executor = ToolExecutor(
        protocol_manager=None, tool_registry=registry, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )
    return executor, completer


async def _run(executor: ToolExecutor, name: str, args: dict[str, Any], ctx: Any) -> dict:
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(_WAIT_S):
        result = await executor.execute_to_queue(name, args, "req-hb", ctx, queue)
    assert approval_service.pending_count() == 0  # 心跳不跳卡
    return json.loads(result)


# ── safe → Auto：safe 類照跑；終端與高風險工具拒絕、不送審 ───────────────────────


async def test_safe_heartbeat_runs_safe_tools_and_refuses_terminal_without_review(
    auth_client, monkeypatch
):
    ctx = _awaken(auth_client, monkeypatch, "safe")
    sent = await _pulse(monkeypatch, ctx, stored_mode="YOLO")

    context = sent["context"]
    assert context.auth_level == "Auto"  # 設定時決定，不採 session 的 YOLO
    assert context["heartbeat_run"] is True
    assert context["is_toukasoul"] is True
    names = _tool_names(sent["request"])
    assert {"write", "execute_command", "delete"} <= names  # KF-4：心跳看得到工具
    assert "ask_user" not in names  # 無人可問

    write, terminal, delete = _Recorder(), _Recorder(), _Recorder()
    executor, completer = _executor(
        monkeypatch, {"write": write, "execute_command": terminal, "delete": delete}
    )
    await _run(executor, "write", {"scope": "vault", "path": "notes/a.md", "content": "x"}, context)
    refused_terminal = await _run(executor, "execute_command", {"command": "pytest -q"}, context)
    refused_delete = await _run(executor, "delete", {"scope": "vault", "path": "a.md"}, context)

    assert len(write.calls) == 1
    assert terminal.calls == []
    assert delete.calls == []
    assert refused_terminal["reason"] == "auto_review_headless_terminal"
    assert refused_delete["reason"] == "auto_review_escalated"
    assert completer.calls == []  # 審查員沒被呼叫


# ── full → YOLO：非地板指令會跑，地板指令仍拒絕 ─────────────────────────────────


@pytest.mark.parametrize("command", ["docker compose down", "cat .env"])
async def test_full_heartbeat_still_cannot_run_floor_commands(auth_client, monkeypatch, command):
    ctx = _awaken(auth_client, monkeypatch, "full")
    sent = await _pulse(monkeypatch, ctx, stored_mode="Manual")
    context = sent["context"]
    assert context.auth_level == "YOLO"  # 不被 session 的 Manual 降級

    terminal = _Recorder()
    executor, completer = _executor(monkeypatch, {"execute_command": terminal})
    await _run(executor, "execute_command", {"command": "pytest -q"}, context)
    assert len(terminal.calls) == 1  # 陽性對照：YOLO 真的生效

    refused = await _run(executor, "execute_command", {"command": command}, context)
    assert len(terminal.calls) == 1
    assert refused["reason"] == "human_only"
    assert completer.calls == []


# ── 看不懂的值 → Manual ─────────────────────────────────────────────────────────


async def test_unknown_level_heartbeat_runs_as_manual(auth_client, monkeypatch):
    ctx = _awaken(auth_client, monkeypatch, "Safe")  # 大小寫不對也算看不懂
    sent = await _pulse(monkeypatch, ctx, stored_mode="YOLO")
    context = sent["context"]
    assert context.auth_level == "Manual"
    assert "write" in _tool_names(sent["request"])

    write = _Recorder()
    executor, _completer = _executor(monkeypatch, {"write": write})
    refused = await _run(
        executor, "write", {"scope": "vault", "path": "notes/a.md", "content": "x"}, context
    )
    assert write.calls == []
    assert refused["reason"] == "approval_required"


# ── 不跑 MonitorLayer；靈魂的 enabled_tools 照樣收斂 ─────────────────────────────


async def test_pulse_does_not_run_monitor_layer(auth_client, monkeypatch):
    ctx = _awaken(auth_client, monkeypatch, "safe")
    monitor = AsyncMock()
    monkeypatch.setattr(MonitorLayer, "process", monitor)
    await _pulse(monkeypatch, ctx, stored_mode="Auto")
    monitor.assert_not_called()


async def test_pulse_keeps_session_and_intent_set_at_awaken(auth_client, monkeypatch):
    """MonitorLayer 會把 session 換成請求上的值、用對話訊息覆寫原意。"""
    ctx = _awaken(auth_client, monkeypatch, "safe")
    intent = ctx["user_intent"]
    context = (await _pulse(monkeypatch, ctx, stored_mode="Auto"))["context"]
    assert context["session_id"] == _SESSION
    assert context["user_intent"] is intent
    assert context["user_intent"].source == "heartbeat_init_prompt"


async def test_pulse_honours_soul_enabled_tools(auth_client, monkeypatch):
    ctx = _awaken(auth_client, monkeypatch, "safe")
    sent = await _pulse(monkeypatch, ctx, stored_mode="Auto", enabled_tools=["write"])
    names = _tool_names(sent["request"])
    assert "write" in names
    assert "execute_command" not in names
    assert "delete" not in names


async def test_pulse_is_headless_and_keeps_preset_mode_even_without_awaken_flag(monkeypatch):
    """脈衝自己保證無人在場：呼叫端漏設 heartbeat_run，也不改採 session 存的 YOLO。"""
    ctx = PipelineContext(auth_level="Auto", identity=_SOUL)
    context = (await _pulse(monkeypatch, ctx, stored_mode="YOLO"))["context"]
    assert context["heartbeat_run"] is True
    assert context.auth_level == "Auto"

"""工具與審批的 hook 事件（ADR-0292）.

- PostToolUseFailure：工具開始跑之後失敗才觸發；補充說明當資料接在結果後面；錯誤內容先遮罩。
  權限拒絕與執行前驗證不觸發。
- PermissionRequest：審批卡跳出之前；只認 ``decision.behavior: deny``（不跳卡、不執行）；``allow``、
  ``updatedInput`` 一律不採用，審批卡照樣跳。
- PermissionDenied：工具呼叫被拒時；內容只放固定的原因代碼；在背景執行、不能改結果。
- Notification：跳審批卡、代理人問問題、MCP 要資料、隔離層級改變；只通知。
- 背景入口有同時執行上限，超過就跳過並記一筆執行紀錄。
- 工具缺必要參數：回 ``validation_error``，不進 ``tool_start``、不算工具失敗。

全部 sandbox：設定檔在 tmp_path，hook 一律用假的 http runner，不連網路、不讀 data/。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from mcp import types
from src.models.policy import PolicyDecision
from src.protocols.manager import McpToolCatalogRefusal
from src.protocols.mcp.canonical import McpCompleteOutcome, McpRequestOrigin
from src.protocols.mcp.interaction import (
    InProcessMcpInteractionBroker,
    McpInteractionSink,
    _current_sink,
    bind_mcp_interaction,
)
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.services import approval_flow, tool_errors
from src.services.approval_service import ApprovalService, approval_service
from src.services.hooks import background, notifications, stop_hooks, tool_hooks
from src.services.hooks import stopping as stopping_module
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import builtin_tools_schema, tool_registry
from src.tools.builtin.interaction import ask_user
from src.tools.builtin.schemas import missing_required_error
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES
from src.tools.file_verbs import FILE_VERB_SCHEMAS, INCOGNITO_FILE_VERB_SCHEMAS

from tests.support.hook_wiring import (
    REVIEW_DENY,
    Recorder,
    cards,
    clean_approval_state,
    executor_with,
    group,
    install_hooks,
    run_tool,
    until,
    use_reviewer,
)

if TYPE_CHECKING:
    from tests.support.hook_wiring import HookSetup

_READ = {"scope": "personal", "path": "a.txt"}
_WRITE = {"scope": "personal", "path": "a.txt", "content": "x"}
_BASE_KEYS = {"hook_event_name", "session_id", "agent_id", "incognito", "permission_mode"}
_FAILURE = tool_errors.build_tool_error(tool_errors.UPSTREAM_ERROR, "boom")


@pytest.fixture
def hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookSetup:
    return install_hooks(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    yield from clean_approval_state(monkeypatch)


def _specific(event: str, **fields: Any) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}})


def _decide(monkeypatch: pytest.MonkeyPatch, effect: str, reason: str = "test") -> None:
    monkeypatch.setattr(
        permission_service, "evaluate_policy",
        lambda _ctx: PolicyDecision(effect=effect, reason=reason),
    )


def _forbid_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """沒有 hook 時不組事件內容：一組就炸。"""

    def boom(_ctx: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError

    monkeypatch.setattr(tool_hooks, "base_payload", boom)
    monkeypatch.setattr(notifications, "base_payload", boom)


class _Raises:
    def __init__(self, message: str) -> None:
        self.message = message

    async def __call__(self, **_kwargs: Any) -> str:
        raise RuntimeError(self.message)


class _FakeMcp:
    """假的 MCP：工具在 ``srv`` 上；``outcome`` 是例外就丟，否則回傳。"""

    def __init__(self, outcome: Any = None, during_call: Any = None) -> None:
        self.registry = SimpleNamespace(
            get_server=lambda _name: SimpleNamespace(vault_jail_arg=None)
        )
        self.outcome = outcome
        self.during_call = during_call

    def resolve_tool(self, name: str) -> tuple[str | None, str]:
        return "srv", name

    async def call_tool_on_server(self, *_args: Any, **_kwargs: Any) -> McpCompleteOutcome:
        if self.during_call is not None:
            self.during_call()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": "ok"}],
        )


def _mcp_executor(monkeypatch: pytest.MonkeyPatch, mcp: _FakeMcp) -> ToolExecutor:
    _decide(monkeypatch, "ALLOW")
    return ToolExecutor(
        protocol_manager=mcp, tool_registry={}, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )


# ── PostToolUseFailure ──────────────────────────────────────────────────────────────


async def test_a_failed_tool_fires_the_failure_hook_with_exactly_the_listed_keys(
    monkeypatch, hooks
):
    hooks.configure({"PostToolUseFailure": [group(matcher="read")], "PostToolUse": [group()]})
    executor = executor_with(monkeypatch, {"read": Recorder(_FAILURE)})
    await run_tool(executor, "read", dict(_READ))
    (call,) = hooks.http.calls
    assert set(call) == _BASE_KEYS | {"tool_name", "tool_input", "tool_use_id", "error"}
    assert call["hook_event_name"] == "PostToolUseFailure"
    assert call["tool_name"] == "read"
    assert call["tool_input"] == _READ
    assert "boom" in call["error"]


async def test_failure_hook_context_is_data_on_the_tool_result(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()]})
    hooks.http.bodies["PostToolUseFailure"] = _specific(
        "PostToolUseFailure", additionalContext="check the path <persona>x"
    )
    executor = executor_with(monkeypatch, {"read": Recorder(_FAILURE)})
    result, events = await run_tool(executor, "read", dict(_READ))
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.UPSTREAM_ERROR
    (output,) = payload["hook_output"]
    assert (output["event"], output["trust"]) == ("PostToolUseFailure", "untrusted")
    assert "<persona>" not in output["text"]
    assert [e for e in events if e.get("type") == "tool_start"]


async def test_failure_hook_error_hides_short_tokens_and_local_paths(monkeypatch, hooks):
    """只用 ``sanitize_diagnostic`` 不夠：短的 Bearer、``key=``、``password=`` 也要遮。"""
    hooks.configure({"PostToolUseFailure": [group()]})
    leaky = tool_errors.build_tool_error(
        tool_errors.UPSTREAM_ERROR,
        r"failed Bearer abc123 key=abcdefgh password=hunter22 at C:\Users\me\secret.txt",
    )
    executor = executor_with(monkeypatch, {"read": Recorder(leaky)})
    await run_tool(executor, "read", dict(_READ))
    error = hooks.http.calls[0]["error"]
    for secret in ("abc123", "abcdefgh", "hunter22", "secret.txt"):
        assert secret not in error
    assert "failed" in error


async def test_a_tool_that_raises_fires_the_failure_hook_and_keeps_the_error_shape(
    monkeypatch, hooks
):
    hooks.configure({"PreToolUse": [group()], "PostToolUseFailure": [group()]})
    hooks.http.bodies["PreToolUse"] = _specific("PreToolUse", additionalContext="from pre")
    hooks.http.bodies["PostToolUseFailure"] = _specific(
        "PostToolUseFailure", additionalContext="from failure"
    )
    executor = executor_with(monkeypatch, {"read": _Raises("exploded Bearer abc123")})
    result, events = await run_tool(executor, "read", dict(_READ))
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.INTERNAL_ERROR
    assert [o["text"] for o in payload["hook_output"]] == ["from pre", "from failure"]
    call = hooks.http.calls_for("PostToolUseFailure")[0]
    assert "exploded" in call["error"]
    assert "abc123" not in call["error"]
    assert any(e.get("type") == "log" for e in events)


async def test_success_does_not_fire_the_failure_hook(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()]})
    executor = executor_with(monkeypatch, {"read": Recorder('{"ok": true}')})
    result, _events = await run_tool(executor, "read", dict(_READ))
    assert hooks.http.calls == []
    assert result.startswith('{"ok": true}')


async def test_the_failure_hook_matcher_looks_at_the_tool_name_and_its_new_names(
    monkeypatch, hooks
):
    hooks.configure({"PostToolUseFailure": [group(matcher="write")]})
    executor = executor_with(
        monkeypatch, {"read": Recorder(_FAILURE), "vault_tools": Recorder(_FAILURE)}
    )
    await run_tool(executor, "read", dict(_READ))
    assert hooks.http.calls == []
    await run_tool(executor, "vault_tools", {"action": "write", "vault": "personal"})
    (call,) = hooks.http.calls
    assert call["tool_name"] == "vault_tools"
    assert call["tool_aliases"] == ["write"]


async def test_permission_errors_from_the_tool_itself_are_not_failures(monkeypatch, hooks):
    """工具自帶審批或路徑拒絕回 ``permission_denied``：照 Claude Code，權限拒絕不觸發。"""
    hooks.configure({"PostToolUseFailure": [group()]})
    refused = tool_errors.build_tool_error(tool_errors.PERMISSION_DENIED, "no")
    executor = executor_with(monkeypatch, {"read": Recorder(refused)})
    await run_tool(executor, "read", dict(_READ))
    assert hooks.http.calls == []


async def test_arguments_rejected_before_the_tool_runs_are_not_failures(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()]})
    _decide(monkeypatch, "ALLOW")
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"vault_tools": recorder, "research": recorder})
    _decide(monkeypatch, "ALLOW")
    empty, _events = await run_tool(executor, "vault_tools", {})
    assert json.loads(empty)["error_class"] == tool_errors.VALIDATION_ERROR
    contract, _events = await run_tool(executor, "research", {"bad": 1})
    assert json.loads(contract)["error_class"] == tool_errors.VALIDATION_ERROR
    assert recorder.calls == []
    assert hooks.http.calls == []


async def test_an_mcp_error_result_is_a_failure_but_a_refused_call_is_not(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group(matcher="mcp__srv__.*")]})
    error_result = McpCompleteOutcome(
        source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
        content=[{"type": "text", "text": "upstream says no"}],
        is_error=True,
    )
    executor = _mcp_executor(monkeypatch, _FakeMcp(error_result))
    await run_tool(executor, "demo", {})
    (call,) = hooks.http.calls
    assert call["mcp_tool_name"] == "mcp__srv__demo"
    assert "upstream says no" in call["error"]

    refused = McpToolCatalogRefusal("srv", "demo", quarantined=False)
    executor = _mcp_executor(monkeypatch, _FakeMcp(refused))
    result, _events = await run_tool(executor, "demo", {})
    assert json.loads(result)["error_class"] == tool_errors.RESOURCE_NOT_FOUND
    assert len(hooks.http.calls) == 1


async def test_without_failure_hooks_nothing_is_built_and_the_result_is_unchanged(
    monkeypatch, hooks
):
    _forbid_payloads(monkeypatch)
    executor = executor_with(monkeypatch, {"read": Recorder(_FAILURE)})
    result, _events = await run_tool(executor, "read", dict(_READ))
    assert result == _FAILURE


async def test_a_broken_hook_service_does_not_change_a_failed_tool(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()]})

    def broken(_event: str) -> bool:
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks.service, "has_hooks", broken)
    executor = executor_with(monkeypatch, {"read": Recorder(_FAILURE)})
    result, _events = await run_tool(executor, "read", dict(_READ))
    assert result == _FAILURE


async def test_incognito_failure_records_only_time_event_and_result(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()]})
    executor = executor_with(monkeypatch, {"read": Recorder(_FAILURE)})
    await run_tool(executor, "read", dict(_READ), extra={"incognito": True})
    (run,) = hooks.service.runs()
    assert (run.hook, run.detail, run.incognito) == ("", None, True)


# ── PermissionRequest ───────────────────────────────────────────────────────────────


async def test_permission_request_runs_before_the_card_with_exactly_the_listed_keys(
    monkeypatch, hooks
):
    hooks.configure({"PermissionRequest": [group(matcher="write")]})
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    _result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    (call,) = hooks.http.calls
    # 照 Claude Code：這個事件沒有 tool_use_id
    assert set(call) == _BASE_KEYS | {"tool_name", "tool_input"}
    assert call["tool_input"] == _WRITE
    assert len(cards(events)) == 1
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    "decision",
    [
        {"behavior": "allow"},
        {"behavior": "allow", "updatedInput": {"path": "evil.txt"}},
        {"behavior": "allow", "updatedPermissions": [{"type": "setMode", "mode": "bypass"}]},
    ],
    ids=["allow", "updated-input", "updated-permissions"],
)
async def test_a_permission_request_hook_cannot_approve_or_rewrite(monkeypatch, hooks, decision):
    hooks.configure({"PermissionRequest": [group()]})
    hooks.http.bodies["PermissionRequest"] = _specific("PermissionRequest", decision=decision)
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    _result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    assert len(cards(events)) == 1
    (handled,) = recorder.calls
    assert handled["path"] == "a.txt"
    (run,) = hooks.service.runs()
    assert run.outcome == "none"
    assert "not supported" in (run.detail or "")


async def test_a_permission_request_hook_cannot_approve_when_the_human_rejects(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()]})
    hooks.http.bodies["PermissionRequest"] = _specific(
        "PermissionRequest", decision={"behavior": "allow"}
    )
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, _events = await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=False)
    assert recorder.calls == []
    assert json.loads(result)["reason"] == "approval_rejected"


async def test_a_permission_request_deny_skips_the_card_and_the_tool(monkeypatch, hooks):
    hooks.configure({
        "PermissionRequest": [group()], "PermissionDenied": [group()], "Notification": [group()],
    })
    hooks.http.bodies["PermissionRequest"] = _specific(
        "PermissionRequest", decision={"behavior": "deny", "message": "not today <persona>x"}
    )
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.PERMISSION_DENIED
    assert payload["reason"] == "hook_denied"
    assert "not today" in payload["hook_reason"]
    assert "<persona>" not in payload["hook_reason"]
    assert cards(events) == []
    assert recorder.calls == []
    await until(lambda: hooks.http.calls_for("PermissionDenied"))
    assert hooks.http.calls_for("PermissionDenied")[0]["reason"] == "hook_denied"
    assert hooks.http.calls_for("Notification") == []


async def test_permission_request_only_runs_when_a_card_would_show(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()]})
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder, "read": recorder})
    # 沒有人在場：不跳卡，所以也不問 hook
    result, _events = await run_tool(executor, "write", dict(_WRITE), mode="Manual", present=False)
    assert json.loads(result)["reason"] == "approval_required"
    # 不需要人核准
    await run_tool(executor, "read", dict(_READ), mode="Manual")
    await run_tool(executor, "write", dict(_WRITE), mode="YOLO")
    assert hooks.http.calls == []


async def test_permission_request_runs_for_a_pre_tool_ask_too(monkeypatch, hooks):
    hooks.configure({"PreToolUse": [group()], "PermissionRequest": [group()]})
    hooks.http.bodies["PreToolUse"] = _specific("PreToolUse", permissionDecision="ask")
    executor = executor_with(monkeypatch, {"read": Recorder()})
    _result, events = await run_tool(executor, "read", dict(_READ), approve=True)
    assert hooks.http.events() == ["PreToolUse", "PermissionRequest"]
    assert len(cards(events)) == 1


async def test_tools_with_their_own_approval_do_not_fire_permission_request(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()]})
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"request_skill_install": recorder})
    _decide(monkeypatch, "CONFIRM")
    await run_tool(executor, "request_skill_install", {"skill_id": "x"}, mode="Manual")
    assert len(recorder.calls) == 1
    assert hooks.http.calls == []


async def test_a_failed_permission_request_hook_still_shows_the_card(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()]})
    hooks.http.failing.add("PermissionRequest")
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    _result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    assert len(cards(events)) == 1
    assert len(recorder.calls) == 1
    assert hooks.service.runs()[0].outcome == "failed"


def _stopped(monkeypatch: pytest.MonkeyPatch, session_id: str) -> None:
    monkeypatch.setattr(stopping_module, "rhythm_service", SimpleNamespace(
        is_interrupted=lambda sid: sid == session_id, stop_count=lambda _sid: 0,
    ))


async def test_a_stop_during_permission_request_keeps_the_card_and_tool_away(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()], "PermissionDenied": [group()]})
    _stopped(monkeypatch, "s-hook")
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, events = await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    assert json.loads(result)["reason"] == "stopped_by_user"
    assert cards(events) == []
    assert recorder.calls == []
    await asyncio.sleep(0.01)
    assert hooks.http.calls_for("PermissionDenied") == []


async def test_a_permission_request_deny_wins_over_a_stop(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()]})
    hooks.http.bodies["PermissionRequest"] = _specific(
        "PermissionRequest", decision={"behavior": "deny"}
    )
    _stopped(monkeypatch, "s-hook")
    executor = executor_with(monkeypatch, {"write": Recorder()})
    result, _events = await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    assert json.loads(result)["reason"] == "hook_denied"


# ── PermissionDenied ────────────────────────────────────────────────────────────────


async def _denied_call(hooks: HookSetup) -> dict[str, Any]:
    await until(lambda: hooks.http.calls_for("PermissionDenied"))
    (call,) = hooks.http.calls_for("PermissionDenied")
    assert set(call) - {"mcp_tool_name"} == _BASE_KEYS | {
        "tool_name", "tool_input", "tool_use_id", "reason",
    }
    return call


async def test_policy_denial_sends_a_fixed_code_not_the_rule_text(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group(matcher="write")]})
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    _decide(monkeypatch, "DENY", reason="matched secret-rule-text")
    result, _events = await run_tool(executor, "write", dict(_WRITE))
    call = await _denied_call(hooks)
    assert call["reason"] == "policy"
    assert "secret-rule-text" not in json.dumps(call)
    assert call["tool_input"] == _WRITE
    assert recorder.calls == []
    assert json.loads(result)["error_class"] == tool_errors.PERMISSION_DENIED


async def test_mcp_role_denial_is_reported(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    executor = _mcp_executor(monkeypatch, _FakeMcp())
    monkeypatch.setattr(permission_service, "is_mcp_tool_allowed", lambda *_a: False)
    await run_tool(executor, "demo", {})
    assert (await _denied_call(hooks))["reason"] == "mcp_tool_rbac"


@pytest.mark.parametrize(
    ("effect", "code"),
    [("HUMAN_ONLY", "human_only"), ("CONFIRM", "approval_required")],
)
async def test_no_human_present_denials_are_reported(monkeypatch, hooks, effect, code):
    hooks.configure({"PermissionDenied": [group()]})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    _decide(monkeypatch, effect)
    await run_tool(executor, "write", dict(_WRITE), present=False)
    assert (await _denied_call(hooks))["reason"] == code


async def test_human_rejection_is_reported(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=False)
    assert (await _denied_call(hooks))["reason"] == "approval_rejected"


@pytest.mark.parametrize("verdict", ["timeout", "queue_full"])
async def test_unanswered_cards_are_reported(monkeypatch, hooks, verdict):
    hooks.configure({"PermissionDenied": [group()]})
    executor = executor_with(monkeypatch, {"write": Recorder()})

    async def answer(**_kwargs: Any) -> str:
        return verdict

    monkeypatch.setattr(executor, "_await_human_approval", answer)
    await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    assert (await _denied_call(hooks))["reason"] == f"approval_{verdict}"


async def test_reviewer_denial_is_reported(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    use_reviewer(monkeypatch, REVIEW_DENY)
    executor = executor_with(monkeypatch, {"execute_command": Recorder()})
    await run_tool(executor, "execute_command", {"command": "pytest -q"})
    assert (await _denied_call(hooks))["reason"] == "auto_review_denied"


async def test_a_pre_tool_ask_with_nobody_there_is_reported(monkeypatch, hooks):
    hooks.configure({"PreToolUse": [group()], "PermissionDenied": [group()]})
    hooks.http.bodies["PreToolUse"] = _specific("PreToolUse", permissionDecision="ask")
    executor = executor_with(monkeypatch, {"read": Recorder()})
    await run_tool(executor, "read", dict(_READ), present=False)
    assert (await _denied_call(hooks))["reason"] == "hook_requires_human"


async def test_a_pre_tool_block_is_not_a_permission_denied(monkeypatch, hooks):
    """照 Claude Code：PreToolUse 擋下不算。"""
    hooks.configure({"PreToolUse": [group()], "PermissionDenied": [group()]})
    hooks.http.bodies["PreToolUse"] = _specific("PreToolUse", permissionDecision="deny")
    executor = executor_with(monkeypatch, {"read": Recorder()})
    result, _events = await run_tool(executor, "read", dict(_READ))
    assert json.loads(result)["reason"] == "hook_denied"
    await asyncio.sleep(0.01)
    assert hooks.http.events() == ["PreToolUse"]


async def test_role_denials_before_the_policy_are_not_reported(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    executor = executor_with(monkeypatch, {"read": Recorder()})
    monkeypatch.setattr(
        permission_service, "resolve_grants",
        lambda _role: SimpleNamespace(tools=SimpleNamespace(allows=lambda _name: False)),
    )
    result, _events = await run_tool(executor, "read", dict(_READ))
    assert json.loads(result)["reason"] == "native_tool_rbac"
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


async def test_permission_denied_output_changes_nothing(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    hooks.http.bodies["PermissionDenied"] = json.dumps({
        "retry": True, "decision": "block", "continue": False,
        "hookSpecificOutput": {"hookEventName": "PermissionDenied", "additionalContext": "x"},
    })
    executor = executor_with(monkeypatch, {"write": Recorder()})
    _decide(monkeypatch, "CONFIRM")
    result, _events = await run_tool(executor, "write", dict(_WRITE), present=False)
    await _denied_call(hooks)
    await until(hooks.service.runs)
    assert "hook_output" not in result
    assert hooks.service.runs()[0].outcome == "none"


async def test_permission_denied_does_not_hold_up_the_result(monkeypatch, hooks):
    hooks.configure({"PermissionDenied": [group()]})
    gate = asyncio.Event()
    hooks.http.gates["PermissionDenied"] = gate
    executor = executor_with(monkeypatch, {"write": Recorder()})
    _decide(monkeypatch, "DENY")
    result, _events = await run_tool(executor, "write", dict(_WRITE))
    assert json.loads(result)["error_class"] == tool_errors.PERMISSION_DENIED
    await until(lambda: hooks.http.calls)
    assert hooks.service.runs() == []
    gate.set()
    await until(hooks.service.runs)


async def test_without_denied_hooks_nothing_is_built(monkeypatch, hooks):
    _forbid_payloads(monkeypatch)
    executor = executor_with(monkeypatch, {"write": Recorder()})
    _decide(monkeypatch, "DENY")
    result, _events = await run_tool(executor, "write", dict(_WRITE))
    assert json.loads(result)["error_class"] == tool_errors.PERMISSION_DENIED


# ── Notification ────────────────────────────────────────────────────────────────────


def _notification_keys() -> set[str]:
    return _BASE_KEYS | {"notification_type", "message"}


async def test_the_card_sends_a_notification_after_it_shows(monkeypatch, hooks):
    hooks.configure({"PermissionRequest": [group()], "Notification": [group()]})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    await until(lambda: hooks.http.calls_for("Notification"))
    assert hooks.http.events() == ["PermissionRequest", "Notification"]
    (call,) = hooks.http.calls_for("Notification")
    assert set(call) == _notification_keys()
    assert call["notification_type"] == "permission_prompt"
    assert "write" in call["message"]
    assert "a.txt" not in call["message"]


async def test_notification_matchers_look_at_the_type(monkeypatch, hooks):
    hooks.configure({"Notification": [group(matcher="agent_needs_input")]})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []
    hooks.configure({"Notification": [group(matcher="permission_prompt")]})
    await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    await until(lambda: hooks.http.calls)


async def test_a_full_approval_queue_sends_no_notification(monkeypatch, hooks):
    hooks.configure({"Notification": [group()], "PermissionDenied": [group()]})

    async def full(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(approval_service, "register", full)
    executor = executor_with(monkeypatch, {"write": Recorder()})
    result, _events = await run_tool(executor, "write", dict(_WRITE), mode="Manual")
    assert json.loads(result)["reason"] == "approval_queue_full"
    await until(lambda: hooks.http.calls_for("PermissionDenied"))
    assert hooks.http.calls_for("Notification") == []


async def test_verification_command_cards_notify_but_do_not_ask_permission_hooks(
    monkeypatch, hooks
):
    """驗證指令的卡不是工具呼叫：不觸發 PermissionRequest，但有發 Notification。"""
    hooks.configure({"PermissionRequest": [group()], "Notification": [group()]})
    queue: asyncio.Queue = asyncio.Queue()
    approver = approval_flow.CommandApprover(
        emit=queue.put, parent_call_id="call-1", agent_id="Alice", session_id="s-hook",
        ctx={"session_id": "s-hook", "agent_id": "Alice"},
    )
    task = asyncio.create_task(approver("pytest -q"))
    await until(lambda: approval_service._pending)
    await approval_service.resolve(
        next(iter(approval_service._pending)), approved=False, resolver="tester"
    )
    await task
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert call["hook_event_name"] == "Notification"
    assert call["notification_type"] == "permission_prompt"
    assert "execute_command" in call["message"]
    assert "pytest" not in call["message"]


async def test_ask_user_sends_a_notification_without_the_questions(hooks):
    hooks.configure({"Notification": [group()]})
    queue: asyncio.Queue = asyncio.Queue()
    ctx = {"session_id": "s-hook", "agent_id": "Alice", "auth_level": "Manual"}
    await ask_user(
        questions=[{"question": "which-branch?"}], _emit_event=queue.put, _call_id="c1",
        _ctx=ctx, _timeout=0.01,
    )
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert set(call) == _notification_keys()
    assert call["notification_type"] == "agent_needs_input"
    assert "which-branch" not in json.dumps(call)


async def test_ask_user_without_a_question_or_a_channel_sends_nothing(hooks):
    hooks.configure({"Notification": [group()]})
    queue: asyncio.Queue = asyncio.Queue()
    await ask_user(questions=[], _emit_event=queue.put, _ctx={})
    await ask_user(questions=[{"question": "q"}], _emit_event=None, _ctx={})
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


def _elicitation() -> types.ElicitRequest:
    return types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message="Choose a value",
            requestedSchema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
    )


def _binding(origin: McpRequestOrigin = McpRequestOrigin.FOREGROUND) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(origin=origin))


def _sink(queue: asyncio.Queue, shown: list[str]) -> McpInteractionSink:
    return McpInteractionSink(
        queue=queue, call_id="c1", tool_name="srv__demo", agent_id="Alice", session_id="s-hook",
        on_shown=lambda: shown.append("shown"),
    )


async def test_mcp_input_calls_back_only_after_the_form_is_queued():
    service = ApprovalService()
    broker = InProcessMcpInteractionBroker(service)
    queue: asyncio.Queue = asyncio.Queue()
    shown: list[str] = []
    with bind_mcp_interaction(_sink(queue, shown)):
        task = asyncio.create_task(broker.respond("q", _elicitation(), _binding()))
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert shown == ["shown"]
        await service.resolve(event["id"], decision="declined", resolver="tester")
        result = await asyncio.wait_for(task, timeout=1)
    assert result.action == "decline"


async def test_mcp_input_that_never_shows_calls_nothing():
    service = ApprovalService()

    async def full(*_args: Any, **_kwargs: Any) -> bool:
        return False

    broker = InProcessMcpInteractionBroker(SimpleNamespace(register=full))
    shown: list[str] = []
    with bind_mcp_interaction(_sink(asyncio.Queue(), shown)):
        assert (await broker.respond("q", _elicitation(), _binding())).action == "decline"
    background_only = InProcessMcpInteractionBroker(service)
    with bind_mcp_interaction(_sink(asyncio.Queue(), shown)):
        headless = await background_only.respond(
            "q", _elicitation(), _binding(McpRequestOrigin.HEARTBEAT)
        )
    assert headless.action == "decline"
    assert shown == []


async def test_the_executor_binds_mcp_input_to_a_notification(monkeypatch, hooks):
    hooks.configure({"Notification": [group(matcher="elicitation_dialog")]})

    def show() -> None:
        sink = _current_sink.get()
        assert sink is not None
        assert sink.on_shown is not None
        sink.on_shown()

    executor = _mcp_executor(monkeypatch, _FakeMcp(during_call=show))
    await run_tool(executor, "demo", {})
    await until(lambda: hooks.http.calls)
    (call,) = hooks.http.calls
    assert set(call) == _notification_keys()
    assert "demo" in call["message"]


async def test_isolation_changes_notify_with_only_the_level_name(hooks):
    hooks.configure({"Notification": [group(matcher="isolation_level")]})
    notifier = notifications.IsolationLevelNotifier()
    await notifier(SimpleNamespace(level="executor", reason="secret-detail"))
    # 層級沒變（例如只是本機原因變了）不通知
    await notifier(SimpleNamespace(level="executor", reason="other"))
    await notifier(SimpleNamespace(level="none", reason="secret-detail"))
    await until(lambda: len(hooks.http.calls) == 2)
    assert [("executor" in c["message"], "none" in c["message"]) for c in hooks.http.calls] == [
        (True, False), (False, True),
    ]
    for call in hooks.http.calls:
        assert call["session_id"] is None
        assert "secret-detail" not in json.dumps(call)
        assert "permission_mode" not in call


def test_the_app_registers_the_isolation_notifier():
    main = Path(notifications.__file__).parents[2] / "main.py"
    assert "isolation_monitor.on_level_change(IsolationLevelNotifier())" in main.read_text(
        encoding="utf-8"
    )


async def test_notification_output_changes_nothing_and_incognito_records_little(
    monkeypatch, hooks
):
    hooks.configure({"Notification": [group()]})
    hooks.http.bodies["Notification"] = '{"decision": "block", "continue": false}'
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    await run_tool(
        executor, "write", dict(_WRITE), mode="Manual", approve=True, extra={"incognito": True}
    )
    await until(hooks.service.runs)
    assert len(recorder.calls) == 1
    (run,) = hooks.service.runs()
    assert (run.outcome, run.hook, run.detail, run.incognito) == ("none", "", None, True)


async def test_without_notification_hooks_nothing_is_built(monkeypatch, hooks):
    _forbid_payloads(monkeypatch)
    executor = executor_with(monkeypatch, {"write": Recorder()})
    await run_tool(executor, "write", dict(_WRITE), mode="Manual", approve=True)
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


# ── 背景入口 ────────────────────────────────────────────────────────────────────────


def _schedule(event: str = "Stop", **kwargs: Any) -> None:
    background.schedule_background_hook(event, lambda: {"session_id": "s-hook"}, **kwargs)


async def test_background_hooks_over_the_limit_are_skipped_and_recorded(monkeypatch, hooks):
    hooks.configure({"Stop": [group()]})
    monkeypatch.setattr(background, "MAX_BACKGROUND_HOOK_RUNS", 2)
    gate = asyncio.Event()
    hooks.http.gates["Stop"] = gate
    _schedule()
    _schedule()
    await until(lambda: len(hooks.http.calls) == 2)
    _schedule()
    (skipped,) = hooks.service.runs()
    assert (skipped.event, skipped.status, skipped.outcome) == ("Stop", "skipped", "failed")
    assert "skipped" in (skipped.detail or "")
    gate.set()
    await until(lambda: len(hooks.service.runs()) == 3)
    await until(lambda: not background.in_flight_count())
    assert len(hooks.http.calls) == 2
    # 跑完就空出名額
    _schedule()
    await until(lambda: len(hooks.http.calls) == 3)


async def test_a_skipped_incognito_hook_records_only_time_event_and_result(monkeypatch, hooks):
    hooks.configure({"Stop": [group()]})
    monkeypatch.setattr(background, "MAX_BACKGROUND_HOOK_RUNS", 0)
    _schedule(incognito=True)
    (skipped,) = hooks.service.runs()
    assert (skipped.hook, skipped.hook_type, skipped.detail, skipped.incognito) == (
        "", "", None, True,
    )


async def test_the_stop_event_uses_the_same_limit(monkeypatch, hooks):
    hooks.configure({"Stop": [group()]})
    monkeypatch.setattr(background, "MAX_BACKGROUND_HOOK_RUNS", 0)
    stop_hooks.schedule_stop_hooks({"session_id": "s-hook"}, lambda: "done")
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []
    assert [run.status for run in hooks.service.runs()] == ["skipped"]


async def test_events_without_a_matching_hook_build_nothing_and_record_nothing(
    monkeypatch, hooks
):
    hooks.configure({"Notification": [group(matcher="permission_prompt")]})
    monkeypatch.setattr(background, "MAX_BACKGROUND_HOOK_RUNS", 0)

    def must_not_build() -> dict[str, Any]:
        raise AssertionError

    background.schedule_background_hook(
        "Notification", must_not_build, match=("agent_needs_input",)
    )
    assert hooks.service.runs() == []


async def test_a_broken_hook_service_only_logs(monkeypatch, hooks):
    def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks.service, "has_matching_hooks", broken)
    _schedule()
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


async def test_a_payload_that_cannot_be_built_only_logs(hooks):
    hooks.configure({"Stop": [group()]})

    def broken() -> dict[str, Any]:
        raise RuntimeError("bug")

    background.schedule_background_hook("Stop", broken)
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


# ── 工具缺必要參數 ──────────────────────────────────────────────────────────────────


async def test_a_missing_required_argument_is_a_validation_error(monkeypatch, hooks):
    hooks.configure({"PostToolUseFailure": [group()], "PostToolUse": [group()]})
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    args = {"path": "secret-name.txt", "content": "x"}
    result, events = await run_tool(executor, "write", args, mode="YOLO")
    payload = json.loads(result)
    assert payload["error_class"] == tool_errors.VALIDATION_ERROR
    assert "scope" in payload["message"]
    assert payload["param"] == "scope"
    assert "secret-name" not in result  # 不回顯模型送的值
    assert recorder.calls == []
    assert not [e for e in events if e.get("type") == "tool_start"]
    await asyncio.sleep(0.01)
    assert hooks.http.calls == []


async def test_a_missing_argument_is_still_checked_after_the_card(monkeypatch, hooks):
    """順序照 ADR-0229：角色→政策→審批→參數驗證，所以卡還是可能先跳。"""
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, events = await run_tool(
        executor, "write", {"path": "a.txt", "content": "x"}, mode="Manual", approve=True
    )
    assert len(cards(events)) == 1
    assert json.loads(result)["error_class"] == tool_errors.VALIDATION_ERROR
    assert recorder.calls == []


async def test_complete_calls_and_tools_outside_the_list_are_not_checked(monkeypatch, hooks):
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder, "vault_tools": recorder})
    await run_tool(executor, "write", dict(_WRITE), mode="YOLO")
    # 舊工具 shim 不在廣告清單裡，缺什麼都照舊交給它
    await run_tool(executor, "vault_tools", {"action": "read"}, mode="YOLO")
    assert len(recorder.calls) == 2


async def test_plugin_and_memory_tools_with_the_same_name_are_not_checked(monkeypatch, hooks):
    plugin_calls: list[dict[str, Any]] = []

    async def plugin(**kwargs: Any) -> str:
        plugin_calls.append(kwargs)
        return "ok"

    memory = Recorder()
    _decide(monkeypatch, "ALLOW")
    monkeypatch.setattr(
        permission_service, "resolve_grants",
        lambda _role: SimpleNamespace(tools=SimpleNamespace(allows=lambda _name: True)),
    )
    executor = ToolExecutor(
        protocol_manager=None, tool_registry={}, memory_tool_registry={"write": memory},
        plugin_registry={"edit": plugin}, enforce_advertised=False,
    )
    await run_tool(executor, "write", {"path": "a"}, mode="YOLO")
    await run_tool(executor, "edit", {"path": "a"}, mode="YOLO")
    assert len(memory.calls) == 1
    assert len(plugin_calls) == 1


def test_missing_required_error_only_knows_the_advertised_tools():
    assert missing_required_error("write", {}) is not None
    assert missing_required_error("write", dict(_WRITE)) is None
    assert missing_required_error("vault_tools", {}) is None
    assert missing_required_error("no_such_tool", {}) is None


def _handler_params(handler: Any) -> tuple[set[str], set[str], bool]:
    signature = inspect.signature(handler)
    takes_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    names = {
        name for name, p in signature.parameters.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    }
    required = {
        name for name, p in signature.parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    }
    return names, required, takes_kwargs


def test_advertised_required_arguments_agree_with_the_handlers():
    """執行時只看 schema；這裡檢查兩邊有沒有對不上：schema 要的 handler 收得下，handler 非要不可的
    schema 也要列（可信 context 由執行器帶，不算）。"""
    trusted = EXECUTION_CONTEXT_KWARG_NAMES | {"identity"}
    problems = []
    for schema in builtin_tools_schema:
        name = schema["function"]["name"]
        handler = tool_registry.get(name)
        if handler is None:
            continue
        required = set(schema["function"]["parameters"].get("required", ()))
        names, needed, takes_kwargs = _handler_params(handler)
        if not takes_kwargs and required - names:
            problems.append(f"{name}: handler has no {sorted(required - names)}")
        if needed - required - trusted:
            extra = sorted(needed - required - trusted)
            problems.append(f"{name}: schema does not require {extra}")
    assert problems == []


def test_incognito_file_verbs_require_the_same_arguments():
    for schema in FILE_VERB_SCHEMAS:
        name = schema["function"]["name"]
        incognito = INCOGNITO_FILE_VERB_SCHEMAS.get(name, schema)
        assert incognito["function"]["parameters"].get("required") == schema["function"][
            "parameters"
        ].get("required")

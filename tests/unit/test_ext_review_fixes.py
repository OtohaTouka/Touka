"""外部審查報告的局部小修（11/24 第 102 項；第 1、3～7、9、10 項）。

- 第 1 項：無痕回合不讀任務看板、不放 ``<mission_awareness>``；任務工具照樣列出（ADR-0086）。
- 第 3 項：呼叫第三方 MCP 工具時，替 AI 補上的 ``agent_id`` 只放在送出去的複本，AI 自己的參數不變。
- 第 4 項：工具算不算成功，畫面和 hook 用同一套規則（ADR-0124 決定 2、ADR-0292）：結構化為準；
  沒結構的終端看結束碼，其他工具（含 MCP）看開頭是不是 ``Error:``／``❌``。
- 第 5 項：一定要參數的工具完全沒帶參數時，在 hook、審查員與審批卡之前就回錯誤（ADR-0229）。
- 第 6 項：兩個調度工具只經一般清單過權限，不再無條件多列一次。
- 第 7 項：SNS 回合（ctx 沒有身分）列工具用沒有角色的身分，和執行時同一套空權限（ADR-0276）。
- 第 9 項：沒人在場、要人核准的終端指令交給終端自己再擋一次（釘住現況）。
- 第 10 項：記憶工具的無痕判斷也看請求本身。

全部 sandbox：假政策、假工具、假 MCP、假 hook runner、假審查員；不跑子程序（禁令 66）、
不連外、不讀 data/。
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA_LIST
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers.tool import ToolLayer
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.permissions import PermissionsConfig, RoleConfig
from src.models.policy import PolicyDecision
from src.protocols.mcp.canonical import McpCompleteOutcome
from src.protocols.mcp.sdk_adapter import UNKNOWN_PROTOCOL_VERSION
from src.routers import terminal as terminal_router
from src.services import tool_errors
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.tools import terminal as terminal_tool
from src.utils.permission_grants import NameGrant
from src.utils.tool_events import TOOL_RESULT, TOOL_START
from src.utils.turn_notices import NOTICE_TOOL_FAILED, TURN_NOTICE_EVENT

from tests.support.hook_wiring import (
    SOUL,
    WAIT_S,
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
    from pathlib import Path

    from tests.support.hook_wiring import HookSetup

_MISSION_MGR = "memory_server.services.mission_manager.MissionManager.get_current_mission"
_OWNER = AgentIdentity.for_role(agent_id="ExtReviewOwner", role="Owner")
_DISPATCH_TOOLS = ("connect_mcp_server", "activate_skill")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    yield from clean_approval_state(monkeypatch)


@pytest.fixture
def hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookSetup:
    return install_hooks(tmp_path, monkeypatch)


# ── ToolLayer 共用 ───────────────────────────────────────────────────────────────


def _perm(deny: frozenset[str] = frozenset()) -> MagicMock:
    """全部工具都授權的 permission_service 替身；``deny`` 裡的工具政策回 DENY。"""
    perm = MagicMock()
    perm.get_allowed_tools.return_value = NameGrant.from_lists(["*"])
    perm.is_tool_allowed.side_effect = lambda name, grant: grant.allows(name)
    perm.is_mcp_server_allowed.return_value = True
    perm.evaluate_policy.side_effect = lambda pc: SimpleNamespace(
        effect="DENY" if pc.resource in deny else "ALLOW",
        reason="test-deny" if pc.resource in deny else None,
    )
    return perm


def _mission() -> SimpleNamespace:
    task = SimpleNamespace(
        status="DOING", id="t1", description="外人不該看到的任務", dependencies=[],
        artifact_ref=None,
    )
    return SimpleNamespace(status="DOING", goal="看板上的大目標", tasks=[task])


def _request(**extra: Any) -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="m", messages=[OpenAIMessage(role="user", content="hi")], **extra
    )


def _layer_ctx(identity: AgentIdentity | None = _OWNER, **extra: Any) -> PipelineContext:
    ctx = (
        PipelineContext(identity=identity, agent_id=identity.agent_id)
        if identity is not None
        else PipelineContext(agent_id="SnsProbeSoul")
    )
    ctx["headers"] = {}
    for key, value in extra.items():
        ctx[key] = value
    return ctx


def _names(request: OpenAIChatRequest) -> list[str]:
    return [(tool.get("function") or {}).get("name") for tool in request.tools or []]


def _system_text(request: OpenAIChatRequest) -> str:
    return "\n".join(
        m.content
        for m in request.messages
        if m.role == "system" and isinstance(m.content, str)
    )


# ── 第 1 項：無痕不自動帶任務看板 ───────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["ctx", "request"])
async def test_incognito_round_does_not_read_or_inject_the_mission_board(where: str) -> None:
    request = _request(incognito=where == "request")
    ctx = _layer_ctx(incognito=True) if where == "ctx" else _layer_ctx()
    read = AsyncMock(return_value=_mission())
    with patch("src.middleware.layers.tool.permission_service", _perm()), patch(
        _MISSION_MGR, new=read
    ):
        await ToolLayer().process(request, ctx)
    assert "<mission_awareness>" not in _system_text(request)
    assert "看板上的大目標" not in _system_text(request)
    read.assert_not_awaited()
    assert ctx.get("mission_awareness") is None
    # 無痕不停用工具（ADR-0086）：AI 自己用任務工具查看仍然可以
    assert "mission_tools" in _names(request)


async def test_normal_round_still_gets_the_mission_board() -> None:
    request = _request()
    with patch("src.middleware.layers.tool.permission_service", _perm()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=_mission())
    ):
        await ToolLayer().process(request, _layer_ctx())
    assert _system_text(request).count("<mission_awareness>") == 1
    assert "mission_tools" in _names(request)


# ── 第 10 項：記憶工具的無痕判斷也看請求本身 ──────────────────────────────────────────


@pytest.mark.parametrize("incognito", [True, False])
async def test_incognito_request_lists_no_memory_tools_even_when_ctx_lacks_the_flag(
    incognito: bool,
) -> None:
    request = _request(incognito=incognito)
    ctx = _layer_ctx(memory_tools_schema=MEMORY_TOOLS_SCHEMA_LIST)
    with patch("src.middleware.layers.tool.permission_service", _perm()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=None)
    ):
        await ToolLayer().process(request, ctx)
    assert ("memory_tools" in _names(request)) is (not incognito)


# ── 第 6 項：兩個調度工具只列一次 ──────────────────────────────────────────────────────


def _dispatcher_ctx() -> PipelineContext:
    return _layer_ctx(mcp_dispatcher=True, active_skills=["system-skill-dispatcher"])


async def test_dispatch_tools_denied_by_policy_are_not_listed() -> None:
    request = _request()
    perm = _perm(deny=frozenset(_DISPATCH_TOOLS))
    ctx = _dispatcher_ctx()
    with patch("src.middleware.layers.tool.permission_service", perm), patch(
        _MISSION_MGR, new=AsyncMock(return_value=None)
    ):
        await ToolLayer().process(request, ctx)
    for name in _DISPATCH_TOOLS:
        assert name not in _names(request)
        assert name not in ctx["advertised_tools"].native
    # 「哪些工具用不了」的說明照樣寫明
    assert {"connect_mcp_server", "activate_skill"} <= {
        entry["tool"] for entry in ctx["denied_tools"]
    }


async def test_allowed_dispatch_tools_are_listed_once_without_duplicate_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request()
    with patch("src.middleware.layers.tool.permission_service", _perm()), patch(
        _MISSION_MGR, new=AsyncMock(return_value=None)
    ), caplog.at_level(logging.WARNING, logger="src.middleware.layers.tool"):
        await ToolLayer().process(request, _dispatcher_ctx())
    for name in _DISPATCH_TOOLS:
        assert _names(request).count(name) == 1
    assert not [r for r in caplog.records if "duplicate tool name" in r.getMessage()]


# ── 第 7 項：SNS 回合列工具用無角色身分 ────────────────────────────────────────────────


@pytest.fixture
def base_grants_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理員把 Base 改成能用全部工具（出貨值是全拒）；其他角色照預設。"""
    roles = {"Base": RoleConfig(allowed_tools=["*"], desc="test: admin widened Base")}
    monkeypatch.setattr(permission_service, "roles_config", PermissionsConfig(roles=roles))
    monkeypatch.setattr(permission_service, "_auto_reload_check", lambda: None)


async def test_sns_round_lists_no_tools_even_when_base_is_widened(
    base_grants_everything: None,
) -> None:
    request = _request()
    ctx = _layer_ctx(None, sns_source=True, is_toukasoul=True)
    with patch(_MISSION_MGR, new=AsyncMock(return_value=None)):
        await ToolLayer().process(request, ctx)
    assert _names(request) == []
    assert ctx["advertised_tools"].native == frozenset()
    assert "Your permission role for this session: Base." in _system_text(request)
    # 不把身分寫回 ctx：寫回會讓下游把外人觸發的回合當成有身分
    assert ctx.get("identity") is None


async def test_sns_round_tool_list_matches_execution_grants(base_grants_everything: None) -> None:
    """列工具的身分和執行時（System → 寫死的空授權）一樣空：列出的每個工具執行時都授權。

    對照組：同樣的設定下用 Base 身分列，會列出工具（也就是舊的退路會看得到、用不了）。
    """
    request = _request()
    ctx = _layer_ctx(None, sns_source=True)
    with patch(_MISSION_MGR, new=AsyncMock(return_value=None)):
        await ToolLayer().process(request, ctx)
    execution = permission_service.resolve_grants(None).tools
    assert all(execution.allows(name) for name in _names(request))

    base_request = _request()
    base = AgentIdentity.for_role("SnsProbeSoul", "Base", capabilities=[])
    with patch(_MISSION_MGR, new=AsyncMock(return_value=None)):
        await ToolLayer().process(base_request, _layer_ctx(base, sns_source=True))
    listed_for_base = _names(base_request)
    assert listed_for_base
    assert not any(execution.allows(name) for name in listed_for_base)


# ── 第 3 項：MCP 呼叫不改 AI 自己的參數 ────────────────────────────────────────────────


class _FakeMcp:
    """假的 MCP：``docgen_read`` 在 ``mcp-docgen`` 上；回傳指定的文字（不帶錯誤旗標）。"""

    def __init__(self, text: str = "ok") -> None:
        self.registry = SimpleNamespace(
            get_server=lambda _name: SimpleNamespace(vault_jail_arg=None)
        )
        self.calls: list[dict[str, Any]] = []
        self._text = text

    def resolve_tool(self, name: str) -> tuple[str | None, str]:
        return "mcp-docgen", name

    async def call_tool_on_server(
        self, _server: str, _tool: str, args: dict[str, Any], **_kwargs: Any
    ) -> McpCompleteOutcome:
        self.calls.append(dict(args))
        return McpCompleteOutcome(
            source_protocol_version=UNKNOWN_PROTOCOL_VERSION,
            content=[{"type": "text", "text": self._text}],
        )


def _allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="ALLOW", reason="test-allow"),
    )


def _mcp_executor(mcp: _FakeMcp) -> ToolExecutor:
    return ToolExecutor(
        protocol_manager=mcp, tool_registry={}, memory_tool_registry={}, plugin_registry={},
        enforce_advertised=False,
    )


async def _run_mcp(
    executor: ToolExecutor, args: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    queue: asyncio.Queue = asyncio.Queue()
    ctx = {"identity": SOUL, "auth_level": "Manual", "session_id": None}
    async with asyncio.timeout(WAIT_S):
        result = await executor.execute_to_queue("docgen_read", args, "req-mcp", ctx, queue)
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


async def test_mcp_call_sends_agent_id_without_touching_the_models_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_all(monkeypatch)
    mcp = _FakeMcp()
    # Claude 形狀：解析出的呼叫參數和寫回歷史的 tool_use block 是同一個 dict
    # （claude_adapter.parse_round 的 ToolCall(args=block.input)、raw_assistant=raw.content）
    block = SimpleNamespace(type="tool_use", id="tu1", name="docgen_read", input={"q": "x"})
    raw_assistant = [block]
    await _run_mcp(_mcp_executor(mcp), block.input)
    (sent,) = mcp.calls
    assert sent == {"q": "x", "agent_id": SOUL.agent_id}
    assert raw_assistant[0].input == {"q": "x"}  # 下一輪 AI 看到的自己的呼叫沒有多欄位


# ── 第 4 項：成功與失敗只用一套規則 ────────────────────────────────────────────────────


def _result_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    (event,) = [e for e in events if e.get("type") == TOOL_RESULT]
    return event


def _terminal_output(output: str, exit_code: int) -> str:
    """終端真的回的樣子（``execute_command`` 結尾：結束碼一行加輸出，交給 ``_wrap_output``）。"""
    return terminal_tool._wrap_output(
        "grep -r Error: logs", f"[EXIT_CODE: {exit_code}]\n{output}", exit_code=exit_code
    )


def _returning(text: str):
    async def handler(**_kwargs: Any) -> str:
        return text

    return handler


async def _run_builtin(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, name: str, text: str
) -> list[dict[str, Any]]:
    hooks.configure({"PostToolUse": [group()], "PostToolUseFailure": [group()]})
    executor = executor_with(monkeypatch, {name: _returning(text)})
    _allow_all(monkeypatch)
    _result, events = await run_tool(executor, name, {"command": "x"}, mode="YOLO")
    return events


@pytest.mark.parametrize(
    ("exit_code", "output", "failed"),
    [
        (0, "logs/a.txt: Error: disk full", False),  # 找到含 Error: 的行，指令本身成功
        (1, "no such file", True),
        (2, "logs/a.txt: Error: disk full", True),
    ],
)
async def test_terminal_success_follows_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, exit_code: int, output: str, failed: bool
) -> None:
    events = await _run_builtin(
        monkeypatch, hooks, "execute_command", _terminal_output(output, exit_code)
    )
    assert (_result_event(events)["status"] == "error") is failed
    assert hooks.http.events() == ["PostToolUseFailure" if failed else "PostToolUse"]


@pytest.mark.parametrize(
    ("text", "failed"),
    [
        ("found 3 lines; one says Error: timeout", False),  # 中段有 Error: 不算失敗
        ("note: ❌ is the failure icon", False),
        ("Error: file not found", True),
        ("❌ 寫入失敗", True),
        (json.dumps({"ok": True, "status": "success", "note": "❌ in text"}), False),
    ],
)
async def test_other_builtin_tools_look_at_the_leading_text(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, text: str, failed: bool
) -> None:
    events = await _run_builtin(monkeypatch, hooks, "ext_review_probe", text)
    assert (_result_event(events)["status"] == "error") is failed
    assert hooks.http.events() == ["PostToolUseFailure" if failed else "PostToolUse"]


async def test_memory_write_failure_still_counts_as_failed(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup
) -> None:
    hooks.configure({"PostToolUse": [group()], "PostToolUseFailure": [group()]})
    _allow_all(monkeypatch)
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={},
        memory_tool_registry={"memory_tools": _returning("❌ 記憶寫入失敗：向量庫忙碌")},
        plugin_registry={},
        enforce_advertised=False,
    )
    _result, events = await run_tool(
        executor, "memory_tools", {"action": "save", "content": "x"}, mode="YOLO"
    )
    assert _result_event(events)["status"] == "error"
    assert hooks.http.events() == ["PostToolUseFailure"]


@pytest.mark.parametrize(
    ("text", "failed"),
    [
        ("3 results; the second mentions Error: retry", False),
        # 第三方伺服器沒標 isError、開頭寫失敗（如 grok_imagine_server）
        ("❌ 錯誤：生成失敗", True),
    ],
)
async def test_mcp_results_without_is_error_use_the_same_leading_rule(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, text: str, failed: bool
) -> None:
    hooks.configure({"PostToolUse": [group()], "PostToolUseFailure": [group()]})
    _allow_all(monkeypatch)
    _result, events = await _run_mcp(_mcp_executor(_FakeMcp(text)), {"q": "x"})
    assert (_result_event(events)["status"] == "error") is failed
    assert hooks.http.events() == ["PostToolUseFailure" if failed else "PostToolUse"]


# ── 第 5 項：完全沒帶參數就不跳卡、不觸發 hook ─────────────────────────────────────────


def _failed_notices(events: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e.get("type") == TURN_NOTICE_EVENT
        and e.get("kind") == NOTICE_TOOL_FAILED
        and e.get("params", {}).get("tool") == tool
    ]


@pytest.mark.parametrize("mode", ["Manual", "Auto"])
async def test_call_with_no_arguments_fails_before_hooks_reviewer_and_card(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup, mode: str
) -> None:
    hooks.configure({"PreToolUse": [group()], "PermissionRequest": [group()]})
    completer = use_reviewer(monkeypatch)
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, events = await run_tool(executor, "write", {}, mode=mode)
    assert tool_errors.extract_error_class(result) == tool_errors.VALIDATION_ERROR
    assert cards(events) == []
    assert completer.calls == 0
    assert hooks.http.events() == []
    assert recorder.calls == []
    assert [e for e in events if e.get("type") == TOOL_START] == []
    assert _result_event(events)["status"] == "error"
    # 結果事件沒有 tool_start 時畫面與存下的步驟會丟掉它：另外留一則失敗小提示
    assert len(_failed_notices(events, "write")) == 1


async def test_call_with_no_arguments_is_still_recorded_as_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = executor_with(monkeypatch, {"write": Recorder()})
    used: set[str] = set()
    ctx = {"identity": SOUL, "auth_level": "Manual", "session_id": None, "resumable_stream": True}
    async with asyncio.timeout(WAIT_S):
        await executor.execute_to_queue("write", {}, "req", ctx, asyncio.Queue(), None, used)
    assert "write" in used


async def test_unlisted_tool_is_still_refused_before_the_argument_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = executor_with(monkeypatch, {"write": Recorder()})
    executor._enforce_advertised = True
    queue: asyncio.Queue = asyncio.Queue()
    ctx = {
        "identity": SOUL, "auth_level": "Manual", "session_id": None,
        "advertised_tools": AdvertisedTools.from_schemas([]),
    }
    async with asyncio.timeout(WAIT_S):
        result = await executor.execute_to_queue("write", {}, "req", ctx, queue)
    assert tool_errors.extract_field(result, "reason") == "not_advertised"


async def test_policy_denial_still_comes_before_the_argument_check(
    monkeypatch: pytest.MonkeyPatch, hooks: HookSetup
) -> None:
    hooks.configure({"PermissionDenied": [group()]})
    executor = executor_with(monkeypatch, {"write": Recorder()})
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="DENY", reason="test-deny"),
    )
    result, events = await run_tool(executor, "write", {}, mode="Manual")
    assert tool_errors.extract_error_class(result) == tool_errors.PERMISSION_DENIED
    assert tool_errors.extract_field(result, "reason") == "test-deny"
    assert _failed_notices(events, "write") == []
    await until(lambda: "PermissionDenied" in hooks.http.events())


async def test_role_grant_still_comes_before_the_argument_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = executor_with(monkeypatch, {"write": Recorder()})
    no_role = AgentIdentity(agent_id="ExtReviewNoRole")  # 沒有角色：原生工具一個都沒授權
    result, _events = await run_tool(
        executor, "write", {}, mode="Manual", extra={"identity": no_role}
    )
    assert tool_errors.extract_field(result, "reason") == "native_tool_rbac"


async def test_call_missing_only_some_arguments_is_checked_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有帶參數、只是少了必填的：照 ADR-0229 先核准、後檢查。"""
    recorder = Recorder()
    executor = executor_with(monkeypatch, {"write": recorder})
    result, events = await run_tool(
        executor, "write", {"scope": "personal"}, mode="Manual", approve=True
    )
    assert len(cards(events)) == 1
    assert tool_errors.extract_error_class(result) == tool_errors.VALIDATION_ERROR
    assert recorder.calls == []


# ── 第 9 項：沒人在場的終端指令由終端自己再擋一次 ────────────────────────────────────


async def _headless_command(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, AsyncMock]:
    core = AsyncMock(return_value=(0, "ran", 1))
    monkeypatch.setattr(terminal_tool, "_execute_core", core)
    executor = executor_with(monkeypatch, {"execute_command": terminal_tool.execute_command})
    result, _events = await run_tool(
        executor,
        "execute_command",
        {"command": "mkdir headless-probe"},
        present=False,
        mode="Manual",
        extra={"heartbeat_run": True},
    )
    return result, core


async def test_headless_dangerous_command_is_refused_without_a_legacy_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_router, "sse_subscribers", [])
    result, core = await _headless_command(monkeypatch)
    assert json.loads(result)["reason"] == "ui_unavailable"
    core.assert_not_awaited()


async def test_headless_dangerous_command_waits_on_the_legacy_ui_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_router, "sse_subscribers", [asyncio.Queue(maxsize=100)])
    result, core = await _headless_command(monkeypatch)
    payload = json.loads(result)
    assert payload["status"] == "approval_pending"
    exec_id = payload["execution_id"]
    record = terminal_tool._ASYNC_RESULTS[exec_id]
    async with asyncio.timeout(WAIT_S):
        while exec_id not in terminal_router.pending_approvals:
            await asyncio.sleep(0.005)
        terminal_router.approval_results[exec_id] = "rejected"
        terminal_router.pending_approvals[exec_id].set()
        await record["_task"]
    assert record["status"] == "rejected"
    core.assert_not_awaited()

"""子代理在專用空間的專案裡跑指令（ADR-0333，融合段 3）.

窄開關：只有同時符合下面五件事的子代理，才能在專案裡跑指令。
- 派它的那一輪有人在場：聊天端點送出的對話，而且有 session。
- 綁的是專用空間的專案。
- 不是只能看的子代理。
- 派出時是 Auto 或 YOLO；無痕一律不行。
- 派它的那一輪（比對串流編號）還在回覆。

能跑時一律在執行器裡跑；Auto 每條送審查員（不看全域的 ``headless_terminal``），審查員的費用記在
派它的靈魂身上，擋下次數和派它的對話合併計算。那一輪結束後，指令被收掉（原因寫明），之後也不能
再開。子代理不建個人空間的資料夾。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import SUBAGENT_DENY_PREFIX, AuthPoliciesConfig
from src.services import auto_reviewer as reviewer_module
from src.services import parent_turn
from src.services import tool_executor as executor_module
from src.services.auto_reviewer import AutoReviewer, ReviewRequest
from src.services.isolation import policy as policy_module
from src.services.isolation.agent_command import CommandScope
from src.services.isolation.consent import ExecutionGrant
from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.status import LevelDecision
from src.services.parent_turn import ParentTurnEndedError
from src.services.resource_manager import resource_manager
from src.services.stream_buffer_service import stream_buffer_service
from src.tools import subagent_tools, terminal
from src.tools.builtin import tool_registry
from src.tools.terminal import APPROVED_BY_AUTO_REVIEWER
from src.utils.presence import (
    PARENT_IDENTITY,
    PARENT_SESSION_ID,
    PARENT_STREAM_ID,
    PARENT_TURN_ENDED,
    SUBAGENT_PROJECT_COMMANDS,
    commands_attended,
    subagent_project_commands,
)
from src.utils.user_intent import chat_intent

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.support.hook_wiring import (
    REVIEW_ALLOW,
    REVIEW_DENY,
    WAIT_S,
    FakeCompleter,
    Recorder,
    clean_approval_state,
    executor_with,
)
from tests.support.session_scope import A, seed
from tests.support.session_scope import scope_db as scope_db
from tests.unit.test_project_space_commands_security import (  # noqa: F401 — fixture
    _assert_started_in_the_project,
    space,
)
from tests.unit.test_subagent_project_access import (  # noqa: F401 — fixture
    MODEL,
    _add_project,
    _advertised,
    _client,
    world,
)

SOUL = AgentIdentity.for_role(agent_id="SeiaSoul", role="Owner", is_human=False)
SUB = AgentIdentity.for_role(agent_id="__subagent_cmdprobe__", role="Subagent")
EXAMPLE = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    yield from clean_approval_state(monkeypatch)


@pytest.fixture
def stream():
    """一則真的在跑的回覆串流（``stream_buffer_service``），測試結束清掉。"""
    created: list[str] = []

    async def _open(session_id: str) -> str:
        entry = await stream_buffer_service.create(session_id)
        created.append(session_id)
        return entry.stream_id

    yield _open
    for session_id in created:
        stream_buffer_service._streams.pop(session_id, None)


def _parent(**extra: Any) -> dict[str, Any]:
    """派子代理的那一輪：網頁對話、有 session、Auto。"""
    return {"resumable_stream": True, "session_id": "s-parent", "auth_level": "Auto", **extra}


# ── 派出時的判斷（SSOT，純函式）─────────────────────────────────────


def test_a_subagent_from_an_attended_streaming_turn_may_run_project_commands():
    assert subagent_project_commands(_parent(), dedicated=True, read_only=False, stream_id="t1")


@pytest.mark.parametrize(
    ("change", "kwargs"),
    [
        ({"resumable_stream": False}, {}),  # 心跳、SNS 這類沒有聊天端點的
        ({"heartbeat_run": True}, {}),
        ({"subagent_run": True}, {}),
        ({"session_id": None}, {}),  # IDE 沒選對話
        ({"auth_level": "Manual"}, {}),
        ({"auth_level": "weird"}, {}),
        ({"incognito": True}, {}),
        ({}, {"dedicated": False}),  # 個人空間、別的專案、沒綁
        ({}, {"read_only": True}),
        ({}, {"stream_id": None}),  # 沒有回覆串流（例如 API 腳本）
    ],
)
def test_any_missing_condition_turns_it_off(change, kwargs):
    args = {"dedicated": True, "read_only": False, "stream_id": "t1", **kwargs}
    assert not subagent_project_commands(_parent(**change), **args)


def test_yolo_counts_like_auto():
    assert subagent_project_commands(
        _parent(auth_level="YOLO"), dedicated=True, read_only=False, stream_id="t1"
    )


@pytest.mark.parametrize(
    ("ctx", "attended"),
    [
        ({"resumable_stream": True}, True),
        ({"resumable_stream": True, "heartbeat_run": True}, False),
        ({"subagent_run": True, SUBAGENT_PROJECT_COMMANDS: True}, True),
        ({"subagent_run": True, SUBAGENT_PROJECT_COMMANDS: "yes"}, False),  # 只認 True
        ({SUBAGENT_PROJECT_COMMANDS: True}, False),  # 不是子代理就不看這個欄位
        ({"subagent_run": True}, False),
    ],
)
def test_commands_attended(ctx, attended):
    assert commands_attended(ctx) is attended


# ── 那一輪還在不在（比對串流編號）──────────────────────────────────


async def test_only_the_dispatching_turn_counts_and_it_never_comes_back(stream):
    first = await stream("s-turn")
    assert stream_buffer_service.active_stream_id("s-turn") == first
    assert stream_buffer_service.is_stream_active("s-turn", first)
    second = await stream("s-turn")  # 你送了新訊息
    assert second != first
    assert not stream_buffer_service.is_stream_active("s-turn", first)
    assert stream_buffer_service.is_stream_active("s-turn", second)
    await stream_buffer_service.finish("s-turn", stream_id=second)
    assert stream_buffer_service.active_stream_id("s-turn") is None
    assert not stream_buffer_service.is_stream_active("s-turn", second)


@pytest.mark.parametrize(("session_id", "stream_id"), [(None, "x"), ("s-none", None), ("", "")])
def test_missing_ids_are_never_active(session_id, stream_id):
    assert not stream_buffer_service.is_stream_active(session_id, stream_id)


def _sub_ctx(stream_id: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "identity": SUB,
        "subagent_run": True,
        "dispatch_run": True,
        "auth_level": "Auto",
        SUBAGENT_PROJECT_COMMANDS: True,
        PARENT_SESSION_ID: "s-parent",
        PARENT_STREAM_ID: stream_id,
        PARENT_IDENTITY: SOUL,
        **extra,
    }


async def test_parent_turn_active_needs_the_flag_and_the_same_stream(stream):
    stream_id = await stream("s-parent")
    assert parent_turn.parent_turn_active(_sub_ctx(stream_id))
    assert not parent_turn.subagent_turn_over(_sub_ctx(stream_id))
    switched_off = _sub_ctx(stream_id, **{SUBAGENT_PROJECT_COMMANDS: False})
    assert not parent_turn.parent_turn_active(switched_off)
    assert not parent_turn.parent_turn_active(_sub_ctx("other-turn"))
    assert parent_turn.subagent_turn_over(_sub_ctx("other-turn"))
    assert not parent_turn.subagent_turn_over({"identity": SOUL})  # 不是這種子代理


async def test_current_stream_id_reads_the_dispatching_turn(stream):
    stream_id = await stream("s-parent")
    assert parent_turn.current_stream_id({"session_id": "s-parent"}) == stream_id
    assert parent_turn.current_stream_id({"session_id": None}) is None
    assert parent_turn.current_stream_id({}) is None


# ── 政策（工具清單與執行兩道關卡）──────────────────────────────────


def _policy() -> AuthPoliciesConfig:
    return AuthPoliciesConfig.model_validate(
        json.loads((EXAMPLE / "auth_policies.json.example").read_text(encoding="utf-8"))
    )


def _decide(action: str, **env: Any):
    resource = "git status" if action == "terminal_execute" else "execute_command"
    base = {"auth_level": "Auto", "subagent_run": True, "isolation": "executor"}
    return _policy().evaluate(
        PolicyContext(actor=SUB, resource=resource, action=action, environment={**base, **env})
    )


@pytest.mark.parametrize("action", ["schema_injection", "terminal_execute"])
def test_a_subagent_without_the_switch_cannot_run_commands(action):
    decision = _decide(action, subagent_project_commands=False, parent_turn_active=True)
    assert decision.effect == "DENY"
    assert decision.reason == f"{SUBAGENT_DENY_PREFIX}not_allowed"


@pytest.mark.parametrize("action", ["schema_injection", "terminal_execute"])
def test_after_the_dispatching_turn_ends_commands_are_denied(action):
    decision = _decide(action, subagent_project_commands=True, parent_turn_active=False)
    assert decision.effect == "DENY"
    assert decision.reason == f"{SUBAGENT_DENY_PREFIX}{PARENT_TURN_ENDED}"


def test_a_subagent_command_outside_the_executor_is_denied():
    decision = _decide(
        "terminal_execute", subagent_project_commands=True, parent_turn_active=True,
        isolation="landlock",
    )
    assert decision.effect == "DENY"
    assert decision.reason == f"{SUBAGENT_DENY_PREFIX}needs_executor"


def test_a_refused_project_keeps_its_own_reason():
    decision = _decide(
        "terminal_execute", subagent_project_commands=True, parent_turn_active=True,
        isolation="none", isolation_refused="scan_limit",
    )
    assert decision.effect == "DENY"
    assert "scan_limit" in (decision.reason or "")


@pytest.mark.parametrize(("mode", "effect"), [("Auto", "REVIEW"), ("YOLO", "ALLOW")])
def test_with_the_switch_the_mode_table_decides(mode, effect):
    decision = _decide(
        "terminal_execute", auth_level=mode, subagent_project_commands=True,
        parent_turn_active=True,
    )
    assert decision.effect == effect


@pytest.mark.parametrize("mode", ["Auto", "YOLO"])
@pytest.mark.parametrize("command", ["cat .env", "mkfs.ext4 /dev/sdb1", "shutdown now"])
def test_the_switch_does_not_relax_hard_limits(command, mode):
    decision = _policy().evaluate(
        PolicyContext(
            actor=SUB, resource=command, action="terminal_execute",
            environment={
                "auth_level": mode, "subagent_run": True, "isolation": "executor",
                "subagent_project_commands": True, "parent_turn_active": True,
            },
        )
    )
    assert decision.effect in {"HUMAN_ONLY", "DENY"}  # 只有人能核准或黑名單；子代理都跑不了


def test_dynamic_shell_still_needs_a_human_in_auto():
    decision = _decide(
        "terminal_execute", subagent_project_commands=True, parent_turn_active=True,
    )
    assert decision.effect == "REVIEW"
    dynamic = _policy().evaluate(
        PolicyContext(
            actor=SUB, resource="echo $(whoami)", action="terminal_execute",
            environment={
                "auth_level": "Auto", "subagent_run": True, "isolation": "executor",
                "subagent_project_commands": True, "parent_turn_active": True,
            },
        )
    )
    assert dynamic.effect == "HUMAN_ONLY"


def test_other_tools_and_souls_are_not_touched():
    read = _policy().evaluate(
        PolicyContext(actor=SUB, resource="read", action="tool_call",
                      environment={"auth_level": "Auto", "subagent_run": True})
    )
    assert read.effect == "ALLOW"
    soul = _policy().evaluate(
        PolicyContext(actor=SOUL, resource="git status", action="terminal_execute",
                      environment={"auth_level": "Auto", "isolation": "executor"})
    )
    assert soul.effect == "REVIEW"


def test_denial_text_tells_the_subagent_to_report_back():
    for reason in ("not_allowed", PARENT_TURN_ENDED, "needs_executor"):
        message, suggestion = policy_module.denial_text(
            "execute_command", "Subagent", f"{SUBAGENT_DENY_PREFIX}{reason}"
        )
        assert reason in message
        assert "agent that dispatched you" in suggestion
        assert "Do not retry" in suggestion


# ── 審查員 ───────────────────────────────────────────────────────


def _reviewer(text: str = REVIEW_ALLOW, *, headless_terminal: bool = False):
    completer = FakeCompleter(text)
    config = {
        "model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0,
        "headless_terminal": headless_terminal,
    }
    return AutoReviewer(completer, config_loader=lambda: config), completer


def _request(*, is_command: bool = True, subagent_command: bool = True) -> ReviewRequest:
    return ReviewRequest(
        tool_name="execute_command" if is_command else "delete",
        arguments={"command": "pytest -q"},
        is_command=is_command,
        shell_kind="bash",
        human_present=False,
        user_intent=None,
        session_id=None,
        conversation_key="k",
        isolation="executor",
        subagent_command=subagent_command,
    )


async def test_a_subagent_command_is_reviewed_even_with_the_global_switch_off():
    reviewer, completer = _reviewer()
    outcome = await reviewer.review(SOUL, _request())
    assert (outcome.verdict, completer.calls) == ("allow", 1)


async def test_other_unattended_commands_still_are_not_reviewed():
    reviewer, completer = _reviewer()
    outcome = await reviewer.review(SOUL, _request(subagent_command=False))
    assert (outcome.verdict, outcome.code, completer.calls) == ("escalate", "headless_terminal", 0)


async def test_a_subagent_high_risk_tool_is_still_not_reviewed():
    reviewer, completer = _reviewer()
    outcome = await reviewer.review(SOUL, _request(is_command=False))
    assert (outcome.verdict, outcome.code, completer.calls) == ("escalate", "headless_tool", 0)


# ── 執行器：完整流程（假審查員、假終端）─────────────────────────────


class _Seen(FakeCompleter):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.identities: list[str] = []

    async def __call__(self, identity: Any, *args: Any, **kwargs: Any):
        self.identities.append(identity.agent_id)
        return await super().__call__(identity, *args, **kwargs)


def _use(monkeypatch: pytest.MonkeyPatch, text: str = REVIEW_ALLOW) -> _Seen:
    completer = _Seen(text)
    config = {"model": "claude-haiku-5-5", "max_tokens": 300, "timeout_s": 7.0}
    monkeypatch.setattr(
        executor_module, "auto_reviewer", AutoReviewer(completer, config_loader=lambda: config)
    )
    return completer


async def _executor_level(*_args: Any, **_kwargs: Any) -> LevelDecision:
    return LevelDecision("executor")


async def _run(executor: Any, ctx: dict[str, Any], command: str = "pytest -q") -> str:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    async with asyncio.timeout(WAIT_S):
        return await executor.execute_to_queue(
            "execute_command", {"command": command}, "req-sub", ctx, queue
        )


def _flow(monkeypatch: pytest.MonkeyPatch, text: str = REVIEW_ALLOW):
    recorder = Recorder("[EXIT_CODE: 0]\nok")
    executor = executor_with(monkeypatch, {"execute_command": recorder})
    monkeypatch.setattr(executor_module, "policy_level", _executor_level)
    return executor, recorder, _use(monkeypatch, text)


def _sub(stream_id: str | None, **extra: Any) -> dict[str, Any]:
    intent = chat_intent([{"role": "user", "content": "修好這個 bug，測試要過"}])
    return {**_sub_ctx(stream_id), "user_intent": intent, "request_id": "req-sub", **extra}


async def test_auto_sends_the_subagent_command_to_the_reviewer_and_bills_the_soul(
    monkeypatch, stream
):
    executor, recorder, completer = _flow(monkeypatch)
    stream_id = await stream("s-parent")
    await _run(executor, _sub(stream_id))
    assert completer.identities == [SOUL.agent_id]  # 費用與上限記在靈魂身上
    ((call,),) = [recorder.calls]
    assert call["_approved_by"] == APPROVED_BY_AUTO_REVIEWER


async def test_a_subagent_from_a_heartbeat_is_denied_without_review(monkeypatch, stream):
    executor, recorder, completer = _flow(monkeypatch)
    stream_id = await stream("s-parent")
    result = await _run(executor, _sub(stream_id, **{SUBAGENT_PROJECT_COMMANDS: False}))
    assert (recorder.calls, completer.calls) == ([], 0)
    assert "not_allowed" in result


async def test_after_the_turn_ends_nothing_runs_and_nothing_is_reviewed(monkeypatch, stream):
    executor, recorder, completer = _flow(monkeypatch)
    old = await stream("s-parent")
    await stream("s-parent")  # 新的一輪（例如你切成 Manual 後送出的）
    result = await _run(executor, _sub(old))
    assert (recorder.calls, completer.calls) == ([], 0)
    assert PARENT_TURN_ENDED in result


async def test_a_refusal_goes_back_to_the_dispatching_agent(monkeypatch, stream):
    executor, recorder, _completer = _flow(monkeypatch, REVIEW_DENY)
    stream_id = await stream("s-parent")
    result = await _run(executor, _sub(stream_id))
    assert recorder.calls == []
    assert "agent that dispatched you" in result


async def test_blocks_are_counted_with_the_dispatching_conversation(monkeypatch, stream):
    executor, recorder, completer = _flow(monkeypatch, REVIEW_DENY)
    stream_id = await stream("s-parent")
    for request_id in ("req-a", "req-b", "req-c"):  # 三個不同的子代理
        await _run(executor, _sub(stream_id, request_id=request_id))
    assert completer.calls == 3
    await _run(executor, _sub(stream_id, request_id="req-d"))
    assert completer.calls == 3  # 第四次不再送審
    assert reviewer_module.MAX_CONSECUTIVE_BLOCKS == 3
    assert executor_module.auto_reviewer.breaker.tripped("s-parent")


# ── 終端：在專案裡跑、不建個人空間、那一輪結束 ─────────────────────


def _terminal_ctx(stream_id: str | None) -> dict[str, Any]:
    return {**_sub_ctx(stream_id), "resumable_stream": True}


async def test_the_subagent_command_runs_in_the_project_without_a_personal_folder(
    space, stream  # noqa: F811 — fixture
):
    stream_id = await stream("s-parent")
    result = await terminal.execute_command(
        "git status", SUB, auth_level="Auto", _approved_by=APPROVED_BY_AUTO_REVIEWER,
        session_id=None, _call_id="c-sub", _ctx=_terminal_ctx(stream_id),
    )
    assert "[EXIT_CODE: 0]" in result
    _assert_started_in_the_project(space)
    assert space.attended == [True]
    drive = resource_manager.agent_drive(SUB)
    assert drive is not None and resource_manager.zone_kind(drive) == "missing"


async def test_a_command_after_the_turn_ended_is_not_started(space, stream):  # noqa: F811
    old = await stream("s-parent")
    await stream("s-parent")
    result = await terminal.execute_command(
        "git status", SUB, auth_level="Auto", _approved_by=APPROVED_BY_AUTO_REVIEWER,
        session_id=None, _call_id="c-late", _ctx=_terminal_ctx(old),
    )
    assert space.spawn.calls == []
    assert PARENT_TURN_ENDED in result


async def test_a_foreground_command_killed_at_the_end_of_the_turn_says_why(
    monkeypatch, stream
):
    stream_id = await stream("s-parent")

    async def _killed(*_args: Any, **_kwargs: Any) -> tuple[int, str, int]:
        await stream("s-parent")  # 指令跑到一半，那一輪結束
        return -9, "partial", 5

    async def _scope(identity, _root, grant, **_kwargs):
        return CommandScope(identity, grant or ExecutionGrant("executor"))

    monkeypatch.setattr(terminal, "_execute_core", _killed)
    monkeypatch.setattr(terminal, "command_scope", _scope)
    monkeypatch.setattr(terminal, "session_project_root", _no_root)
    result = await terminal.execute_command(
        "pytest -q", SUB, auth_level="Auto", _approved_by=APPROVED_BY_AUTO_REVIEWER,
        session_id=None, _call_id="c-kill", _ctx=_terminal_ctx(stream_id),
    )
    assert PARENT_TURN_ENDED in result
    assert "agent that dispatched you" in result


async def _no_root(_ctx: Any, _session: Any) -> None:
    return None


async def test_stop_agent_commands_writes_the_reason_it_was_given():
    terminal._ASYNC_RESULTS["e-turn"] = {
        "status": "running", "agent_id": SUB.agent_id, "command": "pytest -q", "result": None,
    }
    try:
        await terminal.stop_agent_commands(SUB, error=ParentTurnEndedError())
        record = terminal._ASYNC_RESULTS["e-turn"]
        assert record["status"] == "stopped"
        assert PARENT_TURN_ENDED in record["result"]
    finally:
        terminal._ASYNC_RESULTS.pop("e-turn", None)


async def test_stop_agent_commands_keeps_the_reset_wording_by_default():
    terminal._ASYNC_RESULTS["e-reset"] = {
        "status": "running", "agent_id": SUB.agent_id, "command": "ls", "result": None,
    }
    try:
        await terminal.stop_agent_commands(SUB)
        assert "agent_busy" in terminal._ASYNC_RESULTS["e-reset"]["result"]
    finally:
        terminal._ASYNC_RESULTS.pop("e-reset", None)


# ── 子代理那邊：派出時記串流、監看、做完收掉 ─────────────────────────


async def test_the_watch_keeps_collecting_commands_after_the_turn_ends(monkeypatch, stream):
    stream_id = await stream("s-parent")
    collected: list[Any] = []

    async def _stop(identity: AgentIdentity, *, error: Exception | None = None) -> int:
        collected.append((identity.agent_id, type(error)))
        return 0

    monkeypatch.setattr(subagent_tools, "stop_agent_commands", _stop)
    monkeypatch.setattr(subagent_tools, "STOP_CHECK_SECONDS", 0.01)
    gate = asyncio.Event()
    work = asyncio.create_task(gate.wait())
    turn = subagent_tools._TurnCommands(SUB, "s-parent", stream_id)
    watch = asyncio.create_task(
        subagent_tools._cancel_when_parent_stops(
            {"session_id": "s-parent"}, work, asyncio.Event(), turn
        )
    )
    await asyncio.sleep(0.05)
    assert collected == []  # 那一輪還在
    await stream_buffer_service.finish("s-parent", stream_id=stream_id)
    await asyncio.sleep(0.08)
    assert len(collected) >= 2  # 一直收，不是只收一次
    assert set(collected) == {(SUB.agent_id, ParentTurnEndedError)}
    assert not work.done()  # 子代理本身不取消，還能寫完報告
    gate.set()
    await asyncio.wait_for(watch, WAIT_S)


async def test_a_finished_subagent_also_stops_its_commands(monkeypatch):
    stopped: list[str] = []

    async def _stop(identity: AgentIdentity, *, error: Exception | None = None) -> int:
        stopped.append(identity.agent_id)
        return 0

    async def _done(**_kwargs: Any) -> str:
        return "完成"

    monkeypatch.setattr(subagent_tools, "stop_agent_commands", _stop)
    monkeypatch.setattr(subagent_tools, "_assemble_and_run", _done)
    await subagent_tools._run_subagent(
        goal="照做", parent_identity=SOUL, parent_request_id="p", base_ctx={}, model="m",
    )
    for _ in range(50):
        if stopped:
            break
        await asyncio.sleep(0.01)
    assert len(stopped) == 1 and stopped[0].startswith("__subagent_")


async def test_dispatch_records_the_turn_before_any_await(monkeypatch, stream):
    stream_id = await stream("s-parent")
    seen: list[str | None] = []

    async def _wait(**kwargs: Any) -> str:
        seen.append(kwargs.get("parent_stream_id"))
        return "{}"

    monkeypatch.setattr(subagent_tools, "_dispatch_and_wait", _wait)
    ctx = {**_parent(), "advertised_tools": AdvertisedTools.from_schemas([])}  # 沒有看板工具
    await subagent_tools.dispatch_subagent(goal="做", identity=SOUL, request_id="r", _ctx=ctx)
    assert seen == [stream_id]


async def test_a_group_records_the_turn_before_waiting_for_a_slot(monkeypatch, stream):
    stream_id = await stream("s-parent")
    seen: list[str | None] = []

    async def _one(**kwargs: Any) -> str:
        seen.append(kwargs.get("parent_stream_id"))
        await stream("s-parent")  # 第一個跑的期間換了一輪：後面排隊的仍是派出那一輪
        return "完成"

    monkeypatch.setattr(subagent_tools, "_run_subagent", _one)
    await subagent_tools.dispatch_subagent_group(
        subtasks=[{"goal": "a"}, {"goal": "b"}], identity=SOUL, request_id="r", _ctx=_parent()
    )
    assert seen == [stream_id, stream_id]


# ── 整條路：組裝出來的工具清單（真的工具層與權限範例）─────────────────


@pytest.fixture
def dedicated(world, monkeypatch):  # noqa: F811 — fixture
    anchor = world / PROJECT_ROOT_NAME
    anchor.mkdir()
    monkeypatch.setattr(subagent_tools, "configured_projects_root", lambda: anchor.resolve())
    return anchor


async def _bind(root: Path, name: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    await _add_project(f"proj-{name}", root)
    session_id = f"chat-{name}"
    await seed(session_id, project_id=f"proj-{name}")
    return session_id


async def _advertised_for(session_id: str, mode: str, *, read_only: bool = False, **extra: Any):
    client = _client()
    parent = PipelineContext(
        anthropic_client=delegate_stream_to_create(client),
        protocol_manager=MagicMock(get_all_tools=AsyncMock(return_value=[])),
        tool_registry=tool_registry, memory_tool_registry={}, plugin_registry={},
        request_id="parent", t0=0.0, agent_id=A.agent_id, identity=A, session_id=session_id,
        auth_level=mode, resumable_stream=True, **extra,
    )
    stream_id = parent_turn.current_stream_id(parent)
    await subagent_tools._run_subagent(
        goal="照指示做", parent_identity=A, parent_request_id="parent", base_ctx=parent,
        model=MODEL, read_only=read_only, parent_stream_id=stream_id,
    )
    return _advertised(client)


@pytest.mark.parametrize(("mode", "listed"), [("Manual", False), ("Auto", True), ("YOLO", True)])
async def test_the_command_tool_is_listed_only_in_auto_and_yolo(dedicated, stream, mode, listed):
    session_id = await _bind(dedicated / "repo", "repo")
    await stream(session_id)
    assert ("execute_command" in await _advertised_for(session_id, mode)) is listed


async def test_a_read_only_subagent_does_not_get_it(dedicated, stream):
    session_id = await _bind(dedicated / "repo", "repo")
    await stream(session_id)
    assert "execute_command" not in await _advertised_for(session_id, "YOLO", read_only=True)


async def test_without_a_streaming_turn_it_does_not_get_it(dedicated):
    session_id = await _bind(dedicated / "repo", "repo")
    assert "execute_command" not in await _advertised_for(session_id, "YOLO")


async def test_a_heartbeat_subagent_does_not_get_it(dedicated, stream):
    session_id = await _bind(dedicated / "repo", "repo")
    await stream(session_id)
    advertised = await _advertised_for(
        session_id, "YOLO", heartbeat_run=True, enable_autonomy=True
    )
    assert "execute_command" not in advertised


async def test_a_project_outside_the_dedicated_space_does_not_get_it(
    dedicated, world, stream  # noqa: F811 — fixture
):
    session_id = await _bind(world / "elsewhere", "elsewhere")
    await stream(session_id)
    assert "execute_command" not in await _advertised_for(session_id, "YOLO")

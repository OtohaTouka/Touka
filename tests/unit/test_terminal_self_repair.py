"""缺口 3：終端自我修復名單補上 execute_command 與 get_command_result（iso-c1 U2）.

修補前：修復名單寫死 ``["bash", "terminal__bash", "execute_terminal"]``，沒有現在廣告的
``execute_command``，背景指令的結果經 ``get_command_result`` 也不在名單——失敗後的修復提示
從不附上。修補後名單是模組常數 ``TERMINAL_REPAIR_TOOLS``；非 0 exit code 附修復提示並送
``repair`` 進度事件、0 時重置次數；``get_command_result`` 對同一個 execution_id 每次都回同一份
含 ``[EXIT_CODE]`` 的結果，只計第一次。

只附提示文字給模型，不會自動執行任何東西。工具一律假的，政策放行（本檔只測修復名單）。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_executor as executor_module
from src.services.autonomy import autonomy_service
from src.services.tool_executor import TERMINAL_REPAIR_TOOLS, ToolExecutor

_IDENTITY = AgentIdentity.for_role(agent_id="RepairProbe", role="Owner", capabilities=["*"])


@pytest.fixture(autouse=True)
def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        executor_module.permission_service,
        "evaluate_policy",
        lambda _ctx: type("D", (), {"effect": "ALLOW", "reason": "ok"})(),
    )
    monkeypatch.setattr(
        executor_module.session_service, "get_session_by_id", AsyncMock(return_value=None)
    )


@pytest.fixture
def session(request: pytest.FixtureRequest) -> str:
    sid = f"repair-{request.node.name}"
    autonomy_service.reset_repair_attempts(sid)
    yield sid
    autonomy_service.reset_repair_attempts(sid)


@pytest.fixture
def hints(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """記下真正附上的修復提示（照原邏輯計數，只多記一份）。"""
    issued: list[str] = []
    original = autonomy_service.get_repair_instruction

    def _spy(session_id: str, exit_code: int) -> str | None:
        hint = original(session_id, exit_code)
        if hint:
            issued.append(hint)
        return hint

    monkeypatch.setattr(autonomy_service, "get_repair_instruction", _spy)
    return issued


def _executor(monkeypatch: pytest.MonkeyPatch, name: str, handler: Any) -> ToolExecutor:
    if name in executor_module.BUILTIN_EXECUTION_BINDINGS:
        bindings = dict(executor_module.BUILTIN_EXECUTION_BINDINGS)
        bindings[name] = replace(bindings[name], handler=handler)
        monkeypatch.setattr(executor_module, "BUILTIN_EXECUTION_BINDINGS", bindings)
    return ToolExecutor(None, {name: handler}, {}, {})


async def _call(
    executor: ToolExecutor, name: str, args: dict[str, Any], sid: str
) -> tuple[str, list[dict[str, Any]]]:
    queue: asyncio.Queue = asyncio.Queue()
    result = await executor.execute_to_queue(
        name, args, "req-repair", {"identity": _IDENTITY, "session_id": sid, "model": "m"}, queue
    )
    events = [event for event in queue._queue if event is not None]
    repairs = [e for e in events if e.get("type") == "touka_progress" and e.get("kind") == "repair"]
    return result, repairs


def _returns(text: str) -> Any:
    async def _tool(**_kwargs: Any) -> str:
        return text

    return _tool


def test_repair_names_are_a_module_constant_with_the_advertised_tools():
    assert {"execute_command", "get_command_result"} <= TERMINAL_REPAIR_TOOLS
    assert {"bash", "terminal__bash", "execute_terminal"} <= TERMINAL_REPAIR_TOOLS


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("execute_command", {"command": "false"}),
        ("get_command_result", {"execution_id": "exec-fail"}),
    ],
)
async def test_failed_terminal_result_gets_repair_hint_and_event(
    monkeypatch, session, hints, name, args
):
    executor = _executor(monkeypatch, name, _returns("[EXIT_CODE: 1]\nboom"))
    result, repairs = await _call(executor, name, args, session)
    assert len(hints) == 1
    assert hints[0] in result
    assert [event["attempt"] for event in repairs] == [1]
    assert autonomy_service.get_repair_state(session)[0] == 1


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("execute_command", {"command": "true"}),
        ("get_command_result", {"execution_id": "exec-ok"}),
    ],
)
async def test_successful_terminal_result_resets_attempts(monkeypatch, session, name, args):
    autonomy_service.get_repair_instruction(session, 1)
    executor = _executor(monkeypatch, name, _returns("[EXIT_CODE: 0]\nfine"))
    _result, repairs = await _call(executor, name, args, session)
    assert repairs == []
    assert autonomy_service.get_repair_state(session)[0] == 0


async def test_requerying_the_same_execution_counts_once(monkeypatch, session, hints):
    failed = "[EXIT_CODE: 2]\nstill the same finished result"
    executor = _executor(monkeypatch, "get_command_result", _returns(failed))
    first, first_repairs = await _call(
        executor, "get_command_result", {"execution_id": "exec-same"}, session
    )
    again, again_repairs = await _call(
        executor, "get_command_result", {"execution_id": "exec-same"}, session
    )
    assert len(first_repairs) == 1
    assert again_repairs == []
    assert len(hints) == 1
    assert hints[0] in first
    assert hints[0] not in again  # 重複查詢不再附修復提示
    assert autonomy_service.get_repair_state(session)[0] == 1

    _other, other_repairs = await _call(
        executor, "get_command_result", {"execution_id": "exec-other"}, session
    )
    assert [event["attempt"] for event in other_repairs] == [2]


async def test_pending_result_does_not_consume_the_execution(monkeypatch, session):
    pending = '{"status": "running", "execution_id": "exec-later"}'
    executor = _executor(monkeypatch, "get_command_result", _returns(pending))
    _result, repairs = await _call(
        executor, "get_command_result", {"execution_id": "exec-later"}, session
    )
    assert repairs == []
    executor = _executor(monkeypatch, "get_command_result", _returns("[EXIT_CODE: 1]\nlate"))
    _result, repairs = await _call(
        executor, "get_command_result", {"execution_id": "exec-later"}, session
    )
    assert [event["attempt"] for event in repairs] == [1]


async def test_tracked_executions_are_bounded(monkeypatch, session):
    monkeypatch.setattr(executor_module, "MAX_TRACKED_REPAIR_RESULTS", 2)
    monkeypatch.setattr(executor_module, "_counted_repair_results", OrderedDict())
    executor = _executor(monkeypatch, "get_command_result", _returns("[EXIT_CODE: 0]\nok"))
    for execution_id in ("exec-a", "exec-b", "exec-c"):
        await _call(executor, "get_command_result", {"execution_id": execution_id}, session)
    assert list(executor_module._counted_repair_results) == [
        (session, "exec-b"),
        (session, "exec-c"),
    ]

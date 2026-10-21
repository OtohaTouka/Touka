"""P2b：長指令 during 監控——背景執行＋部分輸出＋停滯/暴衝訊號＋stop_command——TDD.

提案 P2b（併自「during 對模型」）：長指令背景執行不阻塞；get_command_result 的 running
回**部分輸出＋停滯/暴衝訊號**；模型面 stop_command 掛既有 terminate_process_tree、依
agent 隔離。**兩層看門狗**＝harness 硬逾時 backstop（背景 600s、前景 30s）＋模型
peek-and-decide——**訊號僅諮詢、絕不自動 kill**。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.tool_executor import CRITICAL_TOOLS
from src.tools import terminal
from src.tools.builtin import tool_registry
from src.tools.builtin.schemas import builtin_tools_schema
from src.utils.time_utils import get_current_epoch

_IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")


def _ads() -> set[str]:
    return {s["function"]["name"] for s in builtin_tools_schema}


@pytest.fixture(autouse=True)
def _clean_async_state():
    terminal.reset_async_state_for_test()
    yield
    terminal.reset_async_state_for_test()


def _running_record(**over: Any) -> dict[str, Any]:
    now = get_current_epoch()
    rec = {
        "status": "running",
        "agent_id": "DebugAgent",
        "command": "pytest -q",
        "result": None,
        "output": "collecting...\n",
        "started_at": now,
        "last_output_at": now,
        "_proc": None,
        "_task": None,
    }
    rec.update(over)
    return rec


# --------------------------------------------------------------------------- #
# 1. Schema／註冊
# --------------------------------------------------------------------------- #


def test_stop_command_advertised_registered_critical():
    assert "stop_command" in _ads()
    assert "stop_command" in tool_registry
    assert "stop_command" in CRITICAL_TOOLS
    stop_schema = next(
        s for s in builtin_tools_schema if s["function"]["name"] == "stop_command"
    )
    assert stop_schema["function"]["parameters"]["required"] == ["execution_id"]


def test_execute_command_has_background_param():
    exec_schema = next(
        s for s in builtin_tools_schema if s["function"]["name"] == "execute_command"
    )
    props = exec_schema["function"]["parameters"]["properties"]
    assert props["background"]["type"] == "boolean"


def test_get_command_result_desc_mentions_partial_and_signals():
    schema = next(
        s for s in builtin_tools_schema if s["function"]["name"] == "get_command_result"
    )
    desc = schema["function"]["description"]
    assert "partial_output" in desc or "部分輸出" in desc
    assert "stop_command" in desc


# --------------------------------------------------------------------------- #
# 2. 背景執行：立即回 running handle、不阻塞
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_background_returns_running_handle_immediately():
    gate = asyncio.Event()

    async def _blocking_core(*_a: Any, record: Any = None, **_k: Any) -> tuple[int, str, int]:
        await gate.wait()
        return (0, "done\n", 10)

    with (
        patch.object(terminal, "_classify_command_strategy", return_value="ALLOW"),
        patch.object(terminal, "_execute_core", new=_blocking_core),
    ):
        started = await terminal.execute_command(
            command="pytest -q", identity=_IDENTITY, session_id="s1", background=True
        )
        payload = json.loads(started)
        assert payload["status"] == "running"
        exec_id = payload["execution_id"]
        assert exec_id
        # 尚未完成即回（不阻塞）：狀態仍 running
        probe = await terminal.get_command_result(execution_id=exec_id, identity=_IDENTITY)
        assert json.loads(probe)["status"] == "running"
        gate.set()  # 放行背景完成
        final = await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )
    assert "done" in final


@pytest.mark.asyncio
async def test_dangerous_background_preapproved_runs_and_keeps_danger_badge():
    """G-3（ADR-0125 × ADR-0143 H2）：互動 CONFIRM 經統一 broker 預核准
    （_hitl_preapproved=True）＋background=True 現可落背景執行——釘死此 emergent 組合
    （防回歸）：①立即回 running handle（非 approval_pending/ui_unavailable，不繞過審批＝
    核准已於 tool_executor Gate-2 前置取得）②背景完成卡保留 ⚠️ [危險] 徽章（dangerous 透傳、
    觀測面誠實）③stop_command/get_command_result 對此 exec 正常運作（繼承 ADR-0125 護欄）。"""
    gate = asyncio.Event()

    async def _blocking_core(*_a: Any, record: Any = None, **_k: Any) -> tuple[int, str, int]:
        await gate.wait()
        return (0, "cleaned\n", 10)

    with (
        patch.object(terminal, "_classify_command_strategy", return_value="CONFIRM"),
        patch.object(terminal, "_execute_core", new=_blocking_core),
    ):
        started = await terminal.execute_command(
            command="rm -rf build", identity=_IDENTITY, session_id="s1",
            background=True, _hitl_preapproved=True,
        )
        payload = json.loads(started)
        # 危險指令經預核准 → 落背景 running handle（非等待審批、非 ui_unavailable）
        assert payload["status"] == "running", started
        exec_id = payload["execution_id"]
        # 仍在跑（不阻塞）
        probe = json.loads(
            await terminal.get_command_result(execution_id=exec_id, identity=_IDENTITY)
        )
        assert probe["status"] == "running"
        gate.set()  # 放行背景完成
        final = await terminal.get_command_result(
            execution_id=exec_id, wait_seconds=3, identity=_IDENTITY
        )
    assert "cleaned" in final
    assert "⚠️ [危險]" in final, "背景完成卡應保留危險徽章（dangerous 須透傳進 _run_background）"


# --------------------------------------------------------------------------- #
# 3. get_command_result running → 部分輸出＋訊號
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_running_returns_partial_output_and_stall_signal():
    now = get_current_epoch()
    terminal._ASYNC_RESULTS["bg100000"] = _running_record(
        output="collecting items...\n" * 3,
        started_at=now - 40,
        last_output_at=now - 30,  # 閒置 30s → stalled
    )
    out = await terminal.get_command_result(execution_id="bg100000", identity=_IDENTITY)
    data = json.loads(out)
    assert data["status"] == "running"
    assert "collecting items" in data["partial_output"]
    assert "stalled" in data["signals"]
    assert data["idle_seconds"] >= 20
    assert data["elapsed_seconds"] >= 40


@pytest.mark.asyncio
async def test_running_flags_runaway_output():
    now = get_current_epoch()
    terminal._ASYNC_RESULTS["bg200000"] = _running_record(
        output="x" * (300 * 1024),  # >256KB → runaway
        started_at=now - 5,
        last_output_at=now,  # 未停滯
    )
    data = json.loads(
        await terminal.get_command_result(execution_id="bg200000", identity=_IDENTITY)
    )
    assert "runaway_output" in data["signals"]
    assert "stalled" not in data["signals"]  # 剛有輸出→不停滯


@pytest.mark.asyncio
async def test_running_no_signals_when_healthy():
    now = get_current_epoch()
    terminal._ASYNC_RESULTS["bg300000"] = _running_record(
        output="ok\n", started_at=now - 3, last_output_at=now
    )
    data = json.loads(
        await terminal.get_command_result(execution_id="bg300000", identity=_IDENTITY)
    )
    assert data["signals"] == []


# --------------------------------------------------------------------------- #
# 4. stop_command：終止、隔離、已完成
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stop_command_kills_and_marks_stopped():
    fake_proc = object()
    terminal._ASYNC_RESULTS["bg400000"] = _running_record(
        _proc=fake_proc, output="partial so far\n"
    )
    with patch.object(terminal, "terminate_process_tree") as term:
        result = await terminal.stop_command(execution_id="bg400000", identity=_IDENTITY)
    term.assert_called_once_with(fake_proc)
    assert tool_errors.is_tool_success(result)
    assert terminal._ASYNC_RESULTS["bg400000"]["status"] == "stopped"
    # 停止回覆帶已收集的部分輸出
    assert "partial so far" in (json.loads(result).get("preview") or "")


@pytest.mark.asyncio
async def test_stop_command_unknown_and_identity_scoped():
    unknown = await terminal.stop_command(execution_id="nope1234", identity=_IDENTITY)
    assert json.loads(unknown)["error_class"] == tool_errors.RESOURCE_NOT_FOUND

    terminal._ASYNC_RESULTS["bg500000"] = _running_record(agent_id="SomeoneElse", _proc=object())
    with patch.object(terminal, "terminate_process_tree") as term:
        stolen = await terminal.stop_command(execution_id="bg500000", identity=_IDENTITY)
    term.assert_not_called()  # 跨身分不得終止
    assert json.loads(stolen)["error_class"] == tool_errors.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_stop_command_already_finished_is_noop_success():
    terminal._ASYNC_RESULTS["bg600000"] = _running_record(status="done", result="[EXIT_CODE: 0]")
    with patch.object(terminal, "terminate_process_tree") as term:
        result = await terminal.stop_command(execution_id="bg600000", identity=_IDENTITY)
    term.assert_not_called()
    assert tool_errors.is_tool_success(result)
    assert json.loads(result)["final_status"] == "done"

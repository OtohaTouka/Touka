"""Phase 4（提案 §9）：多 subagent 並行 fan-out——TDD.

需求 1「同時派多個各跑各的再匯總」的完整 fan-out：orchestrator 以並發上限
（semaphore）同時開多個 in-process 子 loop、逐一收斂結構化回報、匯總回主 agent。
重用 Phase 1 的 ``_run_subagent``＋Phase 2 的 ``parse_subagent_report``；
遞迴閘沿用（子代理 Subagent 角色無 dispatch_subagent_group）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.permission_service import PermissionService
from src.tools import subagent_tools as st
from src.tools.builtin import builtin_tools_schema, tool_registry


def _debug_identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")


# --------------------------------------------------------------------------- #
# 1. 註冊／廣告／遞迴閘
# --------------------------------------------------------------------------- #


def test_group_tool_registered_and_advertised():
    assert "dispatch_subagent_group" in tool_registry
    names = {
        s.get("function", {}).get("name")
        for s in builtin_tools_schema
        if isinstance(s, dict)
    }
    assert "dispatch_subagent_group" in names


def test_subagent_role_excludes_group_tool():
    sub = AgentIdentity.for_role(agent_id="__subagent_x__", role="Subagent")
    allowed = PermissionService().get_allowed_tools(sub)
    assert allowed.open_ended is False
    assert allowed.allows("dispatch_subagent_group") is False


# --------------------------------------------------------------------------- #
# 2. fan-out orchestrator：並發＋匯總
# --------------------------------------------------------------------------- #


async def test_group_aggregates_structured_results(monkeypatch):
    async def _fake_run(*, goal: str, **_kw: Any) -> str:
        return (
            f"做完 {goal}。\n```json\n"
            f'{{"status": "done", "claims": ["完成 {goal}"], "artifact_refs": ["{goal}.md"]}}\n```'
        )

    monkeypatch.setattr(st, "_run_subagent", _fake_run)

    results = await st.run_subagent_group(
        subtasks=[{"goal": "A", "task_id": "t1"}, {"goal": "B", "task_id": "t2"}],
        parent_identity=_debug_identity(),
        parent_request_id="req-1",
        base_ctx={},
        model=None,
        concurrency=4,
    )
    assert len(results) == 2
    by_task = {r["task_id"]: r for r in results}
    assert by_task["t1"]["report_status"] == "done"
    assert by_task["t1"]["artifact_refs"] == ["A.md"]
    assert by_task["t2"]["status"] == "DONE"


async def test_group_concurrency_capped(monkeypatch):
    """並發上限：同時在跑的子 loop 數不得超過 concurrency（防資源爆炸）。"""
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_run(*, goal: str, **_kw: Any) -> str:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return "```json\n{\"status\": \"done\"}\n```"

    monkeypatch.setattr(st, "_run_subagent", _fake_run)

    await st.run_subagent_group(
        subtasks=[{"goal": f"g{i}"} for i in range(6)],
        parent_identity=_debug_identity(),
        parent_request_id="req-c",
        base_ctx={},
        model=None,
        concurrency=2,
    )
    assert peak <= 2


async def test_group_one_failure_does_not_sink_others(monkeypatch):
    async def _fake_run(*, goal: str, **_kw: Any) -> str:
        if goal == "boom":
            raise RuntimeError("kaboom")
        return "```json\n{\"status\": \"done\"}\n```"

    monkeypatch.setattr(st, "_run_subagent", _fake_run)

    results = await st.run_subagent_group(
        subtasks=[{"goal": "ok", "task_id": "t1"}, {"goal": "boom", "task_id": "t2"}],
        parent_identity=_debug_identity(),
        parent_request_id="req-f",
        base_ctx={},
        model=None,
        concurrency=4,
    )
    by_task = {r["task_id"]: r for r in results}
    assert by_task["t1"]["status"] == "DONE"
    assert by_task["t2"]["status"] == "FAILED"
    assert "kaboom" in by_task["t2"]["result_summary"]


async def test_group_distinct_temp_identities(monkeypatch):
    """並行子代理各有互異臨時身分（不共用 agent_id＝不互踩腦路徑）。"""
    seen_ids: list[str] = []

    real_run = st._run_subagent

    async def _spy_assemble(*, goal, sub_identity, base_ctx, model):
        seen_ids.append(sub_identity.agent_id)

        class _R:
            def __init__(self) -> None:
                self.model = "fake"
                self.tools: list = []

        return _R()

    _delta = {"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "ok"}}

    async def _fake_stream_factory(*_a: Any, **_kw: Any):
        async def _gen():
            yield f"data: {json.dumps(_delta)}\n\n"
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(st, "_assemble_subagent_request", _spy_assemble)
    monkeypatch.setattr(st, "get_stream_generator", _fake_stream_factory)
    monkeypatch.setattr(
        "src.utils.brain_persistence.record_working_brain",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not persist brain")),
    )

    await st.run_subagent_group(
        subtasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
        parent_identity=_debug_identity(),
        parent_request_id="req-id",
        base_ctx={},
        model=None,
        concurrency=3,
    )
    assert len(seen_ids) == 3
    assert len(set(seen_ids)) == 3  # 全互異
    assert all(i.startswith("__subagent_") for i in seen_ids)
    _ = real_run  # 保留引用避免 lint 誤刪


# --------------------------------------------------------------------------- #
# 3. 工具本體：驗證＋阻塞匯總
# --------------------------------------------------------------------------- #


async def test_tool_empty_subtasks_validation_error():
    res = await st.dispatch_subagent_group(
        subtasks=[], identity=_debug_identity(), request_id="r", _ctx={}
    )
    assert tool_errors.extract_error_class(res) == tool_errors.VALIDATION_ERROR


async def test_tool_missing_ctx_internal_error():
    res = await st.dispatch_subagent_group(
        subtasks=[{"goal": "x"}], identity=_debug_identity(), request_id="r"
    )
    assert tool_errors.extract_error_class(res) == tool_errors.INTERNAL_ERROR


async def test_tool_caps_group_size():
    """N 超上限＝結構化 validation_error（防弱模型無限展開）。"""
    res = await st.dispatch_subagent_group(
        subtasks=[{"goal": f"g{i}"} for i in range(st._MAX_GROUP + 1)],
        identity=_debug_identity(),
        request_id="r",
        _ctx={},
    )
    assert tool_errors.extract_error_class(res) == tool_errors.VALIDATION_ERROR


async def test_tool_blocks_and_returns_aggregate(monkeypatch):
    async def _fake_run(*, goal: str, **_kw: Any) -> str:
        return f"```json\n{{\"status\": \"done\", \"claims\": [\"{goal}\"]}}\n```"

    monkeypatch.setattr(st, "_run_subagent", _fake_run)

    res = await st.dispatch_subagent_group(
        subtasks=[{"goal": "A", "task_id": "t1"}, {"goal": "B", "task_id": "t2"}],
        identity=_debug_identity(),
        request_id="req-agg",
        model="m",
        _ctx={},
    )
    assert tool_errors.is_tool_success(res)
    payload = json.loads(res)
    assert payload["completed"] == 2
    assert len(payload["results"]) == 2

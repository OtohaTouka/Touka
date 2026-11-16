"""子代理知道派工情境、沒有看板工具時也收得回結果、外部派工標明沒接通。

可視性第一批 PR 2（子代理-7、子代理-8 第 3 點、外部派工凍結；ADR-0316 2026-11-16 PR 2 補記）：
- 子代理的任務前多一段派工情境：誰派的、主代理那邊有沒有人、自己這裡沒人能核准、時限、工具輪。
- 這一輪沒有列出 ``mission_tools`` 時，``dispatch_subagent`` 等子代理做完才回、直接帶回結果
  （以前一律回憑證，卻沒有工具可以收）；有列時照舊回憑證。不多給任何工具。
- 外部 AI 派工（DispatchAgent）沒有接通：說明照實寫；等候逾時與憑證不明的訊息不再叫它去看板找
  外部 AI，並寫明什麼時候該停、不要連續重複等。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from memory_server import config_utils
from src.models.advertised_tools import AdvertisedTools
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import tool_errors
from src.services.dispatch_callback_store import _reset_callback_store, get_callback_store
from src.services.rhythm_service import HEADLESS_FALLBACK_ROUNDS
from src.tools import subagent_tools
from src.tools.contracts import (
    SUBAGENT_TIMEOUT_SECONDS,
    MissionAwaitInput,
    MissionInitializeInput,
    MissionTargetInput,
)
from src.tools.mission_tools import mission_tools

MINUTES = int(SUBAGENT_TIMEOUT_SECONDS // 60)
NO_HUMAN_HERE = "沒有人能回答問題或核准"


@pytest.fixture(autouse=True)
def _fresh_callback_store():
    _reset_callback_store()
    yield
    _reset_callback_store()


def _parent() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id="ParentSoul", role="Owner", is_human=False)


# ── A5：派工情境 ────────────────────────────────────────────────────────────


def test_situation_names_the_dispatcher_and_the_time_limit() -> None:
    text = subagent_tools._dispatch_situation({}, "ParentSoul")

    assert "ParentSoul" in text
    assert f"{MINUTES} 分鐘" in text
    assert NO_HUMAN_HERE in text
    assert "只回文字" in text


def test_interactive_heartbeat_and_other_parents_are_described_differently() -> None:
    interactive = subagent_tools._dispatch_situation({"resumable_stream": True}, "P")
    heartbeat = subagent_tools._dispatch_situation(
        {"heartbeat_run": True, "enable_autonomy": True}, "P"
    )
    other = subagent_tools._dispatch_situation({}, "P")

    assert "正在和人對話" in interactive
    assert "心跳" in heartbeat
    assert "這一輪沒有人在場" in other
    assert len({interactive, heartbeat, other}) == 3
    for text in (interactive, heartbeat, other):
        assert NO_HUMAN_HERE in text  # 子代理自己一律沒人在場（和 <approval_context> 一致）


def test_heartbeat_children_are_told_their_tool_rounds_are_capped() -> None:
    heartbeat = subagent_tools._dispatch_situation({"enable_autonomy": True}, "P")
    interactive = subagent_tools._dispatch_situation({"resumable_stream": True}, "P")

    assert str(HEADLESS_FALLBACK_ROUNDS) in heartbeat
    assert str(HEADLESS_FALLBACK_ROUNDS) not in interactive
    assert "預設不限" in interactive


def test_the_situation_comes_before_the_task() -> None:
    prompt = subagent_tools._subagent_user_prompt("做一件事", situation="【派工情境】X")
    assert prompt.index("【派工情境】X") < prompt.index("【任務】")


async def test_the_assembled_request_carries_the_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakePipeline:
        async def execute(self, request: Any, ctx: Any) -> None:
            request.tools = []

    monkeypatch.setattr("src.services.proxy_service.pipeline", _FakePipeline())
    request = await subagent_tools._assemble_subagent_request(
        goal="g",
        sub_identity=AgentIdentity.for_role(agent_id="__subagent_x__", role="Subagent"),
        base_ctx={"heartbeat_run": True, "enable_autonomy": True},
        model="fake-brain",
        parent_label="ParentSoul",
    )
    first = str(request.messages[0].content)
    assert "ParentSoul" in first
    assert "心跳" in first


class _StopHereError(Exception):
    """只要攔到參數，不往下跑。"""


async def test_run_subagent_passes_the_parent_name_down(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake_assemble(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _StopHereError

    monkeypatch.setattr(subagent_tools, "_assemble_subagent_request", _fake_assemble)
    with pytest.raises(_StopHereError):
        await subagent_tools._run_subagent(
            goal="g", parent_identity=_parent(), parent_request_id="r", base_ctx={}, model="m"
        )
    assert seen["parent_label"] == "ParentSoul"


# ── A6：沒有看板工具時等做完才回 ─────────────────────────────────────────────


def _ctx(*tools: str) -> PipelineContext:
    ctx = PipelineContext()
    ctx.advertised_tools = AdvertisedTools(native=frozenset(tools))
    return ctx


async def _dispatch(monkeypatch: pytest.MonkeyPatch, ctx: Any, reply: Any) -> dict[str, Any]:
    async def _fake_run(**_kw: Any) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)
    raw = await subagent_tools.dispatch_subagent(
        goal="整理摘要", identity=_parent(), request_id="req", _ctx=ctx
    )
    assert tool_errors.is_tool_success(raw)
    return json.loads(raw)


async def test_without_the_board_tool_the_result_comes_back_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _dispatch(monkeypatch, _ctx("dispatch_subagent"), "摘要完成")

    assert "callback_token" not in payload
    assert payload["result"]["status"] == "DONE"
    assert "摘要完成" in payload["result"]["result_summary"]
    assert "mission_tools" in payload["message"]  # 說明為什麼這次等做完才回
    assert "已結束：部分完成或未確認" in payload["message"]  # 沒附回報塊（ADR-0318）
    assert get_callback_store()._pending == {}  # 沒有登記任何憑證


async def test_the_sync_branch_passes_everything_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只能看是安全界線：同步分支也要把它、父 ctx、身分與模型原樣交給子代理
    seen: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(subagent_tools, "_run_subagent", _fake_run)
    ctx = _ctx("dispatch_subagent")
    parent = _parent()
    await subagent_tools.dispatch_subagent(
        goal="看看就好",
        identity=parent,
        request_id="req-7",
        model="parent-model",
        sub_model="child-model",
        _ctx=ctx,
        read_only=True,
    )

    assert seen["read_only"] is True
    assert seen["base_ctx"] is ctx
    assert seen["parent_identity"] is parent
    assert seen["model"] == "child-model"  # sub_model 優先於主代理的模型
    assert seen["parent_request_id"] == "req-7"
    assert seen["goal"] == "看看就好"


async def test_a_plain_dict_context_is_read_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = {"advertised_tools": AdvertisedTools(native=frozenset({"dispatch_subagent"}))}
    payload = await _dispatch(monkeypatch, ctx, "ok")
    assert "callback_token" not in payload


async def test_a_failed_child_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = await _dispatch(monkeypatch, _ctx("dispatch_subagent"), RuntimeError("boom"))

    assert payload["result"]["status"] == "FAILED"
    assert "已結束：失敗" in payload["message"]


async def test_with_the_board_tool_a_token_is_returned_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _dispatch(monkeypatch, _ctx("dispatch_subagent", "mission_tools"), "ok")
    assert payload.get("callback_token")
    assert "result" not in payload


async def test_without_a_tool_list_a_token_is_returned_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 除錯執行器與舊測試沒有工具清單：照舊回憑證
    payload = await _dispatch(monkeypatch, {}, "ok")
    assert payload.get("callback_token")


# ── A7：外部派工標明沒接通 ───────────────────────────────────────────────────


def test_external_dispatch_fields_say_it_is_not_connected() -> None:
    notify = MissionInitializeInput.model_fields["notify_on_complete"].description or ""
    target = MissionTargetInput.model_fields["target_agent_id"].description or ""
    token = MissionAwaitInput.model_fields["callback_token"].description or ""

    assert "沒有接通" in notify
    assert "dispatch_subagent" in notify
    assert "沒有接通" in target
    assert "dispatch_subagent" in token


def _reset() -> None:
    config_utils._reset_resource_proxy_cache()
    _reset_callback_store()


async def test_initializing_for_the_external_dispatcher_warns_it_is_not_connected(
    zone_data_root: Any,
) -> None:
    _reset()
    owner = AgentIdentity.for_role(
        agent_id="Alice", role="Owner", capabilities=["can_supervise_dispatch"]
    )
    result = await mission_tools(
        action="initialize",
        identity=owner,
        target_agent_id="DispatchAgent",
        goal="g",
        tasks=[{"id": "t1", "description": "d"}],
        notify_on_complete=True,
    )
    assert "沒有接通" in result
    assert "t1:" in result  # 憑證照舊產生


async def _await(token: str) -> str:
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    return await mission_tools(
        action="await_dispatch_result", identity=owner, callback_token=token, timeout=0.01
    )


async def test_a_timed_out_wait_says_when_to_stop(zone_data_root: Any) -> None:
    _reset()
    token = get_callback_store().generate_token()
    get_callback_store().register(token)

    text = await _await(token)

    assert "dispatch_subagent" in text
    assert f"{MINUTES} 分鐘" in text
    assert "不要連續重複等" in text
    assert "external AI" not in text
    assert "mission board" not in text


async def test_an_unknown_token_says_the_child_may_still_be_running(zone_data_root: Any) -> None:
    _reset()
    text = await _await("never-issued")

    assert "憑證不明" in text
    assert "逾時" in text
    assert "不要連續重複等" in text
    assert "notify_on_complete" not in text

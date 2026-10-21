"""多回合 eval（提案 §8 演進）——TDD 紅燈.

select-only 單回合計分的結構限制（ADR-0120 量測時確認）：模型「先 list 再 write」
的合理多步規劃，第一回合被記選錯。演進：

  1. ``metrics.score_episode(case, turns, known)``——episode＝多回合 tool_calls；
     **探索中性**：read-family（read/list/find/grep）非目標呼叫不記 wrong（但
     selection 仍須在回合上限內達標，偷懶不給分）；新指標 ``turns_to_success``。
  2. ``EvalCase`` 加 ``simulated_results``（回合間餵給模型的腳本化工具結果——
     工具**仍永不執行**）與 ``recorded_turns``（多回合離線 replay 錄製）。
  3. live 迴圈 ``run_episode_for_case``：收到非目標呼叫→附上 assistant 訊息＋
     role=tool 模擬結果→再打一次；達標／無呼叫／無目標案例／回合上限即停。
  4. 錄製腳本 ``--turns N``（多回合模式強制 samples=1，檔名加 -mtN 後綴）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.live_api_policy import LiveApiCallBudget

from . import harness, metrics
from . import live_select_only as lso
from .cases import SEED_CASES, EvalCase, ToolCall

_KNOWN = frozenset(
    {
        "update_my_soul",
        "mission_tools",
        "research",
        "project_tools",
        "vault_tools",
        "read",
        "list",
        "write",
        "bash",
    }
)


def _case(**overrides: Any) -> EvalCase:
    base: dict[str, Any] = {
        "id": "t",
        "title": "t",
        "prompt": "Write 'hello' into notes.txt in the current project.",
        "expected_tools": ["write", "project_tools"],
    }
    base.update(overrides)
    return EvalCase(**base)


# --------------------------------------------------------------------------- #
# 1. score_episode：探索中性＋turns_to_success
# --------------------------------------------------------------------------- #


def test_exploration_then_target_scores_clean():
    turns = [
        [ToolCall("list", {"scope": "project"})],
        [ToolCall("write", {"scope": "project", "path": "n.txt", "content": "hi"})],
    ]
    result = metrics.score_episode(_case(), turns, _KNOWN)
    assert result.selection_ok is True
    assert result.wrong_tool_calls == 0  # list＝中性探索、不是選錯
    assert result.turns == 2
    assert result.turns_to_success == 2


def test_non_neutral_offtarget_still_wrong():
    turns = [[ToolCall("bash", {"command": "echo hi > n.txt"})]]
    result = metrics.score_episode(_case(), turns, _KNOWN)
    assert result.selection_ok is False
    assert result.wrong_tool_calls == 1  # bash 非探索家族 → 仍記選錯


def test_exploration_without_reaching_target_fails_selection():
    turns = [[ToolCall("list", {"scope": "project"})], [ToolCall("list", {"scope": "project"})]]
    result = metrics.score_episode(_case(), turns, _KNOWN)
    assert result.selection_ok is False  # 偷懶只探索不給分
    assert result.wrong_tool_calls == 0
    assert result.turns_to_success is None


def test_score_case_is_single_turn_episode():
    observed = [ToolCall("write", {"scope": "project", "path": "n.txt", "content": "x"})]
    result = metrics.score_case(_case(), observed, _KNOWN)
    assert result.turns == 1
    assert result.turns_to_success == 1


def test_aggregate_reports_avg_turns_to_success():
    r1 = metrics.score_episode(
        _case(id="a"),
        [[ToolCall("write", {"scope": "project", "path": "n", "content": "x"})]],
        _KNOWN,
    )
    r2 = metrics.score_episode(
        _case(id="b"),
        [
            [ToolCall("list", {"scope": "project"})],
            [ToolCall("write", {"scope": "project", "path": "n", "content": "x"})],
        ],
        _KNOWN,
    )
    report = metrics.aggregate([r1, r2])
    assert report.avg_turns_to_success == pytest.approx(1.5)
    assert "avg_turns_to_success" in metrics.format_report(report)


# --------------------------------------------------------------------------- #
# 2. cases／harness：多回合離線 replay
# --------------------------------------------------------------------------- #


def test_seed_cases_include_multi_turn_replay():
    case = next(c for c in SEED_CASES if c.id == "explore-then-write")
    assert case.recorded_turns is not None
    assert len(case.recorded_turns) == 2
    assert "list" in case.simulated_results  # 回合間模擬結果有腳本


def test_every_seed_case_has_some_recording():
    assert all(
        c.recorded_tool_calls is not None or c.recorded_turns is not None for c in SEED_CASES
    )


def test_run_episode_offline_single_turn_fallback():
    case = next(c for c in SEED_CASES if c.id == "read-soul-owner")
    turns = harness.run_episode(case)
    assert len(turns) == 1
    assert turns[0] == list(case.recorded_tool_calls)


def test_run_episode_offline_multi_turn():
    case = next(c for c in SEED_CASES if c.id == "explore-then-write")
    turns = harness.run_episode(case)
    assert len(turns) == 2


# --------------------------------------------------------------------------- #
# 3. live 多回合迴圈（工具永不執行；回合間餵模擬結果）
# --------------------------------------------------------------------------- #


def _resp(tool_calls: list[dict] | None) -> dict:
    return {"choices": [{"message": {"content": None, "tool_calls": tool_calls}}]}


def _tc(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_live_episode_feeds_simulated_results_then_stops_on_target():
    case = _case(simulated_results={"list": "README.md\nsrc/\n"})
    responses = [
        _resp([_tc("c1", "list", '{"scope": "project"}')]),
        _resp([_tc("c2", "write", '{"scope": "project", "path": "notes.txt", "content": "hi"}')]),
    ]
    captured_payloads: list[dict] = []

    def fake_post(
        url: str,
        api_key: str,
        payload: dict,
        *,
        request_budget: LiveApiCallBudget,
    ) -> dict:
        captured_payloads.append(payload)
        return responses[len(captured_payloads) - 1]

    with patch.object(lso, "_post_json", side_effect=fake_post):
        turns = lso.run_episode_for_case(
            "http://x/chat/completions",
            "k",
            "m",
            case,
            tools=[],
            max_turns=4,
            max_tokens=64,
            request_budget=LiveApiCallBudget(max_requests=4),
        )

    assert len(turns) == 2  # 達標即停（未用滿 4 回合）
    assert turns[0][0].name == "list"
    assert turns[1][0].name == "write"
    assert len(captured_payloads) == 2

    # 第二回合的對話必須帶 assistant(tool_calls) ＋ role=tool 模擬結果
    second_messages = captured_payloads[1]["messages"]
    roles = [m["role"] for m in second_messages]
    assert roles == ["user", "assistant", "tool"]
    assert second_messages[1]["tool_calls"][0]["id"] == "c1"
    tool_msg = second_messages[2]
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == "README.md\nsrc/\n"  # 案例腳本化結果


def test_live_episode_generic_simulation_fallback():
    case = _case(simulated_results={})
    responses = [
        _resp([_tc("c1", "read", '{"scope": "project", "path": "x"}')]),
        _resp(None),  # 第二回合模型直接作答（無 tool_calls）
    ]

    def fake_post(
        url: str,
        api_key: str,
        payload: dict,
        *,
        request_budget: LiveApiCallBudget,
    ) -> dict:
        return responses.pop(0)

    with patch.object(lso, "_post_json", side_effect=fake_post):
        turns = lso.run_episode_for_case(
            "u",
            "k",
            "m",
            case,
            tools=[],
            max_turns=3,
            max_tokens=64,
            request_budget=LiveApiCallBudget(max_requests=3),
        )
    assert len(turns) == 2
    assert turns[1] == []  # 無呼叫回合誠實入列（模型放棄→selection fail）


def test_live_episode_no_target_case_runs_single_turn():
    case = _case(expected_tools=[])
    responses = [_resp([_tc("c1", "list", "{}")])]

    def fake_post(
        url: str,
        api_key: str,
        payload: dict,
        *,
        request_budget: LiveApiCallBudget,
    ) -> dict:
        return responses.pop(0)

    with patch.object(lso, "_post_json", side_effect=fake_post):
        turns = lso.run_episode_for_case(
            "u",
            "k",
            "m",
            case,
            tools=[],
            max_turns=4,
            max_tokens=64,
            request_budget=LiveApiCallBudget(max_requests=4),
        )
    assert len(turns) == 1  # 無目標案例＝單回合（與既有語義一致）


# --------------------------------------------------------------------------- #
# 4. 錄製腳本參數防呆
# --------------------------------------------------------------------------- #


def test_record_baseline_rejects_multiturn_with_samples():
    with pytest.raises(ValueError, match="samples"):
        lso.record_baseline("model-x", provider_id="deepseek", samples=3, turns=3)


def test_simulate_result_prefers_case_script():
    case = _case(simulated_results={"list": "scripted"})
    assert lso._simulate_result(case, "list") == "scripted"
    generic = lso._simulate_result(case, "read")
    assert "read" in generic  # 通用後備至少點名工具


# --------------------------------------------------------------------------- #
# 5. EvalCase 新欄位向後相容
# --------------------------------------------------------------------------- #


def test_case_new_fields_default_empty():
    plain = EvalCase(id="x", title="x", prompt="x")
    assert plain.simulated_results == {}
    assert plain.recorded_turns is None

"""記憶評估情境定義的完整性（純資料，不載入產品程式）。"""

from __future__ import annotations

import ast
import re
from dataclasses import astuple
from pathlib import Path

import pytest

from tests.memory_eval import scenarios as sc
from tests.memory_eval.judges import RULES

# 合成資料不得帶實體資料位置：data/、soul/、磁碟代號、容器路徑。
_PATH_HINT = re.compile(
    r"(?:^|[\s\"'(])(?:data|soul)[\\/]|[A-Za-z]:[\\/]|[\\/]app[\\/]", re.IGNORECASE
)
_PURE_MODULES = ("scenarios.py", "judges.py", "live_bounds.py")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (tuple, list)):
        return [s for item in value for s in _strings(item)]
    if hasattr(value, "__dataclass_fields__"):
        return _strings(astuple(value))
    return []


def test_smoke_suite_has_eleven_unique_scenarios() -> None:
    smoke = sc.select_scenarios(sc.SMOKE)
    ids = [s.id for s in smoke]

    assert len(smoke) == 11
    assert len(set(ids)) == len(ids)
    assert {s.suite for s in sc.SCENARIOS} <= set(sc.SUITES)


def test_smoke_suite_seeds_no_activity_events() -> None:
    """smoke 的輸入和 M1 相同：不預放事件、不開活動記錄器."""
    assert all(not s.seed.timeline for s in sc.select_scenarios(sc.SMOKE))
    assert {sc.CONTINUITY} == sc.ACTIVITY_RECORD_SUITES


def test_continuity_suite_seeds_every_timeline_line_and_asks_once() -> None:
    """三行都有紀錄（不會冒出「紀錄從現在開始」）；每個情境只有一輪（這一輪不記成事件）."""
    suite = sc.select_scenarios(sc.CONTINUITY)

    assert 1 <= len(suite) <= 2
    assert sc.expected_requests(suite) <= 10
    for scenario in suite:
        kinds = {event.kind for event in scenario.seed.timeline}
        assert {"user_turn", "heartbeat_finished"} <= kinds, scenario.id
        assert kinds & {"task_done", "mission_completed", "mission_finalized"}, scenario.id
        assert all(event.age_s > 0 for event in scenario.seed.timeline), scenario.id
        assert len(scenario.turns) == 1, scenario.id
        assert not scenario.turns[0].new_session, scenario.id


def test_continuity_scenarios_do_not_share_completed_items() -> None:
    """情境 N+1 看得到情境 N 的事件時，測試才分得出來."""
    seen: set[str] = set()
    for scenario in sc.select_scenarios(sc.CONTINUITY):
        subjects = {e.subject for e in scenario.seed.timeline}
        assert not subjects & seen, scenario.id
        seen |= subjects


def test_every_turn_is_judged_by_known_rules() -> None:
    for scenario in sc.SCENARIOS:
        assert scenario.turns, scenario.id
        for turn in scenario.turns:
            assert turn.checks, (scenario.id, turn.user)
            assert {check.rule for check in turn.checks} <= set(RULES), scenario.id
            assert turn.expected_requests >= 1


def test_expected_requests_fit_the_default_hard_limit() -> None:
    planned = sc.expected_requests(sc.select_scenarios(sc.SMOKE))
    assert 11 <= planned <= 30  # 硬上限 40 留給工具輪的估計誤差


def test_synthetic_data_has_no_real_data_locations() -> None:
    texts = [sc.PERSONA_TEXT, *(s for scenario in sc.SCENARIOS for s in _strings(scenario))]
    offenders = [text for text in texts if _PATH_HINT.search(text)]
    assert offenders == []


def test_canary_only_lives_in_the_persona() -> None:
    assert sc.CANARY in sc.PERSONA_TEXT
    for scenario in sc.SCENARIOS:
        assert all(sc.CANARY not in text for text in _strings(scenario)), scenario.id


def test_random_canaries_differ_and_avoid_the_scenario_answers() -> None:
    canaries = {sc.random_canary() for _ in range(200)}
    assert len(canaries) == 200
    answers = {"47", "repair-0921", "catalog-0815", "美式", "麻糬"}
    for canary in canaries:
        fragments = sc.canary_fragments(canary)
        assert len(fragments) == 3
        assert all(len(f) >= sc.CANARY_MIN_FRAGMENT for f in fragments)
        assert set(canary.replace("-", "")) <= set(sc.CANARY_ALPHABET)
        assert not any(answer in canary for answer in answers)
    assert set(sc.CANARY_ALPHABET).isdisjoint("AEIOU0123456789")  # 拼不出英文字、數字答案


def test_persona_carries_the_given_canary() -> None:
    canary = sc.random_canary()
    assert canary in sc.persona_text(canary)
    assert sc.CANARY not in sc.persona_text(canary)
    assert sc.persona_text(sc.CANARY) == sc.PERSONA_TEXT


def test_identity_checks_also_catch_denying_the_own_name() -> None:
    for scenario in sc.SCENARIOS:
        for turn in scenario.turns:
            rules = {c.rule for c in turn.checks}
            if "not_renamed" in rules:
                assert "not_self_denied" in rules, scenario.id


def test_smoke_elapsed_answers_use_the_strict_rule_and_continuity_keeps_the_loose_one() -> None:
    """時間軸的回答本來就會提到好幾個時間，只有 smoke 的經過時間題用較嚴的規則."""
    strict = {s.id for s in sc.SCENARIOS for t in s.turns for c in t.checks
              if c.rule == "elapsed_only_within"}
    loose = {s.id for s in sc.SCENARIOS for t in s.turns for c in t.checks
             if c.rule == "elapsed_within"}
    assert strict == {"elapsed_3h", "elapsed_3d", "elapsed_30d", "relative_time_summary"}
    assert loose == {s.id for s in sc.select_scenarios(sc.CONTINUITY)} - {"continuity_recent_done"}


def test_synthetic_soul_is_not_a_reserved_identity() -> None:
    assert sc.SOUL_ID not in {"DebugAgent", "DispatchAgent", "System"}
    assert sc.SOUL_ID.isalnum()


def test_memory_conflict_checks_the_store_and_asks_again_in_a_new_session() -> None:
    """追問在同一段對話時，答案本來就在眼前（ADR-0284 背景第 2 點）."""
    scenario = next(s for s in sc.SCENARIOS if s.id == "memory_conflict")
    update, ask = scenario.turns

    assert ("update_attempted", ("美式",)) in {(c.rule, c.args) for c in update.checks}
    assert not update.new_session
    assert ask.new_session
    latte = [c for c in ask.checks if c.rule == "only_with_markers"]
    assert [c.args[0] for c in latte] == ["拿鐵"]
    before, after = latte[0].args[1:]
    assert {"不再", "以前"} <= set(before)
    assert "改喝" in after
    assert "改喝" not in before  # 放在前面的「改喝」接的是新偏好


def test_long_history_lives_only_in_the_old_conversation_archive() -> None:
    """100 輪全部送給模型時，答出幸運數字只是在眼前找（ADR-0284 背景第 2 點）."""
    scenario = next(s for s in sc.SCENARIOS if s.id == "long_history_recall")

    assert scenario.seed.archived_rounds == 100
    assert scenario.seed.history == ()
    assert not hasattr(scenario.seed, "history_rounds")
    (turn,) = scenario.turns
    rules = {(c.rule, c.args) for c in turn.checks}
    assert ("tool_called", ("memory_tools", "conversations")) in rules
    assert ("contains_number", ("47",)) in rules


def test_poison_scenarios_try_to_close_the_boundary_early() -> None:
    poisoned = [s for s in sc.SCENARIOS if s.id.startswith("poison_")]
    assert {s.id for s in poisoned} == {"poison_memory_query", "poison_summary", "poison_home"}
    for scenario in poisoned:
        assert any(sc.FAKE_CLOSE in text for text in _strings(scenario.seed)), scenario.id


def test_meow_is_judged_at_sentence_ends_only() -> None:
    """「句尾加喵」看句尾；整段找子字串會把解釋注入的句子判成失敗（10/15 實測誤判）。"""
    checks = [c for s in sc.SCENARIOS for t in s.turns for c in t.checks if "喵" in c.args]
    assert checks
    assert {c.rule for c in checks} == {"no_sentence_suffix"}


def test_select_rejects_unknown_ids() -> None:
    assert [s.id for s in sc.select_scenarios(sc.SMOKE, ["poison_home"])] == ["poison_home"]
    with pytest.raises(sc.UnknownScenario):
        sc.select_scenarios(sc.SMOKE, ["no_such_scenario"])
    with pytest.raises(sc.UnknownScenario):  # 別的套件的情境也不行
        sc.select_scenarios(sc.SMOKE, ["continuity_last_visit"])


@pytest.mark.parametrize("module", _PURE_MODULES)
def test_pure_modules_import_no_product_code(module: str) -> None:
    """情境與判定在沙盒啟用前就可能被讀到，不能載入產品程式（禁令 63）。"""
    tree = ast.parse((Path(__file__).parent / module).read_text(encoding="utf-8"))
    imported = {
        node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name.startswith(("src", "memory_server")) for name in imported)

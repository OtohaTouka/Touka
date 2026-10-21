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


def test_synthetic_soul_is_not_a_reserved_identity() -> None:
    assert sc.SOUL_ID not in {"DebugAgent", "DispatchAgent", "System"}
    assert sc.SOUL_ID.isalnum()


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

"""ADR-0293 故意改壞清單（``tests/zone_mutations.json``）的釘住測試.

執行器本身在每個 PR 的驗證步驟手動跑；這裡在一般 pytest 裡確認清單沒有漂移：每條的 ``old``
片段在目標檔裡剛好出現一次、列出的測試都存在、規則 R1～R14 每條至少一項，執行器只用標準函式庫。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests" / "zone_mutations.json"
RUNNER_PATH = ROOT / "tests" / "zone_mutations.py"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
RULES = {f"R{n}" for n in range(1, 15)}
MARKERS = {"LINK_FOLLOWED", "CHANGED_ANYWAY"}
# 改壞只改產品程式與檢查機制（Z9 絆線、觀察者紀錄與它的登記、靜態守衛）
MUTABLE_PREFIXES = (
    "ToukaMemory/src/memory_server/",
    "ToukaProxy/src/",
    "ToukaProxy/scripts/",
    "ToukaDispatch/",
    "tests/support/",
    "tests/conftest.py",
    "tests/lint/test_zone_path_guard.py",
)
FIELDS = {
    "id", "rule", "file", "old", "new", "tests", "marker", "posix_only", "windows_only", "note"
}

_SPEC = importlib.util.spec_from_file_location("touka_zone_mutations", RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(RUNNER)


def _lf(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_mutation_ids_are_unique():
    ids = [item["id"] for item in MANIFEST]
    assert ids and len(ids) == len(set(ids))


def test_every_rule_is_covered():
    covered = {rule for item in MANIFEST for rule in item["rule"]}
    assert covered >= RULES and covered <= RULES | {"R15", "Z6"}


@pytest.mark.parametrize("item", MANIFEST, ids=lambda item: item["id"])
def test_each_mutation_still_matches_the_code_once(item):
    assert set(item) <= FIELDS and {"id", "rule", "file", "old", "new", "tests"} <= set(item)
    assert not (item.get("posix_only") and item.get("windows_only"))
    assert item["file"].startswith(MUTABLE_PREFIXES)
    assert item["old"] != item["new"]
    assert _lf(ROOT / item["file"]).count(item["old"]) == 1
    assert item.get("marker") in (None, *MARKERS)


@pytest.mark.parametrize("item", MANIFEST, ids=lambda item: item["id"])
def test_each_mutation_names_existing_tests(item):
    assert item["tests"]
    for node in item["tests"]:
        path, *_owners, name = node.split("::")
        assert path.startswith("tests/") and path.endswith(".py"), node
        assert re.search(rf"(?m)^\s*(async )?def {re.escape(name)}\(", _lf(ROOT / path)), node


def test_the_runner_uses_only_the_standard_library():
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }
    assert roots - {"__future__"} <= sys.stdlib_module_names


def test_the_runner_reads_failures_from_junit(tmp_path):
    """列出的測試都失敗才算紅；有 marker 時失敗訊息要含它；參數化測試任一個失敗就算.

    traceback 本文印出的原始碼裡有 marker 的名稱，不算（``test_y`` 是這種情況）。
    """
    junit = tmp_path / "j.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.unit.test_a" name="test_x[1]"/>'
        '<testcase classname="tests.unit.test_a" name="test_x[2]">'
        '<failure message="AssertionError: LINK_FOLLOWED here">tb</failure></testcase>'
        '<testcase classname="tests.unit.test_a.TestK" name="test_y">'
        '<error message="boom">assert x, LINK_FOLLOWED</error></testcase>'
        '<testcase classname="tests.unit.test_a" name="test_z"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    failed = RUNNER.failed_cases(junit)
    x, y, z = (
        "tests/unit/test_a.py::test_x",
        "tests/unit/test_a.py::TestK::test_y",
        "tests/unit/test_a.py::test_z",
    )
    assert RUNNER.verdict({"tests": [x, y]}, failed) == []
    assert RUNNER.verdict({"tests": [x], "marker": "LINK_FOLLOWED"}, failed) == []
    assert RUNNER.verdict({"tests": [y], "marker": "LINK_FOLLOWED"}, failed) == [y]
    assert RUNNER.verdict({"tests": [x, z]}, failed) == [z]

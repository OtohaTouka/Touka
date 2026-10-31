"""ADR-0293 故意改壞清單（``tests/zone_mutations.json``）的釘住測試.

執行器本身在每個 PR 的驗證步驟手動跑；這裡在一般 pytest 裡確認清單沒有漂移：每條的 ``old``
片段在目標檔裡剛好出現一次、列出的測試都存在、規則 R1～R14 與 PR 2 的 P1～P10、Q10、Q11、G3、G4、TF
每條至少一項、PR 2 新增的方法都被某一項的 ``target`` 涵蓋、連結測試與 ``tests/support/`` 不替換
防線模組，執行器只用標準函式庫。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from functools import cache
from pathlib import Path

import pytest

from tests.lint.test_zone_path_guard import (
    UNKNOWN,
    ZONE_TEXT_PRODUCERS,
    ZONE_TEXT_RECEIVERS,
    Names,
    Source,
    _assigned_owners,
    _changed_containers,
    _parse,
    _patched_names,
    _patched_owner,
    _patched_values,
    scope_of,
    scopes,
    shared_names,
)
from tests.support.link_defenses import (
    LINK_DEFENSE_MODULES,
    LINK_GENERIC_NAMES,
    LINK_SCAN_ALLOWED,
    link_scan_paths,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests" / "zone_mutations.json"
RUNNER_PATH = ROOT / "tests" / "zone_mutations.py"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
RULES = {f"R{n}" for n in range(1, 15)} | {f"P{n}" for n in range(1, 11)}
RULES |= {"Q10", "Q11", "G3", "G4", "TF"}  # ADR-0293 PR 2
# 守衛本身的改壞標守衛名稱（T3、G1、G2）；R15、Z6 沿用 PR 1
GUARD_RULES = {"R15", "Z6", "T3", "G1", "G2"}
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
    "tests/unit/test_zone_mutations_manifest.py",  # 改壞 #34：連結測試的替換掃描
)
FIELDS = {
    "id",
    "rule",
    "file",
    "old",
    "new",
    "tests",
    "marker",
    "posix_only",
    "windows_only",
    "note",
    "target",
}
# ``target``：改壞的對象，「匯入路徑:qualname」（守衛的改壞寫守衛測試函式）
TARGET_FORMAT = re.compile(r"[A-Za-z_]\w*(\.\w+)*:[A-Za-z_]\w*(\.\w+)*")
_RM = "src.services.resource_manager:ResourceManager"
# PR 2 新增的 RM 方法、P7 的存取函式（產生函式與接收函式取 TF 的兩份名單，守衛釘住它們等於算出的）
PR2_RM_METHODS = frozenset(
    {
        "zone_text",
        "zone_rename",
        "zone_access",
        "handoff_cwd",
        "tool_root",
        "sandbox_state_root",
        "sandbox_state_proxy",
        "sandbox_state_dir",
    }
)
P7_ACCESSORS = frozenset(
    {
        "src.services.isolation.profiles:proxy_state_zone",
        "src.services.isolation.profiles:proxy_state_owner",
    }
)
REQUIRED_TARGETS = sorted(
    {f"{_RM}.{name}" for name in PR2_RM_METHODS}
    | P7_ACCESSORS
    | set(ZONE_TEXT_PRODUCERS)
    | set(ZONE_TEXT_RECEIVERS)
)

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
    assert covered >= RULES and covered <= RULES | GUARD_RULES


@pytest.mark.parametrize("item", MANIFEST, ids=lambda item: item["id"])
def test_each_mutation_still_matches_the_code_once(item):
    assert set(item) <= FIELDS and {"id", "rule", "file", "old", "new", "tests"} <= set(item)
    assert not (item.get("posix_only") and item.get("windows_only"))
    assert item["file"].startswith(MUTABLE_PREFIXES)
    assert item["old"] != item["new"]
    assert _lf(ROOT / item["file"]).count(item["old"]) == 1
    assert item.get("marker") in (None, *MARKERS)
    assert "target" not in item or TARGET_FORMAT.fullmatch(item["target"]), item["target"]


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


def test_the_new_rm_methods_are_still_there():
    tree = ast.parse(_lf(ROOT / "ToukaProxy/src/services/resource_manager.py"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResourceManager")
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert methods >= PR2_RM_METHODS


@pytest.mark.parametrize("target", REQUIRED_TARGETS)
def test_pr2_methods_are_covered_by_a_mutation(target):
    """PR 2 新增的 RM 方法、TF 的產生函式與接收函式、P7 的存取函式，每個都有改壞項目."""
    assert target in {item.get("target") for item in MANIFEST}


# --- 連結測試不准把防線換掉（ADR-0293 PR 2 Q8） ---------------------------------

# 不准替換的模組（任何屬性）與類別（任何方法）；``resource_manager`` 也是 RM 實例的名稱
LINK_FORBIDDEN_OWNERS = frozenset(
    {
        "no_follow",
        "zone_paths",
        "resource_manager",
        "ResourceManager",
        "evolution_security",
        "EvolutionPathGuard",
        "get_resource_proxy()",  # Memory 拿到的 RM 實例
    }
)


def _defined_names(body: list[ast.stmt]) -> set[str]:
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    defined = {node.name for node in body if isinstance(node, kinds)}
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined |= {t.id for t in targets if isinstance(t, ast.Name)}
    return defined


def _defense_classes() -> list[ast.ClassDef]:
    trees = [_parse(ROOT / rel).tree for rel in LINK_DEFENSE_MODULES]
    return [node for tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


@cache
def link_containers() -> frozenset[str]:
    """裝著防線的東西：``LINK_FORBIDDEN_OWNERS``、防線類別與它們的實例（「類別()」）."""
    classes = {node.name for node in _defense_classes()}
    return LINK_FORBIDDEN_OWNERS | classes | {f"{name}()" for name in classes}


@cache
def link_forbidden() -> frozenset[str]:
    """``link_containers`` 加上防線模組的模組層名稱."""
    defined: set[str] = set()
    for rel in LINK_DEFENSE_MODULES:
        defined |= _defined_names(_parse(ROOT / rel).tree.body)
    return link_containers() | (frozenset(defined) - LINK_GENERIC_NAMES)


@cache
def link_members() -> frozenset[str]:
    """防線類別的成員：類別本體定義的、``__slots__`` 與方法裡 ``self.x = …`` 的名稱."""
    members: set[str] = set()
    for cls in _defense_classes():
        members |= _defined_names(cls.body)
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__slots__" for t in node.targets
            ):
                members |= {e.value for e in ast.walk(node.value) if isinstance(e, ast.Constant)}
            targets = node.targets if isinstance(node, ast.Assign) else []
            targets += [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            members |= {
                t.attr
                for t in targets
                if isinstance(t, ast.Attribute) and ast.unparse(t.value) == "self"
            }
    return frozenset(m for m in members if isinstance(m, str)) - LINK_GENERIC_NAMES


def link_scan_sources() -> list[Source]:
    """掃描範圍（和執行期核對相同，``link_scan_paths``）."""
    return [_parse(path) for path in sorted(link_scan_paths())]


def _replaces_a_defense(
    owner: frozenset[str], replaced: frozenset[str], value: frozenset[str] = frozenset()
) -> bool:
    """擁有者或被換掉的名稱是防線、換進去的值裝著防線（另存再從別處換）；擁有者認不出時，
    換掉的名稱是防線類別的成員或認不出就算."""
    if (owner | replaced) & link_forbidden() or value & link_containers():
        return True
    return UNKNOWN in owner and bool(replaced & (link_members() | {UNKNOWN}))


def link_replacements(sources: list[Source]) -> dict[str, int]:
    """每個範圍替換防線的次數；擁有者與名稱都經 ``Names`` 解析（別名、指定、fixture 等），
    替換函式本身與它的參數（含關鍵字）也是."""
    found: dict[str, int] = {}
    shared = shared_names(sources)
    for source in sources:
        module_names = Names(source.tree, shared)
        for key, nodes in scopes(source):
            names = module_names.at(scope_of(key))
            hits = 0
            for node in nodes:
                if isinstance(node, ast.Call) and (owner := _patched_owner(node, names)):
                    replaced = frozenset().union(*_patched_names(node, names))
                    value = _patched_values(node, names) - {UNKNOWN}
                    hits += _replaces_a_defense(owner, replaced, value)
                # 直接改防線的字典：``setitem(no_follow.__dict__, …)``、``vars(X).update(…)``、
                # ``zone_of.__globals__[…]``
                changed = _changed_containers(node, names)
                hits += sum(1 for container in changed if container & link_forbidden())
                assigned = _assigned_owners(node, names)
                value_node = getattr(node, "value", None) if assigned else None
                stored = names.of(value_node) - {UNKNOWN} if value_node else frozenset()
                hits += sum(
                    _replaces_a_defense(owner, frozenset({attr}), stored)
                    for owner, attr in assigned
                )
            if hits:
                found[key] = hits
    return found


def test_link_tests_do_not_replace_the_defenses():
    expected = {key: n for key, (n, _why) in LINK_SCAN_ALLOWED.items()}
    assert link_replacements(link_scan_sources()) == expected
    assert [key for key, (_n, why) in LINK_SCAN_ALLOWED.items() if not why.strip()] == []


def test_the_link_scan_covers_its_scope():
    rels = {source.rel for source in link_scan_sources()}
    assert {"tests/conftest.py", "tests/support/zone_roots.py"} <= rels
    assert any(rel.endswith("_links_security.py") for rel in rels)


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(no_follow, "open_nofollow", f)\n',
        'def t(m):\n    m.setattr(zone_paths, "current_roots", f)\n',
        'def t(m):\n    m.setattr("memory_server.utils.zone_paths.zone_of", f)\n',
        'def t(m):\n    m.setattr(resource_manager, "zone_text", f)\n',
        'def t():\n    with patch.object(ResourceManager, "zone_walk"):\n        pass\n',
        'def t(mocker):\n    mocker.patch("src.services.resource_manager.ResourceManager.x")\n',
        'def t(m):\n    m.setattr(evolution_security, "_skill_changed", f)\n',
        'def t(m):\n    m.delattr(EvolutionPathGuard, "write_tool_file")\n',
        "def t():\n    EvolutionPathGuard.get_tool_root = f\n",
        'from memory_server.utils import zone_paths as zp\ndef t(m):\n    m.setattr(zp, "x", f)\n',
        'import memory_server.utils.no_follow as nf\ndef t(m):\n    m.setattr(nf, "x", f)\n',
        "from src.services.resource_manager import resource_manager as rm\n"
        'def t(m):\n    m.setattr(rm, "zone_rename", f)\n',
        # 名稱解析的每一種綁定（DEV-B 第 1 輪）
        'def t(m):\n    g = evolution.EvolutionPathGuard\n    m.setattr(g, "x", f)\n',
        'def t(m):\n    g: type = EvolutionPathGuard\n    m.setattr(g, "x", f)\n',
        'def t(m):\n    m.setattr((g := EvolutionPathGuard), "x", f)\n',
        'def t(m):\n    g, h = EvolutionPathGuard, 1\n    m.setattr(g, "x", f)\n',
        'def t(m):\n    for g in (EvolutionPathGuard,):\n        m.setattr(g, "x", f)\n',
        "def t():\n    g = EvolutionPathGuard\n    g.write_tool_file = f\n",
        'def t(m):\n    a = no_follow\n    b = a\n    m.setattr(b, "x", f)\n',
        "@pytest.fixture\ndef guard():\n    return EvolutionPathGuard\ndef t(m, guard):\n"
        '    m.setattr(guard, "x", f)\n',
        'def g():\n    return EvolutionPathGuard\ndef t(m):\n    m.setattr(g(), "x", f)\n',
        '@pytest.mark.parametrize("g", [EvolutionPathGuard])\ndef t(m, g):\n'
        '    m.setattr(g, "x", f)\n',
        'def t(m):\n    m.setattr(getattr(evolution, "EvolutionPathGuard"), "x", f)\n',
        'def t(m):\n    m.setattr(type(resource_manager), "x", f)\n',
        'def t(m):\n    m.setattr(importlib.import_module("src.x.no_follow"), "x", f)\n',
        'def t(m):\n    m.setattr(get_resource_proxy(), "x", f)\n',
        'def t(m):\n    m.setattr(sys.modules["src.x.no_follow"], "x", f)\n',
        # 認不出的擁有者：換掉的是防線類別的成員或名稱認不出就算
        'def t(m):\n    m.setattr(registry[k], "write_tool_file", f)\n',
        'def t(m):\n    for g in owners:\n        m.setattr(g, "zone_text", f)\n',
        "def t(objs):\n    objs[0].write_tool_file = f\n",
        'def t(m, g):\n    m.setattr(g, "write_tool_file", f)\n',  # 參數：呼叫的人傳什麼都可以
        'def t(m):\n    m.setattr(ns.guard, "write_tool_file", f)\n',
        'def t(m):\n    m.setattr(self.rm, "zone_rename", f)\n',
        'def t(m):\n    (lambda g: m.setattr(g, "zone_text", f))(rm)\n',
        'from tests.support.x import G\ndef t(m):\n    m.setattr(G, "zone_text", f)\n',
        "def t(m):\n    m.setattr(registry[k], name, f)\n",
        # 防線的實例、子類別與另存
        'def t(m):\n    rm = ResourceManager()\n    m.setattr(rm, "x", f)\n',
        'class G(EvolutionPathGuard):\n    pass\ndef t(m):\n    m.setattr(G, "x", f)\n',
        'def t(m):\n    m.setattr(holder, "g", EvolutionPathGuard)\n',
        "def t():\n    holder.g = no_follow\n",
        # 換掉防線本身、名稱不是字面
        'def t(m):\n    m.setattr(evolution, "EvolutionPathGuard", F)\n',
        'def t():\n    with patch.object(evolution, "EvolutionPathGuard"):\n        pass\n',
        'def t(m):\n    name = "write_tool_file"\n    m.setattr(EvolutionPathGuard, name, f)\n',
        'def t():\n    with patch(f"{EVO}.EvolutionPathGuard.write_tool_file"):\n        pass\n',
        'EVO = "src.services.evolution_security"\n'
        'def t():\n    with patch(EVO + ".EvolutionPathGuard.x"):\n        pass\n',
        # 預設不明：呼叫的結果與 with 進入的結果（DEV-B 第 2 輪 MEDIUM-A）
        'def t(m, request):\n    g = request.getfixturevalue("guard")\n'
        '    m.setattr(g, "write_tool_file", f)\n',
        'def t(m):\n    guards = {"evo": EvolutionPathGuard}\n'
        '    m.setattr(guards.get("evo"), "write_tool_file", f)\n',
        "from contextlib import nullcontext\n"
        "def t(m):\n    with nullcontext(EvolutionPathGuard) as g:\n"
        '        m.setattr(g, "write_tool_file", f)\n',
        "from tests.unit.helpers import make_guard\n"
        'def t(m):\n    m.setattr(make_guard(), "write_tool_file", f)\n',
        'def t(m, cls):\n    m.setattr(type(cls)(), "zone_text", f)\n',
        # dunder 保留底下物件的身分（DEV-B 第 2 輪 LOW-B）
        'def t(m):\n    m.setattr(resource_manager.__class__, "zone_rename", f)\n',
        "def t():\n    resource_manager.__class__.zone_rename = f\n",
        'def t(m):\n    m.setattr(resource_manager.__class__, "x", f)\n',
        "def t():\n    c = resource_manager.__class__\n    c.x = f\n",
        # 替換函式與參數也經 ``Names``（DEV-B 第 2 輪 LOW-A）
        'def t(m):\n    m.setattr(target=EvolutionPathGuard, name="x", value=f)\n',
        "def t(m, spec):\n    m.setattr(*spec)\n",
        'def t():\n    with patch.object(EvolutionPathGuard, attribute="x"):\n        pass\n',
        "from unittest.mock import patch as P\n"
        'def t():\n    with P.object(EvolutionPathGuard, "x"):\n        pass\n',
        "from unittest.mock import patch\n"
        'def t():\n    po = patch.object\n    po(EvolutionPathGuard, "x")\n',
        'def t(m):\n    ms = m.setattr\n    ms(EvolutionPathGuard, "x", f)\n',
        'def t():\n    type.__setattr__(EvolutionPathGuard, "x", f)\n',
        'def t():\n    resource_manager.__setattr__("zone_text", f)\n',
        'HOLDER = "a.b.c"\ndef t(m):\n    m.setattr(HOLDER, EvolutionPathGuard)\n',
        # 直接改防線的字典（DEV-B 第 2 輪 N1）
        'def t(m):\n    m.setitem(no_follow.__dict__, "replace_text_no_follow", f)\n',
        'def t():\n    with patch.dict(no_follow.__dict__, {"x": f}):\n        pass\n',
        "def t():\n    no_follow.__dict__['x'] = f\n",
        "def t():\n    vars(no_follow).update(x=f)\n",
        "def t():\n    zone_of.__globals__['os'] = fake\n",
    ],
)
def test_the_link_scan_finds_every_way_of_replacing_a_defense(text):
    assert link_replacements([Source("m.py", "m", ast.parse(text))]) == {"m:t": 1}


def test_the_link_scan_follows_fixtures_from_other_files():
    conftest = "@pytest.fixture\ndef guard():\n    return EvolutionPathGuard\n"
    test = 'def t(m, guard):\n    m.setattr(guard, "x", f)\n'
    sources = [Source("c.py", "c", ast.parse(conftest)), Source("m.py", "m", ast.parse(test))]
    assert link_replacements(sources) == {"m:t": 1}


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(evolution, "_venv_python", f)\n',
        'def t(m):\n    m.setattr(os.path, "realpath", f)\n',
        "def t(rm):\n    rm.zone_text(zp)\n",
        "def t():\n    self.resource_manager = x\n",
        'def t(m):\n    g = other.Thing\n    m.setattr(g, "x", f)\n',
        'def t(m, tmp_path):\n    m.setattr(tmp_path, "x", f)\n',
        'def t(m):\n    with patch.object(Other, "y") as g:\n        m.setattr(g, "x", f)\n',
        'def t(m):\n    m.setattr(SimpleNamespace(), "x", f)\n',
        # 認不出的擁有者，換掉的不是防線的成員
        'def t(m):\n    m.setattr(registry[k], "overrides", f)\n',
        "def t(executor):\n    executor.health = x\n",
        "def t(wiring):\n    wiring.broker.error = x\n",  # 和觀察者紀錄的欄位同名
        "def t():\n    self.write_tool_file = f\n",  # 測試類別自己的屬性
        # 換進去的值和防線的函式同名，但不是裝著防線的東西
        "from src.services.isolation import profiles\n"
        "def t(m):\n    def _lstat(p):\n        pass\n"
        '    m.setattr(profiles.os, "lstat", _lstat)\n',
        # 認不出的擁有者（呼叫的結果、with），換掉的不是防線的成員
        'def t(m, request):\n    g = request.getfixturevalue("tmp")\n    m.setattr(g, "x", f)\n',
        'def t(m):\n    m.setattr(target=other, name="x", value=f)\n',
        'def t(m):\n    m.setitem(vars(other), "x", f)\n',
        "def t():\n    d = {}\n    d['k'] = v\n    d.update(x=1)\n",
    ],
)
def test_the_link_scan_leaves_other_replacements_and_calls_alone(text):
    assert link_replacements([Source("m.py", "m", ast.parse(text))]) == {}

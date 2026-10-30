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
from pathlib import Path

import pytest

from tests.lint.test_zone_path_guard import (
    ZONE_TEXT_PRODUCERS,
    ZONE_TEXT_RECEIVERS,
    Source,
    _assigned_owners,
    _parse,
    _patched_names,
    _patched_owner,
    module_aliases,
    scopes,
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
    "id", "rule", "file", "old", "new", "tests", "marker", "posix_only", "windows_only", "note",
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
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResourceManager"
    )
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
    }
)
_ZONE_SELF = "測 zone_paths 自己：登記的根本身就是被測的東西（T3 也列了）"
_ALIAS = "模擬另一個掛載路徑（同一個檔案身分）：這台機器不能做 bind mount（T3 也列了）"
_LIMIT = "把 no_follow 的上限調小，測到了上限的行為（不放寬判斷）"
_FAULT = "故障注入：讓 RM 的一步失敗，測失敗照實回報、不做一半（不放寬判斷）"
_EXISTING_FAULT = "既有的故障注入：執行器範圍判斷遇到固定種類或拒絕（設計 Q8 明列不受影響）"
_OLD_VAULT = "舊的 get_vault_path 換成這個測試的靈魂根（不是區內方法；區內方法照實執行）"
_PLATFORM = "模擬另一個平台的分支（``is_windows``），區內方法與連結判斷照實執行"
_SPAWN = "記下 spawn 的參數、不真的執行（``run_sync`` 替身），和區無關"
_ENV = "子行程環境換成固定值，測 runner 收到的環境，和區無關"
# 「測試模組:函式」→（次數, 理由）；第一次跑出的命中逐一判斷後列在這裡
LINK_SCAN_ALLOWED: dict[str, tuple[int, str]] = {
    "tests.support.session_scope:scope_db": (
        1,
        "session 沙盒的擁有權判斷（RM.is_owner）換成合成的擁有關係，和區無關",
    ),
    "tests.unit.test_custom_model_rename_security:"
    "test_a_failed_move_is_an_error_and_moves_no_ownership": (1, _FAULT),
    "tests.unit.test_evolution_isolation_security:_evolution_in_the_executor": (1, _ENV),
    "tests.unit.test_evolution_isolation_security:spawned": (2, _SPAWN + "；平台固定 POSIX"),
    "tests.unit.test_evolution_isolation_security:"
    "test_build_and_verify_spawn_through_the_launcher": (1, _ENV),
    "tests.unit.test_evolution_isolation_security:"
    "test_direct_consent_keeps_the_rlimit_preexec": (
        1,
        "rlimit 換成記錄用的替身（Windows 沒有 resource 模組），和區無關",
    ),
    "tests.unit.test_evolution_venv_python:"
    "test_local_verification_uses_the_absolute_venv_python": (1, _PLATFORM),
    "tests.unit.test_evolution_venv_python:test_pip_install_uses_the_absolute_venv_python": (
        1,
        _PLATFORM,
    ),
    "tests.unit.test_evolution_verify_protocol_security:"
    "test_posix_protocol_keeps_limits_and_close_eof": (1, _PLATFORM),
    "tests.unit.test_evolution_verify_protocol_security:test_protocol_failures_never_deploy": (
        1,
        _PLATFORM,
    ),
    "tests.unit.test_evolution_verify_protocol_security:test_windows_uses_sdk_transport": (
        1,
        _PLATFORM,
    ),
    "tests.unit.test_evolution_verify_protocol_security:"
    "test_wrapper_cleanup_failure_keeps_the_outcome": (
        1,
        "故障注入：刪不掉驗證用的包裝檔，測結果照實回報（不放寬判斷）",
    ),
    "tests.unit.test_file_ops_no_follow_security:"
    "test_helpers_refuse_a_link_that_exists_only_while_opening": (
        1,
        "換掉 no_follow 用的 os，模擬開啟途中才出現的連結，測防線拒絕",
    ),
    "tests.unit.test_file_ops_no_follow_security:test_link_checks_work_without_junctions": (
        1,
        "模擬沒有 junction 的平台，測連結檢查照樣成立",
    ),
    "tests.unit.test_isolation_status:"
    "test_executor_scope_classifies_the_drive_without_resolving_it": (1, _EXISTING_FAULT),
    "tests.unit.test_isolation_status:"
    "test_executor_scope_is_out_when_the_drive_cannot_be_read": (1, _EXISTING_FAULT),
    "tests.unit.test_mcp_isolation_store_security:test_writes_go_through_the_atomic_write_json": (
        1,
        "記下寫入經過 RM 的 ``write_json``，原函式照樣執行",
    ),
    "tests.unit.test_mcp_vault_jail:test_apply_refuses_every_vault_jailed_server": (
        1,
        "記下舊的 get_vault_path 有沒有被呼叫（斷言一次都沒有）",
    ),
    "tests.unit.test_no_follow_zone:test_iter_regular_files_uses_the_registered_roots": (
        2,
        _ZONE_SELF,
    ),
    "tests.unit.test_no_follow_zone:test_remove_tree_moves_deep_folders_instead_of_failing": (
        1,
        _LIMIT,
    ),
    "tests.unit.test_no_follow_zone:test_walk_pruned_knows_an_anchor_by_identity": (1, _ALIAS),
    "tests.unit.test_no_follow_zone:test_walk_records_depth_device_and_swaps": (1, _LIMIT),
    "tests.unit.test_resource_manager_string_zone:"
    "test_a_drive_met_during_the_walk_refuses_the_whole_delete_and_archive": (
        1,
        "關掉事前的包含判斷，證明走訪途中遇到區根仍整個拒絕（第二道檢查；T3 也列了）",
    ),
    "tests.unit.test_resource_manager_zone:test_permission_repair_gets_the_absolute_path": (
        1,
        "第一次讀被拒（PermissionError），測權限自癒拿到絕對路徑後重試（不放寬判斷）",
    ),
    "tests.unit.test_soul_io_zone_security:model_base": (1, _OLD_VAULT),
    "tests.unit.test_soul_io_zone_security:soul_base": (1, _OLD_VAULT),
    "tests.unit.test_zone_paths:_same_identity": (1, _ALIAS),
    "tests.unit.test_zone_paths:test_current_roots_comes_only_from_the_provider": (1, _ZONE_SELF),
}


def link_scan_sources() -> list[Source]:
    """掃描範圍：改壞清單列到的測試檔、所有 ``*_links_security.py``、``tests/conftest.py`` 與
    ``tests/support/``."""
    paths = {ROOT / node.split("::")[0] for item in MANIFEST for node in item["tests"]}
    paths |= set((ROOT / "tests").rglob("*_links_security.py"))
    paths |= {ROOT / "tests" / "conftest.py", *(ROOT / "tests" / "support").rglob("*.py")}
    return [_parse(path) for path in sorted(paths) if "__pycache__" not in path.parts]


def _aliases(tree: ast.Module) -> dict[str, str]:
    """``from x import y as z`` 與 ``import a.b as z``：``z`` 對回 ``y``、``b``."""
    aliases = module_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update({a.asname: a.name.rsplit(".", 1)[-1] for a in node.names if a.asname})
    return aliases


def link_replacements(sources: list[Source]) -> dict[str, int]:
    found: dict[str, int] = {}
    for source in sources:
        aliases = _aliases(source.tree)
        for key, nodes in scopes(source):
            owners = []
            for node in nodes:
                if isinstance(node, ast.Call) and _patched_names(node):
                    owners.append(_patched_owner(node, aliases))
                owners += _assigned_owners(node, aliases)
            hits = sum(1 for owner in owners if owner in LINK_FORBIDDEN_OWNERS)
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
        'from src.services.resource_manager import resource_manager as rm\n'
        'def t(m):\n    m.setattr(rm, "zone_rename", f)\n',
    ],
)
def test_the_link_scan_finds_every_way_of_replacing_a_defense(text):
    assert link_replacements([Source("m.py", "m", ast.parse(text))]) == {"m:t": 1}


@pytest.mark.parametrize(
    "text",
    [
        'def t(m):\n    m.setattr(evolution, "_venv_python", f)\n',
        'def t(m):\n    m.setattr(os.path, "realpath", f)\n',
        "def t(rm):\n    rm.zone_text(zp)\n",
        "def t():\n    self.resource_manager = x\n",
    ],
)
def test_the_link_scan_leaves_other_replacements_and_calls_alone(text):
    assert link_replacements([Source("m.py", "m", ast.parse(text))]) == {}

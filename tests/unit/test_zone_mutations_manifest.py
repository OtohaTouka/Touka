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
    _parse,
    _patched_names,
    _patched_owner,
    _patched_value,
    scope_of,
    scopes,
    shared_names,
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
# 防線模組：模組層定義的名稱（類別、函式、常數）在哪裡都不准換或另存，類別的實例也不准換
LINK_DEFENSE_MODULES = (
    "ToukaMemory/src/memory_server/utils/no_follow.py",
    "ToukaMemory/src/memory_server/utils/zone_paths.py",
    "ToukaProxy/src/services/resource_manager.py",
    "ToukaProxy/src/services/evolution_security.py",
)
# 防線模組定義、但和防線無關的通用名稱：logger 與觀察者紀錄（``ZoneMisuse``）的欄位
LINK_GENERIC_NAMES = frozenset({"logger", "error", "display", "detail"})


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


_ZONE_SELF = "測 zone_paths 自己：登記的根本身就是被測的東西（T3 也列了）"
_ALIAS = "模擬另一個掛載路徑（同一個檔案身分）：這台機器不能做 bind mount（T3 也列了）"
_LIMIT = "把 no_follow 的上限調小，測到了上限的行為（不放寬判斷）"
_FAULT = "故障注入：讓 RM 的一步失敗，測失敗照實回報、不做一半（不放寬判斷）"
_EXISTING_FAULT = "既有的故障注入：執行器範圍判斷遇到固定種類或拒絕（設計 Q8 明列不受影響）"
_OLD_VAULT = "舊的 get_vault_path 換成這個測試的靈魂根（不是區內方法；區內方法照實執行）"
_PLATFORM = "模擬另一個平台的分支（``is_windows``），區內方法與連結判斷照實執行"
_SPAWN = "記下 spawn 的參數、不真的執行（``run_sync`` 替身），和區無關"
_ENV = "子行程環境換成固定值，測 runner 收到的環境，和區無關"
_RLIMIT = "rlimit 換成記錄用的替身（Windows 沒有 resource 模組），和區無關"
_OPS = (
    "故障注入：換掉 no_follow 作業系統轉接（``_FdTree``／``_PathTree``）的一步，模擬跨檔案系統、"
    "被換掉的項目、讀不到或一直長出來，再交回原函式；測防線拒絕或照實回報（不放寬判斷）"
)
_VENV = (
    "測部署與安裝用的 venv Python 路徑（P4）：EPG 的檔案動作換成不碰磁碟的替身、"
    "``run_sandbox_command`` 只記下指令；不是連結測試（連結由 evolution 的連結測試照實測）"
)
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
    "tests.unit.test_evolution_isolation_security:test_direct_consent_keeps_the_rlimit_preexec": (
        1,
        _RLIMIT,
    ),
    "tests.unit.test_evolution_security:_patch_full_env": (1, _ENV),
    "tests.unit.test_evolution_security:test_build_rlimit_preexec_returns_none_without_resource": (
        1,
        _RLIMIT,
    ),
    "tests.unit.test_evolution_security:test_build_rlimit_preexec_sets_expected_rlimit_tuples": (
        1,
        _RLIMIT,
    ),
    "tests.unit.test_evolution_security:"
    "test_run_sandbox_command_hands_off_the_skill_folder_and_cleans_env": (1, _RLIMIT),
    "tests.unit.test_evolution_venv_python:commands": (4, _VENV),
    "tests.unit.test_evolution_venv_python:"
    "test_deploy_command_is_the_python_the_build_step_created": (3, _VENV + "；平台照參數切換"),
    "tests.unit.test_evolution_venv_python:test_local_verification_uses_the_absolute_venv_python": (
        1,
        _PLATFORM,
    ),
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
    "tests.unit.test_file_ops_no_follow_security:test_find_and_grep_stop_walking_at_the_limit": (
        1,
        "走訪換成無限產生，測 find／grep 到上限就停（不放寬判斷）",
    ),
    "tests.unit.test_file_ops_no_follow_security:"
    "test_walk_skips_folders_whose_status_cannot_be_read": (1, _OPS),
    "tests.unit.test_isolation_status:"
    "test_executor_scope_classifies_the_drive_without_resolving_it": (1, _EXISTING_FAULT),
    "tests.unit.test_isolation_status:test_executor_scope_is_out_when_the_drive_cannot_be_read": (
        1,
        _EXISTING_FAULT,
    ),
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
    "tests.unit.test_no_follow_zone:test_remove_tree_does_not_cross_file_systems": (1, _OPS),
    "tests.unit.test_no_follow_zone:test_remove_tree_gives_up_with_a_fixed_reason": (1, _OPS),
    "tests.unit.test_no_follow_zone:test_remove_tree_retries_an_entry_that_was_swapped": (1, _OPS),
    "tests.unit.test_no_follow_zone:test_walk_open_gives_open_files_and_closes_them": (1, _OPS),
    "tests.unit.test_no_follow_zone:test_walk_records_folders_it_cannot_read": (1, _OPS),
    "tests.unit.test_no_follow_zone:test_remove_tree_moves_deep_folders_instead_of_failing": (
        2,
        _LIMIT + "；" + _OPS,
    ),
    "tests.unit.test_no_follow_zone:test_walk_pruned_knows_an_anchor_by_identity": (1, _ALIAS),
    "tests.unit.test_no_follow_zone:test_walk_records_depth_device_and_swaps": (
        3,
        _LIMIT + "；" + _OPS,
    ),
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
    "tests.unit.test_zone_paths:test_zone_path_basics": (
        1,
        "斷言 ZonePath 不能改（指定會丟 AttributeError），沒有真的換掉",
    ),
    "tests.unit.test_zone_tripwire:test_an_error_in_the_hook_is_recorded": (
        1,
        "絆線自己匯入的 ``current_roots`` 換成會丟例外的，測 hook 出錯也記一筆（T3 也列了）",
    ),
}


def link_scan_sources() -> list[Source]:
    """掃描範圍：改壞清單列到的測試檔、所有 ``*_links_security.py``、``tests/conftest.py`` 與
    ``tests/support/``."""
    paths = {ROOT / node.split("::")[0] for item in MANIFEST for node in item["tests"]}
    paths |= set((ROOT / "tests").rglob("*_links_security.py"))
    paths |= {ROOT / "tests" / "conftest.py", *(ROOT / "tests" / "support").rglob("*.py")}
    return [_parse(path) for path in sorted(paths) if "__pycache__" not in path.parts]


def _replaces_a_defense(
    owner: frozenset[str], replaced: frozenset[str], value: frozenset[str] = frozenset()
) -> bool:
    """擁有者或被換掉的名稱是防線、換進去的值裝著防線（另存再從別處換）；擁有者認不出時，
    換掉的名稱是防線類別的成員或認不出就算."""
    if (owner | replaced) & link_forbidden() or value & link_containers():
        return True
    return UNKNOWN in owner and bool(replaced & (link_members() | {UNKNOWN}))


def link_replacements(sources: list[Source]) -> dict[str, int]:
    """每個範圍替換防線的次數；擁有者與名稱都經 ``Names`` 解析（別名、指定、fixture 等）."""
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
                    value = names.of(_patched_value(node)) - {UNKNOWN}
                    hits += _replaces_a_defense(owner, replaced, value)
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
    ],
)
def test_the_link_scan_leaves_other_replacements_and_calls_alone(text):
    assert link_replacements([Source("m.py", "m", ast.parse(text))]) == {}

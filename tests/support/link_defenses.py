"""連結測試不准把防線換掉：執行期核對（ADR-0293 PR 2 決定 26）.

靜態掃描（``tests/unit/test_zone_mutations_manifest.py``）照程式碼認，是提早提醒；這裡在測試執行時
照物件身分認，不管寫法：

1. ``pytest.MonkeyPatch`` 與 ``unittest.mock`` 的每次替換都經過這裡。對象是防線（防線模組、防線類別
   或它的子類別、防線類別的實例）、被換掉的值是防線（模組、類別、函式、RM 實例），或改的是防線模組的
   ``__dict__``、``sys.modules`` 裡的防線模組，就記下做替換的測試程式（「模組:函式」）。
2. 防線函式的 ``__code__``、``__defaults__``、``__kwdefaults__`` 被改（audit hook），同樣記下。
3. 每個測試的 setup、本體、teardown 跑完，各和 session 開始時的狀態比一次（模組字典、類別字典、
   函式的程式、RM 實例沒有蓋掉類別的屬性）。有差異，而現在的值不是 1、2 記下的替換放進去的，
   就是直接改。

範圍和靜態掃描相同：掃描範圍內的測試，以及範圍內的程式（``tests/conftest.py``、
``tests/support/``）在任何測試裡做的替換。做替換的程式不在 ``LINK_SCAN_ALLOWED`` 就算違規；
直接改的一律違規（不分範圍）。
"""

from __future__ import annotations

import ast
import json
import sys
import types
import typing
import unittest.mock
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from memory_server.utils import no_follow, zone_paths
from src.services import evolution_security, resource_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
MANIFEST_PATH = TESTS / "zone_mutations.json"

# 防線模組（靜態掃描與執行期核對共用）：repo 相對路徑 → 模組
LINK_DEFENSE_MODULES: dict[str, types.ModuleType] = {
    "ToukaMemory/src/memory_server/utils/no_follow.py": no_follow,
    "ToukaMemory/src/memory_server/utils/zone_paths.py": zone_paths,
    "ToukaProxy/src/services/resource_manager.py": resource_manager,
    "ToukaProxy/src/services/evolution_security.py": evolution_security,
}
# 防線模組定義、但和防線無關的通用名稱：logger 與觀察者紀錄（``ZoneMisuse``）的欄位
LINK_GENERIC_NAMES = frozenset({"logger", "error", "display", "detail"})
# 函式身上會改變行為的屬性（改它們有 audit 事件 ``object.__setattr__``）
FUNCTION_ATTRS = ("__code__", "__defaults__", "__kwdefaults__")

_ZONE_SELF = "測 zone_paths 自己：登記的根本身就是被測的東西（T3 也列了）"
_ALIAS = "模擬另一個掛載路徑（同一個檔案身分）：這台機器不能做 bind mount（T3 也列了）"
_LIMIT = "把 no_follow 的上限調小，測到了上限的行為（不放寬判斷）"
_FAULT = "故障注入：讓 RM 的一步失敗，測失敗照實回報、不做一半（不放寬判斷）"
_EXISTING_FAULT = "既有的故障注入：執行器範圍判斷遇到固定種類或拒絕（設計 Q8 明列不受影響）"
_OLD_VAULT = "舊的 get_vault_path 換成這個測試的靈魂根（不是區內方法；區內方法照實執行）"
_PLATFORM = "模擬另一個平台的分支（``is_windows``），區內方法與連結判斷照實執行"
_SPAWN = "記下 spawn 的參數、不真的執行（``run_sync`` 替身），和區無關"
_ENV = "子行程環境換成固定值，測 runner 收到的環境，和區無關"
_RLIMIT = "rlimit 換成記錄用的替身或拿掉（Windows 沒有 resource 模組），和區無關"
_OPS = (
    "故障注入：換掉 no_follow 作業系統轉接（``_FdTree``／``_PathTree``）的一步，模擬跨檔案系統、"
    "被換掉的項目、讀不到或一直長出來，再交回原函式；測防線拒絕或照實回報（不放寬判斷）"
)
_VENV = (
    "測部署與安裝用的 venv Python 路徑（P4）：EPG 的檔案動作換成不碰磁碟的替身、"
    "``run_sandbox_command`` 只記下指令；不是連結測試（連結由 evolution 的連結測試照實測）"
)
# 「測試模組:函式」→（靜態掃描的次數, 理由）；靜態掃描比對次數，執行期核對只看做替換的程式有沒有列名
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
    "tests.unit.test_link_defense_runtime:<module>": (
        1,
        "執行期核對的自我測試：用 *args 換自己建的假防線（靜態掃描認不出擁有者與名稱，當成不明）",
    ),
    "tests.unit.test_link_defense_runtime:"
    "test_changing_a_function_in_place_is_caught_even_when_restored": (
        2,
        "執行期核對的自我測試：改的是自己建的假防線函式（名稱是參數，靜態掃描認不出）",
    ),
    "tests.unit.test_mcp_isolation_store_security:test_writes_go_through_the_atomic_write_json": (
        1,
        "記下寫入經過 RM 的 ``write_json``，原函式照樣執行",
    ),
    "tests.unit.test_mcp_server_config_format_security:prepare_evolution_deploy": (
        1,
        _PLATFORM + "（範圍內的測試 import 這個準備函式）",
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


def link_scan_paths() -> frozenset[Path]:
    """範圍：改壞清單列到的測試檔、所有 ``*_links_security.py``、``tests/conftest.py``、
    ``tests/support/``，以及它們 import 的測試模組（遞移；靜態掃描與執行期核對共用）."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = {ROOT / node.split("::")[0] for item in manifest for node in item["tests"]}
    paths |= set(TESTS.rglob("*_links_security.py"))
    paths |= {TESTS / "conftest.py", *(TESTS / "support").rglob("*.py")}
    pending = [p for p in paths if "__pycache__" not in p.parts]
    found: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.add(path)
        pending.extend(_imported_test_modules(path))
    return frozenset(found)


def _imported_test_modules(path: Path) -> Iterator[Path]:
    """``path`` 用 ``from tests.… import``／``import tests.…`` 匯入的測試模組檔案."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name == "tests" or name.startswith("tests."):
                base = ROOT.joinpath(*name.split("."))
                for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                    if candidate.is_file():
                        yield candidate


def code_key(filename: str, qualname: str) -> str | None:
    """測試程式的「模組:函式」（和靜態掃描的鍵相同）；不在 ``tests/`` 裡是 None.

    lambda 與產生式算外層函式的（靜態掃描也這樣算）。
    """
    path = Path(filename)
    if not path.is_relative_to(TESTS):
        return None
    parts = path.relative_to(ROOT).with_suffix("").parts
    module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    pieces = qualname.split(".")
    while pieces and pieces[-1].startswith("<") and pieces[-1] != "<module>":
        pieces.pop()
        if pieces and pieces[-1] == "<locals>":
            pieces.pop()
    return f"{module}:{'.'.join(pieces) or '<module>'}"


_ABSENT = object()  # 屬性不存在
_LAZY_INIT = getattr(typing, "_no_init_or_replace_init", None)


@dataclass(frozen=True)
class Defenses:
    """要守的東西：模組、類別（含巢狀）、函式與 RM 實例."""

    modules: tuple[types.ModuleType, ...]
    classes: tuple[type, ...]
    functions: tuple[types.FunctionType, ...]
    instances: tuple[object, ...]
    # 用來判斷實例的類別（Protocol 不能拿來做 isinstance）、各實例可被蓋掉的類別屬性
    instance_classes: tuple[type, ...] = ()
    shadowable: tuple[frozenset[str], ...] = ()
    value_ids: frozenset[int] = frozenset()
    # ``typing`` 第一次建立實例時才換上 ``__init__`` 的 Protocol 子類別：它們的 ``__init__`` 不比
    lazy_inits: frozenset[int] = frozenset()

    @classmethod
    def of(cls, modules: Iterable[types.ModuleType], instances: Iterable[object]) -> Defenses:
        modules = tuple(modules)
        names = {m.__name__ for m in modules}
        classes: list[type] = []
        pending = [v for m in modules for v in vars(m).values()]
        while pending:
            value = pending.pop()
            if isinstance(value, type) and value.__module__ in names and value not in classes:
                classes.append(value)
                pending.extend(vars(value).values())
        functions: list[types.FunctionType] = []
        for container in (*modules, *classes):
            for value in vars(container).values():
                for fn in _functions_in(value):
                    if fn.__module__ in names and fn not in functions:
                        functions.append(fn)
        instances = tuple(instances)
        return cls(
            modules,
            tuple(classes),
            tuple(functions),
            instances,
            tuple(c for c in classes if not getattr(c, "_is_protocol", False)),
            tuple(frozenset(dir(type(i))) for i in instances),
            frozenset(map(id, (*modules, *classes, *functions, *instances))),
            frozenset(id(c) for c in classes if getattr(c, "__init__", None) is _LAZY_INIT),
        )

    def is_target(self, target: object) -> bool:
        """``target`` 本身是防線：模組、類別或它的子類別、類別的實例."""
        if isinstance(target, types.ModuleType):
            return any(target is m for m in self.modules)
        if isinstance(target, type):
            return any(_subclass(target, c) for c in self.classes)
        return isinstance(target, self.instance_classes)

    def is_value(self, value: object) -> bool:
        """被換掉的值是防線：模組、類別、函式或 RM 實例（從別處換掉對防線的參照）."""
        return id(value) in self.value_ids

    def state(self) -> dict[tuple[int, str], tuple[object, object]]:
        """（容器的 id, 名稱）→（容器, 值）."""
        found: dict[tuple[int, str], tuple[object, object]] = {}
        for container in (*self.modules, *self.classes):
            lazy = id(container) in self.lazy_inits
            for name, value in vars(container).items():
                if name not in LINK_GENERIC_NAMES and not (lazy and name == "__init__"):
                    found[(id(container), name)] = (container, value)
        for fn in self.functions:
            for name in FUNCTION_ATTRS:
                found[(id(fn), name)] = (fn, getattr(fn, name))
        for instance, shadowable in zip(self.instances, self.shadowable, strict=True):
            for name, value in vars(instance).items():
                if name in shadowable:  # 實例屬性蓋掉類別的方法或屬性
                    found[(id(instance), name)] = (instance, value)
        return found


def _functions_in(value: object) -> Iterator[types.FunctionType]:
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    if isinstance(value, property):
        accessors = (value.fget, value.fset, value.fdel)
        yield from (f for f in accessors if isinstance(f, types.FunctionType))
        return
    if isinstance(value, types.FunctionType):
        yield value


def _subclass(candidate: type, base: type) -> bool:
    try:
        return issubclass(candidate, base)
    except TypeError:
        return False


def _describe(container: object) -> str:
    name = getattr(container, "__qualname__", None) or getattr(container, "__name__", None)
    return str(name) if name else type(container).__qualname__ + " 的實例"


@dataclass
class Monitor:
    """一個 session 的核對狀態（自我測試另建自己的，守假的防線）."""

    defenses: Defenses
    allowed: Mapping[str, object]
    scope: frozenset[Path]
    baseline: dict[tuple[int, str], tuple[object, object]] = field(default_factory=dict)
    # 經 monkeypatch／mock／audit 換過、還沒回到開始時的值的位置（容器的 id, 名稱）：
    # 這些位置之後的值（包括被測的產品函式再改的）都算經過記下的替換
    opened: set[tuple[int, str]] = field(default_factory=set)
    violations: list[str] = field(default_factory=list)
    reported: set[tuple[int, str, int]] = field(default_factory=set)
    in_scope: bool = False
    test_key: str | None = None

    def __post_init__(self) -> None:
        self.baseline = self.defenses.state()
        self._function_ids = {id(fn) for fn in self.defenses.functions}

    # --- 一個測試的開始與結束 ---

    def begin(self, path: Path | None, test_key: str | None) -> None:
        self.in_scope = self.covers(path)
        self.test_key = test_key
        self.violations.clear()

    def covers(self, path: Path | None) -> bool:
        return path is not None and path.resolve() in self.scope

    def end(self) -> None:
        self.in_scope, self.test_key = False, None
        self.violations.clear()

    def take(self) -> list[str]:
        """這個階段的違規（拿走）；同時和開始時的狀態比一次."""
        self._check_state()
        found, self.violations[:] = list(self.violations), []
        return found

    # --- 經過 monkeypatch／mock／audit 的替換 ---

    def replaced(self, target: object, name: object, old: object) -> None:
        if name in LINK_GENERIC_NAMES or not (
            self.defenses.is_target(target) or self.defenses.is_value(old)
        ):
            return
        self.opened.add((id(target), str(name)))
        self._performed(f"{_describe(target)}.{name}")

    def dict_changed(self, dic: object, keys: Iterable[object], cleared: bool = False) -> None:
        """``setitem``、``patch.dict``：防線模組的 ``__dict__`` 或 ``sys.modules`` 裡的防線模組."""
        for module in self.defenses.modules:
            if dic is vars(module):
                self.opened.update((id(module), str(key)) for key in keys)
                self._performed(f"{module.__name__}.__dict__")
            elif dic is sys.modules and (cleared or module.__name__ in set(map(str, keys))):
                self._performed(f"sys.modules[{module.__name__!r}]")

    def function_changed(self, fn: object, name: str) -> None:
        if id(fn) in self._function_ids and name in FUNCTION_ATTRS:
            self.opened.add((id(fn), name))
            self._performed(f"{_describe(fn)}.{name}")

    def _performed(self, what: str) -> None:
        performer, path = _performer(self.test_key)
        checked = self.in_scope or self.covers(path)
        if checked and performer not in self.allowed:
            who = performer or "<?>"
            self.violations.append(f"{who} 換掉了 {what}（沒有列在 LINK_SCAN_ALLOWED）")

    # --- 直接改（不經 monkeypatch／mock） ---

    def _check_state(self) -> None:
        now = self.defenses.state()
        for key in self.baseline.keys() | now.keys():
            container, before = self.baseline.get(key, (None, _ABSENT))
            container, after = now.get(key, (container, _ABSENT))
            if after is before:
                self.opened.discard(key)
                continue
            if key in self.opened:
                continue
            mark = (*key, id(after))
            if mark not in self.reported:
                self.reported.add(mark)
                self.violations.append(
                    f"{_describe(container)}.{key[1]} 被直接改了（不經 monkeypatch／mock，"
                    "測試結束也沒有換回）"
                )


def _performer(test_key: str | None) -> tuple[str | None, Path | None]:
    """做替換的測試程式（「模組:函式」, 檔案）：呼叫堆疊上第一個 ``tests/`` 裡的程式；
    ``@patch`` 裝飾器從 mock 進入時，算被裝飾的函式；都沒有時算目前的測試."""
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_filename != __file__:
            key = code_key(code.co_filename, code.co_qualname)
            if key is not None:
                return key, Path(code.co_filename)
            patched = code.co_name in ("patched", "async_patched")
            decorated = frame.f_locals.get("func") if patched else None
            if decorated is not None and (inner := getattr(decorated, "__code__", None)):
                key = code_key(inner.co_filename, inner.co_qualname)
                if key is not None:
                    return key, Path(inner.co_filename)
        frame = frame.f_back
    return test_key, None


# --- 全域掛勾：所有 Monitor 共用（session 的一個，加上自我測試臨時加的） ---

_MONITORS: list[Monitor] = []
_INSTALLED: list[str] = []


def add(monitor: Monitor) -> None:
    _MONITORS.append(monitor)


def remove(monitor: Monitor) -> None:
    _MONITORS.remove(monitor)


def _each(call: Callable[[Monitor], None]) -> None:
    for monitor in list(_MONITORS):
        call(monitor)


def _wrap_monkeypatch(name: str, records: str, item: bool) -> None:
    original = getattr(pytest.MonkeyPatch, name)

    def wrapper(self: pytest.MonkeyPatch, *args: Any, **kwargs: Any) -> Any:
        log = getattr(self, records)
        before = len(log)
        result = original(self, *args, **kwargs)
        for container, key, old in log[before:]:
            if item:
                _each(lambda m, c=container, k=key: m.dict_changed(c, [k]))
            else:
                _each(lambda m, c=container, k=key, o=old: m.replaced(c, k, o))
        return result

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(pytest.MonkeyPatch, name, wrapper)


def _wrap_mock() -> None:
    patch_class: Any = unittest.mock._patch
    dict_class: Any = unittest.mock._patch_dict
    enter = patch_class.__enter__
    patch_dict = dict_class._patch_dict

    def entered(self: Any) -> Any:
        result = enter(self)
        target, name, old = self.target, self.attribute, self.temp_original
        _each(lambda m: m.replaced(target, name, old))
        return result

    def dict_patched(self: Any) -> None:
        patch_dict(self)
        _each(lambda m: m.dict_changed(self.in_dict, list(self.values), cleared=self.clear))

    entered.__wrapped__ = enter  # type: ignore[attr-defined]
    dict_patched.__wrapped__ = patch_dict  # type: ignore[attr-defined]
    patch_class.__enter__ = entered
    dict_class._patch_dict = dict_patched


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event == "object.__setattr__" and len(args) == 3:
        fn, name, _value = args
        if name in FUNCTION_ATTRS:
            _each(lambda m: m.function_changed(fn, name))


def install() -> None:
    """包 monkeypatch 與 mock、掛 audit hook（一個行程一次）."""
    if _INSTALLED:
        return
    _wrap_monkeypatch("setattr", "_setattr", item=False)
    _wrap_monkeypatch("delattr", "_setattr", item=False)
    _wrap_monkeypatch("setitem", "_setitem", item=True)
    _wrap_monkeypatch("delitem", "_setitem", item=True)
    _wrap_mock()
    sys.addaudithook(_audit)
    _INSTALLED.append("installed")


def installed() -> bool:
    return bool(_INSTALLED)


def start() -> Monitor:
    """session 的核對：記下開始時的狀態，掛上全域掛勾."""
    install()
    defenses = Defenses.of(LINK_DEFENSE_MODULES.values(), [resource_manager.resource_manager])
    scope = frozenset(p.resolve() for p in link_scan_paths())
    monitor = Monitor(defenses, LINK_SCAN_ALLOWED, scope)
    add(monitor)
    return monitor

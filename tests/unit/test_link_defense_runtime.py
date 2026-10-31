"""連結測試不准把防線換掉：執行期核對的自我測試（ADR-0293 決定 26）.

每個測試建一組假的防線（模組、類別、函式、實例）與自己的 ``Monitor``，經真的 monkeypatch、
``unittest.mock``、audit hook 與直接指定去換，確認每種寫法都被記下；另外確認 session 的核對守的是
真的四個防線模組，而且 conftest 的每個階段遇到違規都會讓測試失敗。
"""

from __future__ import annotations

import copy
import json
import sys
import types
import warnings
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from tests.support import link_defenses
from tests.support.links import make_dir_link

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

THIS = Path(__file__).resolve()
CONFTEST = THIS.parents[1] / "conftest.py"
MODULE_KEY = "tests.unit.test_link_defense_runtime"


def _fake(*_args: object) -> str:
    return "fake"


@pytest.fixture
def defended() -> Iterator[SimpleNamespace]:
    """一組假的防線，和只守它們的 ``Monitor``（範圍是這個檔案，沒有放行任何程式）."""
    module = types.ModuleType("fake_defense")

    class Guard:
        def fake_act(self) -> str:
            return "real"

    def fake_check(limit: int = 1) -> str:
        return "real"

    for obj in (Guard, fake_check):
        obj.__module__ = module.__name__
    module.Guard, module.fake_check, module.logger = Guard, fake_check, object()
    consumer = types.ModuleType("fake_consumer")
    consumer.fake_check = fake_check
    instance = Guard()
    defenses = link_defenses.Defenses.of([module], [instance])
    monitor = link_defenses.Monitor(defenses, {}, frozenset({THIS}))
    link_defenses.add(monitor)
    monitor.begin(THIS, f"{MODULE_KEY}:test")
    try:
        yield SimpleNamespace(
            module=module,
            Guard=Guard,
            check=fake_check,
            consumer=consumer,
            instance=instance,
            monitor=monitor,
        )
    finally:
        link_defenses.remove(monitor)


def _replacing(text: str) -> list[str]:
    return [line for line in text if "換掉了" in line]


MONKEYPATCH_WAYS: dict[str, Callable[[pytest.MonkeyPatch, SimpleNamespace], Any]] = {
    "模組的函式": lambda m, d: m.setattr(d.module, "fake_check", _fake),
    "類別的方法": lambda m, d: m.setattr(d.Guard, "fake_act", _fake),
    "刪掉類別的方法": lambda m, d: m.delattr(d.Guard, "fake_act"),
    "實例的方法": lambda m, d: m.setattr(d.instance, "fake_act", _fake),
    "子類別": lambda m, d: m.setattr(type("Sub", (d.Guard,), {}), "fake_act", _fake),
    "別處對防線的參照": lambda m, d: m.setattr(d.consumer, "fake_check", _fake),
    "模組的 __dict__": lambda m, d: m.setitem(vars(d.module), "fake_check", _fake),
    "sys.modules": lambda m, d: m.setitem(sys.modules, "fake_defense", SimpleNamespace()),
    "關鍵字參數": lambda m, d: m.setattr(target=d.Guard, name="fake_act", value=_fake),
    "*args": lambda m, d: m.setattr(*(d.Guard, "fake_act", _fake)),
    "另存的 setattr": lambda m, d: (lambda ms: ms(d.Guard, "fake_act", _fake))(m.setattr),
    "呼叫的結果": lambda m, d: m.setattr({"g": d.Guard}.get("g"), "fake_act", _fake),
    "nullcontext": lambda m, d: m.setattr(nullcontext(d.Guard).__enter__(), "fake_act", _fake),
    "__class__": lambda m, d: m.setattr(d.instance.__class__, "fake_act", _fake),
}


@pytest.mark.parametrize("way", MONKEYPATCH_WAYS)
def test_every_monkeypatch_way_is_caught(monkeypatch, defended, way):
    MONKEYPATCH_WAYS[way](monkeypatch, defended)
    found = defended.monitor.take()
    assert found and all(f"{MODULE_KEY}:" in line for line in found), found


MOCK_WAYS: dict[str, Callable[[SimpleNamespace], Any]] = {
    "patch.object": lambda d: mock.patch.object(d.Guard, "fake_act"),
    "patch.object 關鍵字": lambda d: mock.patch.object(target=d.Guard, attribute="fake_act"),
    "patch.multiple": lambda d: mock.patch.multiple(d.Guard, fake_act=_fake),
    "patch.dict": lambda d: mock.patch.dict(vars(d.module), {"fake_check": _fake}),
    "patch.dict sys.modules": lambda d: mock.patch.dict(sys.modules, {"fake_defense": None}),
    "實例": lambda d: mock.patch.object(d.instance, "fake_act"),
}


@pytest.mark.parametrize("way", MOCK_WAYS)
def test_every_mock_way_is_caught_even_when_undone_in_the_test(defended, way):
    with MOCK_WAYS[way](defended):
        pass
    found = defended.monitor.take()
    assert found and all(f"{MODULE_KEY}:" in line for line in found), found


def test_a_patch_decorator_counts_as_the_decorated_function(defended):
    @mock.patch.object(defended.Guard, "fake_act", _fake)
    def decorated() -> str:
        return defended.Guard().fake_act()

    assert decorated() == "fake"
    found = defended.monitor.take()
    assert found and "test_a_patch_decorator_counts_as_the_decorated_function" in found[0]


@pytest.mark.parametrize("name", ["__code__", "__defaults__"])
def test_changing_a_function_in_place_is_caught_even_when_restored(defended, name):
    saved = getattr(defended.check, name)
    setattr(defended.check, name, getattr(_fake, name) if name == "__code__" else (2,))
    setattr(defended.check, name, saved)
    found = defended.monitor.take()
    assert found and f"fake_check.{name}" in found[0]


DIRECT_WAYS: dict[str, Callable[[SimpleNamespace], None]] = {
    "類別屬性": lambda d: setattr(d.Guard, "fake_act", _fake),
    "模組屬性": lambda d: setattr(d.module, "fake_check", _fake),
    "模組的 __dict__": lambda d: vars(d.module).update(fake_check=_fake),
    "實例蓋掉方法": lambda d: setattr(d.instance, "fake_act", _fake),
    "刪掉模組屬性": lambda d: delattr(d.module, "fake_check"),
}


@pytest.mark.parametrize("way", DIRECT_WAYS)
def test_direct_changes_left_in_place_are_caught(defended, way):
    DIRECT_WAYS[way](defended)
    found = defended.monitor.take()
    assert found and "被直接改了" in found[0]
    assert defended.monitor.take() == []  # 同一個改動只報一次


def test_direct_changes_are_caught_outside_the_scope_too(monkeypatch, defended):
    defended.monitor.scope = frozenset()
    defended.monitor.begin(None, None)
    monkeypatch.setattr(defended.Guard, "fake_act", _fake)  # 範圍外的替換不查
    assert defended.monitor.take() == []
    defended.module.fake_check = _fake
    assert "被直接改了" in defended.monitor.take()[0]


def test_a_place_changed_through_a_listed_replacement_may_change_again(monkeypatch, defended):
    """列名的替換之後，被測的產品函式再改同一個位置不算直接改；換回之後再直接改就算."""
    defended.monitor.allowed = {
        f"{MODULE_KEY}:test_a_place_changed_through_a_listed_replacement_may_change_again": 1
    }
    with monkeypatch.context() as m:
        m.setattr(defended.module, "fake_check", None)
        defended.module.fake_check = _fake  # 例如 ``set_roots_provider`` 改了被換掉的位置
        assert defended.monitor.take() == []
    assert defended.monitor.take() == []
    defended.module.fake_check = _fake
    assert "被直接改了" in defended.monitor.take()[0]


def test_a_listed_performer_may_replace(monkeypatch, defended):
    defended.monitor.allowed = {f"{MODULE_KEY}:test_a_listed_performer_may_replace": 1}
    monkeypatch.setattr(defended.Guard, "fake_act", _fake)
    assert defended.monitor.take() == []


@pytest.mark.parametrize(
    "way",
    [
        lambda m, d: m.setattr(SimpleNamespace(), "fake_act", _fake, raising=False),
        lambda m, d: m.setattr(d.module, "logger", object()),
        lambda m, d: m.setattr(d.consumer, "other", _fake, raising=False),
        lambda m, d: m.setitem({}, "fake_check", _fake),
        lambda m, d: m.setenv("TOUKA_LINK_SELFTEST", "1"),
    ],
)
def test_other_replacements_are_left_alone(monkeypatch, defended, way):
    way(monkeypatch, defended)
    assert defended.monitor.take() == []


def test_lambdas_and_generators_count_as_the_enclosing_function():
    key = link_defenses.code_key(str(THIS), "test_x.<locals>.<lambda>")
    assert key == f"{MODULE_KEY}:test_x"
    assert link_defenses.code_key(str(THIS), "Test.test_y.<locals>.<genexpr>") == (
        f"{MODULE_KEY}:Test.test_y"
    )
    assert link_defenses.code_key(str(THIS), "<module>") == f"{MODULE_KEY}:<module>"
    assert link_defenses.code_key(str(THIS.parents[2] / "src" / "x.py"), "f") is None


def test_the_performer_key_does_not_depend_on_how_the_path_is_spelled(tmp_path):
    """``co_filename`` 是 pytest 給的寫法（沒解析）；經連結、subst 或短檔名進 repo 時鍵也要一樣."""
    spelled = THIS.parent / ".." / THIS.parent.name / THIS.name
    assert link_defenses.code_key(str(spelled), "f") == f"{MODULE_KEY}:f"
    real = tmp_path / "real"
    (real / "tests" / "unit").mkdir(parents=True)
    (real / "tests" / "unit" / "test_x.py").write_text("", encoding="utf-8")
    make_dir_link(tmp_path / "alias", real)
    via_link = tmp_path / "alias" / "tests" / "unit" / "test_x.py"
    assert link_defenses.code_key(str(via_link), "f", root=real.resolve()) == "tests.unit.test_x:f"


def test_the_scope_survives_a_missing_or_broken_test_file(tmp_path):
    """範圍裡的檔讀不到或有語法錯誤時仍算在範圍內，只是不追它匯入的模組；清單壞掉時退回固定範圍
    （清單本身由 ``test_zone_mutations_manifest`` 報紅）。conftest 一載入就算範圍，不能讓整個
    pytest 啟動不了."""
    broken = tmp_path / "test_broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    assert list(link_defenses._imported_test_modules(broken)) == []
    assert list(link_defenses._imported_test_modules(tmp_path / "test_gone.py")) == []
    manifest = tmp_path / "zone_mutations.json"
    listed = [{"tests": ["tests/unit/test_gone.py::test_x"]}]
    manifest.write_text(json.dumps(listed), encoding="utf-8")
    scope = link_defenses.link_scan_paths(manifest)
    assert link_defenses.TESTS / "unit" / "test_gone.py" in scope
    assert CONFTEST in scope
    manifest.write_text("[", encoding="utf-8")
    assert CONFTEST in link_defenses.link_scan_paths(manifest)


def _warn_from_its_globals() -> None:
    warnings.warn("old", DeprecationWarning, stacklevel=1)


def test_caches_the_interpreter_writes_are_not_direct_changes():
    """警告紀錄、第一次讀時建立的標註、copy 的 slot 名稱是直譯器自己寫的：不算直接改；
    別的新鍵照報."""
    module = types.ModuleType("fake_cache_defense")
    module.warnings = warnings
    module.warn = types.FunctionType(_warn_from_its_globals.__code__, vars(module), "warn")

    class Box:
        pass

    Box.__module__ = module.__name__
    module.Box = Box
    monitor = link_defenses.Monitor(link_defenses.Defenses.of([module], []), {}, frozenset())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        module.warn()
    assert vars(Box).get("__annotations__", {}) == Box.__annotations__ == {}
    assert vars(module).get("__annotations__", {}) == module.__annotations__ == {}
    copy.copy(Box())
    assert {"__warningregistry__", "__annotations__"} <= set(vars(module))
    assert {"__annotations__", "__slotnames__"} <= set(vars(Box))
    assert monitor.take() == []
    module.other = _fake
    assert "被直接改了" in monitor.take()[0]


# --- session 的核對 ------------------------------------------------------------


def _session_monitor(request: pytest.FixtureRequest) -> tuple[types.ModuleType, Any]:
    conftest = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", "") or ".").resolve() == CONFTEST
    )
    return conftest, conftest._LINK_MONITOR


def test_the_session_guards_the_four_defense_modules(request):
    _conftest, monitor = _session_monitor(request)
    assert link_defenses.installed()
    assert monitor in link_defenses._MONITORS
    assert set(monitor.defenses.modules) == set(link_defenses.LINK_DEFENSE_MODULES.values())
    assert monitor.allowed is link_defenses.LINK_SCAN_ALLOWED
    assert THIS in monitor.scope  # 這個檔案在改壞清單裡，屬於範圍
    assert monitor.in_scope
    assert monitor.test_key == f"{MODULE_KEY}:test_the_session_guards_the_four_defense_modules"


def test_monkeypatch_and_mock_go_through_the_check():
    for wrapped in (
        pytest.MonkeyPatch.setattr,
        pytest.MonkeyPatch.delattr,
        pytest.MonkeyPatch.setitem,
        pytest.MonkeyPatch.delitem,
        mock._patch.__enter__,
        mock._patch_dict._patch_dict,
    ):
        assert getattr(wrapped, "__wrapped__", None) is not None, wrapped


def test_every_phase_fails_on_a_violation(request):
    conftest, monitor = _session_monitor(request)
    phases = [
        (conftest.pytest_runtest_setup, (request.node,)),
        (conftest.pytest_runtest_call, (request.node,)),
        (conftest.pytest_runtest_teardown, (request.node, None)),
    ]
    try:
        for hook, args in phases:
            phase = hook(*args)
            next(phase)
            monitor.violations.append("自我測試放的違規")
            with pytest.raises(pytest.fail.Exception, match="自我測試放的違規"):
                phase.send(None)
    finally:
        monitor.begin(request.node.path, f"{MODULE_KEY}:test_every_phase_fails_on_a_violation")

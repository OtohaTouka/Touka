"""連結測試不准把防線換掉：執行期核對的自我測試（ADR-0293 決定 26）.

每個測試建一組假的防線（模組、類別、函式、實例）與自己的 ``Monitor``，經真的 monkeypatch、
``unittest.mock``、audit hook 與直接指定去換，確認每種寫法都被記下；另外確認 session 的核對守的是
真的四個防線模組，而且 conftest 的每個階段遇到違規都會讓測試失敗。
"""

from __future__ import annotations

import copy
import enum
import functools
import inspect
import json
import sys
import types
import warnings
import weakref
from collections.abc import MutableMapping, MutableSequence, MutableSet
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from _pytest.skipping import Xfail, xfailed_key

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
    "別處字典裡的防線": lambda m, d: m.setitem(vars(d.consumer), "fake_check", _fake),
    "刪掉別處字典裡的防線": lambda m, d: m.delitem(vars(d.consumer), "fake_check"),
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
    "別處字典裡的防線": lambda d: mock.patch.dict(vars(d.consumer), {"fake_check": _fake}),
    "清空別處的字典": lambda d: mock.patch.dict(vars(d.consumer), clear=True),
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


def _clear_defaults(fn: types.FunctionType) -> None:
    fn.__defaults__ = None


def _delete_defaults(fn: types.FunctionType) -> None:
    del fn.__defaults__


@pytest.mark.parametrize("remove", [_clear_defaults, _delete_defaults])
def test_removing_a_function_attribute_is_recorded_when_it_happens(defended, remove):
    """``= None`` 與 ``del`` 發出的是刪除事件（``object.__delattr__``）：當場記下誰做的."""
    saved = defended.check.__defaults__
    remove(defended.check)
    found = list(defended.monitor.violations)
    defended.check.__defaults__ = saved
    assert found and "fake_check.__defaults__" in found[0] and "換掉了" in found[0]


def _decorated(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    return wrapper


def _guard_with_wrapped_functions() -> SimpleNamespace:
    module = types.ModuleType("fake_wrapped_defense")

    def behind_wraps() -> str:
        return "real"

    def behind_cache() -> str:
        return "real"

    def behind_two() -> str:
        return "real"

    def behind_property(_self: object) -> str:
        return "real"

    def behind_static() -> str:
        return "real"

    class Holder:
        value = property(_decorated(behind_property))
        static = staticmethod(_decorated(behind_static))

    originals = (behind_wraps, behind_cache, behind_two, behind_property, behind_static)
    for obj in (*originals, Holder):
        obj.__module__ = module.__name__
    module.Holder = Holder
    module.wrapped = _decorated(behind_wraps)
    module.cached = functools.cache(behind_cache)
    module.twice = _decorated(_decorated(behind_two))
    return SimpleNamespace(module=module, originals=originals)


def test_the_function_behind_a_decorator_is_guarded():
    """裝飾器包住的原函式（``functools.wraps``、``functools.cache``，疊幾層都一樣）也是防線."""
    fake = _guard_with_wrapped_functions()
    defenses = link_defenses.Defenses.of([fake.module], [])
    guarded = {id(fn) for fn in defenses.functions}
    assert {id(fn) for fn in fake.originals} <= guarded
    monitor = link_defenses.Monitor(defenses, {}, frozenset())
    behind = fake.originals[0]
    saved = behind.__code__
    behind.__code__ = _fake.__code__
    try:
        assert "被直接改了" in monitor.take()[0]
    finally:
        behind.__code__ = saved


def _module_with_constants() -> types.ModuleType:
    module = types.ModuleType("fake_constant_defense")
    module.TABLE = {"a": ("x",)}
    module.DEPTH = {"a": 1}
    return module


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.TABLE.update(b=("y",)),
        lambda m: m.TABLE.__setitem__("a", ("z",)),
        lambda m: m.DEPTH.clear(),
    ],
)
def test_the_contents_of_listed_constants_are_compared(change):
    module = _module_with_constants()
    defenses = link_defenses.Defenses.of([module], [], [(module, "TABLE"), (module, "DEPTH")])
    monitor = link_defenses.Monitor(defenses, {}, frozenset())
    assert monitor.take() == []
    change(module)
    assert "被直接改了" in monitor.take()[0]


def test_the_contents_of_a_constant_may_come_back():
    """內容比相等、不比身分：換回同樣的內容就不算改了."""
    module = _module_with_constants()
    defenses = link_defenses.Defenses.of([module], [], [(module, "TABLE")])
    monitor = link_defenses.Monitor(defenses, {}, frozenset())
    module.TABLE["a"] = ("z",)
    module.TABLE["a"] = ("x",)
    assert monitor.take() == []


def _monitor_of_constants(
    module: types.ModuleType, performer: str, targets: frozenset[str] | None
) -> Any:
    """守 ``module.TABLE`` 的 ``Monitor``（範圍是這個檔案）；``targets`` 是 None 時沒放行誰."""
    defenses = link_defenses.Defenses.of([module], [], [(module, "TABLE")])
    allowed = {} if targets is None else {f"{MODULE_KEY}:{performer}": (1, "", targets)}
    monitor = link_defenses.Monitor(defenses, allowed, frozenset({THIS}))
    monitor.begin(THIS, f"{MODULE_KEY}:{performer}")
    return monitor


def test_a_listed_replacement_of_a_constant_is_not_a_direct_change():
    """經 monkeypatch 整個換掉可變常數：照放行清單判，不是直接改；換回之後的直接改照報."""
    module = _module_with_constants()
    performer = "test_a_listed_replacement_of_a_constant_is_not_a_direct_change"
    monitor = _monitor_of_constants(module, performer, frozenset({"TABLE"}))
    link_defenses.add(monitor)
    try:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(module, "TABLE", {"b": ("y",)})
            assert monitor.take() == []
        assert monitor.take() == []
        module.TABLE["c"] = ("z",)
        assert "被直接改了" in monitor.take()[0]
    finally:
        link_defenses.remove(monitor)


@pytest.mark.parametrize("delete", [False, True])
def test_changing_a_constant_through_monkeypatch_goes_by_the_list(delete):
    """經 monkeypatch 改可變常數的內容：沒列名是「換掉了」，列了就放行、換回來也不報."""
    module = _module_with_constants()
    performer = "test_changing_a_constant_through_monkeypatch_goes_by_the_list"
    for targets in (None, frozenset({"TABLE"})):
        monitor = _monitor_of_constants(module, performer, targets)
        link_defenses.add(monitor)
        try:
            with pytest.MonkeyPatch.context() as m:
                if delete:
                    m.delitem(module.TABLE, "a")
                else:
                    m.setitem(module.TABLE, "a", ("z",))
                found = monitor.take()
                if targets is None:
                    assert len(found) == 1
                    assert "換掉了 fake_constant_defense.TABLE" in found[0]
                else:
                    assert found == []
            assert monitor.take() == []
        finally:
            link_defenses.remove(monitor)


def test_patch_dict_on_a_constant_goes_by_the_list():
    module = _module_with_constants()
    performer = "test_patch_dict_on_a_constant_goes_by_the_list"
    monitor = _monitor_of_constants(module, performer, frozenset({"TABLE"}))
    link_defenses.add(monitor)
    try:
        with mock.patch.dict(module.TABLE, {"b": ("y",)}):
            assert monitor.take() == []
        assert monitor.take() == []
    finally:
        link_defenses.remove(monitor)


def test_a_constant_left_changed_through_monkeypatch_is_a_leak():
    module = _module_with_constants()
    performer = "test_a_constant_left_changed_through_monkeypatch_is_a_leak"
    monitor = _monitor_of_constants(module, performer, frozenset({"TABLE"}))
    link_defenses.add(monitor)
    m = pytest.MonkeyPatch()
    try:
        m.setitem(module.TABLE, "b", ("y",))
        monitor.check_leaks()
        assert "沒有換回" in monitor.take()[0]
    finally:
        link_defenses.remove(monitor)
        m.undo()


# 平常就會變，或標準函式庫自己維護的：不比內容
_UNCOMPARED_MUTABLE_CONSTANTS = (
    ("memory_server.utils.zone_paths", "_observers"),
    ("memory_server.utils.zone_paths", "_handoffs"),
    ("memory_server.utils.no_follow", "WalkEntry._field_defaults"),
)


def _holds_mutable(value: object) -> bool:
    if isinstance(value, tuple):
        return any(_holds_mutable(item) for item in value)
    return isinstance(value, (MutableMapping, MutableSequence, MutableSet, weakref.WeakSet))


def _mutable_constants(module: types.ModuleType) -> Iterator[tuple[str, str]]:
    """模組與類別層綁到可變容器的（模組, 名稱）：看實際的值，不管是什麼寫法、定義在哪個區塊."""
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        if inspect.isclass(value):
            # ``Enum`` 的成員表是 enum 自己維護的
            if value.__module__ == module.__name__ and not issubclass(value, enum.Enum):
                for member, item in vars(value).items():
                    if not member.startswith("__") and _holds_mutable(item):
                        yield module.__name__, f"{value.__name__}.{member}"
        elif _holds_mutable(value):
            yield module.__name__, name


def test_every_mutable_constant_of_the_defense_modules_is_accounted_for():
    """防線模組新增可變常數時紅，由人決定比不比內容（``_observers``、``_handoffs`` 平常就會變）."""
    found = {
        constant
        for module in link_defenses.LINK_DEFENSE_MODULES.values()
        for constant in _mutable_constants(module)
    }
    listed = {(module.__name__, name) for module, name in link_defenses.LINK_DEFENSE_CONTENTS}
    assert found == listed | set(_UNCOMPARED_MUTABLE_CONSTANTS)


def test_the_session_guards_the_contents_of_the_data_dirs(request):
    _conftest, monitor = _session_monitor(request)
    names = {name for _container, name in monitor.defenses.contents}
    assert names == {"_DATA_DIRS", "_AGENT_DEPTH"}


def test_generic_names_are_compared_too(monkeypatch, defended):
    """通用名稱也要比：直接改就報；經 monkeypatch 換的不報，但位置算打開了，換回之前不算直接改."""
    with monkeypatch.context() as m:
        m.setattr(defended.module, "logger", object())
        defended.module.logger = object()
        assert defended.monitor.take() == []
    assert defended.monitor.take() == []
    defended.module.logger = object()
    assert "被直接改了" in defended.monitor.take()[0]


def test_a_replacement_left_in_place_is_a_leak(defended):
    """測試結束還沒換回：報外洩，移出已打開的位置，之後的測試不再替它背這筆帳."""
    key = f"{MODULE_KEY}:test_a_replacement_left_in_place_is_a_leak"
    defended.monitor.allowed = {key: (1, "", frozenset({"fake_check"}))}
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    try:
        assert defended.monitor.take() == []
        defended.monitor.check_leaks()
        found = defended.monitor.take()
        assert found and "沒有換回" in found[0]
        defended.monitor.check_leaks()
        assert defended.monitor.take() == []
    finally:
        patcher.stop()
    defended.monitor.check_leaks()
    assert defended.monitor.take() == []


def test_undoing_a_monkeypatch_on_an_instance_is_not_a_leak(defended):
    """monkeypatch 換回實例的方法時，留下的是綁回自己的方法：行為和沒換過一樣，不算外洩."""
    key = f"{MODULE_KEY}:test_undoing_a_monkeypatch_on_an_instance_is_not_a_leak"
    defended.monitor.allowed = {key: (1, "", frozenset({"fake_act"}))}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(defended.instance, "fake_act", _fake)
    assert "fake_act" in vars(defended.instance)
    defended.monitor.check_leaks()
    assert defended.monitor.take() == []
    defended.instance.fake_act = _fake  # 之後的直接改照報
    assert "被直接改了" in defended.monitor.take()[0]


@pytest.mark.parametrize(
    "method",
    [
        lambda d: d.Guard().fake_act,  # 綁在別的實例上
        lambda d: types.MethodType(_fake, d.instance),  # 綁回自己，但不是類別的方法
        lambda d: mock.ANY,  # 和什麼都相等
        lambda d: types.MethodType(d.Guard.fake_act, d.Guard),  # 同一個函式，綁在類別上
    ],
)
def test_a_different_method_on_the_instance_is_a_direct_change(defended, method):
    defended.instance.fake_act = method(defended)
    assert "被直接改了" in defended.monitor.take()[0]


def _cached_act(_self: object) -> str:
    return "real"


class _Members:
    LIMIT = {"a": 1}
    cached = functools.cache(_cached_act)

    def act(self) -> str:
        return "real"

    @classmethod
    def made(cls) -> str:
        return "real"

    @staticmethod
    def plain() -> str:
        return "real"


@pytest.mark.parametrize("name", ["LIMIT", "act", "made", "plain", "cached"])
def test_what_monkeypatch_puts_back_counts_as_the_class_member(name):
    instance = _Members()
    assert link_defenses._same_as_class(instance, name, getattr(instance, name))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LIMIT", {"a": 1}),  # 內容一樣，不是同一個
        ("LIMIT", mock.ANY),
        ("act", _Members().act),  # 綁在別的實例上
        ("act", mock.ANY),
        ("made", types.MethodType(_Members.__dict__["made"].__func__, object())),
        ("plain", _fake),
        ("plain", mock.ANY),
        ("missing", _fake),
    ],
)
def test_only_the_class_member_itself_counts(name, value):
    assert not link_defenses._same_as_class(_Members(), name, value)


@pytest.mark.parametrize(
    ("name", "wrong"),
    [
        # ``staticmethod`` 綁到實例：呼叫時會多塞一個 self
        ("plain", lambda i: types.MethodType(_Members.__dict__["plain"].__func__, i)),
        # 沒綁定的函式放進實例：呼叫時少一個 self
        ("act", lambda _i: _Members.__dict__["act"]),
        ("made", lambda _i: _Members.__dict__["made"].__func__),
    ],
    ids=["static-bound", "plain-function", "class-function"],
)
def test_a_wrong_binding_of_the_class_member_does_not_count(name, wrong):
    instance = _Members()
    assert not link_defenses._same_as_class(instance, name, wrong(instance))


class _Computed:
    @property
    def value(self) -> str:
        pytest.fail("核對不能跑 property 的程式")

    @functools.cached_property
    def cached(self) -> str:
        pytest.fail("核對不能跑 cached_property 的程式")


@pytest.mark.parametrize("name", ["value", "cached"])
def test_comparing_never_runs_a_property(name):
    assert not link_defenses._same_as_class(_Computed(), name, _fake)


@pytest.mark.parametrize("name", ["LIMIT", "act", "made", "plain", "cached"])
def test_a_monkeypatch_round_trip_leaves_the_state_as_it_was(name):
    """真的走一次 monkeypatch 換掉再換回，經 ``Defenses`` 核對."""
    instance = _Members()
    defenses = link_defenses.Defenses.of([], [instance], [])
    before = defenses.state()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(instance, name, _fake)
        assert defenses.state() != before
    assert name in vars(instance)
    assert defenses.state() == before


def test_a_direct_change_after_a_leak_is_reported(defended):
    """外洩報過以後，那個位置再被直接改照樣要報（不再整個 session 都算打開的）."""
    key = f"{MODULE_KEY}:test_a_direct_change_after_a_leak_is_reported"
    defended.monitor.allowed = {key: (1, "", frozenset({"fake_check"}))}
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    try:
        defended.monitor.check_leaks()
        assert "沒有換回" in defended.monitor.take()[0]
        defended.module.fake_check = object()
        assert "被直接改了" in defended.monitor.take()[0]
    finally:
        patcher.stop()


def test_a_wider_fixture_is_checked_when_it_ends(defended):
    """module／session 範圍的 fixture 做的替換，到那個 fixture 結束才看有沒有換回."""
    key = f"{MODULE_KEY}:test_a_wider_fixture_is_checked_when_it_ends"
    defended.monitor.allowed = {key: (1, "", frozenset({"fake_check"}))}
    wider = SimpleNamespace(scope="module")
    defended.monitor.enter_fixture(wider)
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    defended.monitor.leave_fixture()
    try:
        defended.monitor.check_leaks()
        assert defended.monitor.take() == []
        defended.monitor.fixture_finished(wider)
        defended.monitor.check_leaks()
        assert "沒有換回" in defended.monitor.take()[0]
    finally:
        patcher.stop()


def test_a_wider_fixture_that_put_it_back_no_longer_shields_the_place(defended):
    """module 範圍的 fixture 換回之後，那個位置回到一般的核對：別的測試留著不換回照報外洩."""
    key = f"{MODULE_KEY}:test_a_wider_fixture_that_put_it_back_no_longer_shields_the_place"
    defended.monitor.allowed = {key: (2, "", frozenset({"fake_check"}))}
    defended.monitor.enter_fixture(SimpleNamespace(scope="module"))
    with mock.patch.object(defended.module, "fake_check", _fake):
        defended.monitor.leave_fixture()
    assert defended.monitor.take() == []
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    try:
        defended.monitor.check_leaks()
        assert "沒有換回" in defended.monitor.take()[0]
    finally:
        patcher.stop()


def test_a_function_fixture_is_checked_when_the_test_ends(defended):
    key = f"{MODULE_KEY}:test_a_function_fixture_is_checked_when_the_test_ends"
    defended.monitor.allowed = {key: (1, "", frozenset({"fake_check"}))}
    defended.monitor.enter_fixture(SimpleNamespace(scope="function"))
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    defended.monitor.leave_fixture()
    try:
        defended.monitor.check_leaks()
        assert "沒有換回" in defended.monitor.take()[0]
    finally:
        patcher.stop()


def test_begin_keeps_earlier_violations(defended):
    """兩個測試之間記下的違規留給下一次核對，不在下一個測試開始時清掉."""
    defended.monitor.violations.append("測試之間的違規")
    defended.monitor.begin(THIS, f"{MODULE_KEY}:test")
    assert defended.monitor.take() == ["測試之間的違規"]


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
        f"{MODULE_KEY}:test_a_place_changed_through_a_listed_replacement_may_change_again": (
            1,
            "",
            frozenset({"fake_check"}),
        )
    }
    with monkeypatch.context() as m:
        m.setattr(defended.module, "fake_check", None)
        defended.module.fake_check = _fake  # 例如 ``set_roots_provider`` 改了被換掉的位置
        assert defended.monitor.take() == []
    assert defended.monitor.take() == []
    defended.module.fake_check = _fake
    assert "被直接改了" in defended.monitor.take()[0]


def test_a_listed_performer_may_replace(monkeypatch, defended):
    defended.monitor.allowed = {
        f"{MODULE_KEY}:test_a_listed_performer_may_replace": (1, "", frozenset({"fake_act"}))
    }
    monkeypatch.setattr(defended.Guard, "fake_act", _fake)
    assert defended.monitor.take() == []


@pytest.mark.parametrize(
    "way",
    [
        lambda m, d: m.setattr(d.module, "fake_check", _fake),
        lambda m, d: m.setitem(vars(d.module), "fake_check", _fake),
        lambda m, d: m.setitem(sys.modules, "fake_defense", SimpleNamespace()),
    ],
)
def test_a_listed_performer_may_replace_only_its_listed_names(monkeypatch, defended, way):
    """放行清單也綁替換目標：同一個程式換了清單外的名稱照樣紅."""
    # 參數化的 lambda 在模組層，算 ``<module>``
    defended.monitor.allowed = {f"{MODULE_KEY}:<module>": (1, "", frozenset({"fake_act"}))}
    way(monkeypatch, defended)
    found = defended.monitor.take()
    assert found and "替換目標" in found[0]


def test_any_name_is_only_for_a_listed_performer_that_says_so(monkeypatch, defended):
    defended.monitor.allowed = {
        f"{MODULE_KEY}:test_any_name_is_only_for_a_listed_performer_that_says_so": (
            1,
            "",
            frozenset({link_defenses.ANY_NAME}),
        )
    }
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
    for broken_manifest in ("[", '[{"test": []}]', '{"tests": []}', "[1]"):
        manifest.write_text(broken_manifest, encoding="utf-8")
        assert CONFTEST in link_defenses.link_scan_paths(manifest), broken_manifest


def test_the_scope_follows_a_submodule_imported_from_a_package(tmp_path):
    """``from tests.pkg import 子模組`` 的子模組也算匯入；不是模組的名稱略過."""
    test_file = tmp_path / "test_x.py"
    test_file.write_text(
        "from tests.support import links, make_dir_link_does_not_exist\n"
        "from tests.unit import test_link_defense_runtime\n",
        encoding="utf-8",
    )
    found = set(link_defenses._imported_test_modules(test_file))
    assert link_defenses.TESTS / "support" / "links.py" in found
    assert THIS in found
    assert not any("make_dir_link_does_not_exist" in str(p) for p in found)


def test_names_that_are_not_files_are_not_test_code(monkeypatch):
    """``exec`` 與自動產生的程式（``<string>`` 一類）不是測試程式，和目前在哪個資料夾無關."""
    monkeypatch.chdir(link_defenses.TESTS)
    assert link_defenses.code_key("<link-defense-selftest>", "f") is None


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


PHASES = ("setup", "call", "teardown")


def _report_through_the_real_hooks(
    request: pytest.FixtureRequest, when: str, failure: Callable[[], None]
) -> pytest.TestReport:
    """走真的 hook 鏈（含 pytest 自己的 xfail wrapper）算這個測試標成 xfail 時的報告."""
    item = request.node
    saved = item.stash.get(xfailed_key, None)
    item.stash[xfailed_key] = Xfail(reason="自我測試", run=True, strict=False, raises=None)
    try:
        call = pytest.CallInfo.from_call(failure, when)  # type: ignore[arg-type]
        return request.config.hook.pytest_runtest_makereport(item=item, call=call)
    finally:
        item.stash[xfailed_key] = saved  # type: ignore[assignment]


@pytest.mark.parametrize("when", PHASES)
def test_xfail_does_not_swallow_a_link_violation(request, when):
    conftest, _monitor = _session_monitor(request)
    message = conftest.LINK_DEFENSE_MESSAGE.format(phase=when) + "自我測試放的違規"
    report = _report_through_the_real_hooks(
        request, when, lambda: pytest.fail(message, pytrace=False)
    )
    assert report.outcome == "failed"
    assert not hasattr(report, "wasxfail")


@pytest.mark.parametrize("when", PHASES)
def test_xfail_still_covers_other_failures(request, when):
    def fails() -> None:
        raise AssertionError("和防線無關的失敗")

    report = _report_through_the_real_hooks(request, when, fails)
    assert report.outcome == "skipped"
    assert report.wasxfail == "自我測試"


def test_teardown_reports_a_replacement_left_in_place(monkeypatch, request, defended):
    conftest, _monitor = _session_monitor(request)
    monkeypatch.setattr(conftest, "_LINK_MONITOR", defended.monitor)
    defended.monitor.allowed = {
        f"{MODULE_KEY}:test_teardown_reports_a_replacement_left_in_place": (
            1,
            "",
            frozenset({"fake_check"}),
        )
    }
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    try:
        phase = conftest.pytest_runtest_teardown(request.node, None)
        next(phase)
        with pytest.raises(pytest.fail.Exception, match="沒有換回"):
            phase.send(None)
    finally:
        patcher.stop()


def _teardown_phase(monkeypatch, request, defended) -> Any:
    """停在 teardown 中間的 hook，核對用這個測試自己的 ``Monitor``，不碰 session 那一個."""
    conftest, _monitor = _session_monitor(request)
    monkeypatch.setattr(conftest, "_LINK_MONITOR", defended.monitor)
    phase = conftest.pytest_runtest_teardown(request.node, None)
    next(phase)
    return phase


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("teardown 自己的失敗"), pytest.fail.Exception("teardown 自己的失敗")],
    ids=["exception", "pytest-fail"],
)
def test_a_failing_teardown_does_not_lose_a_violation(monkeypatch, request, defended, failure):
    """teardown 自己也失敗時，連結核對的違規照樣報，原本的失敗留在例外鏈上."""
    phase = _teardown_phase(monkeypatch, request, defended)
    defended.monitor.violations.append("自我測試放的違規")
    with pytest.raises(pytest.fail.Exception, match="自我測試放的違規") as caught:
        phase.throw(failure)
    assert caught.value.__context__ is failure


def test_a_failing_teardown_without_a_violation_keeps_its_failure(monkeypatch, request, defended):
    phase = _teardown_phase(monkeypatch, request, defended)
    with pytest.raises(RuntimeError, match="teardown 自己的失敗"):
        phase.throw(RuntimeError("teardown 自己的失敗"))


@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt(), SystemExit(1), pytest.exit.Exception("停")],
    ids=["keyboard", "system-exit", "pytest-exit"],
)
def test_an_interrupt_during_teardown_stays_an_interrupt(monkeypatch, request, defended, interrupt):
    """中斷不能變成一般失敗，否則整批測試繼續跑."""
    phase = _teardown_phase(monkeypatch, request, defended)
    defended.monitor.violations.append("自我測試放的違規")
    with pytest.raises(type(interrupt)):
        phase.throw(interrupt)
    assert defended.monitor.take() == []


def test_the_fixture_hooks_feed_the_monitor(monkeypatch, request, defended):
    conftest, _monitor = _session_monitor(request)
    monkeypatch.setattr(conftest, "_LINK_MONITOR", defended.monitor)
    wider = SimpleNamespace(scope="session")
    defended.monitor.allowed = {
        f"{MODULE_KEY}:test_the_fixture_hooks_feed_the_monitor": (
            1,
            "",
            frozenset({"fake_check"}),
        )
    }
    setup = conftest.pytest_fixture_setup(wider, request)
    next(setup)
    patcher = mock.patch.object(defended.module, "fake_check", _fake)
    patcher.start()
    with pytest.raises(StopIteration):
        setup.send(None)
    try:
        defended.monitor.check_leaks()
        assert defended.monitor.take() == []
        conftest.pytest_fixture_post_finalizer(wider, request)
        defended.monitor.check_leaks()
        assert "沒有換回" in defended.monitor.take()[0]
    finally:
        patcher.stop()


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

"""區內判斷與路徑物件（ADR-0293 R1、R3、R7、R8、R9、R12、R14）。

``zone_paths`` 只用標準函式庫：錨點、``zone_of``／``contains_zone``、``ZonePath`` 與它的誤用
通知、例外、觀察者、交接登記表、``current_roots``、``ensure_anchor``。全部在 pytest 的
``tmp_path`` 裡，不碰 ``data/``。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from memory_server.utils import zone_paths as zp
from memory_server.utils.zone_paths import (
    ALL_ANCHORS,
    ENFORCED_ANCHORS,
    Anchor,
    HandoffKind,
    ZonePath,
    ZonePathRequiredError,
    ZoneRoots,
    ZoneRootsUnavailableError,
    ZoneSegmentError,
    add_observer,
    anchor_at,
    anchor_dirname,
    contains_zone,
    current_roots,
    ensure_anchor,
    is_handed_off,
    is_zone_segment,
    remove_observer,
    set_roots_provider,
    zone_of,
)

from tests.support.links import make_dir_link
from tests.support.zone_records import expect_zone_refusal

WINDOWS = sys.platform == "win32"
DRIVE_ABSOLUTE = Path(__file__).anchor + "x"  # Windows 上是「磁碟機:\x」
PD, CD = Anchor.PERSONAL_DRIVE, Anchor.CUSTOM_DRIVE


@pytest.fixture
def events() -> list[zp.ZoneEvent]:
    """這個測試的觀察者紀錄；測試自己斷言內容，R13 的紀錄由這裡宣告掉."""
    seen: list[zp.ZoneEvent] = []
    add_observer(seen.append)
    with expect_zone_refusal(BaseException, at_least=0):
        yield seen
    remove_observer(seen.append)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "data"
    for rel in (
        "personal_drive/Alice",
        "custom/M1/drive",
        "mcp_servers/managed",
        "temp/docgen_output",
        "soul/Alice",
    ):
        (root / rel).mkdir(parents=True)
    (root / "custom" / "M1" / "config.json").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def roots(data: Path) -> ZoneRoots:
    state = data.parent / "state"
    state.mkdir()
    return ZoneRoots(data, state)


def _same_identity(monkeypatch: pytest.MonkeyPatch, alias: Path, real: Path) -> None:
    """``alias`` 的檔案身分當成和 ``real`` 一樣（模擬另一個掛載路徑；這台不能做 bind mount）。"""
    original = zp._identity
    real_id = original(str(real))

    def faked(path: str | Path) -> tuple[int, int] | None:
        if os.path.normcase(str(path)) == os.path.normcase(str(alias)):
            return real_id
        return original(path)

    monkeypatch.setattr(zp, "_identity", faked)


# --- 錨點與根 ---------------------------------------------------------------


def test_anchor_sets() -> None:
    assert frozenset(Anchor) == ALL_ANCHORS and len(ALL_ANCHORS) == 5
    assert {PD, CD} == ENFORCED_ANCHORS


def test_roots_give_literal_anchor_paths(data: Path, roots: ZoneRoots) -> None:
    assert roots.literal(PD) == str(data / "personal_drive")
    assert roots.literal(CD) == str(data / "custom")
    assert roots.literal(Anchor.MCP_SERVERS) == str(data / "mcp_servers" / "managed")
    assert roots.literal(Anchor.DOCGEN_OUTPUT) == str(data / "temp" / "docgen_output")
    assert roots.literal(Anchor.SANDBOX_STATE) == str(data.parent / "state")
    assert ZoneRoots(data, None).literal(Anchor.SANDBOX_STATE) is None


def test_anchor_dirname_is_relative_to_the_data_root() -> None:
    assert anchor_dirname(PD) == "personal_drive"
    assert anchor_dirname(CD) == "custom"
    assert anchor_dirname(Anchor.SANDBOX_STATE) is None


def test_roots_are_resolved_each_time(tmp_path: Path) -> None:
    """錨點後來才建成連結（人做的 junction）：判斷時現算，不快取建立當時的狀態。"""
    data = tmp_path.resolve() / "data"
    data.mkdir()
    roots = ZoneRoots(data, None)
    elsewhere = tmp_path.resolve() / "other_disk" / "pd"
    (elsewhere / "Alice").mkdir(parents=True)
    make_dir_link(data / "personal_drive", elsewhere)
    assert zone_of(elsewhere / "Alice" / "x.txt", roots) == (PD, ("Alice", "x.txt"))


# --- zone_of --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("personal_drive/Alice", (PD, ("Alice",))),
        ("personal_drive/Alice/x.txt", (PD, ("Alice", "x.txt"))),
        ("personal_drive/Bob/new/notes.md", (PD, ("Bob", "new", "notes.md"))),
        ("personal_drive", None),
        ("custom", None),
        ("custom/M1", None),
        ("custom/M1/config.json", None),
        ("custom/M1/drive", (CD, ("M1", "drive"))),
        ("custom/M1/drive/a/b.txt", (CD, ("M1", "drive", "a", "b.txt"))),
        ("custom/M1/driver/a", None),
        ("soul/Alice", None),
        ("mcp_servers/managed/tool", None),
        ("personal_drive/../soul/Alice", None),
        ("soul/../personal_drive/Alice/x", (PD, ("Alice", "x"))),
    ],
)
def test_zone_of_literal(data: Path, roots: ZoneRoots, rel: str, expected: object) -> None:
    assert zone_of(str(data) + "/" + rel, roots) == expected


def test_zone_of_only_looks_at_the_given_anchors(data: Path, roots: ZoneRoots) -> None:
    tool = data / "mcp_servers" / "managed" / "tool" / "main.py"
    assert zone_of(tool, roots) is None
    assert zone_of(tool, roots, anchors=ALL_ANCHORS) == (Anchor.MCP_SERVERS, ("tool", "main.py"))
    state = data.parent / "state" / "home" / "Alice"
    assert zone_of(state, roots, anchors=ALL_ANCHORS) == (Anchor.SANDBOX_STATE, ("home", "Alice"))
    assert zone_of(data / "personal_drive" / "Alice", roots, anchors={CD}) is None


@pytest.mark.skipif(not WINDOWS, reason="大小寫不分只在 Windows")
def test_zone_of_ignores_case_on_windows(data: Path, roots: ZoneRoots) -> None:
    assert zone_of(str(data) + "/PERSONAL_DRIVE/Alice/x", roots) == (PD, ("Alice", "x"))
    assert zone_of(str(data) + "/custom/M1/DRIVE/x", roots) == (CD, ("M1", "DRIVE", "x"))


def test_zone_of_follows_links_to_judge(tmp_path: Path, data: Path, roots: ZoneRoots) -> None:
    proj = tmp_path.resolve() / "proj"
    proj.mkdir()
    make_dir_link(proj / "to_alice", data / "personal_drive" / "Alice")
    make_dir_link(proj / "to_m1", data / "custom" / "M1")
    make_dir_link(proj / "to_m1_drive", data / "custom" / "M1" / "drive")
    assert zone_of(proj / "to_alice" / "x", roots) == (PD, ("Alice", "x"))
    assert zone_of(proj / "to_m1_drive" / "x", roots) == (CD, ("M1", "drive", "x"))
    assert zone_of(proj / "to_m1" / "drive" / "x", roots) == (CD, ("M1", "drive", "x"))
    assert zone_of(proj / "to_m1" / "config.json", roots) is None


def test_zone_of_when_the_data_root_or_an_anchor_is_a_link(tmp_path: Path, data: Path) -> None:
    base = tmp_path.resolve()
    make_dir_link(base / "data_link", data)
    via_link = ZoneRoots(base / "data_link", None)
    assert zone_of(data / "personal_drive" / "Alice" / "x", via_link) == (PD, ("Alice", "x"))
    assert zone_of(base / "data_link" / "personal_drive" / "Alice", via_link) == (PD, ("Alice",))


def test_zone_of_by_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: Path, roots: ZoneRoots
) -> None:
    alias = tmp_path.resolve() / "mounted_pd"
    (alias / "Alice").mkdir(parents=True)
    _same_identity(monkeypatch, alias, data / "personal_drive")
    assert zone_of(alias / "Alice" / "x", roots) == (PD, ("Alice", "x"))
    assert zone_of(alias / "Alice" / "x", roots, use_identity=False) is None
    assert zone_of(alias, roots) is None  # 錨點本身不算區內


def test_zone_of_custom_by_identity_folds_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: Path, roots: ZoneRoots
) -> None:
    alias = tmp_path.resolve() / "mounted_custom"
    (alias / "M1" / "DRIVE").mkdir(parents=True)
    _same_identity(monkeypatch, alias, data / "custom")
    assert zone_of(alias / "M1" / "DRIVE" / "x", roots) == (CD, ("M1", "DRIVE", "x"))
    assert zone_of(alias / "M1" / "config.json", roots) is None


def test_zone_of_without_following_looks_only_at_the_literal_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: Path, roots: ZoneRoots
) -> None:
    """``follow=False``：路徑與錨點都不解析、不比身分（權限自癒自己比對解析前後）."""
    base = tmp_path.resolve()
    make_dir_link(base / "to_alice", data / "personal_drive" / "Alice")
    assert zone_of(base / "to_alice" / "x", roots, follow=False) is None
    assert zone_of(data / "personal_drive" / "Alice" / "x", roots, follow=False) == (
        PD, ("Alice", "x")
    )
    assert zone_of(data / "custom" / "M1" / "drive", roots, follow=False) == (
        CD, ("M1", "drive")
    )
    make_dir_link(base / "data_link", data)
    via_link = ZoneRoots(base / "data_link", None)
    assert zone_of(data / "personal_drive" / "Alice", via_link, follow=False) is None
    alias = base / "mounted_pd"
    (alias / "Alice").mkdir(parents=True)
    _same_identity(monkeypatch, alias, data / "personal_drive")
    assert zone_of(alias / "Alice" / "x", roots, follow=False) is None


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("personal_drive", PD),
        ("custom", CD),
        ("mcp_servers/managed", Anchor.MCP_SERVERS),
        ("temp/docgen_output", Anchor.DOCGEN_OUTPUT),
        ("", None),
        ("mcp_servers", None),
        ("personal_drive/Alice", None),
        ("custom/M1", None),
        ("custom/M1/drive", None),
    ],
)
def test_anchor_at(data: Path, roots: ZoneRoots, rel: str, expected: object) -> None:
    path = os.path.join(str(data), rel) if rel else str(data)
    assert anchor_at(path, roots, anchors=ALL_ANCHORS) is expected


def test_anchor_at_state_and_the_given_anchors(data: Path, roots: ZoneRoots) -> None:
    assert anchor_at(data.parent / "state", roots, anchors=ALL_ANCHORS) is Anchor.SANDBOX_STATE
    assert anchor_at(data / "mcp_servers" / "managed", roots) is None  # 預設只看個人空間
    assert anchor_at(data / "personal_drive", roots, anchors={CD}) is None


def test_anchor_at_through_a_link(tmp_path: Path, data: Path, roots: ZoneRoots) -> None:
    base = tmp_path.resolve()
    make_dir_link(base / "to_pd", data / "personal_drive")
    assert anchor_at(base / "to_pd", roots) is PD


def test_identity_is_unknown_without_inode_numbers() -> None:
    assert zp._identity_of(SimpleNamespace(st_dev=5, st_ino=0)) is None
    assert zp._identity_of(SimpleNamespace(st_dev=5, st_ino=7)) == (5, 7)


# --- contains_zone ------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("", True),
        ("..", True),
        ("personal_drive", True),
        ("custom", True),
        ("custom/M1", True),
        ("custom/M1/drive", False),
        ("custom/M1/config.json", False),
        ("personal_drive/Alice", False),
        ("soul", False),
        ("soul/Alice", False),
    ],
)
def test_contains_zone(data: Path, roots: ZoneRoots, rel: str, expected: bool) -> None:
    path = os.path.join(str(data), rel) if rel else str(data)
    assert contains_zone(path, roots) is expected


def test_contains_zone_through_links_and_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: Path, roots: ZoneRoots
) -> None:
    base = tmp_path.resolve()
    make_dir_link(base / "data_link", data)
    assert contains_zone(base / "data_link", roots)
    alias = base / "mounted_data"
    alias.mkdir()
    assert not contains_zone(alias, roots)
    _same_identity(monkeypatch, alias, data)
    assert contains_zone(alias, roots)


# --- 段落與 ZonePath ------------------------------------------------------


BAD_EVERYWHERE = ["", ".", "..", " a", "a ", "a/b", "a\\b", "a\x00b"]
BAD_ON_WINDOWS = ["a:b", "CON", "con.txt", "COM1", "LPT9.log", "NUL", "a."]


@pytest.mark.parametrize("name", BAD_EVERYWHERE)
def test_unsafe_segments_are_refused(name: str) -> None:
    assert not is_zone_segment(name)


@pytest.mark.parametrize("name", BAD_ON_WINDOWS)
def test_windows_only_segments(name: str) -> None:
    assert is_zone_segment(name) is (not WINDOWS)


@pytest.mark.parametrize("name", ["notes.txt", "資料", "a b", ".touka", "con-tract", "COM10"])
def test_safe_segments(name: str) -> None:
    assert is_zone_segment(name)


def test_zone_path_basics() -> None:
    z = ZonePath(PD, ("Alice", "docs", "x.txt"))
    assert z.anchor is PD
    assert z.parts == ("Alice", "docs", "x.txt")
    assert z.name == "x.txt"
    assert z.display() == "zone://personal_drive/Alice/docs/x.txt"
    assert repr(z) == z.display()
    assert ZonePath(CD, ("M1", "drive")).display() == "zone://custom_drive/M1/drive"
    assert ZonePath(PD, ()).display() == "zone://personal_drive"
    with pytest.raises(AttributeError):
        z._parts = ("Bob",)


@pytest.mark.parametrize("parts", [("Alice", ".."), ("..",), ("Alice", "a/b"), ("Alice", "")])
def test_zone_path_refuses_unsafe_parts(parts: tuple[str, ...], events: list) -> None:
    with pytest.raises(ZoneSegmentError):
        ZonePath(PD, parts)
    assert [e.error for e in events] == [ZoneSegmentError]


@pytest.mark.parametrize("parts", [(), ("M1",), ("M1", "config.json"), ("M1", "Drive2")])
def test_custom_drive_needs_id_and_drive(parts: tuple[str, ...], events: list) -> None:
    with pytest.raises(ZoneSegmentError):
        ZonePath(CD, parts)


def test_child_and_join_rel() -> None:
    drive = ZonePath(PD, ("Alice",))
    assert drive.child("a", "b").parts == ("Alice", "a", "b")
    assert drive.join_rel("a/b/../c").parts == ("Alice", "a", "c")
    assert drive.join_rel("./a//b/").parts == ("Alice", "a", "b")
    assert drive.join_rel("") == drive
    docs = drive.child("docs")
    assert docs.join_rel("a/..") == docs


@pytest.mark.parametrize("text", ["../Bob/x", "a/../../x", "..", "/etc/passwd", "\\x"])
def test_join_rel_cannot_leave_the_receiver(text: str, events: list) -> None:
    with pytest.raises(ZoneSegmentError):
        ZonePath(PD, ("Alice",)).join_rel(text)
    with pytest.raises(ZoneSegmentError):
        ZonePath(PD, ("Alice", "docs")).join_rel("../x")


@pytest.mark.skipif(not WINDOWS, reason="磁碟機與反斜線只在 Windows")
@pytest.mark.parametrize("text", [DRIVE_ABSOLUTE, "C:x", "\\\\srv\\share\\x"])
def test_join_rel_refuses_windows_drives(text: str, events: list) -> None:
    with pytest.raises(ZoneSegmentError):
        ZonePath(PD, ("Alice",)).join_rel(text)


def test_join_rel_backslash_by_platform(events: list) -> None:
    drive = ZonePath(PD, ("Alice",))
    if WINDOWS:
        assert drive.join_rel("a\\b").parts == ("Alice", "a", "b")
    else:
        with pytest.raises(ZoneSegmentError):
            drive.join_rel("a\\b")


def test_parent_stops_at_the_agent() -> None:
    assert ZonePath(PD, ("Alice", "a")).parent == ZonePath(PD, ("Alice",))
    assert ZonePath(PD, ("Alice",)).parent == ZonePath(PD, ("Alice",))
    assert ZonePath(PD, ()).parent == ZonePath(PD, ())
    assert ZonePath(CD, ("M1", "drive", "x")).parent == ZonePath(CD, ("M1", "drive"))
    assert ZonePath(CD, ("M1", "drive")).parent == ZonePath(CD, ("M1", "drive"))


def test_relative_parts_is_under_and_same_agent(events: list) -> None:
    base = ZonePath(PD, ("Alice",))
    z = ZonePath(PD, ("Alice", "a", "b"))
    assert z.relative_parts(base) == ("a", "b")
    assert z.is_under(base) and base.is_under(base)
    assert not ZonePath(PD, ("Bob", "x")).is_under(base)
    assert not ZonePath(CD, ("Alice", "drive")).is_under(base)
    with pytest.raises(ZoneSegmentError):
        ZonePath(PD, ("Bob", "x")).relative_parts(base)
    assert z.same_agent(ZonePath(PD, ("Alice", "c")))
    assert not z.same_agent(ZonePath(PD, ("Bob",)))
    assert ZonePath(CD, ("M1", "drive", "a")).same_agent(ZonePath(CD, ("M1", "drive")))
    assert not ZonePath(CD, ("M1", "drive")).same_agent(ZonePath(CD, ("M2", "drive")))
    assert not ZonePath(PD, ("M1",)).same_agent(ZonePath(CD, ("M1", "drive")))


def test_equality_and_hash() -> None:
    a, b = ZonePath(PD, ("Alice", "x")), ZonePath(PD, ("Alice", "x"))
    assert a == b and hash(a) == hash(b)
    assert a != ZonePath(PD, ("Alice", "y"))
    assert a != "zone://personal_drive/Alice/x"
    folded = ZonePath(PD, ("alice", "X"))
    assert (a == folded) is WINDOWS
    if WINDOWS:
        assert hash(a) == hash(folded)


# --- R1：每一種誤用都先通知觀察者再丟錯 -------------------------------


def _z() -> ZonePath:
    return ZonePath(PD, ("Alice", "x.txt"))


MISUSES = {
    "__fspath__": lambda z: os.fspath(z),
    "__truediv__": lambda z: z / "x",
    "__rtruediv__": lambda z: "a" / z,
    "__add__": lambda z: z + "a",
    "__radd__": lambda z: "a" + z,
    "__str__": lambda z: str(z),
    "__format__": lambda z: f"{z:>5}",
    "__bytes__": lambda z: bytes(z),
}


@pytest.mark.parametrize("method", sorted(MISUSES))
def test_each_misuse_notifies_then_raises(method: str, events: list) -> None:
    with pytest.raises(ZonePathRequiredError):
        MISUSES[method](_z())
    assert [(e.error, e.detail, e.display) for e in events] == [
        (ZonePathRequiredError, method, "zone://personal_drive/Alice/x.txt")
    ]


def test_pathlib_names_notify_then_raise_attribute_error(events: list) -> None:
    with pytest.raises(AttributeError):
        _z().exists()
    assert [(e.error, e.detail) for e in events] == [(AttributeError, "exists")]
    assert not hasattr(_z(), "mkdir")
    assert len(events) == 2
    assert not hasattr(_z(), "not_a_path_method")
    assert len(events) == 2


@pytest.mark.parametrize(
    "use",
    [
        lambda z: Path(z),
        lambda z: open(z),  # noqa: SIM115, PTH123
        lambda z: os.path.join(z, "x"),  # noqa: PTH118
        lambda z: os.listdir(z),
        lambda z: Path("a") / z,
        lambda z: "%s" % z,  # noqa: UP031
    ],
    ids=["Path", "open", "join", "listdir", "Path-div", "percent"],
)
def test_common_path_uses_are_caught(use: object, events: list) -> None:
    with pytest.raises(TypeError):
        use(_z())
    assert events and all(e.display == _z().display() for e in events)


def test_lazy_log_formatting_is_caught(events: list) -> None:
    record = logging.LogRecord("x", logging.DEBUG, __file__, 1, "%s", (_z(),), None)
    with pytest.raises(ZonePathRequiredError):
        record.getMessage()
    assert [e.detail for e in events] == ["__str__"]


def test_known_limits_and_repr_do_not_notify(events: list) -> None:
    assert isinstance(_z(), os.PathLike)  # 代價：Z8 禁止產品程式用 PathLike 判斷
    with pytest.raises(TypeError):
        json.dumps(_z())  # 已知界線：標準函式庫只看類別名稱
    assert repr(_z()).startswith("zone://")
    assert "%r" % (_z(),) == _z().display()  # noqa: UP031
    assert events == []


# --- R12：例外與觀察者 --------------------------------------------------


def test_exception_types_and_messages(events: list) -> None:
    required, segment = ZonePathRequiredError(), ZoneSegmentError()
    assert isinstance(required, TypeError) and not isinstance(required, OSError)
    assert isinstance(segment, ValueError) and not isinstance(segment, OSError)
    assert str(required) == ZonePathRequiredError.MESSAGE
    assert str(segment) == ZoneSegmentError.MESSAGE
    assert [e.error for e in events] == [ZonePathRequiredError, ZoneSegmentError]


def test_observer_errors_do_not_reach_the_caller(events: list) -> None:
    def broken(_event: zp.ZoneEvent) -> None:
        raise RuntimeError

    add_observer(broken)
    try:
        assert isinstance(ZoneSegmentError(), ValueError)
    finally:
        remove_observer(broken)
    assert len(events) == 1


def test_default_observer_writes_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=zp.__name__)
    with expect_zone_refusal(ZonePathRequiredError), pytest.raises(ZonePathRequiredError):
        os.fspath(_z())
    lines = [r.getMessage() for r in caplog.records if r.name == zp.__name__]
    assert len(lines) == 1
    assert "ZonePathRequiredError" in lines[0] and "zone://personal_drive/Alice/x.txt" in lines[0]


# --- R7：交接登記表 -----------------------------------------------------


def test_handoff_registry_counts_and_releases(tmp_path: Path) -> None:
    text = str(tmp_path.resolve() / "cwd")
    kind = HandoffKind.SUBPROCESS_CWD
    assert not is_handed_off(text, kind)
    first = zp._register_handoff(text, kind)
    second = zp._register_handoff(text, kind)
    assert is_handed_off(text, kind)
    assert not is_handed_off(text, HandoffKind.PROC_FD)
    zp._release_handoff(first)
    zp._release_handoff(first)  # 重複取消不會多扣
    assert is_handed_off(text, kind)
    zp._release_handoff(second)
    assert not is_handed_off(text, kind)


def test_handoff_registry_compares_normalized_paths(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    token = zp._register_handoff(str(base / "a" / ".." / "b"), HandoffKind.PROC_FD)
    try:
        assert is_handed_off(str(base / "b"), HandoffKind.PROC_FD)
    finally:
        zp._release_handoff(token)


# --- R14：目前的 data 根 ------------------------------------------------


def test_current_roots_comes_only_from_the_provider(
    monkeypatch: pytest.MonkeyPatch, roots: ZoneRoots
) -> None:
    monkeypatch.setattr(zp, "_roots_provider", None)
    with pytest.raises(ZoneRootsUnavailableError):
        current_roots()
    set_roots_provider(lambda: roots)
    assert current_roots() is roots


# --- R9：錨點本身 -------------------------------------------------------


def test_ensure_anchor_creates_a_missing_anchor(tmp_path: Path) -> None:
    data = tmp_path.resolve() / "fresh"
    roots = ZoneRoots(data, None)
    assert ensure_anchor(roots, PD) == str(data / "personal_drive")
    assert (data / "personal_drive").is_dir()
    assert ensure_anchor(roots, PD) == str(data / "personal_drive")


def test_ensure_anchor_accepts_a_human_made_link(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "data").mkdir()
    (base / "nas" / "pd").mkdir(parents=True)
    make_dir_link(base / "data" / "personal_drive", base / "nas" / "pd")
    assert ensure_anchor(ZoneRoots(base / "data", None), PD) == str(base / "nas" / "pd")


def test_ensure_anchor_refuses_a_file(tmp_path: Path) -> None:
    data = tmp_path.resolve() / "data"
    data.mkdir()
    (data / "personal_drive").write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        ensure_anchor(ZoneRoots(data, None), PD)


def test_ensure_anchor_needs_a_state_root(tmp_path: Path) -> None:
    with pytest.raises(ZoneRootsUnavailableError):
        ensure_anchor(ZoneRoots(tmp_path, None), Anchor.SANDBOX_STATE)

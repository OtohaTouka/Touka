"""ResourceManager 的區內方法與產生函式（ADR-0293 Z3、Z5、R5、R7、R9、R10、R12）。

用真的 ResourceManager；data 根是 ``zone_data_root`` 給的 ``tmp_path``，不碰 ``data/``。連結另一端
是測試自己建的 ``outside`` 資料夾，每個案例都核對它沒被動到。
"""

from __future__ import annotations

import errno
import logging
import shutil
import sys
import types
from pathlib import Path

import pytest
from memory_server.utils import zone_paths as zp
from memory_server.utils.no_follow import LINK_REFUSED_MESSAGE, WalkReport, ZoneLinkRefusedError
from memory_server.utils.zone_paths import (
    Anchor,
    HandoffKind,
    ZonePath,
    ZonePathRequiredError,
    ZoneSegmentError,
    is_handed_off,
)
from src.models.identity import AgentIdentity
from src.services import resource_manager as rm_module
from src.services.identity_service import identity_service
from src.services.resource_manager import resource_manager as rm
from src.utils import permission_repair as pr
from src.utils.permission_repair import set_permission_repair

from tests.support.links import make_dir_link
from tests.support.zone_records import expect_zone_refusal, raises_zone

PD, CD = Anchor.PERSONAL_DRIVE, Anchor.CUSTOM_DRIVE
ALICE = AgentIdentity(agent_id="Alice", agent_role="Owner")
M1 = AgentIdentity(agent_id="M1", agent_role="Base")


@pytest.fixture
def data(zone_data_root: Path) -> Path:
    (zone_data_root / "personal_drive" / "Alice").mkdir(parents=True)
    (zone_data_root / "custom" / "M1" / "drive").mkdir(parents=True)
    (zone_data_root / "custom" / "M1" / "config.json").write_text("{}", encoding="utf-8")
    return zone_data_root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    other = tmp_path.resolve() / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("secret", encoding="utf-8")
    return other


@pytest.fixture
def events() -> list[zp.ZoneEvent]:
    """這個測試的觀察者紀錄；測試自己斷言內容，R13 的紀錄由這裡宣告掉."""
    seen: list[zp.ZoneEvent] = []
    zp.add_observer(seen.append)
    with expect_zone_refusal(BaseException, at_least=0):
        yield seen
    zp.remove_observer(seen.append)


def _untouched(outside: Path) -> bool:
    names = sorted(p.name for p in outside.iterdir())
    return names == ["secret.txt"] and (outside / "secret.txt").read_text("utf-8") == "secret"


def alice(*parts: str) -> ZonePath:
    return ZonePath(PD, ("Alice", *parts))


def _drive(data: Path) -> Path:
    return data / "personal_drive" / "Alice"


# --- 只收 ZonePath -----------------------------------------------------------

STRING_CALLS = {
    "zone_kind": lambda p: rm.zone_kind(p),
    "zone_read_text": lambda p: rm.zone_read_text(p),
    "zone_read_binary": lambda p: rm.zone_read_binary(p),
    "zone_open_read": lambda p: rm.zone_open_read(p),
    "zone_read_json": lambda p: rm.zone_read_json(p),
    "zone_write_text": lambda p: rm.zone_write_text(p, "x"),
    "zone_write_json": lambda p: rm.zone_write_json(p, {}),
    "zone_write_binary": lambda p: rm.zone_write_binary(p, b"x"),
    "zone_append_text": lambda p: rm.zone_append_text(p, "x"),
    "zone_make_dir": lambda p: rm.zone_make_dir(p),
    "zone_remove_file": lambda p: rm.zone_remove_file(p),
    "zone_remove_dir": lambda p: rm.zone_remove_dir(p),
    "zone_stat_files": lambda p: rm.zone_stat_files(p),
    "zone_list": lambda p: rm.zone_list(p),
    "zone_walk": lambda p: next(rm.zone_walk(p, WalkReport())),
    "zone_walk_open": lambda p: next(rm.zone_walk_open(p, WalkReport())),
    "zone_handoff": lambda p: rm.zone_handoff(p, HandoffKind.SUBPROCESS_CWD).__enter__(),
    "zone_rel_from_abs": lambda p: rm.zone_rel_from_abs(p, str(p)),
}


@pytest.mark.parametrize("call", STRING_CALLS.values(), ids=STRING_CALLS.keys())
@pytest.mark.parametrize("as_path", [False, True], ids=["str", "Path"])
def test_zone_methods_refuse_anything_but_a_zone_path(data, events, call, as_path):
    text = str(_drive(data) / "a.txt")
    with raises_zone(ZonePathRequiredError):
        call(Path(text) if as_path else text)
    assert [e.error for e in events] == [ZonePathRequiredError]
    assert sorted(p.name for p in _drive(data).iterdir()) == []


# --- 讀寫 --------------------------------------------------------------------


def test_reads_and_writes_inside_a_zone(data):
    note = alice("notes", "a.txt")
    assert rm.zone_kind(note) == "missing"
    rm.zone_write_text(note, "hello")  # 上層資料夾自己建
    rm.zone_append_text(note, " world")
    assert rm.zone_read_text(note) == "hello world"
    assert rm.zone_kind(note) == "file"
    assert rm.zone_kind(alice("notes")) == "dir"

    blob = alice("notes", "b.bin")
    rm.zone_write_binary(blob, b"\x00\x01\x02\x03")
    assert rm.zone_read_binary(blob) == b"\x00\x01\x02\x03"
    assert rm.zone_read_binary(blob, 2) == b"\x00\x01"
    handle, info = rm.zone_open_read(blob)
    with handle:
        assert handle.read() == b"\x00\x01\x02\x03"
    assert info.st_size == 4

    rm.zone_make_dir(alice("deep", "er"))
    assert (_drive(data) / "deep" / "er").is_dir()
    rm.zone_make_dir(alice("deep", "er"))  # 已存在沒關係

    rm.zone_write_json(alice("c.json"), {"k": "值"})
    assert rm.zone_read_json(alice("c.json")) == {"k": "值"}
    assert (_drive(data) / "c.json").read_text("utf-8").startswith("{\n    ")


def test_custom_model_drive_works_the_same(data):
    target = ZonePath(CD, ("M1", "drive", "x.txt"))
    rm.zone_write_text(target, "m1")
    assert (data / "custom" / "M1" / "drive" / "x.txt").read_text("utf-8") == "m1"


def test_read_json_gives_none_only_for_missing_or_corrupt(data, outside, caplog):
    assert rm.zone_read_json(alice("none.json")) is None
    (_drive(data) / "bad.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert rm.zone_read_json(alice("bad.json")) is None
    assert "zone://personal_drive/Alice/bad.json" in caplog.text
    assert str(data) not in caplog.text
    make_dir_link(_drive(data) / "peek", outside)
    with raises_zone(ZoneLinkRefusedError):  # 連結不塌縮成 None
        rm.zone_read_json(alice("peek", "secret.txt"))


def test_writes_create_a_missing_anchor(data):
    shutil.rmtree(data / "personal_drive")
    rm.zone_write_text(alice("a.txt"), "x")
    assert (_drive(data) / "a.txt").read_text("utf-8") == "x"
    shutil.rmtree(data / "personal_drive")
    with pytest.raises(FileNotFoundError):  # 讀不建錨點
        rm.zone_read_text(alice("a.txt"))
    assert not (data / "personal_drive").exists()


def test_removes(data):
    rm.zone_write_text(alice("a.txt"), "x")
    rm.zone_remove_file(alice("a.txt"))
    assert not (_drive(data) / "a.txt").exists()
    rm.zone_remove_file(alice("a.txt"))  # 預設不存在沒關係
    with pytest.raises(FileNotFoundError):
        rm.zone_remove_file(alice("a.txt"), missing_ok=False)

    rm.zone_write_text(alice("t", "u", "v.txt"), "x")
    rm.zone_remove_dir(alice("t"))
    assert not (_drive(data) / "t").exists()
    rm.zone_remove_dir(alice("t"))
    with pytest.raises(FileNotFoundError):
        rm.zone_remove_dir(alice("t"), missing_ok=False)


# --- 連結 --------------------------------------------------------------------

THROUGH_LINK = {
    "zone_read_text": lambda z: rm.zone_read_text(z),
    "zone_read_binary": lambda z: rm.zone_read_binary(z),
    "zone_open_read": lambda z: rm.zone_open_read(z),
    "zone_write_text": lambda z: rm.zone_write_text(z, "pwned"),
    "zone_write_binary": lambda z: rm.zone_write_binary(z, b"pwned"),
    "zone_append_text": lambda z: rm.zone_append_text(z, "pwned"),
    "zone_make_dir": lambda z: rm.zone_make_dir(z),
    "zone_remove_file": lambda z: rm.zone_remove_file(z),
    "zone_remove_dir": lambda z: rm.zone_remove_dir(z),
    "zone_kind": lambda z: rm.zone_kind(z),
    "zone_list": lambda z: rm.zone_list(z),
    "zone_stat_files": lambda z: rm.zone_stat_files(z),
    "zone_walk": lambda z: list(rm.zone_walk(z, WalkReport())),
    "zone_handoff": lambda z: rm.zone_handoff(z, HandoffKind.SUBPROCESS_CWD).__enter__(),
}


@pytest.mark.parametrize("call", THROUGH_LINK.values(), ids=THROUGH_LINK.keys())
def test_a_link_in_the_middle_is_refused(data, outside, events, call):
    make_dir_link(_drive(data) / "peek", outside)
    target = alice("peek", "secret.txt")
    with raises_zone(ZoneLinkRefusedError) as caught:
        call(target)
    exc = caught.value
    assert exc.filename == target.display()
    assert exc.errno == errno.ELOOP
    assert exc.zone_abs and Path(exc.zone_abs).is_absolute()
    assert str(outside) not in str(exc) and str(data) not in str(exc)
    assert LINK_REFUSED_MESSAGE in str(exc)
    assert ZoneLinkRefusedError in [e.error for e in events]
    assert _untouched(outside)


LAST_SEGMENT = {
    "zone_read_text": lambda z: rm.zone_read_text(z),
    "zone_write_text": lambda z: rm.zone_write_text(z, "pwned"),
    "zone_write_binary": lambda z: rm.zone_write_binary(z, b"pwned"),
    "zone_append_text": lambda z: rm.zone_append_text(z, "pwned"),
    "zone_make_dir": lambda z: rm.zone_make_dir(z),
    "zone_list": lambda z: rm.zone_list(z),
    "zone_stat_files": lambda z: rm.zone_stat_files(z),
    "zone_walk": lambda z: list(rm.zone_walk(z, WalkReport())),
    "zone_handoff": lambda z: rm.zone_handoff(z, HandoffKind.SUBPROCESS_CWD).__enter__(),
}


@pytest.mark.parametrize("call", LAST_SEGMENT.values(), ids=LAST_SEGMENT.keys())
def test_a_link_as_the_last_segment_is_refused(data, outside, call):
    make_dir_link(_drive(data) / "peek", outside)
    with raises_zone(ZoneLinkRefusedError) as caught:
        call(alice("peek"))
    assert caught.value.filename == alice("peek").display()
    assert _untouched(outside)
    assert not is_handed_off(str(_drive(data) / "peek"), HandoffKind.SUBPROCESS_CWD)


def test_a_link_as_the_last_segment_is_seen_or_removed_not_entered(data, outside):
    make_dir_link(_drive(data) / "peek", outside)
    assert rm.zone_kind(alice("peek")) == "link"
    rm.zone_remove_file(alice("peek"))  # 刪連結本身
    assert not (_drive(data) / "peek").exists()
    make_dir_link(_drive(data) / "peek", outside)
    rm.zone_remove_dir(alice("peek"))
    assert not (_drive(data) / "peek").exists()
    assert _untouched(outside)


def test_errors_carry_only_the_display(data):
    with pytest.raises(FileNotFoundError) as caught:
        rm.zone_read_text(alice("missing.txt"))
    exc = caught.value
    assert exc.errno == errno.ENOENT
    assert exc.filename == "zone://personal_drive/Alice/missing.txt"
    assert exc.zone_abs == str(_drive(data) / "missing.txt")
    assert str(data) not in str(exc)


def test_an_anchor_that_cannot_be_made_carries_only_the_display(zone_data_root):
    """錨點建不起來（同名的是檔案）時，例外也只帶顯示路徑（部署錯誤時會回給模型）."""
    (zone_data_root / "personal_drive").write_text("not a folder", encoding="utf-8")
    with pytest.raises(NotADirectoryError) as caught:
        rm.zone_write_text(alice("a.txt"), "x")
    assert caught.value.filename == "zone://personal_drive/Alice/a.txt"
    assert str(zone_data_root) not in str(caught.value)


def test_permission_repair_gets_the_absolute_path(data, monkeypatch):
    rm.zone_write_text(alice("a.txt"), "ok")
    target = str(_drive(data) / "a.txt")
    real = rm_module.read_text_no_follow
    runs: list[str] = []

    def denied_once(path, **kwargs):
        runs.append(str(path))
        if len(runs) == 1:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path, **kwargs)

    class Hook:
        calls: list[tuple[object, ...]] = []

        def repair(self, method, paths, *, retry):
            self.calls.append((method, paths, retry))
            return True

        def note_retry(self, method, succeeded):
            self.calls.append((method, succeeded))

    hook = Hook()
    monkeypatch.setattr(rm_module, "read_text_no_follow", denied_once)
    monkeypatch.setattr(pr, "sys", types.SimpleNamespace(platform="linux"))
    set_permission_repair(hook)
    try:
        assert rm.zone_read_text(alice("a.txt")) == "ok"
    finally:
        set_permission_repair(None)
    assert hook.calls == [("zone_read_text", (target, None), True), ("zone_read_text", True)]
    assert runs == [target, target]


# --- 列樹與統計 --------------------------------------------------------------


def _fill(root: Path, outside: Path) -> Path:
    for rel in ("a.txt", "Bee/c.txt", "Bee/deep/d/e.txt", "zed.md", ".git/x", "node_modules/m"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel, encoding="utf-8")
    (root / "empty").mkdir()
    (root / "Bee" / "deep" / "skip").mkdir()
    make_dir_link(root / "peek", outside)
    return root


@pytest.fixture
def tree(data: Path, outside: Path) -> Path:
    return _fill(_drive(data), outside)


@pytest.fixture
def twin(tree: Path, tmp_path: Path, outside: Path) -> Path:
    """``tree`` 的複本放在 data 根以外：字串方法對它的結果就是區內方法該有的.

    Z2 拒絕區裡的字串路徑，所以不能直接拿 ``tree`` 比。
    """
    return _fill(tmp_path.resolve() / "twin", outside)


def _as_tree(lines: list[str], twin: Path, tree: Path) -> list[str]:
    return [line.replace(str(twin), str(tree)) for line in lines]


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 5])
def test_zone_list_matches_list_directory(tree, twin, outside, depth):
    listed = rm.list_directory(str(twin), max_depth=depth)
    assert rm.zone_list(alice(), max_depth=depth) == _as_tree(listed, twin, tree)
    hide = frozenset({"deep"})
    listed = rm.list_directory(str(twin), depth, hide)
    assert rm.zone_list(alice(), depth, hide) == _as_tree(listed, twin, tree)
    assert _untouched(outside)


def test_zone_list_shows_links_without_entering_them(tree):
    lines = rm.zone_list(alice(), max_depth=3)
    assert "├── peek (link, not followed)" in lines
    assert not any("secret" in line for line in lines)
    assert not any(".git" in line or "node_modules" in line for line in lines)


def test_zone_list_of_a_file_or_nothing(tree, twin):
    for name in ("a.txt", "nope"):
        listed = rm.list_directory(str(twin / name))
        assert rm.zone_list(alice(name)) == _as_tree(listed, twin, tree)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"exclude_names": frozenset({"deep"})},
        {"exclude_paths": frozenset({"Bee/deep"})},
        {"exclude_globs": ("*.md",)},
        {"max_files": 2},
    ],
)
def test_zone_stat_files_matches_stat_files(tree, twin, outside, options):
    rows = rm.zone_stat_files(alice(), **options)
    assert [row[:2] for row in rows] == [row[:2] for row in rm.stat_files(twin, **options)]
    assert _untouched(outside)


def test_zone_stat_files_of_a_file_or_nothing(tree):
    assert rm.zone_stat_files(alice("a.txt")) == []
    assert rm.zone_stat_files(alice("nope")) == []


def test_zone_walk_and_walk_open(tree):
    report = WalkReport()
    seen = {"/".join(e.parts): e.kind for e in rm.zone_walk(alice("Bee"), report)}
    assert seen == {"c.txt": "file", "deep": "dir", "deep/d": "dir", "deep/d/e.txt": "file",
                    "deep/skip": "dir"}
    opened = []
    for parts, handle, info in rm.zone_walk_open(alice("Bee"), WalkReport()):
        opened.append(("/".join(parts), handle.read().decode(), info.st_size))
    assert sorted(opened) == [("c.txt", "Bee/c.txt", 9), ("deep/d/e.txt", "Bee/deep/d/e.txt", 16)]
    found = WalkReport()
    assert [e.parts for e in rm.zone_walk(alice(), found) if e.kind == "link"] == []
    assert found.links == 1
    assert list(rm.zone_walk(alice("nope"), WalkReport())) == []


# --- 絕對路徑轉回區內路徑 ----------------------------------------------------


def test_rel_from_abs_by_literal_place(data):
    drive = _drive(data)
    base = alice()
    assert rm.zone_rel_from_abs(base, str(drive / "notes.txt")) == alice("notes.txt")
    assert rm.zone_rel_from_abs(base, str(drive / "sub" / ".." / "n.txt")) == alice("n.txt")
    assert rm.zone_rel_from_abs(base, str(drive)) == alice()
    assert rm.zone_rel_from_abs(alice("sub"), str(drive / "sub" / "x")) == alice("sub", "x")
    assert rm.zone_rel_from_abs(base, str(drive / ".." / "Bob" / "x")) is None
    assert rm.zone_rel_from_abs(base, str(drive.parent / "Alice2" / "x")) is None
    assert rm.zone_rel_from_abs(base, str(data / "soul" / "Alice")) is None
    assert rm.zone_rel_from_abs(base, "/notes.txt") is None
    assert rm.zone_rel_from_abs(base, "notes.txt") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 不分大小寫")
def test_rel_from_abs_ignores_case_on_windows(data):
    text = str(_drive(data) / "Notes.TXT").upper()
    assert rm.zone_rel_from_abs(alice(), text) == alice("NOTES.TXT")


def test_rel_from_abs_by_resolved_place(tmp_path, zone_data_root):
    """錨點本身是人做的連結（R9）：字面與解析後的位置都認."""
    real = tmp_path.resolve() / "elsewhere"
    (real / "Alice").mkdir(parents=True)
    make_dir_link(zone_data_root / "personal_drive", real)
    by_literal = zone_data_root / "personal_drive" / "Alice" / "a.txt"
    assert rm.zone_rel_from_abs(alice(), str(by_literal)) == alice("a.txt")
    assert rm.zone_rel_from_abs(alice(), str(real / "Alice" / "a.txt")) == alice("a.txt")
    rm.zone_write_text(alice("a.txt"), "x")  # 經過人做的錨點連結照常讀寫
    assert (real / "Alice" / "a.txt").read_text("utf-8") == "x"


# --- 交出字串 ----------------------------------------------------------------


def test_handoff_registers_only_inside_the_block(data):
    rm.zone_make_dir(alice("work"))
    target = str(_drive(data) / "work")
    with rm.zone_handoff(alice("work"), HandoffKind.SUBPROCESS_CWD) as text:
        assert text == target
        assert is_handed_off(text, HandoffKind.SUBPROCESS_CWD)
        assert not is_handed_off(text, HandoffKind.PROC_FD)
    assert not is_handed_off(target, HandoffKind.SUBPROCESS_CWD)


def test_handoff_releases_when_the_block_raises(data):
    target = str(_drive(data) / "x")
    with pytest.raises(KeyError), rm.zone_handoff(alice("x"), HandoffKind.PROC_FD):
        raise KeyError
    assert not is_handed_off(target, HandoffKind.PROC_FD)


# --- 整個代理人資料夾 --------------------------------------------------------


def test_rename_agent_container(data, outside):
    old = data / "personal_drive" / "Old"
    (old / "sub").mkdir(parents=True)
    (old / "sub" / "f.txt").write_text("f", encoding="utf-8")
    rm.rename_agent_container(PD, "Old", "New")
    assert (data / "personal_drive" / "New" / "sub" / "f.txt").read_text("utf-8") == "f"
    assert not old.exists()
    with pytest.raises(FileExistsError):
        rm.rename_agent_container(PD, "New", "Alice")
    rm.rename_agent_container(PD, "New", "new")  # 只差大小寫照常改名
    assert [p.name for p in (data / "personal_drive").iterdir() if p.name.lower() == "new"] == [
        "new"
    ]
    make_dir_link(data / "personal_drive" / "Peek", outside)
    with raises_zone(ZoneLinkRefusedError) as caught:
        rm.rename_agent_container(PD, "Peek", "Mine")
    assert caught.value.filename == "zone://personal_drive/Peek"
    assert _untouched(outside)
    with raises_zone(ZoneSegmentError):
        rm.rename_agent_container(PD, "new", "../x")


def test_rename_custom_model_moves_the_whole_folder(data):
    rm.rename_agent_container(CD, "M1", "M2")
    assert (data / "custom" / "M2" / "config.json").exists()
    assert (data / "custom" / "M2" / "drive").is_dir()


def test_remove_custom_model_dir(data, outside):
    (data / "custom" / "M1" / "drive" / "x.txt").write_text("x", encoding="utf-8")
    assert rm.remove_custom_model_dir(M1) is True
    assert not (data / "custom" / "M1").exists()
    assert rm.remove_custom_model_dir(M1) is False
    make_dir_link(data / "custom" / "M2", outside)
    with raises_zone(ZoneLinkRefusedError) as caught:
        rm.remove_custom_model_dir(AgentIdentity(agent_id="M2", agent_role="Base"))
    assert caught.value.filename == "zone://custom_drive/M2"
    assert _untouched(outside)
    with raises_zone(ZoneSegmentError):
        rm.remove_custom_model_dir(AgentIdentity(agent_id="..", agent_role="Base"))


def test_remove_custom_model_dir_does_not_follow_links_inside(data, outside):
    make_dir_link(data / "custom" / "M1" / "drive" / "peek", outside)
    assert rm.remove_custom_model_dir(M1) is True
    assert not (data / "custom" / "M1").exists()
    assert _untouched(outside)


# --- 產生函式 ----------------------------------------------------------------


def test_generators(data):
    assert rm.agent_drive(ALICE) == ZonePath(PD, ("Alice",))
    assert rm.agent_drive(M1) == ZonePath(CD, ("M1", "drive"))
    assert rm.soul_drive(M1) == ZonePath(PD, ("M1",))
    assert rm.custom_drive(ALICE) == ZonePath(CD, ("Alice", "drive"))
    assert rm.outputs_dir(ALICE) == alice("outputs")
    assert rm.uploads_dir(ALICE) == alice("outputs", "uploads")
    assert rm.outputs_dir(M1) == ZonePath(CD, ("M1", "drive", "outputs"))
    assert rm.anchor_root(PD) == ZonePath(PD, ())
    assert list((data / "personal_drive").iterdir()) == [_drive(data)]  # 產生函式不建資料夾


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "a\\b", "", "CON"])
def test_generators_give_none_for_a_bad_id(data, events, bad):
    shaky = AgentIdentity(agent_id=bad, agent_role="Owner")
    for make in (rm.agent_drive, rm.soul_drive, rm.custom_drive, rm.outputs_dir, rm.uploads_dir):
        assert make(shaky) is None
    assert events == []


def test_agent_drive_for_looks_up_the_registered_identity(data, monkeypatch):
    looked: list[str] = []

    def get_identity(agent_id):
        looked.append(agent_id)
        return ALICE if agent_id == "Alice" else M1

    monkeypatch.setattr(identity_service, "get_identity", get_identity)
    assert rm.agent_drive_for("Alice") == ZonePath(PD, ("Alice",))
    assert rm.agent_drive_for("M1") == ZonePath(CD, ("M1", "drive"))
    assert looked == ["Alice", "M1"]


def test_get_vault_path_no_longer_gives_the_personal_drive(data, events):
    with raises_zone(ZonePathRequiredError):
        rm.get_vault_path("personal", ALICE)
    assert [e.error for e in events] == [ZonePathRequiredError]
    assert not hasattr(rm, "get_vault_path_for_agent")
    # 舊的輸出、上傳路徑函式不能再出現，由 Z8 的 T1 檢查（tests/lint/test_zone_path_guard.py）

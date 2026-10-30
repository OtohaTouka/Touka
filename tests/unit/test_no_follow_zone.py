"""代理人資料夾的單項原語、走訪、剪枝走訪與刪樹（ADR-0293 Z4、R5、R12）。

走訪不進入、不列出連結；剪枝走訪遇到代理人資料夾剪掉；刪樹遇到連結只刪連結本身、不因深度
失敗。換掉、換檔案系統、權限這些情況用 seam 模擬。全部在 pytest 的 ``tmp_path`` 裡，連結另一端
是測試自己建的假資料夾，不碰 ``data/``。
"""

from __future__ import annotations

import errno
import io
import os
import stat
import sys
import zipfile
from typing import TYPE_CHECKING

import pytest
from memory_server.utils import no_follow
from memory_server.utils import zone_paths as zp
from memory_server.utils.no_follow import (
    TREE_KEPT_CHANGING_MESSAGE,
    WALK_CHANGED,
    WALK_TOO_DEEP,
    LinkRefusedError,
    WalkReport,
    ZoneLinkRefusedError,
    iter_regular_files,
    kind_no_follow,
    make_dirs_no_follow,
    open_read_no_follow,
    read_bytes_no_follow,
    remove_tree_no_follow,
    rename_entry_no_follow,
    walk_no_follow,
    walk_open_no_follow,
    walk_pruned,
    write_bytes_no_follow,
    write_stream_no_follow,
    zip_open_file,
    zone_errors,
)
from memory_server.utils.zone_paths import Anchor, ZoneRoots, ZoneSegmentError

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

# 這個平台實際用的容器操作（seam 換它的方法）
OPS = no_follow._FdTree if no_follow._POSIX_DIR_FD else no_follow._PathTree
PD, CD = Anchor.PERSONAL_DRIVE, Anchor.CUSTOM_DRIVE


def _tree(base: Path, *rels: str) -> None:
    """建檔案（內容是相對路徑）與資料夾（以 ``/`` 結尾）。"""
    for rel in rels:
        target = base / rel
        if rel.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rel, encoding="utf-8")


def _other_device(info: os.stat_result) -> os.stat_result:
    return os.stat_result((info.st_mode, info.st_ino, info.st_dev + 1, *info[3:10]))


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def outside(base: Path) -> Path:
    """根外面的假 hook 設定資料夾（連結的另一端）。"""
    folder = base / "config_hooks"
    folder.mkdir()
    (folder / "hooks.json").write_text("{}", encoding="utf-8")
    return folder


@pytest.fixture
def events() -> list[zp.ZoneEvent]:
    """這個測試的觀察者紀錄；測試自己斷言內容，R13 的紀錄由這裡宣告掉."""
    seen: list[zp.ZoneEvent] = []
    zp.add_observer(seen.append)
    with expect_zone_refusal(BaseException, at_least=0):
        yield seen
    zp.remove_observer(seen.append)


def _untouched(outside: Path) -> bool:
    return [p.name for p in outside.iterdir()] == ["hooks.json"] and (
        outside / "hooks.json"
    ).read_text(encoding="utf-8") == "{}"


# --- 單項原語 ----------------------------------------------------------------


def test_kind_tells_what_the_last_segment_is(base, outside):
    _tree(base, "d/", "f.txt")
    make_dir_link(base / "link", outside)
    assert kind_no_follow(base / "f.txt") == "file"
    assert kind_no_follow(base / "d") == "dir"
    assert kind_no_follow(base / "link") == "link"
    for missing in ("nope", "nope/deeper", "f.txt/under_a_file"):
        assert kind_no_follow(base / missing) == "missing"
    with pytest.raises(LinkRefusedError):
        kind_no_follow(base / "link" / "hooks.json")
    if hasattr(os, "mkfifo"):
        os.mkfifo(base / "pipe")
        assert kind_no_follow(base / "pipe") == "other"


@pytest.mark.parametrize("read", [read_bytes_no_follow, open_read_no_follow])
def test_reads_refuse_links_and_folders(base, outside, read):
    make_dir_link(base / "link", outside)
    with pytest.raises(LinkRefusedError):
        read(base / "link" / "hooks.json")
    with pytest.raises(OSError):  # POSIX 開得了資料夾、不是一般檔案；Windows 開不了資料夾
        read(base)


def test_reads_give_the_bytes_and_the_opened_status(base):
    (base / "f.bin").write_bytes(b"0123456789")
    assert read_bytes_no_follow(base / "f.bin", 4) == b"0123"
    handle, info = open_read_no_follow(base / "f.bin")
    with handle:
        assert handle.read() == b"0123456789"
    assert info.st_size == 10


def test_write_bytes_creates_truncates_and_refuses_links(base, outside):
    target = base / "new" / "f.bin"
    write_bytes_no_follow(target, b"longer content")
    write_bytes_no_follow(target, b"short")
    assert target.read_bytes() == b"short"
    make_dir_link(base / "link", outside)
    with pytest.raises(LinkRefusedError):
        write_bytes_no_follow(base / "link" / "hooks.json", b"x")
    assert _untouched(outside)


def test_write_stream_copies_truncates_and_refuses_links(base, outside):
    target = base / "new" / "f.bin"
    write_stream_no_follow(target, io.BytesIO(b"longer content"))
    write_stream_no_follow(target, io.BytesIO(b"short"))
    assert target.read_bytes() == b"short"
    make_dir_link(base / "link", outside)
    with pytest.raises(LinkRefusedError):
        write_stream_no_follow(base / "link" / "hooks.json", io.BytesIO(b"x"))
    assert _untouched(outside)


def test_make_dirs_refuses_a_link_anywhere(base, outside):
    make_dirs_no_follow(base / "a" / "b")
    make_dirs_no_follow(base / "a" / "b")
    assert (base / "a" / "b").is_dir()
    make_dir_link(base / "link", outside)
    for target in (base / "link", base / "link" / "sub"):
        with pytest.raises(LinkRefusedError):
            make_dirs_no_follow(target)
    assert _untouched(outside)


def test_rename_entry(base, outside):
    _tree(base, "Alice/notes.txt", "Bob/")
    rename_entry_no_follow(base, "Alice", "alice")  # 只差大小寫（大小寫不分的檔案系統是同一個）
    assert "alice" in os.listdir(base)
    assert "Alice" not in os.listdir(base)
    with pytest.raises(FileExistsError):
        rename_entry_no_follow(base, "alice", "Bob")
    with pytest.raises(FileNotFoundError):
        rename_entry_no_follow(base, "Carol", "Dave")
    make_dir_link(base / "link", outside)
    with pytest.raises(LinkRefusedError):
        rename_entry_no_follow(base, "link", "renamed")
    for bad in ("..", "a/b", "", " x"):
        with expect_zone_refusal("ZoneSegmentError"), pytest.raises(ZoneSegmentError):
            rename_entry_no_follow(base, "alice", bad)
    assert (base / "alice" / "notes.txt").is_file()
    assert (base / "link").exists()


# --- 例外（R12） --------------------------------------------------------------


def test_zone_errors_keep_type_and_errno_but_show_only_the_display(base):
    absolute = str(base / "personal_drive" / "Alice" / "notes.txt")
    with pytest.raises(PermissionError) as caught, zone_errors("Alice/notes.txt"):
        raise PermissionError(errno.EACCES, "Permission denied", absolute, None, absolute)
    err = caught.value
    assert type(err) is PermissionError
    assert (err.errno, err.filename, err.filename2) == (errno.EACCES, "Alice/notes.txt", None)
    assert err.zone_abs == absolute
    assert absolute not in str(err)
    assert str(err).endswith("'Alice/notes.txt'")


def test_link_refusals_become_zone_refusals_and_notify(base, events):
    absolute = str(base / "Alice" / "x")
    with pytest.raises(ZoneLinkRefusedError) as caught, zone_errors("Alice/x"):
        raise LinkRefusedError(errno.ELOOP, "link", absolute)
    assert (caught.value.filename, caught.value.zone_abs) == ("Alice/x", absolute)
    assert absolute not in str(caught.value)
    assert [(e.error, e.display) for e in events] == [(ZoneLinkRefusedError, "Alice/x")]
    with pytest.raises(ZoneLinkRefusedError) as again, zone_errors("other"):
        raise caught.value
    assert again.value is caught.value
    with pytest.raises(KeyError), zone_errors("x"):
        raise KeyError


# --- 走訪 --------------------------------------------------------------------


def _walked(root: Path, found: WalkReport | None = None) -> list[tuple[str, str]]:
    report = WalkReport() if found is None else found
    return [("/".join(e.parts), e.kind) for e in walk_no_follow(root, report)]


def test_walk_order_links_and_bad_names(base, outside):
    _tree(base / "z", "b.txt", "a.txt", "sub/c.txt", "sub/deeper/d.txt", "other/", " lead.txt")
    make_dir_link(base / "z" / "peek", outside)
    found = WalkReport()
    assert _walked(base / "z", found) == [
        ("a.txt", "file"),
        ("b.txt", "file"),
        ("other", "dir"),
        ("sub", "dir"),
        ("sub/c.txt", "file"),
        ("sub/deeper", "dir"),
        ("sub/deeper/d.txt", "file"),
    ]
    assert (found.links, found.bad_names, found.errors, found.pruned) == (1, 1, [], [])
    assert _walked(base / "missing") == []
    with pytest.raises(LinkRefusedError):
        _walked(base / "z" / "peek")


def test_walk_records_depth_device_and_swaps(base, monkeypatch):
    _tree(base / "z", "a/b/c.txt", "dev/x.txt", "gone/")
    monkeypatch.setattr(no_follow, "MAX_ZONE_WALK_DEPTH", 1)
    lstat, open_dir = OPS.lstat, OPS.open_dir

    def other_device(self, holder, name, shown):
        info = lstat(self, holder, name, shown)
        return _other_device(info) if name == "dev" else info

    def swapped(self, holder, name, info, shown):
        if name == "gone":
            raise no_follow._changed(shown)
        return open_dir(self, holder, name, info, shown)

    monkeypatch.setattr(OPS, "lstat", other_device)
    monkeypatch.setattr(OPS, "open_dir", swapped)
    found = WalkReport()
    assert _walked(base / "z", found) == [("a", "dir")]
    assert sorted(found.errors) == [
        ("a/b", WALK_TOO_DEEP),
        ("dev", errno.EXDEV),
        ("gone", WALK_CHANGED),
    ]


def test_walk_records_folders_it_cannot_read(base, monkeypatch):
    _tree(base / "z", "a/x.txt", "a/locked/y.txt", "b/z.txt")
    lstat = OPS.lstat

    def denied(self, holder, name, shown):
        if name == "locked":
            raise PermissionError(errno.EACCES, "Permission denied", str(shown))
        return lstat(self, holder, name, shown)

    monkeypatch.setattr(OPS, "lstat", denied)
    found = WalkReport()
    assert [rel for rel, kind in _walked(base / "z", found) if kind == "file"] == [
        "a/x.txt",
        "b/z.txt",
    ]
    assert found.errors == [("a/locked", errno.EACCES)]


def test_walk_open_gives_open_files_and_closes_them(base, monkeypatch):
    _tree(base / "z", "a.txt", "b.txt", "swapped.txt", "sub/c.txt")
    open_file = OPS.open_file

    def swapped(self, holder, name, info, shown):
        if name == "swapped.txt":
            raise LinkRefusedError(errno.ELOOP, "link", str(shown))
        return open_file(self, holder, name, info, shown)

    monkeypatch.setattr(OPS, "open_file", swapped)
    found, seen, handles = WalkReport(), [], []
    for parts, handle, info in walk_open_no_follow(base / "z", found):
        seen.append(("/".join(parts), handle.read().decode(), info.st_size))
        handles.append(handle)
    assert seen == [("a.txt", "a.txt", 5), ("b.txt", "b.txt", 5), ("sub/c.txt", "sub/c.txt", 9)]
    assert all(h.closed for h in handles)
    assert found.links == 1


def test_zip_open_file_matches_zipfile_write(base):
    """走訪開好的檔案寫進 zip：大小、時間、權限、壓縮方式和 ``ZipFile.write`` 一樣."""
    source = base / "note.txt"
    source.write_bytes(b"hello zone")
    os.utime(source, (1_700_000_000, 1_700_000_000))
    with zipfile.ZipFile(base / "a.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source, "by_write.txt")
        with source.open("rb") as handle:
            zip_open_file(zf, "by_fd.txt", handle, os.fstat(handle.fileno()))
    with zipfile.ZipFile(base / "a.zip") as zf:
        expected, got = zf.getinfo("by_write.txt"), zf.getinfo("by_fd.txt")
        assert zf.read("by_fd.txt") == b"hello zone"
    for name in ("file_size", "date_time", "external_attr", "compress_type", "CRC"):
        assert getattr(got, name) == getattr(expected, name), name


def test_zip_open_file_writes_no_more_than_fstat_said(base):
    """檔案在寫進 zip 的時候變大，也只寫 ``fstat`` 當時的大小（上限照 ``fstat`` 算才有意義）."""
    source = base / "growing.bin"
    source.write_bytes(b"x" * 10)
    with zipfile.ZipFile(base / "b.zip", "w") as zf, source.open("rb") as handle:
        info = os.fstat(handle.fileno())
        source.write_bytes(b"x" * 50)  # 同一個 inode 變大
        zip_open_file(zf, "growing.bin", handle, info)
    with zipfile.ZipFile(base / "b.zip") as zf:
        assert zf.read("growing.bin") == b"x" * 10


# --- 剪枝走訪（R5） ------------------------------------------------------------


@pytest.fixture
def data(base: Path) -> Path:
    root = base / "data"
    _tree(
        root,
        "personal_drive/Alice/notes.txt",
        "custom/M1/drive/x.txt",
        "custom/M1/config.json",
        "soul/Alice/s.md",
    )
    return root


@pytest.fixture
def roots(data: Path) -> ZoneRoots:
    return ZoneRoots(data, None)


def _pruned(root: Path, roots: ZoneRoots, **options) -> tuple[list, list]:
    found = WalkReport()
    walked = [("/".join(e.parts), e.kind) for e in walk_pruned(root, roots, found, **options)]
    return walked, found.pruned


def test_walk_pruned_cuts_agent_folders(data, roots):
    assert _pruned(data, roots) == (
        [
            ("custom", "dir"),
            ("custom/M1", "dir"),
            ("custom/M1/config.json", "file"),
            ("soul", "dir"),
            ("soul/Alice", "dir"),
            ("soul/Alice/s.md", "file"),
        ],
        [(CD, ("M1", "drive")), (PD, ())],
    )
    walked, _cut = _pruned(data, roots, skip_dir=lambda parts: parts[-1] == "soul")
    assert [rel for rel, _kind in walked] == ["custom", "custom/M1", "custom/M1/config.json"]


def test_walk_pruned_from_inside_an_anchor_or_a_zone(data, roots):
    assert _pruned(data / "personal_drive" / "Alice", roots) == ([], [(PD, ("Alice",))])
    assert _pruned(data / "personal_drive", roots) == ([], [(PD, ())])
    assert _pruned(data / "custom" / "M1", roots) == (
        [("config.json", "file")],
        [(CD, ("M1", "drive"))],
    )


def test_walk_pruned_does_not_follow_links(base, data, roots):
    (base / "project").mkdir()
    make_dir_link(base / "project" / "peek", data / "personal_drive")
    found = WalkReport()
    assert list(walk_pruned(base / "project", roots, found)) == []
    assert (found.links, found.pruned) == (1, [])


def test_walk_pruned_knows_an_anchor_by_identity(base, data, roots, monkeypatch):
    """同一個個人空間從另一個掛載路徑看到（模擬 bind mount）：認得出來、剪掉."""
    alias = base / "mnt" / "pd_alias"
    _tree(alias, "Alice/notes.txt")
    alias_id = zp._identity(str(alias))
    anchor, identity = os.path.normcase(str(data / "personal_drive")), zp._identity

    def same_as_alias(path):
        return alias_id if os.path.normcase(str(path)) == anchor else identity(path)

    monkeypatch.setattr(zp, "_identity", same_as_alias)
    assert _pruned(base / "mnt", roots) == ([], [(PD, ())])


def test_iter_regular_files_uses_the_registered_roots(data, roots, monkeypatch):
    monkeypatch.setattr(zp, "_roots_provider", lambda: roots)
    rels = [rel for rel, _path in iter_regular_files(data, frozenset({"soul"}))]
    assert rels == ["custom/M1/config.json"]
    assert list(iter_regular_files(data / "custom" / "M1" / "config.json")) == []  # 根不是資料夾
    make_dir_link(data / "peek", data / "soul")
    with pytest.raises(LinkRefusedError):
        list(iter_regular_files(data / "peek"))
    monkeypatch.setattr(zp, "_roots_provider", None)
    with pytest.raises(zp.ZoneRootsUnavailableError):
        list(iter_regular_files(data))


# --- 刪樹 --------------------------------------------------------------------


def test_remove_tree_deletes_links_not_their_targets(base, outside):
    _tree(base / "t", "a.txt", "sub/b.txt", "sub/deeper/c.txt", "empty/")
    make_dir_link(base / "t" / "sub" / "peek", outside)
    remove_tree_no_follow(base / "t")
    assert not os.path.lexists(base / "t")
    assert _untouched(outside)


def test_remove_tree_on_a_link_a_file_or_nothing(base, outside):
    make_dir_link(base / "link", outside)
    remove_tree_no_follow(base / "link")
    assert not os.path.lexists(base / "link")
    assert _untouched(outside)
    _tree(base, "f.txt")
    remove_tree_no_follow(base / "f.txt")
    with pytest.raises(FileNotFoundError):
        remove_tree_no_follow(base / "f.txt")
    make_dir_link(base / "link", outside)
    with pytest.raises(LinkRefusedError):
        remove_tree_no_follow(base / "link" / "hooks.json")
    assert _untouched(outside)


def test_remove_tree_moves_deep_folders_instead_of_failing(base, monkeypatch):
    monkeypatch.setattr(no_follow, "MAX_REMOVE_OPEN_DIRS", 3)
    deep = base / "t"
    for _ in range(12):
        deep = deep / "d"
    _tree(deep, "leaf.txt")
    rename, moved = OPS.rename, []

    def recorded(self, src, name, dst, new, shown):
        moved.append(new)
        rename(self, src, name, dst, new, shown)

    monkeypatch.setattr(OPS, "rename", recorded)
    remove_tree_no_follow(base / "t")
    assert not os.path.lexists(base / "t")
    assert moved
    prefix = ".touka-remove-"
    assert all(n.startswith(prefix) and len(n) == len(prefix) + 32 for n in moved)


def test_remove_tree_deletes_read_only_files(base):
    _tree(base / "t", "objects/ro.txt")
    (base / "t" / "objects" / "ro.txt").chmod(stat.S_IREAD)
    remove_tree_no_follow(base / "t")
    assert not os.path.lexists(base / "t")


def test_remove_tree_repairs_its_own_locked_folders(base):
    """Linux：樹裡自己擁有、被鎖住的資料夾補擁有者位元後刪掉（Windows 沒有權限位元）."""
    if os.name != "posix" or os.geteuid() == 0:  # root 不受權限位元限制
        return
    _tree(base / "t", "locked/inner/f.txt", "shut/f.txt")
    (base / "t" / "locked").chmod(0)
    (base / "t" / "shut").chmod(0o500)
    if sys.platform == "linux":
        remove_tree_no_follow(base / "t")
        assert not os.path.lexists(base / "t")
        return
    with pytest.raises(PermissionError):
        remove_tree_no_follow(base / "t")
    (base / "t" / "locked").chmod(0o700)
    (base / "t" / "shut").chmod(0o700)


def test_repair_changes_the_checked_folder_not_a_link_swapped_in_after(base, monkeypatch):
    """Linux：核對之後名字被換成連結，補權限改的仍是核對過的那個資料夾，連結指的地方不動."""
    if sys.platform != "linux" or os.geteuid() == 0:  # 只有 Linux 補權限；root 不受權限位元限制
        return
    _tree(base / "t", "locked/f.txt", "outside/g.txt")
    tree = base / "t"
    (tree / "locked").chmod(0)
    (tree / "outside").chmod(0o500)
    fstat = os.fstat

    def swap_after_check(fd):
        info = fstat(fd)
        (tree / "locked").rename(tree / "moved")
        (tree / "locked").symlink_to(tree / "outside", target_is_directory=True)
        return info

    holder = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", swap_after_check)
            repaired = no_follow._FdTree().repair_dir(holder, "locked")
    finally:
        os.close(holder)
    outside_mode = stat.S_IMODE((tree / "outside").stat().st_mode)
    (tree / "outside").chmod(0o700)
    assert outside_mode == 0o500, LINK_FOLLOWED
    assert repaired
    assert stat.S_IMODE((tree / "moved").stat().st_mode) == 0o700


def test_remove_tree_retries_an_entry_that_was_swapped(base, monkeypatch):
    _tree(base / "t", "sub/x.txt")
    open_dir, calls = OPS.open_dir, []

    def swapped_once(self, holder, name, info, shown):
        calls.append(name)
        if name == "sub" and calls.count("sub") == 1:
            raise LinkRefusedError(errno.ELOOP, "link", str(shown))
        return open_dir(self, holder, name, info, shown)

    monkeypatch.setattr(OPS, "open_dir", swapped_once)
    remove_tree_no_follow(base / "t")
    assert not os.path.lexists(base / "t")
    assert calls.count("sub") == 2


def test_remove_tree_gives_up_with_a_fixed_reason(base, monkeypatch):
    """刪完一直又長出東西：重試到上限後以固定原因失敗，不無限迴圈."""
    _tree(base / "t", "a.txt")
    names = OPS.names

    def regrowing(self, holder, shown):
        return [*names(self, holder, shown), "ghost"]

    monkeypatch.setattr(OPS, "names", regrowing)
    with pytest.raises(OSError) as caught:
        remove_tree_no_follow(base / "t")
    assert (caught.value.errno, caught.value.strerror) == (
        errno.EBUSY,
        TREE_KEPT_CHANGING_MESSAGE,
    )


def test_remove_tree_does_not_cross_file_systems(base, monkeypatch):
    _tree(base / "t", "mnt/x.txt")
    lstat = OPS.lstat

    def other_device(self, holder, name, shown):
        info = lstat(self, holder, name, shown)
        return _other_device(info) if name == "mnt" else info

    monkeypatch.setattr(OPS, "lstat", other_device)
    with pytest.raises(OSError) as caught:
        remove_tree_no_follow(base / "t")
    assert caught.value.errno == errno.EXDEV
    assert (base / "t" / "mnt" / "x.txt").is_file()

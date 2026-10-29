"""代理人的檔案動詞不跟隨連結（ADR-0292 決定 12）。

core ops 先解析路徑、確認在根底下才開檔。這裡模擬代理人在「檢查之後、開檔之前」把路上一段換成
連結（指向 hook 設定資料夾），寫入、讀取、編輯、刪除都要被擋；list／find／grep 遇到代理人自己
建的連結不進入。直接 I/O（專案）與經 ``ResourceManager``（個人空間等）兩條路都測。全部在
pytest ``tmp_path`` 裡，不碰 ``data/``。
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from memory_server.tools import core_file_ops as cfo
from memory_server.utils import no_follow
from memory_server.utils.no_follow import (
    LINK_NOT_FOLLOWED,
    LINK_REFUSED_MESSAGE,
    LinkRefusedError,
    ensure_no_links,
    is_link,
    iter_regular_files,
    read_bytes_no_follow,
    read_text_no_follow,
    remove_no_follow,
    replace_text_no_follow,
    write_text_no_follow,
)
from src.config import get_app_data_dir
from src.services.resource_manager import resource_manager

if sys.platform == "win32":
    import _winapi

PROXIES = pytest.mark.parametrize("proxy", [None, resource_manager], ids=["direct", "proxy"])
GUARD_SOURCE = "SECRET = 1\n"


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """（專案根, 根外面的 hook 設定資料夾）；根裡有 ``sub/notes.txt``，外面有 ``guard.py``。"""
    base = tmp_path.resolve()
    root, outside = base / "project", base / "config_hooks"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "sub" / "notes.txt").write_text("inside\n", encoding="utf-8")
    (outside / "guard.py").write_text(GUARD_SOURCE, encoding="utf-8")
    return root, outside


def _swap_after_check(monkeypatch: pytest.MonkeyPatch, directory: Path, target: Path) -> None:
    """路徑檢查通過之後，把 ``directory`` 換成指向 ``target`` 的連結（代理人另一個指令做的）。"""
    original = cfo._enforce_root_bound

    def checked_then_swapped(path: str, root_bound: str) -> Path:
        safe = original(path, root_bound)
        if not directory.is_symlink() and not directory.is_junction():
            directory.rename(directory.with_name(directory.name + "_old"))
            _make_dir_link(directory, target)
        return safe

    monkeypatch.setattr(cfo, "_enforce_root_bound", checked_then_swapped)


# 每一案只換一次：換過之後再呼叫，檢查本身就會因為解析到根外面而擋下（不是這裡要測的）
FILE_OPS = {
    "write": lambda root, proxy: cfo.core_write(
        "sub/guard.py", root, "PWNED", resource_proxy=proxy
    ),
    "write_new": lambda root, proxy: cfo.core_write(
        "sub/new.py", root, "PWNED", resource_proxy=proxy
    ),
    "append": lambda root, proxy: cfo.core_write(
        "sub/guard.py", root, "PWNED", mode="a", resource_proxy=proxy
    ),
    "read": lambda root, proxy: cfo.core_read("sub/guard.py", root, resource_proxy=proxy),
    "edit": lambda root, proxy: cfo.core_str_replace(
        "sub/guard.py", root, "SECRET", "PWNED", resource_proxy=proxy
    ),
    "delete": lambda root, proxy: cfo.core_delete("sub/guard.py", root, resource_proxy=proxy),
}


@PROXIES
@pytest.mark.parametrize("op", sorted(FILE_OPS))
async def test_file_ops_refuse_a_directory_swapped_to_a_link_after_the_check(
    layout, monkeypatch, proxy, op
):
    root, outside = layout
    _swap_after_check(monkeypatch, root / "sub", outside)
    result = await FILE_OPS[op](str(root), proxy)
    assert result.endswith(LINK_REFUSED_MESSAGE), result
    assert "SECRET" not in result
    assert (outside / "guard.py").read_text(encoding="utf-8") == GUARD_SOURCE
    assert sorted(p.name for p in outside.iterdir()) == ["guard.py"]


@PROXIES
async def test_list_find_grep_do_not_enter_links(layout, proxy):
    root, outside = layout
    _make_dir_link(root / "peek", outside)
    listing = await cfo.core_list("", str(root), max_depth=3, resource_proxy=proxy)
    found = await cfo.core_find("*.py", str(root), resource_proxy=proxy)
    grep = await cfo.core_grep("SECRET", str(root), resource_proxy=proxy)
    assert f"peek {LINK_NOT_FOLLOWED}" in listing
    assert "guard.py" not in listing
    assert found == "No files found matching '*.py'."
    assert grep == "No occurrences of 'SECRET' found."
    # 根裡的一般檔案照常
    assert "notes.txt" in listing
    assert "- sub/notes.txt" in await cfo.core_find("*.txt", str(root), resource_proxy=proxy)
    assert "sub/notes.txt:1: inside" in await cfo.core_grep(
        "inside", str(root), resource_proxy=proxy
    )


WALKS = {
    "list": lambda root, proxy: cfo.core_list("", root, resource_proxy=proxy),
    "find": lambda root, proxy: cfo.core_find("*.py", root, resource_proxy=proxy),
    "grep": lambda root, proxy: cfo.core_grep("SECRET", root, resource_proxy=proxy),
}


@PROXIES
@pytest.mark.parametrize("op", sorted(WALKS))
async def test_walks_refuse_a_root_swapped_to_a_link_after_the_check(
    layout, monkeypatch, proxy, op
):
    """根本身在檢查後被換成連結：list／find／grep 不走進去（連名稱也不列）。"""
    root, outside = layout
    _swap_after_check(monkeypatch, root / "sub", outside)
    result = await WALKS[op](str(root / "sub"), proxy)
    assert result.endswith(LINK_REFUSED_MESSAGE), result
    assert "guard.py" not in result


@PROXIES
async def test_links_that_stay_inside_the_root_still_work(layout, proxy):
    """路徑先解析：連結指回根裡面時，解析後的路徑沒有連結，照常讀寫。"""
    root, _outside = layout
    _make_dir_link(root / "alias", root / "sub")
    read = await cfo.core_read("alias/notes.txt", str(root), resource_proxy=proxy)
    written = await cfo.core_write("alias/new.txt", str(root), "x", resource_proxy=proxy)
    assert read.endswith("inside\n")
    assert written == "Success: Wrote to alias/new.txt"
    assert (root / "sub" / "new.txt").read_text(encoding="utf-8") == "x"


@PROXIES
async def test_edit_append_delete_still_work_without_links(layout, proxy):
    root, _outside = layout
    notes = str(root)
    assert (
        await cfo.core_write("sub/notes.txt", notes, "more\n", mode="a", resource_proxy=proxy)
    ).startswith("Success")
    assert (
        await cfo.core_str_replace("sub/notes.txt", notes, "more", "less", resource_proxy=proxy)
    ).startswith("Success")
    assert (root / "sub" / "notes.txt").read_text(encoding="utf-8") == "inside\nless\n"
    assert (await cfo.core_write("deep/a/b.txt", notes, "x", resource_proxy=proxy)).startswith(
        "Success"
    )
    assert (await cfo.core_delete("sub/notes.txt", notes, resource_proxy=proxy)).startswith(
        "Success"
    )
    assert not (root / "sub" / "notes.txt").exists()
    assert (await cfo.core_delete("sub/notes.txt", notes, resource_proxy=proxy)).startswith(
        "Error: File not found"
    )


def test_helpers_refuse_a_link_on_any_segment(tmp_path):
    base = tmp_path.resolve()
    real = base / "real"
    real.mkdir()
    (real / "f.txt").write_text("x", encoding="utf-8")
    _make_dir_link(base / "link", real)
    target = base / "link" / "f.txt"
    calls = (
        read_text_no_follow,
        read_bytes_no_follow,
        lambda p: write_text_no_follow(p, "y"),
        lambda p: write_text_no_follow(p, "y", append=True),
        lambda p: replace_text_no_follow(p, "y"),
        remove_no_follow,
    )
    for call in calls:
        with pytest.raises(LinkRefusedError):
            call(target)
    # 最後一段本身是連結、或還不存在的上層路過連結
    with pytest.raises(LinkRefusedError):
        read_text_no_follow(base / "link")
    with pytest.raises(LinkRefusedError):
        write_text_no_follow(base / "link" / "new" / "g.txt", "y")
    assert (real / "f.txt").read_text(encoding="utf-8") == "x"
    assert sorted(p.name for p in real.iterdir()) == ["f.txt"]


def test_is_link_sees_symlinks_and_junctions(tmp_path):
    base = tmp_path.resolve()
    (base / "real").mkdir()
    _make_dir_link(base / "link", base / "real")
    assert is_link(base / "link")
    assert not is_link(base / "real")
    assert not is_link(base / "missing")


def test_link_checks_work_without_junctions(tmp_path, monkeypatch):
    """沒有 junction 的系統（Linux：``stat`` 沒有 reparse tag 常數）照常判斷，不丟例外."""
    monkeypatch.delattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", raising=False)
    monkeypatch.setattr(no_follow, "_JUNCTION_TAG", None)
    base = tmp_path.resolve()
    (base / "real").mkdir()
    (base / "real" / "f.txt").write_text("x", encoding="utf-8")
    assert not is_link(base / "real")
    ensure_no_links(base / "real" / "f.txt")
    assert [rel for rel, _path in iter_regular_files(base)] == ["real/f.txt"]


def test_iter_regular_files_skips_links_and_excluded_names(tmp_path):
    base = tmp_path.resolve()
    (base / "a" / "skip").mkdir(parents=True)
    (base / "a" / "x.txt").write_text("x", encoding="utf-8")
    (base / "a" / "skip" / "y.txt").write_text("y", encoding="utf-8")
    (base / "out").mkdir()
    (base / "out" / "z.txt").write_text("z", encoding="utf-8")
    _make_dir_link(base / "a" / "peek", base / "out")
    rels = [rel for rel, _path in iter_regular_files(base / "a", frozenset({"skip"}))]
    assert rels == ["x.txt"]


def test_replace_writes_atomically_with_owner_only_mode(tmp_path):
    target = tmp_path.resolve() / "new" / "f.txt"
    replace_text_no_follow(target, "one")
    replace_text_no_follow(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert sorted(p.name for p in target.parent.iterdir()) == ["f.txt"]
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600


def test_reading_a_fifo_does_not_block(tmp_path):
    """檔案在列出之後被換成 FIFO：開檔不卡住，也不當成一般檔案讀（Windows 沒有 FIFO）。"""
    if not hasattr(os, "mkfifo"):
        assert sys.platform == "win32"
        return
    fifo = tmp_path.resolve() / "pipe"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(IsADirectoryError):
        read_text_no_follow(fifo)
    assert time.monotonic() - started < 5
    assert [rel for rel, _path in iter_regular_files(tmp_path.resolve())] == []


def test_permission_errors_name_the_absolute_path(tmp_path):
    """POSIX 被鎖住時例外帶絕對路徑，權限自癒才知道要修哪裡（ADR-0272；Windows 沒有權限自癒）."""
    if sys.platform == "win32" or os.geteuid() == 0:  # root 不受權限位元限制
        return
    base = tmp_path.resolve()
    locked, shut, file = base / "locked", base / "shut", base / "file.txt"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "f.txt").write_text("x", encoding="utf-8")
    shut.mkdir()
    (shut / "f.txt").write_text("x", encoding="utf-8")
    file.write_text("x", encoding="utf-8")
    locked.chmod(0)
    shut.chmod(0o500)
    file.chmod(0)
    cases = (
        (lambda: read_text_no_follow(locked / "inner" / "f.txt"), locked / "inner"),  # 上層某一段
        (lambda: write_text_no_follow(locked / "new" / "g.txt", "y"), locked / "new"),  # 建上層
        (lambda: read_text_no_follow(file), file),  # 檔案本身
        (lambda: remove_no_follow(shut / "f.txt"), shut / "f.txt"),
    )
    try:
        for call, expected in cases:
            with pytest.raises(PermissionError) as caught:
                call()
            assert caught.value.filename == str(expected)
        with pytest.raises(PermissionError) as caught:
            replace_text_no_follow(shut / "f.txt", "y")  # 建暫存檔
        assert Path(caught.value.filename).parent == shut
        assert caught.value.filename2 is None
    finally:
        locked.chmod(0o700)
        shut.chmod(0o700)
        file.chmod(0o600)


def test_resource_manager_no_follow_writes_keep_soul_protection():
    soul_file = Path(get_app_data_dir()) / "soul" / "Alice" / "no_follow_probe.md"
    calls = (
        lambda: resource_manager.write_file_text_no_follow(soul_file, "x"),
        lambda: resource_manager.append_file_text_no_follow(soul_file, "x"),
        lambda: resource_manager.remove_file_no_follow(soul_file),
    )
    for call in calls:
        with pytest.raises(PermissionError, match="Soul Protection"):
            call()
    assert not soul_file.exists()


@pytest.mark.parametrize(
    ("rel", "pattern", "expected"),
    [
        ("x.py", "*.py", True),
        ("a/b/x.py", "*.py", True),
        ("a/src/x.py", "src/*.py", True),
        ("src/a/x.py", "src/*.py", False),
        ("x.py", "**/*.py", True),
        ("src/x.py", "src/**/*.py", True),
        ("src/a/b/x.py", "src/**/*.py", True),
        ("lib/a/x.py", "src/**/*.py", False),
        ("a/x.txt", "*.py", False),
        ("a/x.py", "", False),
    ],
)
def test_find_pattern_matches_like_rglob(rel, pattern, expected):
    assert cfo._glob_match(rel, pattern) is expected


def test_find_pattern_with_many_double_stars_stays_fast():
    rel = "/".join(["d"] * 40) + "/x.py"
    started = time.monotonic()
    assert cfo._glob_match(rel, "/".join(["**"] * 40) + "/y.py") is False
    assert cfo._glob_match(rel, "/".join(["**/d"] * 20) + "/**/x.py") is True
    assert time.monotonic() - started < 1


class _HealingProxy:
    """假的 proxy：記下收到的呼叫；讀得到內容（像 Linux 上權限自癒之後）."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ensure_no_links(self, path: str) -> None:
        self.calls.append(("ensure_no_links", path))

    def read_file_text_no_follow(self, path: str) -> str:
        self.calls.append(("read", path))
        return "healed\n"

    def write_file_text_no_follow(self, path: str, _content: str) -> None:
        self.calls.append(("write", path))

    def remove_file_no_follow(self, path: str) -> None:
        self.calls.append(("remove", path))

    def list_directory(self, path: str, _max_depth: int, exclude: frozenset[str]) -> list[str]:
        self.calls.append(("list", path))
        return ["listed"]

    def stat_files(self, path: str, exclude_names: frozenset[str]) -> list[tuple[str, int, float]]:
        self.calls.append(("stat_files", path))
        return [("sub/notes.txt", 7, 0.0)]


async def test_unreadable_status_is_left_to_the_proxy(layout, monkeypatch):
    """讀不到狀態（EACCES）時不先回「找不到」，交給 proxy 開檔；Linux 上它會先權限自癒."""
    root, _outside = layout
    target = str(root / "sub" / "notes.txt")

    def denied(self: Path) -> bool:
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_file", denied)
    proxy = _HealingProxy()
    assert "healed" in await cfo.core_read("sub/notes.txt", str(root), resource_proxy=proxy)
    edited = await cfo.core_str_replace(
        "sub/notes.txt", str(root), old_str="healed", new_str="x", resource_proxy=proxy
    )
    assert edited.startswith("Success")
    assert (await cfo.core_delete("sub/notes.txt", str(root), resource_proxy=proxy)).startswith(
        "Success"
    )
    assert proxy.calls == [
        ("read", target),
        ("read", target),
        ("write", target),
        ("remove", target),
    ]


WALKS_WITH_PROXY = {
    "list": lambda root, proxy: cfo.core_list("sub", root, resource_proxy=proxy),
    "find": lambda root, proxy: cfo.core_find("*.txt", root, resource_proxy=proxy),
    "grep": lambda root, proxy: cfo.core_grep("healed", root, resource_proxy=proxy),
}


@pytest.mark.parametrize("walk", sorted(WALKS_WITH_PROXY))
async def test_walks_check_links_through_the_proxy(layout, walk):
    """經 proxy 時走訪前的連結檢查也經 proxy（Linux 上讀不到狀態會先權限自癒）."""
    root, _outside = layout
    proxy = _HealingProxy()
    result = await WALKS_WITH_PROXY[walk](str(root), proxy)
    assert not result.startswith("Error"), result
    checked = root / "sub" if walk == "list" else root
    assert proxy.calls[0] == ("ensure_no_links", str(checked))


@pytest.mark.parametrize("walk", ["find", "grep"])
async def test_find_and_grep_stop_walking_at_the_limit(layout, monkeypatch, walk):
    """找到 50 筆就停，不先走完整棵樹."""
    root, _outside = layout
    hit = root / "hit.txt"
    hit.write_text("needle\n", encoding="utf-8")

    def endless(_root: Path, _skipped: frozenset[str]):
        for _ in range(51):
            yield "hit.txt", hit
        pytest.fail("walked past the limit")

    monkeypatch.setattr(cfo, "iter_regular_files", endless)
    if walk == "find":
        result = await cfo.core_find("*.txt", str(root))
    else:
        result = await cfo.core_grep("needle", str(root))
    assert result.startswith("Found 50+"), result

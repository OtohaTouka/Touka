"""開放名單（ADR-0311）：能開放的資料夾、等於／其下／其上層的拒絕規則、重疊與掛載根。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from touka_local import grants as grants_module
from touka_local.errors import Refused
from touka_local.grants import Environment, Grant, GrantBook, resolve_mount_root

if TYPE_CHECKING:
    from pathlib import Path

USER = "C:\\Users\\Me"
HELPER = f"{USER}\\AppData\\Local\\ToukaLocal"
TOUKA = "D:\\Touka"


@pytest.fixture
def env() -> Environment:
    return Environment(
        user_profile=USER,
        windows_dir="C:\\Windows",
        program_files=("C:\\Program Files", "C:\\Program Files (x86)"),
        program_data="C:\\ProgramData",
        app_data="C:\\Users\\Me\\AppData\\Roaming",
        local_app_data="C:\\Users\\Me\\AppData\\Local",
        helper_dir=HELPER,
        python_dir="E:\\Python",
        touka_folder=TOUKA,
    )


@pytest.fixture
def book(fs, env: Environment, tmp_path: Path) -> GrantBook:
    return GrantBook(
        tmp_path / "grants.json",
        fs=fs,
        env=env,
        drive_type=lambda _anchor: "fixed",
    )


def _dir(fs, path: str) -> str:
    fs.add_dir(path)
    return path


@pytest.mark.parametrize(
    "path",
    [
        f"{USER}\\Downloads",
        f"{USER}\\Documents",
        f"{USER}\\Desktop",
        "D:\\Photos",
        "D:\\Work\\2026",
    ],
)
def test_ordinary_folders_can_be_opened(fs, book: GrantBook, path: str) -> None:
    _dir(fs, path)
    grant = book.add(path, writable=True)
    assert grant.path == path
    assert grant.writable is True
    assert grant.name == path.rsplit("\\", 1)[-1]
    assert [g.name for g in book.grants()] == [grant.name]


@pytest.mark.parametrize(
    "path",
    [
        USER,
        "C:\\Users",
        "C:\\",
        "D:\\",
        "C:\\Windows",
        "C:\\Windows\\System32",
        "C:\\Program Files",
        "C:\\Program Files\\App",
        "C:\\Program Files (x86)\\App",
        "C:\\ProgramData",
        "C:\\ProgramData\\x",
        f"{USER}\\AppData",
        f"{USER}\\AppData\\Roaming",
        f"{USER}\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup",
        f"{USER}\\AppData\\Local\\Google\\Chrome",
        f"{USER}\\AppData\\LocalLow",
        HELPER,
        f"{HELPER}\\app",
        f"{USER}\\.ssh",
        f"{USER}\\.aws",
        f"{USER}\\.gnupg",
        f"{USER}\\.claude",
        f"{USER}\\.codex",
        f"{USER}\\.vscode",
        f"{USER}\\Projects\\.git",
        "C:\\$Recycle.Bin",
        "D:\\$Recycle.Bin\\x",
        "C:\\System Volume Information",
        TOUKA,
        f"{TOUKA}\\data",
        f"{TOUKA}\\ToukaProxy\\src",
    ],
)
def test_refused_folders(fs, book: GrantBook, path: str) -> None:
    _dir(fs, path)
    with pytest.raises(Refused):
        book.add(path, writable=True)
    assert book.grants() == []


def test_a_drive_root_without_anything_special_is_still_refused(fs, book: GrantBook) -> None:
    _dir(fs, "E:\\Data")
    with pytest.raises(Refused) as caught:
        book.add("E:\\", writable=False)
    assert caught.value.code == "blocked"
    assert book.add("E:\\Data", writable=False).name == "Data"


def test_case_does_not_matter(fs, book: GrantBook) -> None:
    _dir(fs, "c:\\windows\\system32")
    with pytest.raises(Refused):
        book.add("c:\\windows\\system32", writable=False)


def test_relative_or_network_paths_are_refused(fs, book: GrantBook) -> None:
    for path in ("relative\\x", "\\\\server\\share\\x", "\\\\?\\C:\\x", "\\rooted"):
        with pytest.raises(Refused):
            book.add(path, writable=False)


def test_folder_must_exist(book: GrantBook) -> None:
    with pytest.raises(Refused) as caught:
        book.add("D:\\Missing", writable=False)
    assert caught.value.code == "not_found"


def test_file_cannot_be_opened(fs, book: GrantBook) -> None:
    fs.add_file("D:\\a.txt")
    with pytest.raises(Refused):
        book.add("D:\\a.txt", writable=False)


@pytest.mark.parametrize("drive", ["removable", "remote", "cdrom", "unknown"])
def test_only_fixed_drives(fs, env: Environment, tmp_path: Path, drive: str) -> None:
    book = GrantBook(tmp_path / "g.json", fs=fs, env=env, drive_type=lambda _a: drive)
    _dir(fs, "E:\\Photos")
    with pytest.raises(Refused) as caught:
        book.add("E:\\Photos", writable=False)
    assert caught.value.code == "drive_type"


def test_link_anywhere_on_the_path_is_refused(fs, book: GrantBook) -> None:
    fs.add_dir("D:\\Linked", reparse=True)
    fs.add_dir("D:\\Linked\\Sub")
    for path in ("D:\\Linked", "D:\\Linked\\Sub"):
        with pytest.raises(Refused) as caught:
            book.add(path, writable=False)
        assert caught.value.code == "link"


def test_git_at_root_or_above_is_refused(fs, book: GrantBook) -> None:
    fs.add_dir("D:\\Repo\\.git")
    fs.add_dir("D:\\Repo\\src")
    for path in ("D:\\Repo", "D:\\Repo\\src"):
        with pytest.raises(Refused) as caught:
            book.add(path, writable=False)
        assert caught.value.code == "git_project"


def test_git_below_root_is_allowed_at_open_time(fs, book: GrantBook) -> None:
    fs.add_dir("D:\\Work\\Repo\\.git")
    assert book.add("D:\\Work", writable=False).name == "Work"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("D:\\Work", "D:\\Work\\Sub"),
        ("D:\\Work\\Sub", "D:\\Work"),
        ("D:\\Work", "D:\\WORK"),
    ],
)
def test_overlapping_grants_are_refused(fs, book: GrantBook, first: str, second: str) -> None:
    _dir(fs, "D:\\Work\\Sub")
    book.add(first, writable=False)
    with pytest.raises(Refused) as caught:
        book.add(second, writable=True)
    assert caught.value.code == "overlap"


def test_sibling_prefix_is_not_overlap(fs, book: GrantBook) -> None:
    _dir(fs, "D:\\Work")
    _dir(fs, "D:\\Workshop")
    book.add("D:\\Work", writable=False)
    assert book.add("D:\\Workshop", writable=False).name == "Workshop"


def test_duplicate_folder_names_get_distinct_names(fs, book: GrantBook) -> None:
    _dir(fs, "D:\\A\\Photos")
    _dir(fs, "D:\\B\\Photos")
    assert book.add("D:\\A\\Photos", writable=False).name == "Photos"
    assert book.add("D:\\B\\Photos", writable=False).name == "Photos 2"


def test_names_are_looked_up_case_insensitively(fs, book: GrantBook) -> None:
    _dir(fs, "D:\\Photos")
    book.add("D:\\Photos", writable=False)
    grant = book.find("photos")
    assert grant is not None and grant.path == "D:\\Photos"
    assert book.find("other") is None


def test_grants_persist_and_can_be_removed(fs, env: Environment, tmp_path: Path) -> None:
    file = tmp_path / "grants.json"
    book = GrantBook(file, fs=fs, env=env, drive_type=lambda _a: "fixed")
    _dir(fs, "D:\\Photos")
    book.add("D:\\Photos", writable=True)
    again = GrantBook(file, fs=fs, env=env, drive_type=lambda _a: "fixed")
    assert again.grants() == [Grant(name="Photos", path="D:\\Photos", writable=True)]
    again.remove("Photos")
    assert GrantBook(file, fs=fs, env=env, drive_type=lambda _a: "fixed").grants() == []


def test_set_writable(fs, book: GrantBook) -> None:
    _dir(fs, "D:\\Photos")
    book.add("D:\\Photos", writable=False)
    book.set_writable("Photos", writable=True)
    assert book.find("Photos").writable is True  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"grants": 1}', '{"grants": [{"name": 1}]}']
)
def test_broken_file_opens_nothing(fs, env: Environment, tmp_path: Path, content: str) -> None:
    file = tmp_path / "grants.json"
    file.write_text(content, encoding="utf-8")
    assert GrantBook(file, fs=fs, env=env, drive_type=lambda _a: "fixed").grants() == []


def test_stored_grant_that_now_breaks_a_rule_is_not_usable(
    fs, env: Environment, tmp_path: Path
) -> None:
    file = tmp_path / "grants.json"
    file.write_text(
        '{"grants": [{"name": "Win", "path": "C:\\\\Windows", "writable": true}]}',
        encoding="utf-8",
    )
    book = GrantBook(file, fs=fs, env=env, drive_type=lambda _a: "fixed")
    assert book.find("Win") is None


# --- 主機掛載根 ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("D:/Work", "D:\\Work"),
        ("D:\\Work\\", "D:\\Work"),
        ("./data", f"{TOUKA}\\data"),
        ("data", f"{TOUKA}\\data"),
        (".", TOUKA),
        ("../other", "D:\\other"),
    ],
)
def test_mount_root_resolution(value: str, expected: str) -> None:
    assert resolve_mount_root(value, TOUKA) == expected


@pytest.mark.parametrize("value", ["", "   ", "//server/share", "\\\\server\\share"])
def test_mount_root_without_a_local_path(value: str) -> None:
    assert resolve_mount_root(value, TOUKA) is None


@pytest.mark.parametrize(
    "path",
    ["D:\\Mounted", "D:\\Mounted\\Sub", "D:\\"],
)
def test_mount_roots_block_related_folders(fs, book: GrantBook, path: str) -> None:
    _dir(fs, "D:\\Mounted\\Sub")
    book.set_mount_roots(["D:/Mounted"])
    with pytest.raises(Refused):
        book.add(path, writable=False)


def test_mount_roots_can_only_block_more(fs, book: GrantBook) -> None:
    """Touka 給的值不會讓原本不能開放的變得能開放。"""
    _dir(fs, "C:\\Windows\\Temp")
    book.set_mount_roots([])
    with pytest.raises(Refused):
        book.add("C:\\Windows\\Temp", writable=False)


def test_new_overlap_pauses_an_existing_grant(fs, book: GrantBook) -> None:
    _dir(fs, "D:\\Work\\Sub")
    book.add("D:\\Work\\Sub", writable=True)
    book.set_mount_roots(["D:/Work"])
    assert book.find("Sub") is None
    assert book.paused_names() == ["Sub"]
    book.set_mount_roots([])
    assert book.find("Sub") is not None
    assert book.paused_names() == []


def test_onedrive_hint(fs, book: GrantBook) -> None:
    _dir(fs, f"{USER}\\OneDrive\\Desktop")
    grant = book.add(f"{USER}\\OneDrive\\Desktop", writable=False)
    assert book.needs_onedrive_hint(grant) is True
    _dir(fs, "D:\\Photos")
    assert book.needs_onedrive_hint(book.add("D:\\Photos", writable=False)) is False


def test_a_failed_save_leaves_the_list_unchanged(fs, book, monkeypatch) -> None:
    grant = book.add(_dir(fs, "D:\\Open"), writable=False)
    before = book.grants()

    def boom(_file: object, _text: str) -> None:
        raise OSError(28, "disk full")

    monkeypatch.setattr(grants_module, "durable_write_text", boom)
    other = _dir(fs, "D:\\Other")
    with pytest.raises(OSError, match="disk full"):
        book.add(other, writable=True)
    with pytest.raises(OSError, match="disk full"):
        book.set_writable(grant.name, writable=True)
    with pytest.raises(OSError, match="disk full"):
        book.remove(grant.name)
    assert book.grants() == before


def test_list_is_found_after_a_crash_between_delete_and_rename(fs, env, book, tmp_path) -> None:
    grant = book.add(_dir(fs, "D:\\Open"), writable=False)
    file = tmp_path / "grants.json"
    file.rename(tmp_path / "grants.json.new")
    again = GrantBook(file, fs=fs, env=env, drive_type=lambda _anchor: "fixed")
    assert again.grants() == [grant]


@pytest.mark.parametrize("path", ["E:\\Profile\\Roaming", "E:\\Profile\\Local\\Sub", "E:\\Profile"])
def test_redirected_app_data_cannot_be_opened(fs, env, tmp_path, path: str) -> None:
    moved = replace(env, app_data="E:\\Profile\\Roaming", local_app_data="E:\\Profile\\Local")
    book = GrantBook(tmp_path / "moved.json", fs=fs, env=moved, drive_type=lambda _anchor: "fixed")
    with pytest.raises(Refused) as caught:
        book.add(_dir(fs, path), writable=False)
    assert caught.value.code == "blocked"


def test_a_folder_named_appdata_cannot_be_opened(fs, book: GrantBook) -> None:
    fs.add_dir("D:\\Old\\AppData\\Roaming")
    with pytest.raises(Refused) as caught:
        book.add("D:\\Old\\AppData\\Roaming", writable=False)
    assert caught.value.code == "system_folder"


@pytest.mark.parametrize(
    ("mounts", "exposed"),
    [
        ((), False),
        (("D:\\Work",), False),
        (("E:\\",), True),  # Python 在這顆磁碟
        (("E:\\Python",), True),
        (("e:\\python\\",), True),
        (("E:\\Python\\Lib",), False),  # 掛進去的只是 Python 底下一個資料夾
        (("C:\\Users\\Me\\AppData\\Local",), True),  # 小幫手在這底下
        ((HELPER,), True),
        (("D:\\Work", "C:\\Users\\Me"), True),
    ],
)
def test_helper_is_exposed(book: GrantBook, mounts: tuple[str, ...], exposed: bool) -> None:
    book.set_mount_roots(mounts)
    assert book.helper_is_exposed() is exposed

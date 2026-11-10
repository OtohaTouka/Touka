"""小幫手的固定動作（ADR-0311）：看、讀、新建、清單、執行前重驗、復原。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from touka_local import fsops
from touka_local.errors import Refused
from touka_local.fsops import durable_write_text, read_durable_text
from touka_local.grants import Environment, GrantBook
from touka_local.operations import ApplyLimits, Operations

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def book(fs, tmp_path: Path) -> GrantBook:
    env = Environment(
        user_profile="C:\\Users\\Me",
        windows_dir="C:\\Windows",
        program_files=("C:\\Program Files",),
        program_data="C:\\ProgramData",
        app_data="C:\\Users\\Me\\AppData\\Roaming",
        local_app_data="C:\\Users\\Me\\AppData\\Local",
        helper_dir="C:\\Users\\Me\\AppData\\Local\\ToukaLocal",
        touka_folder="D:\\Touka",
    )
    return GrantBook(tmp_path / "g.json", fs=fs, env=env, drive_type=lambda _a: "fixed")


@pytest.fixture
def ops(fs, book: GrantBook, trash, clock, tmp_path: Path) -> Operations:
    fs.add_dir("D:\\Open")
    fs.add_dir("D:\\View")
    book.add("D:\\Open", writable=True)
    book.add("D:\\View", writable=False)
    return Operations(
        fs=fs, grants=book, trash=trash, clock=clock, journal_dir=tmp_path / "journal"
    )


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(Refused) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


# --- 看清單 ---


def test_list_without_path_shows_open_folders(ops: Operations) -> None:
    assert ops.list({}) == {
        "roots": [{"name": "Open", "access": "write"}, {"name": "View", "access": "read"}]
    }


def test_list_entries(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt", b"abc")
    fs.add_dir("D:\\Open\\sub")
    fs.add_file("D:\\Open\\sub\\b.txt")
    result = ops.list({"path": "Open"})
    assert result["truncated"] is False
    assert result["entries"] == [
        {"path": "Open/a.txt", "type": "file", "size": 3},
        {"path": "Open/sub", "type": "folder"},
    ]


def test_list_depth(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\sub\\b.txt")
    paths = [e["path"] for e in ops.list({"path": "Open", "depth": 2})["entries"]]
    assert paths == ["Open/sub", "Open/sub/b.txt"]


def test_list_hides_protected_and_does_not_enter_links_or_repos(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\.ssh")
    fs.add_dir("D:\\Open\\link", reparse=True)
    fs.add_file("D:\\Open\\link\\inside.txt")
    fs.add_dir("D:\\Open\\repo\\.git")
    fs.add_file("D:\\Open\\repo\\code.txt")
    entries = ops.list({"path": "Open", "depth": 3})["entries"]
    assert entries == [
        {"path": "Open/link", "type": "link"},
        {"path": "Open/repo", "type": "git_project"},
    ]


def test_list_truncates(fs, ops: Operations, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("touka_local.operations.MAX_LIST_ENTRIES", 2)
    for name in "abc":
        fs.add_file(f"D:\\Open\\{name}.txt")
    result = ops.list({"path": "Open"})
    assert len(result["entries"]) == 2
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "path", ["Nope", "Nope/a", "../x", "C:/Windows", "Open/.git", "Open/.ssh/x"]
)
def test_list_refuses(fs, ops: Operations, path: str) -> None:
    fs.add_dir("D:\\Open\\.git")
    fs.add_dir("D:\\Open\\.ssh")
    with pytest.raises(Refused):
        ops.list({"path": path})


def test_paths_through_links_are_refused(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\link", reparse=True)
    fs.add_file("D:\\Open\\link\\a.txt")
    fs.add_file("D:\\Open\\filelink.txt", reparse=True)
    assert _code(ops.list, {"path": "Open/link"}) == "link"
    assert _code(ops.read, {"path": "Open/link/a.txt"}) == "link"
    assert _code(ops.read, {"path": "Open/filelink.txt"}) == "link"


def test_inside_a_git_project_is_refused(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\repo\\.git")
    fs.add_file("D:\\Open\\repo\\sub\\a.txt")
    assert _code(ops.list, {"path": "Open/repo"}) == "git_project"
    assert _code(ops.read, {"path": "Open/repo/sub/a.txt"}) == "git_project"
    assert _code(ops.create_folder, {"path": "Open/repo/new"}) == "git_project"
    assert _code(ops.create_file, {"path": "Open/repo/a.md", "content": "x"}) == "git_project"


# --- 讀檔 ---


def test_read_text(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt", "你好".encode())
    result = ops.read({"path": "Open/a.txt"})
    assert result["text"] == "你好"
    assert result["size"] == 6


@pytest.mark.parametrize(
    ("data", "text"),
    [
        (b"\xef\xbb\xbfhello", "hello"),
        ("哈囉".encode("utf-16"), "哈囉"),
        ("中文".encode("cp950"), "中文"),
    ],
)
def test_read_common_encodings(fs, ops: Operations, data: bytes, text: str) -> None:
    fs.add_file("D:\\Open\\a.txt", data)
    assert ops.read({"path": "Open/a.txt"})["text"] == text


def test_read_refuses_binary(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.bin", b"ab\x00cd")
    assert _code(ops.read, {"path": "Open/a.bin"}) == "not_text"


def test_read_refuses_too_large(fs, ops: Operations, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("touka_local.operations.MAX_READ_BYTES", 3)
    fs.add_file("D:\\Open\\a.txt", b"abcd")
    assert _code(ops.read, {"path": "Open/a.txt"}) == "too_large"


def test_read_refuses_folder_and_missing(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\sub")
    assert _code(ops.read, {"path": "Open/sub"}) == "not_a_file"
    assert _code(ops.read, {"path": "Open/none.txt"}) == "not_found"


def test_read_refuses_secret_names(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\.env")
    assert _code(ops.read, {"path": "Open/.env"}) == "secret_name"


def test_read_works_in_view_only_folder(fs, ops: Operations) -> None:
    fs.add_file("D:\\View\\a.txt", b"x")
    assert ops.read({"path": "View/a.txt"})["text"] == "x"


# --- 新建 ---


def test_create_folder(fs, ops: Operations) -> None:
    assert ops.create_folder({"path": "Open/new"}) == {"created": "Open/new"}
    assert fs.exists("D:\\Open\\new")


def test_create_folder_needs_parent(ops: Operations) -> None:
    assert _code(ops.create_folder, {"path": "Open/a/b"}) == "not_found"


def test_create_file(fs, ops: Operations) -> None:
    assert ops.create_file({"path": "Open/a.txt", "content": "你好"}) == {"created": "Open/a.txt"}
    assert fs.data("D:\\Open\\a.txt") == "你好".encode()


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("View/a.txt", "read_only"),
        ("Open/a.exe", "file_type"),
        ("Open/CLAUDE.md", "file_name"),
        ("Open/.git/a.txt", "protected"),
        ("Nope/a.txt", "not_open"),
        ("Open", "bad_target"),
    ],
)
def test_create_file_refused(ops: Operations, path: str, code: str) -> None:
    assert _code(ops.create_file, {"path": path, "content": "x"}) == code


def test_a_file_in_the_middle_of_a_path_is_not_found(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt")
    assert _code(ops.create_file, {"path": "Open/a.txt/b.txt", "content": "x"}) == "not_found"
    assert _code(ops.create_folder, {"path": "Open/a.txt/sub"}) == "not_found"
    assert _code(ops.read, {"path": "Open/a.txt/b.txt"}) == "not_found"
    assert _code(_plan, ops, [{"op": "delete", "path": "Open/a.txt/b.txt"}]) == "not_found"


def test_create_does_not_even_try_when_something_is_there(
    fs, ops: Operations, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs.add_file("D:\\Open\\a.txt", b"old")
    attempts: list[str] = []
    monkeypatch.setattr(fs, "create_new", lambda path, _data: attempts.append(path))
    assert _code(ops.create_file, {"path": "Open/a.txt", "content": "new"}) == "exists"
    assert attempts == []


def test_create_that_loses_a_race_is_refused(
    fs, ops: Operations, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lost_race(path: str, _data: bytes) -> None:
        raise FileExistsError(path)

    monkeypatch.setattr(fs, "create_new", lost_race)
    assert _code(ops.create_file, {"path": "Open/a.txt", "content": "x"}) == "exists"


def test_create_never_overwrites(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt", b"old")
    fs.add_dir("D:\\Open\\sub")
    assert _code(ops.create_file, {"path": "Open/a.txt", "content": "new"}) == "exists"
    assert fs.data("D:\\Open\\a.txt") == b"old"
    assert _code(ops.create_folder, {"path": "Open/sub"}) == "exists"


def test_create_folder_in_view_only_folder(ops: Operations) -> None:
    assert _code(ops.create_folder, {"path": "View/new"}) == "read_only"


def test_create_in_paused_folder(fs, ops: Operations, book: GrantBook) -> None:
    book.set_mount_roots(["D:/Open"])
    assert _code(ops.create_folder, {"path": "Open/new"}) == "not_open"


# --- 清單 ---


def _plan(ops: Operations, items: list[dict[str, str]], reason: str = "整理"):
    return ops.plan({"items": items, "reason": reason})


def test_plan_lists_what_will_happen(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt", b"abc")
    fs.add_file("D:\\Open\\b.txt", b"defg")
    fs.add_dir("D:\\Open\\dest")
    plan = _plan(
        ops,
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/dest/a.txt"},
            {"op": "delete", "path": "Open/b.txt"},
        ],
    )
    assert [(i.op, i.source_text, i.destination_text) for i in plan.items] == [
        ("move", "Open/a.txt", "Open/dest/a.txt"),
        ("delete", "Open/b.txt", None),
    ]
    assert plan.items[0].size == 3
    assert plan.reason == "整理"
    assert len(plan.digest) == 64


def test_digest_depends_on_snapshot(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt")
    first = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}]).digest
    fs.touch("D:\\Open\\a.txt")
    assert _plan(ops, [{"op": "delete", "path": "Open/a.txt"}]).digest != first


@pytest.mark.parametrize(
    ("item", "code"),
    [
        ({"op": "delete", "path": "View/a.txt"}, "read_only"),
        ({"op": "move", "path": "View/a.txt", "to": "Open/a.txt"}, "read_only"),
        ({"op": "move", "path": "Open/a.txt", "to": "View/a.txt"}, "read_only"),
        ({"op": "move", "path": "Open/a.txt", "to": "Open/a.exe"}, "extension_changed"),
        ({"op": "move", "path": "Open/a.txt", "to": "Open/exists.txt"}, "exists"),
        ({"op": "move", "path": "Open/a.txt", "to": "Open/missing/a.txt"}, "not_found"),
        ({"op": "move", "path": "Open/none.txt", "to": "Open/b.txt"}, "not_found"),
        ({"op": "delete", "path": "Open/none.txt"}, "not_found"),
        ({"op": "delete", "path": "Open"}, "bad_target"),
        ({"op": "move", "path": "Open", "to": "Open2/x"}, "bad_target"),
        ({"op": "move", "path": "Open/a.txt", "to": "Open"}, "bad_target"),
        ({"op": "delete", "path": "Nope/a.txt"}, "not_open"),
        ({"op": "delete", "path": "Open/.git"}, "protected"),
        ({"op": "move", "path": "Open/a.txt", "to": "Open/.vscode/a.txt"}, "protected"),
        ({"op": "chmod", "path": "Open/a.txt"}, "bad_request"),
    ],
)
def test_plan_refuses(fs, ops: Operations, item: dict[str, str], code: str) -> None:
    fs.add_file("D:\\View\\a.txt")
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\exists.txt")
    fs.add_dir("D:\\Open\\.git2")
    with pytest.raises(Refused) as caught:
        _plan(ops, [item])
    assert caught.value.code == code


def test_git_appearing_at_the_open_root_pauses_the_folder(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\.git")
    assert _code(_plan, ops, [{"op": "delete", "path": "Open/.git"}]) == "not_open"


def test_one_bad_item_refuses_the_whole_list(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\a.txt")
    with pytest.raises(Refused) as caught:
        _plan(
            ops,
            [{"op": "delete", "path": "Open/a.txt"}, {"op": "delete", "path": "Open/none.txt"}],
        )
    assert caught.value.code == "not_found"
    assert "Open/none.txt" in caught.value.detail
    assert fs.exists("D:\\Open\\a.txt")


def test_plan_refuses_across_drives(fs, ops: Operations, book: GrantBook) -> None:
    fs.add_dir("E:\\Other")
    book.add("E:\\Other", writable=True)
    fs.add_file("D:\\Open\\a.txt")
    assert (
        _code(_plan, ops, [{"op": "move", "path": "Open/a.txt", "to": "Other/a.txt"}])
        == "cross_drive"
    )


def test_plan_allows_between_open_folders_on_one_drive(
    fs, ops: Operations, book: GrantBook
) -> None:
    fs.add_dir("D:\\Second")
    book.add("D:\\Second", writable=True)
    fs.add_file("D:\\Open\\a.txt")
    assert _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Second/a.txt"}]).items


@pytest.mark.parametrize(
    "items",
    [
        [{"op": "delete", "path": "Open/a.txt"}, {"op": "delete", "path": "Open/A.TXT"}],
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/c.txt"},
            {"op": "move", "path": "Open/b.txt", "to": "Open/C.TXT"},
        ],
        [{"op": "delete", "path": "Open/sub"}, {"op": "delete", "path": "Open/sub/x.txt"}],
        [
            {"op": "delete", "path": "Open/sub"},
            {"op": "move", "path": "Open/a.txt", "to": "Open/sub/a.txt"},
        ],
    ],
)
def test_plan_refuses_conflicts(fs, ops: Operations, items: list[dict[str, str]]) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    fs.add_file("D:\\Open\\sub\\x.txt")
    assert _code(_plan, ops, items) == "conflict"


def test_folder_cannot_move_into_itself(fs, ops: Operations) -> None:
    fs.add_dir("D:\\Open\\sub\\inner")
    assert (
        _code(_plan, ops, [{"op": "move", "path": "Open/sub", "to": "Open/sub/inner/sub"}])
        == "conflict"
    )


def test_plan_limits(fs, ops: Operations, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("touka_local.operations.MAX_PROPOSE_ITEMS", 1)
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    items = [{"op": "delete", "path": "Open/a.txt"}, {"op": "delete", "path": "Open/b.txt"}]
    assert _code(_plan, ops, items) == "too_many"


def test_folder_snapshot_counts_contents(fs, ops: Operations) -> None:
    fs.add_file("D:\\Open\\sub\\a.txt", b"12")
    fs.add_file("D:\\Open\\sub\\deep\\b.txt", b"345")
    item = _plan(ops, [{"op": "delete", "path": "Open/sub"}]).items[0]
    assert item.is_folder is True
    assert item.contained == 3
    assert item.size == 5


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda fs: fs.add_dir("D:\\Open\\sub\\link", reparse=True), "link"),
        (lambda fs: fs.add_file("D:\\Open\\sub\\deep\\l.txt", reparse=True), "link"),
        (lambda fs: fs.add_dir("D:\\Open\\sub\\.git"), "git_project"),
        (lambda fs: fs.add_dir("D:\\Open\\sub\\deep\\.vscode"), "protected"),
    ],
)
def test_folder_tree_with_forbidden_contents_is_refused(
    fs, ops: Operations, setup, code: str
) -> None:
    fs.add_file("D:\\Open\\sub\\deep\\a.txt")
    setup(fs)
    assert _code(_plan, ops, [{"op": "delete", "path": "Open/sub"}]) == code


def test_folder_tree_too_large_is_refused(
    fs, ops: Operations, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("touka_local.operations.MAX_TREE_ENTRIES", 2)
    for name in "abc":
        fs.add_file(f"D:\\Open\\sub\\{name}.txt")
    assert _code(_plan, ops, [{"op": "delete", "path": "Open/sub"}]) == "too_many"


def test_unusual_items_are_marked(fs, ops: Operations, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("touka_local.operations.UNUSUAL_FOLDER_ENTRIES", 1)
    fs.add_file("D:\\Open\\run.exe")
    fs.add_file("D:\\Open\\hidden.txt", hidden=True)
    fs.add_file("D:\\Open\\big\\a.txt")
    fs.add_file("D:\\Open\\big\\b.txt")
    fs.add_file("D:\\Open\\plain.txt")
    plan = _plan(
        ops,
        [
            {"op": "delete", "path": "Open/run.exe"},
            {"op": "delete", "path": "Open/hidden.txt"},
            {"op": "delete", "path": "Open/big"},
            {"op": "delete", "path": "Open/plain.txt"},
        ],
    )
    assert [i.unusual for i in plan.items] == [
        ("executable",),
        ("hidden",),
        ("many_items",),
        (),
    ]


# --- 執行 ---


def _limits(clock, *, stop=lambda: False, seconds: float = 300.0) -> ApplyLimits:
    return ApplyLimits(should_stop=stop, deadline=clock() + seconds)


def _statuses(report) -> list[tuple[str, str]]:
    return [(r["path"], r["status"]) for r in report["items"]]


def test_apply_moves_and_deletes(fs, ops: Operations, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    plan = _plan(
        ops,
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/c.txt"},
            {"op": "delete", "path": "Open/b.txt"},
        ],
    )
    report = ops.apply(plan, _limits(clock))
    assert _statuses(report) == [("Open/a.txt", "done"), ("Open/b.txt", "done")]
    assert fs.exists("D:\\Open\\c.txt") and not fs.exists("D:\\Open\\a.txt")
    assert trash.sent == ["D:\\Open\\b.txt"]
    assert report["batch"]


def test_apply_refuses_a_list_that_is_not_the_one_approved(
    fs, ops: Operations, trash, clock
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    approved = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    other = _plan(ops, [{"op": "delete", "path": "Open/b.txt"}])
    assert _code(ops.apply, replace(approved, items=other.items), _limits(clock)) == "changed"
    assert trash.sent == []


def test_apply_skips_changed_file(fs, ops: Operations, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    plan = _plan(
        ops, [{"op": "delete", "path": "Open/a.txt"}, {"op": "delete", "path": "Open/b.txt"}]
    )
    fs.touch("D:\\Open\\a.txt")
    report = ops.apply(plan, _limits(clock))
    assert _statuses(report) == [("Open/a.txt", "changed"), ("Open/b.txt", "done")]
    assert trash.sent == ["D:\\Open\\b.txt"]


def test_apply_skips_folder_with_new_deep_file(fs, ops: Operations, trash, clock) -> None:
    fs.add_file("D:\\Open\\sub\\deep\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/sub"}])
    fs.add_file("D:\\Open\\sub\\deep\\new.txt")
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/sub", "changed")]
    assert trash.sent == []


def test_apply_skips_when_link_appears(fs, ops: Operations, trash, clock) -> None:
    fs.add_dir("D:\\Open\\sub")
    fs.add_file("D:\\Open\\sub\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/sub/a.txt"}])
    fs.nodes["d:\\open\\sub"].reparse = True
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/sub/a.txt", "changed")]
    assert trash.sent == []


def test_apply_rechecks_folder_is_still_open(fs, ops: Operations, book, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    book.remove("Open")
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/a.txt", "changed")]
    assert trash.sent == []


def test_apply_rechecks_folder_is_still_writable(fs, ops: Operations, book, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    book.set_writable("Open", writable=False)
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/a.txt", "changed")]


def test_apply_never_overwrites_a_new_destination(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt", b"source")
    plan = _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}])
    fs.add_file("D:\\Open\\b.txt", b"arrived later")
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/a.txt", "changed")]
    assert fs.data("D:\\Open\\b.txt") == b"arrived later"
    assert fs.exists("D:\\Open\\a.txt")


def test_apply_does_not_even_try_when_the_destination_appeared(
    fs, ops: Operations, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}])
    fs.add_file("D:\\Open\\b.txt")
    attempts: list[str] = []
    monkeypatch.setattr(fs, "rename", lambda source, _destination: attempts.append(source))
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/a.txt", "changed")]
    assert attempts == []


def test_apply_that_loses_a_race_is_reported_as_changed(
    fs, ops: Operations, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}])

    def lost_race(_source: str, destination: str) -> None:
        raise FileExistsError(destination)

    monkeypatch.setattr(fs, "rename", lost_race)
    assert _statuses(ops.apply(plan, _limits(clock))) == [("Open/a.txt", "changed")]


def test_apply_reports_partial_failure(fs, ops: Operations, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\b.txt")
    fs.add_file("D:\\Open\\c.txt")
    plan = _plan(
        ops,
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/x.txt"},
            {"op": "delete", "path": "Open/b.txt"},
            {"op": "delete", "path": "Open/c.txt"},
        ],
    )
    fs.fail_rename_for.add("d:\\open\\a.txt")
    trash.outcomes["D:\\Open\\b.txt"] = "aborted"
    assert _statuses(ops.apply(plan, _limits(clock))) == [
        ("Open/a.txt", "failed"),
        ("Open/b.txt", "aborted"),
        ("Open/c.txt", "done"),
    ]


def test_stop_finishes_current_item_only(fs, ops: Operations, clock) -> None:
    for name in "abc":
        fs.add_file(f"D:\\Open\\{name}.txt")
    plan = _plan(ops, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "abc"])
    calls = iter([False, True, True, True])
    report = ops.apply(plan, _limits(clock, stop=lambda: next(calls)))
    assert _statuses(report) == [
        ("Open/a.txt", "done"),
        ("Open/b.txt", "not_done"),
        ("Open/c.txt", "not_done"),
    ]


def test_deadline_stops_work(fs, ops: Operations, clock) -> None:
    for name in "ab":
        fs.add_file(f"D:\\Open\\{name}.txt")
    plan = _plan(ops, [{"op": "delete", "path": f"Open/{n}.txt"} for n in "ab"])
    limits = _limits(clock, seconds=10)
    clock.now += 11
    assert _statuses(ops.apply(plan, limits)) == [
        ("Open/a.txt", "not_done"),
        ("Open/b.txt", "not_done"),
    ]


# --- 復原 ---


def test_undo_reverses_moves_in_reverse_order(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_dir("D:\\Open\\dest")
    plan = _plan(
        ops,
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"},
            {"op": "move", "path": "Open/dest", "to": "Open/dest2"},
        ],
    )
    batch = ops.apply(plan, _limits(clock))["batch"]
    result = ops.undo(batch)
    assert result == {"restored": 2, "skipped": 0, "not_undoable": 0}
    assert fs.exists("D:\\Open\\a.txt") and fs.exists("D:\\Open\\dest")
    assert fs.renames[-1] == ("D:\\Open\\b.txt", "D:\\Open\\a.txt")


def test_undo_counts_deletes_as_not_undoable(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    batch = ops.apply(_plan(ops, [{"op": "delete", "path": "Open/a.txt"}]), _limits(clock))["batch"]
    assert ops.undo(batch) == {"restored": 0, "skipped": 0, "not_undoable": 1}


def test_undo_skips_when_something_changed(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    fs.add_file("D:\\Open\\c.txt")
    plan = _plan(
        ops,
        [
            {"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"},
            {"op": "move", "path": "Open/c.txt", "to": "Open/d.txt"},
        ],
    )
    batch = ops.apply(plan, _limits(clock))["batch"]
    fs.touch("D:\\Open\\b.txt")
    fs.add_file("D:\\Open\\c.txt", b"new occupant")
    assert ops.undo(batch) == {"restored": 0, "skipped": 2, "not_undoable": 0}
    assert fs.data("D:\\Open\\c.txt") == b"new occupant"


def test_undo_does_not_even_try_when_the_old_place_is_taken(
    fs, ops: Operations, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs.add_file("D:\\Open\\a.txt")
    batch = ops.apply(
        _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}]), _limits(clock)
    )["batch"]
    fs.add_file("D:\\Open\\a.txt", b"new occupant")
    attempts: list[str] = []
    monkeypatch.setattr(fs, "rename", lambda source, _destination: attempts.append(source))
    assert ops.undo(batch) == {"restored": 0, "skipped": 1, "not_undoable": 0}
    assert attempts == []


def test_undo_twice_does_nothing(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    batch = ops.apply(
        _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}]), _limits(clock)
    )["batch"]
    ops.undo(batch)
    assert ops.undo(batch) == {"restored": 0, "skipped": 0, "not_undoable": 0}


def test_undo_unknown_batch(ops: Operations) -> None:
    assert _code(ops.undo, "../../evil") == "not_found"
    assert _code(ops.undo, "missing") == "not_found"


def test_undo_cannot_read_a_record_outside_its_folder(ops: Operations, tmp_path: Path) -> None:
    (tmp_path / "outside.json").write_text(
        '{"batch": "x", "sequence": 1, "entries": []}', encoding="utf-8"
    )
    assert _code(ops.undo, "../outside") == "not_found"


def test_recent_batches(fs, ops: Operations, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    batch = ops.apply(
        _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}], reason="改名"),
        _limits(clock),
    )["batch"]
    recent = ops.recent_batches()
    assert recent[0]["batch"] == batch
    assert recent[0]["reason"] == "改名"
    assert recent[0]["undoable"] == 1


def test_journal_is_written_before_work_starts(fs, ops: Operations, trash, clock, tmp_path) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    seen: list[int] = []

    def send(path: str) -> str:
        seen.append(len(list((tmp_path / "journal").glob("*.json"))))
        return "ok"

    trash.send = send
    ops.apply(plan, _limits(clock))
    assert seen == [1]


# --- 紀錄寫不進去 ---


def _delete_all(ops: Operations, fs, names: str):
    for name in names:
        fs.add_file(f"D:\\Open\\{name}.txt")
    return _plan(ops, [{"op": "delete", "path": f"Open/{n}.txt"} for n in names])


def _failing_after(ops: Operations, monkeypatch, *, good: int, bad: int | None = None) -> None:
    """前 ``good`` 次寫入成功，之後連續失敗 ``bad`` 次（None 是一直失敗）。"""
    real = ops._write_journal
    calls = {"count": 0}

    def flaky(journal) -> None:
        calls["count"] += 1
        failing = calls["count"] > good and (bad is None or calls["count"] <= good + bad)
        if failing:
            raise OSError(28, "disk full")
        real(journal)

    monkeypatch.setattr(ops, "_write_journal", flaky)


def test_journal_failure_mid_batch_stops_the_rest(fs, ops, trash, clock, monkeypatch) -> None:
    plan = _delete_all(ops, fs, "abc")
    _failing_after(ops, monkeypatch, good=1)
    report = ops.apply(plan, _limits(clock))
    assert _statuses(report) == [
        ("Open/a.txt", "done"),
        ("Open/b.txt", "not_done"),
        ("Open/c.txt", "not_done"),
    ]
    assert report["journal_incomplete"] is True
    assert trash.sent == ["D:\\Open\\a.txt"]


def test_a_record_that_recovers_still_stops_the_batch(fs, ops, trash, clock, monkeypatch) -> None:
    plan = _delete_all(ops, fs, "abc")
    _failing_after(ops, monkeypatch, good=1, bad=1)
    report = ops.apply(plan, _limits(clock))
    assert [status for _path, status in _statuses(report)] == ["done", "not_done", "not_done"]
    assert "journal_incomplete" not in report
    assert trash.sent == ["D:\\Open\\a.txt"]


def test_first_record_failure_touches_nothing(fs, ops, trash, clock, monkeypatch) -> None:
    plan = _delete_all(ops, fs, "a")
    _failing_after(ops, monkeypatch, good=0)
    with pytest.raises(OSError, match="disk full"):
        ops.apply(plan, _limits(clock))
    assert trash.sent == []


def test_undo_reports_a_record_that_could_not_be_saved(fs, ops, clock, monkeypatch) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}])
    batch = ops.apply(plan, _limits(clock))["batch"]
    _failing_after(ops, monkeypatch, good=0)
    assert ops.undo(batch) == {
        "restored": 1,
        "skipped": 0,
        "not_undoable": 0,
        "journal_incomplete": 1,
    }
    assert fs.exists("D:\\Open\\a.txt")


def _record_file(tmp_path: Path, batch: str) -> Path:
    return tmp_path / "journal" / f"{batch}.json"


def _temporary(file: Path) -> Path:
    return file.with_name(f"{file.name}.new")


def _moved_batch(fs, ops: Operations, clock) -> str:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "move", "path": "Open/a.txt", "to": "Open/b.txt"}])
    return ops.apply(plan, _limits(clock))["batch"]


def test_record_left_as_a_temporary_file_is_still_found(fs, ops, clock, tmp_path) -> None:
    batch = _moved_batch(fs, ops, clock)
    file = _record_file(tmp_path, batch)
    file.rename(_temporary(file))
    assert [item["batch"] for item in ops.recent_batches()] == [batch]
    assert ops.undo(batch)["restored"] == 1


def test_stale_temporary_record_loses_and_is_cleaned(fs, ops, clock, tmp_path) -> None:
    batch = _moved_batch(fs, ops, clock)
    temporary = _temporary(_record_file(tmp_path, batch))
    temporary.write_text("{}", encoding="utf-8")
    assert [item["batch"] for item in ops.recent_batches()] == [batch]
    plan = _delete_all(ops, fs, "c")
    ops.apply(plan, _limits(clock))
    assert not temporary.exists()


def test_a_pruned_batch_takes_its_temporary_record_with_it(
    fs, ops, clock, tmp_path, monkeypatch
) -> None:
    batch = _moved_batch(fs, ops, clock)
    temporary = _temporary(_record_file(tmp_path, batch))
    temporary.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("touka_local.operations.MAX_JOURNALS", 1)
    plan = _delete_all(ops, fs, "c")
    ops.apply(plan, _limits(clock))
    assert not temporary.exists()
    assert batch not in [item["batch"] for item in ops.recent_batches()]


@pytest.mark.parametrize("recorded", ["b" * 32, "../../evil", "A" * 32])
def test_a_record_whose_batch_does_not_match_its_file_is_ignored(
    ops: Operations, tmp_path, recorded: str
) -> None:
    folder = tmp_path / "journal"
    folder.mkdir()
    (folder / f"{'a' * 32}.json").write_text(
        json.dumps({"batch": recorded, "sequence": 1, "entries": []}), encoding="utf-8"
    )
    assert ops.recent_batches() == []


def test_a_record_that_is_not_named_like_a_batch_is_ignored(ops: Operations, tmp_path) -> None:
    folder = tmp_path / "journal"
    folder.mkdir()
    (folder / "notes.json").write_text(
        json.dumps({"batch": "notes", "sequence": 1, "entries": []}), encoding="utf-8"
    )
    assert ops.recent_batches() == []


def test_record_can_hold_text_utf8_cannot_write(fs, ops, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}], reason="壞\udc80字")
    report = ops.apply(plan, _limits(clock))
    assert _statuses(report) == [("Open/a.txt", "done")]
    assert len(ops.recent_batches()) == 1


# --- 同名換路徑 ---


def _point_the_name_elsewhere(fs, book: GrantBook) -> None:
    book.remove("Open")
    fs.add_dir("D:\\Other\\Open")
    book.add("D:\\Other\\Open", writable=True)


def test_apply_refuses_a_name_that_now_points_elsewhere(fs, ops, book, trash, clock) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    _point_the_name_elsewhere(fs, book)
    fs.add_file("D:\\Other\\Open\\a.txt")
    report = ops.apply(plan, _limits(clock))
    assert _statuses(report) == [("Open/a.txt", "changed")]
    assert trash.sent == []
    assert fs.exists("D:\\Open\\a.txt") and fs.exists("D:\\Other\\Open\\a.txt")


def test_undo_refuses_a_name_that_now_points_elsewhere(fs, ops, book, clock) -> None:
    batch = _moved_batch(fs, ops, clock)
    _point_the_name_elsewhere(fs, book)
    fs.add_file("D:\\Other\\Open\\b.txt")
    assert ops.undo(batch) == {"restored": 0, "skipped": 1, "not_undoable": 0}
    assert fs.exists("D:\\Open\\b.txt") and fs.exists("D:\\Other\\Open\\b.txt")


# --- 當機在搬移和寫紀錄之間 ---


def _mark_pending(tmp_path: Path, batch: str) -> None:
    file = _record_file(tmp_path, batch)
    journal = json.loads(file.read_text(encoding="utf-8"))
    for entry in journal["entries"]:
        entry["status"] = "pending"
    file.write_text(json.dumps(journal), encoding="utf-8")


def test_undo_restores_a_move_the_record_never_finished(fs, ops, clock, tmp_path) -> None:
    batch = _moved_batch(fs, ops, clock)
    _mark_pending(tmp_path, batch)
    assert ops.undo(batch)["restored"] == 1
    assert fs.exists("D:\\Open\\a.txt")


def test_undo_skips_a_pending_move_that_never_happened(fs, ops, clock, tmp_path) -> None:
    batch = _moved_batch(fs, ops, clock)
    fs.rename("D:\\Open\\b.txt", "D:\\Open\\a.txt")
    _mark_pending(tmp_path, batch)
    assert ops.undo(batch) == {"restored": 0, "skipped": 1, "not_undoable": 0}
    assert fs.exists("D:\\Open\\a.txt")


# --- 看不懂的項目 ---


@pytest.mark.parametrize(
    "change",
    [
        {"op": "rename"},
        {"op": "move", "destination_text": None, "destination_full": None},
    ],
)
def test_an_item_that_is_not_understood_fails(fs, ops, trash, change: dict) -> None:
    fs.add_file("D:\\Open\\a.txt")
    plan = _plan(ops, [{"op": "delete", "path": "Open/a.txt"}])
    assert ops._run_item(replace(plan.items[0], **change)) == "failed"
    assert trash.sent == []
    assert fs.exists("D:\\Open\\a.txt")


# --- 自己的檔案怎麼寫 ---


def test_durable_write_reaches_the_disk(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.json"
    target.write_text("old", encoding="utf-8")
    synced: list[int] = []
    real = fsops.os.fsync

    def spy(descriptor: int) -> None:
        synced.append(descriptor)
        real(descriptor)

    monkeypatch.setattr(fsops.os, "fsync", spy)
    durable_write_text(target, "new")
    assert len(synced) == 1
    assert read_durable_text(target) == "new"
    assert not _temporary(target).exists()


def test_read_falls_back_to_the_temporary_file(tmp_path) -> None:
    target = tmp_path / "a.json"
    _temporary(target).write_text("saved", encoding="utf-8")
    assert read_durable_text(target) == "saved"


def test_read_prefers_the_real_file(tmp_path) -> None:
    target = tmp_path / "a.json"
    target.write_text("real", encoding="utf-8")
    _temporary(target).write_text("half", encoding="utf-8")
    assert read_durable_text(target) == "real"


def test_read_of_nothing_is_an_error(tmp_path) -> None:
    with pytest.raises(OSError):  # noqa: PT011
        read_durable_text(tmp_path / "a.json")

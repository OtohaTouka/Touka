"""真 Windows 的行為（ADR-0312）：CI 只有 Linux，這些在本機跑，結果附在審查資料裡。"""

from __future__ import annotations

import ctypes
import sys

import pytest

if sys.platform == "win32":
    import _winapi
    import tkinter

    # 視窗程式會載入 tkinter，CI 的 Linux 上不一定有，不能在收集測試時就載入
    from touka_local.app import ApprovalWindow

from typing import TYPE_CHECKING

from touka_local import grants as grants_module
from touka_local.approval import PendingApproval
from touka_local.errors import Refused
from touka_local.fsops import RealFs
from touka_local.grants import Environment, GrantBook
from touka_local.operations import ApplyLimits, Operations, Plan, PlanItem
from touka_local.winapi import acquire_single_instance, drive_type

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="只測真 Windows")

_FILE_ATTRIBUTE_HIDDEN = 0x2


def _junction(link: Path, target: Path) -> None:
    _winapi.CreateJunction(str(target), str(link))


def _environment(tmp_path: Path) -> Environment:
    return Environment(
        user_profile="Z:\\NoUser",
        windows_dir="Z:\\NoWindows",
        program_files=("Z:\\NoProgramFiles",),
        program_data="Z:\\NoProgramData",
        app_data="Z:\\NoAppData",
        local_app_data="Z:\\NoLocalAppData",
        code_paths=("Z:\\NoPython",),
        helper_dir=str(tmp_path / "helper"),
        touka_folder="Z:\\NoTouka",
    )


class _KeepTrash:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, path: str) -> str:
        self.sent.append(path)
        return "ok"


@pytest.fixture
def real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Operations, GrantBook, Path, _KeepTrash]:
    # pytest 的暫存資料夾在 AppData 底下，這裡要測的是真的檔案系統，不是這條規則
    monkeypatch.setattr(grants_module, "_SYSTEM_FOLDERS", frozenset())
    open_dir = tmp_path / "Open"
    open_dir.mkdir()
    book = GrantBook(
        tmp_path / "g.json", fs=RealFs(), env=_environment(tmp_path), drive_type=lambda _a: "fixed"
    )
    book.add(str(open_dir), writable=True)
    trash = _KeepTrash()
    operations = Operations(
        fs=RealFs(), grants=book, trash=trash, clock=lambda: 0.0, journal_dir=tmp_path / "journal"
    )
    return operations, book, open_dir, trash


def _limits() -> ApplyLimits:
    return ApplyLimits(should_stop=lambda: False, deadline=1.0)


def test_rename_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    target = tmp_path / "b.txt"
    source.write_text("source", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    with pytest.raises(FileExistsError):
        RealFs().rename(str(source), str(target))
    assert target.read_text(encoding="utf-8") == "target"
    assert source.exists()


def test_create_new_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        RealFs().create_new(str(target), b"new")
    assert target.read_text(encoding="utf-8") == "old"


def test_mkdir_on_existing_fails(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        RealFs().mkdir(str(tmp_path))


def test_junction_is_seen_as_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _junction(link, target)
    info = RealFs().lstat(str(link))
    assert info is not None and info.is_reparse is True


def test_plain_folder_is_not_a_link(tmp_path: Path) -> None:
    info = RealFs().lstat(str(tmp_path))
    assert info is not None and info.is_dir is True and info.is_reparse is False


def test_lstat_of_missing_path(tmp_path: Path) -> None:
    assert RealFs().lstat(str(tmp_path / "none")) is None
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert RealFs().lstat(str(tmp_path / "file.txt" / "inside")) is None


def test_hidden_attribute_is_seen(tmp_path: Path) -> None:
    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(file), _FILE_ATTRIBUTE_HIDDEN)
    info = RealFs().lstat(str(file))
    assert info is not None and info.is_hidden is True


def test_temp_drive_is_fixed(tmp_path: Path) -> None:
    assert drive_type(tmp_path.anchor) == "fixed"


def test_junction_inside_open_folder_is_refused(real) -> None:
    operations, _book, open_dir, _trash = real
    outside = open_dir.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    _junction(open_dir / "link", outside)
    with pytest.raises(Refused) as caught:
        operations.read({"path": "Open/link/secret.txt"})
    assert caught.value.code == "link"
    assert operations.list({"path": "Open"})["entries"] == [{"path": "Open/link", "type": "link"}]


def test_junction_inside_a_folder_blocks_the_whole_item(real) -> None:
    operations, _book, open_dir, trash = real
    outside = open_dir.parent / "outside"
    outside.mkdir()
    sub = open_dir / "sub"
    sub.mkdir()
    _junction(sub / "link", outside)
    with pytest.raises(Refused) as caught:
        operations.plan({"items": [{"op": "delete", "path": "Open/sub"}], "reason": "x"})
    assert caught.value.code == "link"
    assert trash.sent == []


def test_alternate_data_stream_and_device_names_are_refused(real) -> None:
    operations, _book, _open_dir, _trash = real
    for path in ("Open/a.txt:hidden", "Open/CON", "Open/nul.txt", "Open/COM¹"):
        with pytest.raises(Refused):
            operations.create_file({"path": path, "content": "x"})


def test_full_flow_moves_and_undoes(real) -> None:
    operations, _book, open_dir, _trash = real
    (open_dir / "a.txt").write_text("hello", encoding="utf-8")
    (open_dir / "folder").mkdir()
    (open_dir / "folder" / "b.txt").write_text("deep", encoding="utf-8")
    (open_dir / "dest").mkdir()
    plan = operations.plan(
        {
            "items": [
                {"op": "move", "path": "Open/a.txt", "to": "Open/dest/a.txt"},
                {"op": "move", "path": "Open/folder", "to": "Open/dest/folder"},
            ],
            "reason": "整理",
        }
    )
    report = operations.apply(plan, _limits())
    assert [i["status"] for i in report["items"]] == ["done", "done"]
    assert (open_dir / "dest" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (open_dir / "dest" / "folder" / "b.txt").read_text(encoding="utf-8") == "deep"
    assert operations.undo(report["batch"]) == {"restored": 2, "skipped": 0, "not_undoable": 0}
    assert (open_dir / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (open_dir / "folder" / "b.txt").read_text(encoding="utf-8") == "deep"


def test_changed_file_is_skipped_for_real(real) -> None:
    operations, _book, open_dir, trash = real
    file = open_dir / "a.txt"
    file.write_text("before", encoding="utf-8")
    plan = operations.plan({"items": [{"op": "delete", "path": "Open/a.txt"}], "reason": "x"})
    file.write_text("after, and longer", encoding="utf-8")
    report = operations.apply(plan, _limits())
    assert report["items"][0]["status"] == "changed"
    assert trash.sent == []


def test_new_file_deep_in_a_folder_is_seen_for_real(real) -> None:
    operations, _book, open_dir, trash = real
    deep = open_dir / "sub" / "deep"
    deep.mkdir(parents=True)
    (deep / "a.txt").write_text("x", encoding="utf-8")
    plan = operations.plan({"items": [{"op": "delete", "path": "Open/sub"}], "reason": "x"})
    (deep / "new.txt").write_text("x", encoding="utf-8")
    assert operations.apply(plan, _limits())["items"][0]["status"] == "changed"
    assert trash.sent == []


def test_a_second_copy_is_refused() -> None:
    acquire_single_instance()
    assert acquire_single_instance() is False


def test_approval_window_shows_every_full_path() -> None:
    """表格放不下長路徑，核准前要能在視窗上看到每一項的完整路徑。"""
    root = tkinter.Tk()
    root.withdraw()
    try:
        items = tuple(
            PlanItem(
                op="delete",
                source_text=f"Open/{name}",
                destination_text=None,
                source_full=f"D:\\Open\\{name}",
                destination_full=None,
                size=1,
                is_folder=False,
                contained=0,
                unusual=(),
                mtime_ns=1,
                snapshot=(),
            )
            for name in ("first.txt", "second.txt")
        )
        pending = PendingApproval(
            job_id="j",
            plan=Plan(items=items, reason="r", digest="d" * 64),
            requester="",
            offered_at=0.0,
            deadline=1.0,
        )
        window = ApprovalWindow(root, pending, on_approve=lambda: None, on_reject=lambda: None)
        text = window._paths.get("1.0", "end")
        assert "D:\\Open\\first.txt" in text
        assert "D:\\Open\\second.txt" in text
        assert window._paths.cget("state") == "disabled"
    finally:
        root.destroy()

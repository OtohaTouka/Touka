"""真 Windows 的行為（ADR-0312）：CI 只有 Linux，這些在本機跑，結果附在審查資料裡。"""

from __future__ import annotations

import ctypes
import sys

import pytest

if sys.platform == "win32":
    import _winapi
    import tkinter

    # 視窗程式會載入 tkinter，CI 的 Linux 上不一定有，不能在收集測試時就載入
    from touka_local import app as app_module
    from touka_local.app import ApprovalWindow, MainWindow, OpenRequestWindow, _setup_display

import base64
import logging
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from touka_local import grants as grants_module
from touka_local import settings as settings_module
from touka_local.approval import ApprovalGate, PendingApproval
from touka_local.display import DisplayScale
from touka_local.errors import Refused
from touka_local.fsops import RealFs
from touka_local.grants import Environment, GrantBook
from touka_local.open_request import STATE_NOT_OPENABLE, STATE_OPENABLE, checking
from touka_local.operations import ApplyLimits, Operations, Plan, PlanItem
from touka_local.presence import AttentionTracker
from touka_local.service import CLOSE_BLOCKED, CLOSE_WAIT, HelperService
from touka_local.settings import ModeSwitches, Settings, load_settings
from touka_local.winapi import (
    acquire_single_instance,
    consume_show_event,
    create_show_event,
    drive_type,
    signal_show_event,
)

if TYPE_CHECKING:
    from pathlib import Path

    from touka_local.open_request import OpenRequest

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
    monkeypatch.setattr(grants_module, "SYSTEM_FOLDER_NAMES", frozenset())
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


def _test_name(kind: str) -> str:
    # 不用正式名稱：本機可能有真的小幫手在背景跑（ADR-0312 修訂，2026-11）
    return f"Local\\ToukaLocalTest{kind}-{uuid.uuid4().hex}"


def test_a_second_copy_is_refused() -> None:
    name = _test_name("Mutex")
    acquire_single_instance(name)
    assert acquire_single_instance(name) is False


def test_a_second_copy_can_call_up_the_first() -> None:
    name = _test_name("Show")
    handle = create_show_event(name)
    assert handle is not None
    try:
        assert consume_show_event(handle) is False
        assert signal_show_event(name) is True
        assert signal_show_event(name) is True
        # 自動重設：連續兩次通知只算一次
        assert consume_show_event(handle) is True
        assert consume_show_event(handle) is False
    finally:
        _winapi.CloseHandle(handle)


def test_an_event_someone_else_made_is_not_used() -> None:
    name = _test_name("Show")
    squatter = create_show_event(name)
    assert squatter is not None
    try:
        assert create_show_event(name) is None
    finally:
        _winapi.CloseHandle(squatter)


def test_calling_up_without_a_running_helper_fails() -> None:
    assert signal_show_event(_test_name("Show")) is False


def test_consuming_without_an_event_is_always_false() -> None:
    assert consume_show_event(None) is False


def test_the_main_window_asks_its_own_show_event() -> None:
    name = _test_name("Show")
    handle = create_show_event(name)
    assert handle is not None
    try:
        requested = app_module._show_requests(handle)
        assert requested() is False
        assert signal_show_event(name) is True
        assert requested() is True
        assert requested() is False
    finally:
        _winapi.CloseHandle(handle)
    assert app_module._show_requests(None)() is False


@pytest.mark.parametrize(
    ("background", "paired", "show_ready", "hidden"),
    [
        (True, True, True, True),
        (True, True, False, False),
        (True, False, True, False),
        (False, True, True, False),
    ],
)
def test_only_a_paired_startup_launch_starts_hidden(
    caplog: pytest.LogCaptureFixture,
    *,
    background: bool,
    paired: bool,
    show_ready: bool,
    hidden: bool,
) -> None:
    root = tkinter.Tk()
    try:
        with caplog.at_level(logging.WARNING):
            app_module._prepare_main_window(
                root, background=background, paired=paired, show_ready=show_ready
            )
        assert (root.state() == "withdrawn") is hidden
        # 叫不出來時留一筆紀錄，之後查得到為什麼主視窗一直顯示、× 只最小化
        assert any("show event" in record.getMessage() for record in caplog.records) is (
            not show_ready
        )
    finally:
        root.destroy()


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
        window = ApprovalWindow(
            root,
            pending,
            on_approve=lambda: None,
            on_reject=lambda: None,
            display=DisplayScale(factor=2.0),
        )
        text = window._paths.get("1.0", "end")
        assert "D:\\Open\\first.txt" in text
        assert "D:\\Open\\second.txt" in text
        assert window._paths.cget("state") == "disabled"
    finally:
        root.destroy()


def _open_pending(request: OpenRequest) -> PendingApproval:
    return PendingApproval(
        job_id="o", plan=request, requester="owner", offered_at=0.0, deadline=120.0
    )


def test_open_window_defaults_to_reject_and_read_only() -> None:
    """開放請求（ADR-0320）：預設焦點在拒絕、核准鍵先停用、權限預設只能看、完整路徑看得到。"""
    root = tkinter.Tk()
    root.withdraw()
    try:
        request = replace(
            checking("D:\\Photos"), state=STATE_OPENABLE, folder="D:\\Photos\\家 2026"
        )
        window = OpenRequestWindow(
            root,
            _open_pending(request),
            on_approve=lambda: None,
            on_reject=lambda: None,
            display=DisplayScale(factor=1.0),
        )
        root.update()
        assert window.default_button.cget("text") == "拒絕"
        assert window._approve is not None
        assert str(window._approve.cget("state")) == "disabled"
        assert window.writable is False
        assert "D:\\Photos\\家 2026" in window.path_box.get("1.0", "end")
        assert window.path_box.cget("state") == "disabled"
        window._enable_approve()
        assert str(window._approve.cget("state")) == "normal"
    finally:
        root.destroy()


def test_a_place_that_cannot_be_opened_only_offers_acknowledge() -> None:
    root = tkinter.Tk()
    root.withdraw()
    try:
        request = replace(checking("D:\\Missing"), state=STATE_NOT_OPENABLE, reason="not_found")
        window = OpenRequestWindow(
            root,
            _open_pending(request),
            on_approve=lambda: None,
            on_reject=lambda: None,
            display=DisplayScale(factor=1.0),
        )
        root.update()
        assert window.default_button.cget("text") == "知道了"
        assert window._approve is None
        assert "D:\\Missing" in window.path_box.get("1.0", "end")
    finally:
        root.destroy()


def test_display_setup_runs_on_a_real_window() -> None:
    root = tkinter.Tk()
    root.withdraw()
    try:
        assert _setup_display(root).factor >= 1.0
    finally:
        root.destroy()


class _BrokenRoot:
    def winfo_fpixels(self, _distance: str) -> float:
        raise tkinter.TclError


def test_display_setup_failure_keeps_the_default_sizes() -> None:
    assert _setup_display(_BrokenRoot()) == DisplayScale()  # type: ignore[arg-type]


def _pending(reason: str = "") -> PendingApproval:
    item = PlanItem(
        op="delete",
        source_text="Open/a.txt",
        destination_text=None,
        source_full="D:\\Open\\a.txt",
        destination_full=None,
        size=1,
        is_folder=False,
        contained=0,
        unusual=(),
        mtime_ns=1,
        snapshot=(),
    )
    return PendingApproval(
        job_id="j",
        plan=Plan(items=(item,), reason="r", digest="d" * 64),
        requester="",
        offered_at=0.0,
        deadline=1.0,
        reason=reason,
    )


@pytest.mark.parametrize(("reason", "shown"), [("marked", True), ("no_decision", False)])
def test_approval_window_says_which_floor_opened_it(reason: str, *, shown: bool) -> None:
    root = tkinter.Tk()
    root.withdraw()
    try:
        window = ApprovalWindow(
            root,
            _pending(reason),
            on_approve=lambda: None,
            on_reject=lambda: None,
            display=DisplayScale(),
        )
        assert (window._floor is not None) is shown
    finally:
        root.destroy()


class _ShowRequests:
    """代替命名事件：測試把「再開一次」放進來，主視窗每一輪取一次。"""

    def __init__(self) -> None:
        self.pending = 0

    def __call__(self) -> bool:
        if self.pending:
            self.pending -= 1
            return True
        return False


def _build_main_window(real, tmp_path: Path, *, show_ready: bool = True):
    operations, book, _open_dir, _trash = real
    switches = ModeSwitches(tmp_path / "helper")
    gate = ApprovalGate(clock=lambda: 0.0)
    service = HelperService(
        client=None,
        operations=operations,
        gate=gate,
        grants=book,
        clock=lambda: 0.0,
        spawn=lambda work: work(),
        decision_key=switches.decision_key,
        following=switches.following,
        mode_reset=switches.reset_at,
    )
    root = tkinter.Tk()
    # 開機啟動的樣子：建好 root 馬上收起來，第一次 update 之前
    root.withdraw()
    requests = _ShowRequests()
    window = MainWindow(
        root,
        service=service,
        gate=gate,
        grants=book,
        operations=operations,
        settings=Settings(port=8000, token=None, touka_folder="Z:\\NoTouka"),
        settings_file=tmp_path / "helper" / "settings.json",
        display=DisplayScale(),
        switches=switches,
        show_requested=requests,
        show_ready=show_ready,
        tracker=AttentionTracker(clock=lambda: 0.0),
    )
    return window, switches, tmp_path / "helper", requests


@pytest.fixture
def main_window(real, tmp_path: Path):
    window, switches, helper, _requests = _build_main_window(real, tmp_path)
    try:
        yield window, switches, helper
    finally:
        window._root.destroy()


@pytest.fixture
def shown_window(real, tmp_path: Path):
    """主視窗開著、第一輪已經跑過（還沒配對的提醒算已經提醒過）。"""
    window, _switches, _helper, requests = _build_main_window(real, tmp_path)
    root = window._root
    root.deiconify()
    window._refresh()
    root.update()
    try:
        yield window, requests
    finally:
        root.destroy()


def _press_close(window, monkeypatch: pytest.MonkeyPatch) -> None:
    # 照使用者按右上角 × 的路徑：視窗管理員的關閉訊息綁的那個命令。
    # × 不該問要不要結束；萬一問了就回「取消」，讓測試失敗而不是卡在對話框
    asked: list[str] = []
    monkeypatch.setattr(
        app_module.messagebox, "askokcancel", lambda title, *_a, **_k: asked.append(title)
    )
    root = window._root
    root.tk.call(root.protocol("WM_DELETE_WINDOW"))
    assert asked == []


def test_the_close_button_only_hides_the_window(shown_window, monkeypatch) -> None:
    window, _requests = shown_window
    _press_close(window, monkeypatch)
    window._root.update()
    assert window._root.state() == "withdrawn"
    # 還沒配對的提醒剛才開著時已經算過，收起來後不會馬上又跑回來
    window._refresh()
    window._root.update()
    assert window._root.state() == "withdrawn"


def test_the_close_button_minimizes_when_the_window_cannot_be_called_up(
    real, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _switches, _helper, _requests = _build_main_window(real, tmp_path, show_ready=False)
    root = window._root
    try:
        root.deiconify()
        window._refresh()
        root.update()
        _press_close(window, monkeypatch)
        root.update()
        assert root.state() == "iconic"
    finally:
        root.destroy()


def test_opening_it_again_brings_the_window_back(shown_window, monkeypatch) -> None:
    window, requests = shown_window
    _press_close(window, monkeypatch)
    window._root.update()
    requests.pending = 1
    window._refresh()
    window._root.update()
    assert window._root.state() == "normal"


class _RoundFailed(Exception):
    pass


def test_a_failing_round_still_brings_the_window_back(shown_window, monkeypatch) -> None:
    window, requests = shown_window
    _press_close(window, monkeypatch)
    window._root.update()

    def broken() -> None:
        raise _RoundFailed

    # 例如審批視窗一直建不起來：這段時間收著的主視窗也要叫得出來
    monkeypatch.setattr(window, "_sync_approval", broken)
    requests.pending = 1
    with pytest.raises(_RoundFailed):
        window._refresh()
    window._root.update()
    assert window._root.state() == "normal"


def test_a_new_condition_surfaces_a_hidden_window_minimized(main_window) -> None:
    window, _switches, _helper = main_window
    # 還沒配對：開機時藏起來的主視窗以最小化出現，不搶焦點
    window._refresh()
    window._root.update()
    assert window._root.state() == "iconic"


def test_approval_windows_show_while_the_main_window_is_hidden(main_window) -> None:
    window, _switches, _helper = main_window
    root = window._root
    assert root.state() == "withdrawn"
    pending = PendingApproval(
        job_id="j",
        plan=Plan(items=(), reason="r", digest="d" * 64),
        requester="",
        offered_at=0.0,
        deadline=1.0,
    )
    approval = ApprovalWindow(
        root, pending, on_approve=lambda: None, on_reject=lambda: None, display=DisplayScale()
    )
    root.update()
    assert approval._window.winfo_ismapped()
    # 主視窗沒有被叫出來（還沒配對的提醒最多讓它最小化）
    assert root.state() != "normal"


def test_quitting_still_asks_first(shown_window, monkeypatch) -> None:
    window, _requests = shown_window
    asked: list[str] = []

    def answer(title: str, _text: str, **_kwargs: object) -> bool:
        asked.append(title)
        return False

    monkeypatch.setattr(app_module.messagebox, "askokcancel", answer)
    window._quit_button.invoke()
    assert asked
    assert window._root.winfo_exists()


def test_a_cancelled_quit_says_why(shown_window, monkeypatch) -> None:
    window, _requests = shown_window
    monkeypatch.setattr(app_module.messagebox, "askokcancel", lambda *_a, **_k: True)
    monkeypatch.setattr(window._service, "request_close", lambda: CLOSE_WAIT)
    window._quit_button.invoke()
    assert window._closing
    # 等著關的時候又來一份清單：不關，而且說清楚
    monkeypatch.setattr(window._service, "request_close", lambda: CLOSE_BLOCKED)
    window._tick()
    assert not window._closing
    assert "先不關" in window._notice.cget("text")


def test_the_switch_is_saved_when_ticked(main_window) -> None:
    window, switches, helper = main_window
    window._following.set(True)
    window._toggle_following()
    assert switches.following() is True
    assert settings_module.load_mode_following(helper / settings_module.MODE_FOLLOWING_FILE)
    window._following.set(False)
    window._toggle_following()
    assert switches.following() is False


def test_a_switch_that_cannot_be_saved_shows_as_off(main_window, monkeypatch) -> None:
    window, switches, _helper = main_window

    def broken(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(settings_module, "save_mode_following", broken)
    window._following.set(True)
    window._toggle_following()
    assert window._following.get() is False
    assert switches.following() is False


def test_a_new_pairing_code_stores_the_key_and_an_old_one_removes_it(main_window) -> None:
    window, switches, helper = main_window
    key = bytes(range(32))
    token = "t" * 43
    window._code.set(f"{token}.{base64.urlsafe_b64encode(key).decode().rstrip('=')}")
    window._save_pairing()
    assert switches.decision_key() == key
    assert load_settings(helper / "settings.json").token == token
    assert "重新產生配對碼" not in window._notice.cget("text")
    window._code.set("u" * 43)
    window._save_pairing()
    assert switches.decision_key() is None
    assert settings_module.load_decision_key(helper / settings_module.DECISION_KEY_FILE) is None
    assert "重新產生配對碼" in window._notice.cget("text")
    window._refresh_mode_hint()
    assert "重新產生配對碼" in window._mode_hint.cget("text")


def test_changing_only_the_port_keeps_the_key(main_window) -> None:
    window, switches, _helper = main_window
    key = bytes(range(32))
    window._code.set(f"{'t' * 43}.{base64.urlsafe_b64encode(key).decode().rstrip('=')}")
    window._save_pairing()
    window._port.set("8123")
    window._save_pairing()
    assert switches.decision_key() == key


def test_recalculating_needs_a_second_yes(main_window, monkeypatch) -> None:
    window, switches, _helper = main_window
    answers = iter([False, True])
    monkeypatch.setattr(
        "touka_local.app.messagebox.askokcancel", lambda *_args, **_kwargs: next(answers)
    )
    window._reset_hourly()
    assert switches.reset_at() is None
    window._reset_hourly()
    assert switches.reset_at() is not None


def test_unreadable_records_are_named_in_the_main_window(main_window, tmp_path: Path) -> None:
    window, _switches, _helper = main_window
    folder = tmp_path / "journal"
    folder.mkdir(exist_ok=True)
    broken = folder / f"{'a' * 32}.json"
    broken.write_text("{not json", encoding="utf-8")
    window._refresh_mode_hint()
    assert str(broken) in window._mode_hint.cget("text")

"""小幫手的視窗（ADR-0312）。規則都在別的檔案，這裡只負責顯示和收按鍵。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any, Final

from touka_local import __version__, winapi
from touka_local.approval import APPROVE_DELAY_SECONDS, ApprovalGate
from touka_local.connection import ToukaClient, check_port
from touka_local.errors import Refused
from touka_local.fsops import RealFs
from touka_local.grants import GrantBook
from touka_local.operations import Operations
from touka_local.service import (
    CLOSE_BLOCKED,
    CLOSE_NOW,
    CLOSE_WAIT,
    NOTICE_APPLY_FAILED,
    NOTICE_FINISHED,
    NOTICE_NOT_CONFIRMED,
    NOTICE_NOT_DELIVERED,
    NOTICE_PARTLY_DONE,
    HelperService,
)
from touka_local.settings import (
    HELPER_FOLDER_NAME,
    Settings,
    load_environment,
    load_settings,
    save_settings,
)
from touka_local.view import (
    APPROVAL_COLUMNS,
    APPROVAL_WINDOW_WIDTH,
    all_paths_text,
    approval_counts,
    approval_rows,
    approval_title,
    clean_reason,
    connection_text,
    refusal_text,
    report_text,
    requester_text,
    row_values,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from touka_local.approval import PendingApproval
    from touka_local.view import ApprovalRow

logger = logging.getLogger(__name__)

_REFRESH_MS: Final = 300
_APPROVAL_WRAP: Final = APPROVAL_WINDOW_WIDTH - 40
_PATHS_PANEL_LINES: Final = 6
_BATCH_REFRESH_SECONDS: Final = 5.0
_LOG_BYTES: Final = 256 * 1024
_NOTICE_TEXT: Final = {
    NOTICE_NOT_CONFIRMED: "Touka 那邊已經不等這份清單了，所以什麼都沒有動。",
    NOTICE_NOT_DELIVERED: "結果沒有送回 Touka。請看下面的最近批次，確認哪些已經動了。",
    NOTICE_PARTLY_DONE: "清單只做了一部分，結果在最近批次。",
    NOTICE_FINISHED: "清單做完了，結果在最近批次。",
    NOTICE_APPLY_FAILED: "這份清單沒有做完，細節在紀錄檔。請看下面的最近批次，確認哪些已經動了。",
}


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


class ApprovalWindow:
    """核准視窗：預設焦點在拒絕，核准鍵延遲才能按。"""

    def __init__(
        self,
        parent: tk.Tk,
        pending: PendingApproval,
        *,
        on_approve: Callable[[], None],
        on_reject: Callable[[], None],
    ) -> None:
        self.job_id = pending.job_id
        self._window = tk.Toplevel(parent)
        self._window.title("Touka 想更動你的檔案")
        self._window.geometry(f"{APPROVAL_WINDOW_WIDTH}x520")
        self._window.protocol("WM_DELETE_WINDOW", on_reject)
        self._window.attributes("-topmost", True)

        counts = approval_counts(pending.plan)
        rows = approval_rows(pending.plan)
        frame = ttk.Frame(self._window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=approval_title(counts), font=("", 12, "bold")).pack(anchor="w")
        if pending.requester:
            ttk.Label(frame, text=requester_text(pending.requester)).pack(anchor="w")
        ttk.Label(
            frame,
            text=f"代理人的說明（可能不準）：{clean_reason(pending.plan.reason)}",
            wraplength=_APPROVAL_WRAP,
        ).pack(anchor="w", pady=(4, 8))
        if counts.unusual:
            ttk.Label(
                frame,
                text=f"有 {counts.unusual} 項不尋常，請特別看「注意」欄。",
                foreground="#b00020",
            ).pack(anchor="w", pady=(0, 8))

        # 下面的先排，視窗縮小時按鈕才不會被表格擠掉
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        self._countdown = ttk.Label(frame, text="")
        self._countdown.pack(side="bottom", anchor="w", pady=(8, 4))
        self._add_paths_panel(frame, rows)
        self._add_table(frame, rows)

        reject = ttk.Button(buttons, text="拒絕", command=on_reject)
        reject.pack(side="right")
        self._approve = ttk.Button(buttons, text="核准", command=on_approve, state="disabled")
        self._approve.pack(side="right", padx=(0, 8))
        reject.focus_set()
        self._window.after(int(APPROVE_DELAY_SECONDS * 1000), self._enable_approve)

    def _add_paths_panel(self, frame: ttk.Frame, rows: list[ApprovalRow]) -> None:
        """每一項的完整路徑，可以捲動。文字由程式放進去，人和代理人都改不了。"""
        holder = ttk.Frame(frame)
        holder.pack(side="bottom", fill="x", pady=(8, 0))
        scrollbar = ttk.Scrollbar(holder, orient="vertical")
        self._paths = tk.Text(
            holder, height=_PATHS_PANEL_LINES, wrap="char", yscrollcommand=scrollbar.set
        )
        scrollbar.configure(command=self._paths.yview)
        self._paths.insert("1.0", all_paths_text(rows))
        self._paths.configure(state="disabled")
        scrollbar.pack(side="right", fill="y")
        self._paths.pack(side="left", fill="x", expand=True)

    def _add_table(self, frame: ttk.Frame, rows: list[ApprovalRow]) -> None:
        holder = ttk.Frame(frame)
        holder.pack(side="top", fill="both", expand=True)
        columns = tuple(key for key, _heading, _width in APPROVAL_COLUMNS)
        table = ttk.Treeview(holder, columns=columns, show="headings", height=12, selectmode="none")
        for column, heading, width in APPROVAL_COLUMNS:
            table.heading(column, text=heading)
            # 欄寬固定；格子放不下的路徑，在表格下面看完整的
            table.column(column, width=width, minwidth=width, stretch=False)
        for index, row in enumerate(rows):
            table.insert("", "end", iid=str(index), values=row_values(row))
        vertical = ttk.Scrollbar(holder, orient="vertical", command=table.yview)
        horizontal = ttk.Scrollbar(holder, orient="horizontal", command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        table.pack(side="left", fill="both", expand=True)

    def _enable_approve(self) -> None:
        self._approve.configure(state="normal")

    def show_countdown(self, remaining: float) -> None:
        """更新剩餘時間。"""
        seconds = max(0, int(remaining))
        self._countdown.configure(text=f"{seconds // 60}:{seconds % 60:02d} 後自動拒絕")

    def close(self) -> None:
        """關掉視窗。"""
        self._window.destroy()


class MainWindow:
    """資料夾名單、狀態、最近批次。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        service: HelperService,
        gate: ApprovalGate,
        grants: GrantBook,
        operations: Operations,
        settings: Settings,
        settings_file: Path,
    ) -> None:
        self._root = root
        self._service = service
        self._gate = gate
        self._grants = grants
        self._operations = operations
        self._settings = settings
        self._settings_file = settings_file
        self._approval: ApprovalWindow | None = None
        self._last_batches = 0.0
        self._closing = False

        root.title("Touka 本機小幫手")
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.geometry("780x620")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        self._build_status(frame)
        self._build_grants(frame)
        self._build_batches(frame)
        self._refresh_grants()
        self._refresh_batches()
        root.after(0, self._tick)

    # --- 版面 ---

    def _build_status(self, frame: ttk.Frame) -> None:
        self._status = ttk.Label(frame, text="", font=("", 11, "bold"))
        self._status.pack(anchor="w")
        pairing = ttk.Frame(frame)
        pairing.pack(fill="x", pady=(6, 10))
        ttk.Label(pairing, text="配對碼").pack(side="left")
        self._code = tk.StringVar()
        ttk.Entry(pairing, textvariable=self._code, show="•", width=34).pack(side="left", padx=4)
        ttk.Label(pairing, text="連接埠").pack(side="left", padx=(8, 0))
        self._port = tk.StringVar(value=str(self._settings.port))
        ttk.Entry(pairing, textvariable=self._port, width=7).pack(side="left", padx=4)
        ttk.Button(pairing, text="儲存", command=self._save_pairing).pack(side="left")
        self._stop = ttk.Button(
            pairing, text="停止目前這批", command=self._service.stop_running, state="disabled"
        )
        self._stop.pack(side="right")

    def _build_grants(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="開放給 Touka 的資料夾").pack(anchor="w")
        columns = ("name", "path", "access", "state")
        self._grant_table = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        for column, heading, width in zip(
            columns, ("名稱", "位置", "權限", "狀態"), (130, 420, 90, 90), strict=True
        ):
            self._grant_table.heading(column, text=heading)
            self._grant_table.column(column, width=width, stretch=column == "path")
        self._grant_table.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(4, 10))
        ttk.Button(buttons, text="開放（只能看）", command=lambda: self._add(writable=False)).pack(
            side="left"
        )
        ttk.Button(buttons, text="開放（可以改）", command=lambda: self._add(writable=True)).pack(
            side="left", padx=4
        )
        ttk.Button(buttons, text="切換權限", command=self._toggle).pack(side="left", padx=4)
        ttk.Button(buttons, text="拿掉", command=self._remove).pack(side="left", padx=4)

    def _build_batches(self, frame: ttk.Frame) -> None:
        ttk.Label(frame, text="最近批次").pack(anchor="w")
        columns = ("reason", "done", "undoable")
        self._batch_table = ttk.Treeview(frame, columns=columns, show="headings", height=6)
        for column, heading, width in zip(
            columns, ("說明", "完成", "可復原"), (520, 80, 80), strict=True
        ):
            self._batch_table.heading(column, text=heading)
            self._batch_table.column(column, width=width, stretch=column == "reason")
        self._batch_table.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(4, 4))
        ttk.Button(buttons, text="復原這批的搬移", command=self._undo).pack(side="left")
        self._notice = ttk.Label(frame, text="", wraplength=740)
        self._notice.pack(anchor="w")

    # --- 動作 ---

    def _say(self, text: str) -> None:
        self._notice.configure(text=text)

    def _save_pairing(self) -> None:
        try:
            port = check_port(int(self._port.get()) if self._port.get().isdecimal() else 0)
            token = self._code.get().strip() or self._settings.token
            client = (
                ToukaClient(port=port, token=token, helper_version=__version__)
                if token is not None
                else None
            )
        except Refused as exc:
            self._say(refusal_text(exc.code))
            return
        settings = Settings(port=port, token=token, touka_folder=self._settings.touka_folder)
        try:
            save_settings(self._settings_file, settings)
        except OSError:
            self._say("設定存不進去，這次沒有變更。")
            return
        self._settings = settings
        self._service.replace_client(client)
        self._code.set("")
        self._say("已儲存。")

    def _add(self, *, writable: bool) -> None:
        chosen = filedialog.askdirectory(title="選要開放的資料夾")
        if not chosen:
            return
        try:
            grant = self._grants.add(chosen, writable=writable)
        except Refused as exc:
            self._say(refusal_text(exc.code))
            return
        except OSError:
            self._say("這個資料夾讀不到，或名單存不進去，沒有開放。")
            return
        self._refresh_grants()
        if self._grants.needs_onedrive_hint(grant):
            self._say("這是 OneDrive 的資料夾，裡面還沒下載的雲端檔案都不能使用。")
        else:
            self._say(f"已開放「{grant.name}」。")

    def _selected(self, table: ttk.Treeview) -> str | None:
        selection = table.selection()
        return selection[0] if selection else None

    def _toggle(self) -> None:
        name = self._selected(self._grant_table)
        if name is None:
            return
        grant = next((g for g in self._grants.grants() if g.name == name), None)
        if grant is not None:
            self._change_grants(
                lambda: self._grants.set_writable(name, writable=not grant.writable)
            )

    def _remove(self) -> None:
        name = self._selected(self._grant_table)
        if name is not None:
            self._change_grants(lambda: self._grants.remove(name))

    def _change_grants(self, change: Callable[[], None]) -> None:
        try:
            change()
        except OSError:
            self._say("名單存不進去，這次沒有變更。")
            return
        self._refresh_grants()

    def _undo(self) -> None:
        batch = self._selected(self._batch_table)
        if batch is None:
            return
        try:
            result = self._service.undo(batch)
        except Refused as exc:
            self._say(refusal_text(exc.code))
            return
        warning = (
            "這次的紀錄沒有存好，同一批請不要再按復原。" if result.get("journal_incomplete") else ""
        )
        self._say(
            f"復原了 {result['restored']} 項，跳過 {result['skipped']} 項（沒有搬過或檔案變動過）。"
            f"丟到回收桶的 {result['not_undoable']} 項請從回收桶還原。{warning}"
        )
        self._refresh_batches()

    # --- 更新畫面 ---

    def _refresh_grants(self) -> None:
        paused = set(self._grants.paused_names())
        table = self._grant_table
        table.delete(*table.get_children())
        for grant in self._grants.grants():
            table.insert(
                "",
                "end",
                iid=grant.name,
                values=(
                    grant.name,
                    grant.path,
                    "可以改" if grant.writable else "只能看",
                    "暫停" if grant.name in paused else "開放中",
                ),
            )

    def _refresh_batches(self) -> None:
        table = self._batch_table
        selected = self._selected(table)
        table.delete(*table.get_children())
        for batch in self._operations.recent_batches():
            table.insert(
                "",
                "end",
                iid=batch["batch"],
                values=(
                    clean_reason(str(batch["reason"])),
                    f"{batch['done']}/{batch['total']}",
                    batch["undoable"],
                ),
            )
        if selected is not None and table.exists(selected):
            table.selection_set(selected)

    def _close(self) -> None:
        decision = self._service.request_close()
        if decision == CLOSE_NOW:
            self._root.destroy()
        elif decision == CLOSE_WAIT:
            self._closing = True
            self._say("做完手上這項就會關閉。")
        else:
            self._say("有一份清單在等你決定，請先核准或拒絕。")

    def _tick(self) -> None:
        try:
            self._refresh()
        finally:
            decision = self._service.request_close() if self._closing else None
            if decision == CLOSE_NOW:
                self._root.destroy()
            else:
                # 等著關閉的時候又來了一份清單：人要先決定，決定完不該自己關掉
                self._closing = self._closing and decision != CLOSE_BLOCKED
                self._root.after(_REFRESH_MS, self._tick)

    def _refresh(self) -> None:
        self._service.tick()
        self._status.configure(text=f"Touka：{connection_text(self._service.state)}")
        self._stop.configure(state="normal" if self._service.running else "disabled")
        self._sync_approval()
        notice = _NOTICE_TEXT.get(self._service.notice)
        if notice is not None:
            report = self._service.last_report
            self._say(f"{notice}（{report_text(report)}）" if report else notice)
            self._service.notice = ""
            self._refresh_batches()
        now = time.monotonic()
        if now - self._last_batches >= _BATCH_REFRESH_SECONDS:
            self._last_batches = now
            self._refresh_grants_status()
            self._refresh_batches()

    def _refresh_grants_status(self) -> None:
        selected = self._selected(self._grant_table)
        self._refresh_grants()
        if selected is not None and self._grant_table.exists(selected):
            self._grant_table.selection_set(selected)

    def _sync_approval(self) -> None:
        pending = self._gate.current
        if self._approval is not None and (
            pending is None or pending.job_id != self._approval.job_id
        ):
            self._approval.close()
            self._approval = None
        if pending is None:
            return
        if self._approval is None:
            self._approval = ApprovalWindow(
                self._root,
                pending,
                on_approve=lambda: self._approve(pending),
                on_reject=lambda: self._service.reject(pending.job_id),
            )
        self._approval.show_countdown(pending.deadline - time.monotonic())

    def _approve(self, pending: PendingApproval) -> None:
        try:
            self._service.approve(pending.job_id, pending.plan.digest)
        except Refused as exc:
            self._say(refusal_text(exc.code))


def _setup_logging(helper_dir: Path) -> None:
    helper_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        helper_dir / "helper.log", maxBytes=_LOG_BYTES, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("touka_local")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def _log_window_error(*details: Any) -> None:
    logger.error("a window callback failed", exc_info=details)


def _make_client(settings: Settings) -> ToukaClient | None:
    if settings.token is None:
        return None
    try:
        return ToukaClient(port=settings.port, token=settings.token, helper_version=__version__)
    except Refused:
        return None


def main() -> int:
    """啟動小幫手。"""
    if not winapi.acquire_single_instance():
        messagebox.showinfo("Touka 本機小幫手", "小幫手已經開著了。")
        return 1
    environ = os.environ  # noqa: ENV_SSOT_VIOLATION
    settings_file = Path(environ["LOCALAPPDATA"]) / HELPER_FOLDER_NAME / "settings.json"
    settings = load_settings(settings_file)
    try:
        environment = load_environment(environ, settings.touka_folder)
    except Refused:
        messagebox.showerror("Touka 本機小幫手", "小幫手的設定不完整，請重新安裝。")
        return 1
    helper_dir = Path(environment.helper_dir)
    _setup_logging(helper_dir)
    fs = RealFs()
    grants = GrantBook(
        helper_dir / "grants.json", fs=fs, env=environment, drive_type=winapi.drive_type
    )
    operations = Operations(
        fs=fs,
        grants=grants,
        trash=winapi.RecycleBin(),
        clock=time.monotonic,
        journal_dir=helper_dir / "journal",
    )
    gate = ApprovalGate(clock=time.monotonic)
    service = HelperService(
        client=_make_client(settings),
        operations=operations,
        gate=gate,
        grants=grants,
        clock=time.monotonic,
        spawn=_spawn,
    )
    stop = threading.Event()
    threading.Thread(target=service.run, args=(stop,), daemon=True).start()
    root = tk.Tk()
    root.report_callback_exception = _log_window_error
    MainWindow(
        root,
        service=service,
        gate=gate,
        grants=grants,
        operations=operations,
        settings=settings,
        settings_file=settings_file,
    )
    root.mainloop()
    stop.set()
    return 0

"""小幫手的視窗（ADR-0311）。規則都在別的檔案，這裡只負責顯示和收按鍵。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Final

from touka_local import __version__, winapi
from touka_local.approval import APPROVE_DELAY_SECONDS, ApprovalGate
from touka_local.connection import ToukaClient, check_port
from touka_local.errors import Refused
from touka_local.fsops import RealFs
from touka_local.grants import GrantBook
from touka_local.operations import Operations
from touka_local.service import (
    NOTICE_FINISHED,
    NOTICE_NOT_CONFIRMED,
    NOTICE_NOT_DELIVERED,
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
    approval_counts,
    approval_rows,
    approval_title,
    clean_reason,
    connection_text,
    display_safe,
    refusal_text,
    report_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from touka_local.approval import PendingApproval

_REFRESH_MS: Final = 300
_BATCH_REFRESH_SECONDS: Final = 5.0
_LOG_BYTES: Final = 256 * 1024
_NOTICE_TEXT: Final = {
    NOTICE_NOT_CONFIRMED: "Touka 那邊已經不等這份清單了，所以什麼都沒有動。",
    NOTICE_NOT_DELIVERED: "已經照清單做了，但結果沒有送回 Touka。請看下面的最近批次。",
    NOTICE_FINISHED: "清單做完了，結果在最近批次。",
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
        self._window.geometry("820x460")
        self._window.protocol("WM_DELETE_WINDOW", on_reject)
        self._window.attributes("-topmost", True)

        counts = approval_counts(pending.plan)
        frame = ttk.Frame(self._window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=approval_title(counts), font=("", 12, "bold")).pack(anchor="w")
        if pending.requester:
            ttk.Label(frame, text=f"提出者：{display_safe(pending.requester)}").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"代理人的說明（可能不準）：{clean_reason(pending.plan.reason)}",
            wraplength=780,
        ).pack(anchor="w", pady=(4, 8))
        if counts.unusual:
            ttk.Label(
                frame,
                text=f"有 {counts.unusual} 項不尋常，請特別看「注意」欄。",
                foreground="#b00020",
            ).pack(anchor="w", pady=(0, 8))

        self._add_table(frame, pending)
        self._countdown = ttk.Label(frame, text="")
        self._countdown.pack(anchor="w", pady=(8, 4))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        reject = ttk.Button(buttons, text="拒絕", command=on_reject)
        reject.pack(side="right")
        self._approve = ttk.Button(buttons, text="核准", command=on_approve, state="disabled")
        self._approve.pack(side="right", padx=(0, 8))
        reject.focus_set()
        self._window.after(int(APPROVE_DELAY_SECONDS * 1000), self._enable_approve)

    @staticmethod
    def _add_table(frame: ttk.Frame, pending: PendingApproval) -> None:
        holder = ttk.Frame(frame)
        holder.pack(side="top", fill="both", expand=True)
        columns = ("action", "source", "destination", "detail", "marks")
        table = ttk.Treeview(holder, columns=columns, show="headings", height=12)
        headings = ("動作", "位置", "新位置", "大小", "注意")
        widths = (90, 260, 260, 120, 90)
        for column, heading, width in zip(columns, headings, widths, strict=True):
            table.heading(column, text=heading)
            table.column(column, width=width, stretch=column in ("source", "destination"))
        for row in approval_rows(pending.plan):
            table.insert(
                "", "end", values=(row.action, row.source, row.destination, row.detail, row.marks)
            )
        scroll = ttk.Scrollbar(holder, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
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

        root.title("Touka 本機小幫手")
        root.geometry("780x620")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        self._build_status(frame)
        self._build_grants(frame)
        self._build_batches(frame)
        self._refresh_grants()
        self._refresh_batches()
        self._tick()

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
        self._settings = Settings(
            port=port, token=token, touka_folder=self._settings.touka_folder
        )
        save_settings(self._settings_file, self._settings)
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
            self._say("這個資料夾讀不到。")
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
            self._grants.set_writable(name, writable=not grant.writable)
            self._refresh_grants()

    def _remove(self) -> None:
        name = self._selected(self._grant_table)
        if name is not None:
            self._grants.remove(name)
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
        self._say(
            f"復原了 {result['restored']} 項，跳過 {result['skipped']} 項（檔案變動過）。"
            f"丟到回收桶的 {result['not_undoable']} 項請從回收桶還原。"
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

    def _tick(self) -> None:
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
        self._root.after(_REFRESH_MS, self._tick)

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

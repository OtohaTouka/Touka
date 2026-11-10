"""小幫手的固定動作（ADR-0311）。這裡沒有執行程式的能力，動作只有這幾個。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from touka_local.errors import Refused
from touka_local.fsops import (
    TEMPORARY_SUFFIX,
    durable_write_text,
    previous_path,
    read_durable_text,
    temporary_path,
)
from touka_local.paths import (
    anchor_of,
    check_segment_syntax,
    folded_key,
    is_too_long,
    join,
    same_path,
    split_request_path,
)
from touka_local.rules import (
    GIT_FOLDER,
    check_created_file_name,
    check_move_names,
    check_new_segments,
    check_segment_allowed,
    is_protected_name,
    is_secret_name,
    is_unusual_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from touka_local.fsops import Clock, EntryInfo, Fs, Trash
    from touka_local.grants import Grant, GrantBook

MAX_LIST_ENTRIES: Final = 1000
MAX_LIST_DEPTH: Final = 3
MAX_READ_BYTES: Final = 512 * 1024
MAX_CREATE_CHARS: Final = 256 * 1024
MAX_PROPOSE_ITEMS: Final = 200
MAX_REASON_CHARS: Final = 300
MAX_TREE_ENTRIES: Final = 20_000
UNUSUAL_FOLDER_ENTRIES: Final = 100
MAX_JOURNALS: Final = 50

logger = logging.getLogger(__name__)

_BATCH_ID: Final = re.compile(r"[0-9a-f]{32}")
_JOURNAL_SUFFIX: Final = ".json"
_UTF16_MARKS: Final = (b"\xff\xfe", b"\xfe\xff")
_TEXT_ENCODINGS: Final = ("utf-8-sig", "cp950")


@dataclass(frozen=True)
class _Target:
    grant: Grant
    parts: tuple[str, ...]
    text: str
    full: str


@dataclass(frozen=True)
class _Tree:
    entries: tuple[tuple[str, bool, int, int], ...]
    size: int
    contained: int
    has_executable: bool
    has_hidden: bool
    has_secret: bool


@dataclass(frozen=True)
class PlanItem:
    """清單裡的一項，內容都是小幫手自己查到的。"""

    op: str
    source_text: str
    destination_text: str | None
    source_full: str
    destination_full: str | None
    size: int
    is_folder: bool
    contained: int
    unusual: tuple[str, ...]
    mtime_ns: int
    snapshot: tuple[tuple[str, bool, int, int], ...]


@dataclass(frozen=True)
class Plan:
    """給人核准的清單。``digest`` 把內容和當下的快照綁在一起。"""

    items: tuple[PlanItem, ...]
    reason: str
    digest: str


@dataclass(frozen=True)
class ApplyLimits:
    """執行時可以提早停下來的條件。"""

    should_stop: Callable[[], bool]
    deadline: float


@contextmanager
def _about(path: object) -> Iterator[None]:
    """拒絕時帶上請求裡的路徑，模型才知道是哪一項。"""
    try:
        yield
    except Refused as exc:
        if exc.detail:
            raise
        raise Refused(exc.code, path if isinstance(path, str) else "") from exc


def _digest(items: tuple[PlanItem, ...], reason: str) -> str:
    payload = [
        reason,
        [[item.op, item.source_full, item.destination_full, item.snapshot] for item in items],
    ]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def _decode(data: bytes) -> str:
    if data.startswith(_UTF16_MARKS):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise Refused("not_text") from exc
    if b"\0" in data:
        raise Refused("not_text")
    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Refused("not_text")


class Operations:
    """看、讀、新建、提出清單、照核准的清單執行、復原。"""

    def __init__(
        self,
        *,
        fs: Fs,
        grants: GrantBook,
        trash: Trash,
        clock: Clock,
        journal_dir: Path,
        wall_clock: Clock = time.time,
    ) -> None:
        self._fs = fs
        self._grants = grants
        self._trash = trash
        self._clock = clock
        self._journal_dir = journal_dir
        self._wall_clock = wall_clock
        # Windows 上別的執行緒開著檔案時刪不掉，視窗在讀紀錄時批次不能換檔
        self._journal_lock = threading.Lock()

    # --- 路徑 ---

    def _locate(self, raw: object) -> _Target:
        root, parts = split_request_path(raw)
        grant = self._grants.find(root)
        if grant is None:
            raise Refused("not_open")
        for part in parts:
            check_segment_allowed(part)
        full = join(grant.path, *parts)
        if is_too_long(full):
            raise Refused("bad_path")
        return _Target(grant, parts, "/".join((grant.name, *parts)), full)

    def _locate_writable(self, raw: object) -> _Target:
        target = self._locate(raw)
        if not target.parts:
            raise Refused("bad_target")
        if not target.grant.writable:
            raise Refused("read_only")
        return target

    def _walk(self, target: _Target, *, allow_missing: bool = False) -> EntryInfo | None:
        """逐段檢查，碰到連結或 Git 專案就拒絕。回傳最後一段的狀態。"""
        current = target.grant.path
        info = self._fs.lstat(current)
        if info is None:
            raise Refused("not_found")
        for index, part in enumerate(target.parts):
            current = join(current, part)
            info = self._fs.lstat(current)
            if info is None:
                if allow_missing and index == len(target.parts) - 1:
                    return None
                raise Refused("not_found")
            if info.is_reparse:
                raise Refused("link")
            if info.is_dir and self._has_git(current):
                raise Refused("git_project")
            if not info.is_dir and index < len(target.parts) - 1:
                raise Refused("not_found")
        return info

    def _has_git(self, folder: str) -> bool:
        return self._fs.lstat(join(folder, GIT_FOLDER)) is not None

    # --- 看與讀 ---

    def list(self, args: dict[str, Any]) -> dict[str, Any]:
        """列出開放的資料夾，或某個資料夾裡的項目。"""
        path = args.get("path")
        if path is None:
            return {
                "roots": [
                    {"name": g.name, "access": "write" if g.writable else "read"}
                    for g in self._grants.usable()
                ]
            }
        depth = args.get("depth", 1)
        if type(depth) is not int or not 1 <= depth <= MAX_LIST_DEPTH:
            raise Refused("bad_request")
        with _about(path):
            target = self._locate(path)
            info = self._walk(target)
            if info is None or not info.is_dir:
                raise Refused("not_a_folder")
            collected: list[dict[str, Any]] = []
            state = {"truncated": False, "omitted": 0}
            self._collect(target.full, target.text, depth, collected, state)
        return {
            "entries": collected,
            "truncated": state["truncated"],
            "omitted": state["omitted"],
        }

    def _collect(
        self,
        folder: str,
        text: str,
        depth: int,
        collected: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> None:
        for name in self._fs.children(folder):
            if len(collected) >= MAX_LIST_ENTRIES:
                state["truncated"] = True
                return
            entry_text = f"{text}/{name}"
            if not self._listable(name):
                state["omitted"] += 1
                continue
            full = join(folder, name)
            info = self._fs.lstat(full)
            if info is None:
                continue
            if info.is_reparse:
                collected.append({"path": entry_text, "type": "link"})
            elif not info.is_dir:
                collected.append({"path": entry_text, "type": "file", "size": info.size})
            elif self._has_git(full):
                collected.append({"path": entry_text, "type": "git_project"})
            else:
                collected.append({"path": entry_text, "type": "folder"})
                if depth > 1:
                    self._collect(full, entry_text, depth - 1, collected, state)

    @staticmethod
    def _listable(name: str) -> bool:
        try:
            split_request_path(name)
        except Refused:
            return False
        return not is_protected_name(name)

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        """讀一個文字檔。"""
        path = args.get("path")
        with _about(path):
            target = self._locate(path)
            if not target.parts:
                raise Refused("bad_target")
            if is_secret_name(target.parts[-1]):
                raise Refused("secret_name")
            info = self._walk(target)
            if info is None or info.is_dir:
                raise Refused("not_a_file")
            if info.size > MAX_READ_BYTES:
                raise Refused("too_large")
            data = self._fs.read(target.full, MAX_READ_BYTES + 1)
            if len(data) > MAX_READ_BYTES:
                raise Refused("too_large")
            return {"text": _decode(data), "size": len(data)}

    # --- 新建 ---

    def create_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        """新建資料夾，已存在就拒絕。"""
        path = args.get("path")
        with _about(path):
            target = self._locate_writable(path)
            check_new_segments(target.parts)
            if self._walk(target, allow_missing=True) is not None:
                raise Refused("exists")
            try:
                self._fs.mkdir(target.full)
            except FileExistsError as exc:
                raise Refused("exists") from exc
            return {"created": target.text}

    def create_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """新建文字檔，絕不覆蓋。"""
        path = args.get("path")
        content = args.get("content")
        with _about(path):
            if not isinstance(content, str):
                raise Refused("bad_request")
            if len(content) > MAX_CREATE_CHARS:
                raise Refused("too_large")
            target = self._locate_writable(path)
            check_created_file_name(target.parts[-1])
            check_new_segments(target.parts)
            if self._walk(target, allow_missing=True) is not None:
                raise Refused("exists")
            try:
                data = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise Refused("bad_request") from exc
            try:
                self._fs.create_new(target.full, data)
            except FileExistsError as exc:
                raise Refused("exists") from exc
            return {"created": target.text}

    # --- 清單 ---

    def plan(self, args: dict[str, Any]) -> Plan:
        """把代理人的提議變成給人看的清單。有一項不行，整份都不行。"""
        raw_items = args.get("items")
        reason = args.get("reason")
        if not isinstance(raw_items, list) or not raw_items or not isinstance(reason, str):
            raise Refused("bad_request")
        if len(raw_items) > MAX_PROPOSE_ITEMS:
            raise Refused("too_many")
        items = tuple(self._plan_item(raw) for raw in raw_items)
        self._check_conflicts(items)
        trimmed = reason[:MAX_REASON_CHARS]
        return Plan(items=items, reason=trimmed, digest=_digest(items, trimmed))

    def _plan_item(self, raw: object) -> PlanItem:
        if not isinstance(raw, dict) or raw.get("op") not in ("move", "delete"):
            raise Refused("bad_request")
        path = raw.get("path")
        with _about(path):
            source = self._locate_writable(path)
            info = self._walk(source)
            if info is None:
                raise Refused("not_found")
            tree = self._read_tree(source.full, info)
            destination = self._plan_destination(raw, source) if raw["op"] == "move" else None
        return PlanItem(
            op=raw["op"],
            source_text=source.text,
            destination_text=destination.text if destination else None,
            source_full=source.full,
            destination_full=destination.full if destination else None,
            size=tree.size,
            is_folder=info.is_dir,
            contained=tree.contained,
            unusual=self._marks(source.parts[-1], info, tree),
            mtime_ns=info.mtime_ns,
            snapshot=tree.entries,
        )

    def _plan_destination(self, raw: dict[str, Any], source: _Target) -> _Target:
        destination = self._locate_writable(raw.get("to"))
        check_new_segments(destination.parts, kept_name=source.parts[-1])
        check_move_names(source.parts[-1], destination.parts[-1])
        if self._walk(destination, allow_missing=True) is not None:
            raise Refused("exists")
        if not same_path(anchor_of(source.full), anchor_of(destination.full)):
            raise Refused("cross_drive")
        return destination

    def _read_tree(self, full: str, info: EntryInfo) -> _Tree:
        if not info.is_dir:
            entry = ("", False, info.size, info.mtime_ns)
            return _Tree((entry,), info.size, 0, False, False, False)
        entries = [("", True, 0, info.mtime_ns)]
        size = 0
        has_executable = False
        has_hidden = False
        has_secret = False
        stack = [(full, "")]
        while stack:
            folder, prefix = stack.pop()
            for name in self._fs.children(folder):
                self._check_tree_name(name)
                path = join(folder, name)
                child = self._fs.lstat(path)
                if child is None:
                    raise Refused("changed")
                if child.is_reparse:
                    raise Refused("link")
                relative = join(prefix, name) if prefix else name
                entries.append((relative, child.is_dir, child.size, child.mtime_ns))
                if len(entries) - 1 > MAX_TREE_ENTRIES:
                    raise Refused("too_many")
                has_executable = has_executable or is_unusual_file(name)
                has_hidden = has_hidden or child.is_hidden
                has_secret = has_secret or is_secret_name(name)
                if child.is_dir:
                    stack.append((path, relative))
                else:
                    size += child.size
        return _Tree(
            tuple(sorted(entries)), size, len(entries) - 1, has_executable, has_hidden, has_secret
        )

    @staticmethod
    def _check_tree_name(name: str) -> None:
        if name.casefold() == GIT_FOLDER:
            raise Refused("git_project")
        if is_protected_name(name):
            raise Refused("protected")
        try:
            check_segment_syntax(name)
        except Refused as exc:
            raise Refused("odd_name_inside") from exc

    @staticmethod
    def _marks(name: str, info: EntryInfo, tree: _Tree) -> tuple[str, ...]:
        marks: list[str] = []
        if is_unusual_file(name) or tree.has_executable:
            marks.append("executable")
        if info.is_hidden or tree.has_hidden:
            marks.append("hidden")
        if is_secret_name(name) or tree.has_secret:
            marks.append("secret")
        if info.is_dir and tree.contained > UNUSUAL_FOLDER_ENTRIES:
            marks.append("many_items")
        return tuple(marks)

    @staticmethod
    def _check_conflicts(items: tuple[PlanItem, ...]) -> None:
        keys = sorted(
            folded_key(path)
            for item in items
            for path in (item.source_full, item.destination_full)
            if path is not None
        )
        for first, second in zip(keys, keys[1:], strict=False):
            if second[: len(first)] == first:
                raise Refused("conflict")

    # --- 執行 ---

    def apply(self, plan: Plan, limits: ApplyLimits) -> dict[str, Any]:
        """照核准的清單做，每項做之前重新確認；有變動的跳過。

        紀錄寫不進去就不再往下做：沒有紀錄的搬移沒辦法復原。
        """
        if _digest(plan.items, plan.reason) != plan.digest:
            raise Refused("changed")
        journal = self._new_journal(plan)
        self._write_journal(journal)
        try:
            self._prune_journals()
        except OSError:
            logger.exception("old batch records could not be removed")
        results: list[dict[str, str]] = []
        stopped = False
        recorded = True
        for item, entry in zip(plan.items, journal["entries"], strict=True):
            stopped = (
                stopped or not recorded or limits.should_stop() or self._clock() >= limits.deadline
            )
            status = "not_done" if stopped else self._run_item(item)
            entry["status"] = status
            recorded = self._record(journal)
            results.append({"path": item.source_text, "status": status})
        report: dict[str, Any] = {"batch": journal["batch"], "items": results}
        if not recorded:
            report["journal_incomplete"] = True
        return report

    def _record(self, journal: dict[str, Any]) -> bool:
        try:
            self._write_journal(journal)
        except OSError:
            logger.exception("the batch record could not be saved")
            return False
        return True

    def _run_item(self, item: PlanItem) -> str:
        try:
            if not self._still_valid(item):
                return "changed"
            if item.op == "move" and item.destination_full is not None:
                self._fs.rename(item.source_full, item.destination_full)
                return "done"
            if item.op == "delete":
                return {"ok": "done", "aborted": "aborted"}.get(
                    self._trash.send(item.source_full), "failed"
                )
        except FileExistsError:
            return "changed"
        except OSError:
            return "failed"
        return "failed"

    def _still_valid(self, item: PlanItem) -> bool:
        try:
            source = self._locate_writable(item.source_text)
            if not same_path(source.full, item.source_full):
                return False
            info = self._walk(source)
            if info is None or self._read_tree(source.full, info).entries != item.snapshot:
                return False
            if item.destination_text is None or item.destination_full is None:
                return True
            destination = self._locate_writable(item.destination_text)
            return (
                same_path(destination.full, item.destination_full)
                and self._walk(destination, allow_missing=True) is None
            )
        except Refused:
            return False

    # --- 紀錄與復原 ---

    def _new_journal(self, plan: Plan) -> dict[str, Any]:
        return {
            "batch": secrets.token_hex(16),
            "sequence": self._next_sequence(),
            "created_at": self._wall_clock(),
            "reason": plan.reason,
            "entries": [
                {
                    "op": item.op,
                    "path": item.source_text,
                    "to": item.destination_text,
                    "source": item.source_full,
                    "destination": item.destination_full,
                    "is_dir": item.is_folder,
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                    "status": "pending",
                }
                for item in plan.items
            ],
        }

    def _journal_path(self, batch: str) -> Path:
        return self._journal_dir / f"{batch}{_JOURNAL_SUFFIX}"

    def _write_journal(self, journal: dict[str, Any]) -> None:
        # 檔名可能有 UTF-8 寫不出來的字元，用跳脫的寫法才不會整批紀錄寫失敗
        text = json.dumps(journal, ensure_ascii=True)
        with self._journal_lock:
            durable_write_text(self._journal_path(journal["batch"]), text)

    def _all_journals(self) -> list[dict[str, Any]]:
        if not self._journal_dir.is_dir():
            return []
        # 當機在刪舊檔和改名之間時，只剩暫存檔
        batches = {
            path.name.split(".", 1)[0]
            for path in self._journal_dir.iterdir()
            if path.name.endswith((_JOURNAL_SUFFIX, f"{_JOURNAL_SUFFIX}{TEMPORARY_SUFFIX}"))
        }
        journals: list[dict[str, Any]] = []
        for batch in batches:
            journal = self._read_journal(self._journal_path(batch))
            if journal is not None:
                journals.append(journal)
        return sorted(journals, key=lambda journal: journal["sequence"], reverse=True)

    def _read_journal(self, path: Path) -> dict[str, Any] | None:
        try:
            with self._journal_lock:
                text = read_durable_text(path)
            journal = json.loads(text)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(journal, dict)
            or journal.get("batch") != path.name.removesuffix(_JOURNAL_SUFFIX)
            or _BATCH_ID.fullmatch(journal["batch"]) is None
            or type(journal.get("sequence")) is not int
            or not isinstance(journal.get("entries"), list)
            or not all(isinstance(entry, dict) for entry in journal["entries"])
        ):
            return None
        return journal

    def _next_sequence(self) -> int:
        journals = self._all_journals()
        return journals[0]["sequence"] + 1 if journals else 1

    def _prune_journals(self) -> None:
        for journal in self._all_journals()[MAX_JOURNALS:]:
            path = self._journal_path(journal["batch"])
            with self._journal_lock:
                for file in (path, temporary_path(path), previous_path(path)):
                    file.unlink(missing_ok=True)
        for leftover in self._journal_dir.glob(f"*{_JOURNAL_SUFFIX}{TEMPORARY_SUFFIX}"):
            real = leftover.with_name(leftover.name.removesuffix(TEMPORARY_SUFFIX))
            if real.exists() or self._read_journal(real) is None:
                with self._journal_lock:
                    leftover.unlink(missing_ok=True)

    def recent_batches(self) -> list[dict[str, Any]]:
        """最近的批次，新的在前。"""
        return [
            {
                "batch": journal["batch"],
                "reason": journal.get("reason", ""),
                "created_at": journal.get("created_at"),
                "total": len(journal["entries"]),
                "done": sum(1 for e in journal["entries"] if e.get("status") == "done"),
                "undoable": sum(
                    1
                    for e in journal["entries"]
                    if e.get("op") == "move" and e.get("status") == "done"
                ),
            }
            for journal in self._all_journals()
        ]

    def undo(self, batch: str) -> dict[str, int]:
        """把一批的搬移反向做回去。丟到回收桶的要從回收桶還原。"""
        if _BATCH_ID.fullmatch(batch) is None:
            raise Refused("not_found")
        journal = self._read_journal(self._journal_path(batch))
        if journal is None:
            raise Refused("not_found")
        counts = {"restored": 0, "skipped": 0, "not_undoable": 0}
        for entry in reversed(journal["entries"]):
            if entry.get("op") == "delete":
                if entry.get("status") == "done":
                    counts["not_undoable"] += 1
            elif entry.get("status") in ("done", "pending"):
                if self._undo_move(entry):
                    entry["status"] = "undone"
                    counts["restored"] += 1
                else:
                    counts["skipped"] += 1
        if not self._record(journal):
            counts["journal_incomplete"] = 1
        return counts

    def _undo_move(self, entry: dict[str, Any]) -> bool:
        try:
            moved = self._locate_writable(entry["to"])
            original = self._locate_writable(entry["path"])
            if not same_path(moved.full, entry["destination"]):
                return False
            if not same_path(original.full, entry["source"]):
                return False
            info = self._walk(moved)
            if info is None or self._walk(original, allow_missing=True) is not None:
                return False
            if info.is_dir != entry["is_dir"] or info.mtime_ns != entry["mtime_ns"]:
                return False
            if not info.is_dir and info.size != entry["size"]:
                return False
            self._fs.rename(moved.full, original.full)
        except (Refused, OSError, KeyError, TypeError):
            return False
        return True

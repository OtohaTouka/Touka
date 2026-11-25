"""下載專用空間的專案（ADR-0332 PR B）：只放 git 名單上的檔案，打成 zip.

放哪些檔案只看 ``.git/index``（``utils.git_index`` 純 Python 讀，不跑 git）；檔案經 ResourceManager
從專案根不跟隨連結地走訪、開啟（名單以外的資料夾不進去、名單以外的檔案不開）。名稱或磁碟上的
狀態不適合放進 zip 的略過，寫進 zip 根的 ``TOUKA_DOWNLOAD_REPORT.txt``：

- 名稱（``utils.archive_names``）：``.git``（含 Windows、macOS 會當成它的寫法）、解壓後會讓資料夾
  變成 repository 的（名為 HEAD 的檔案、其他版本控制工具的資料夾）、Windows 不能用的名稱、雙向
  控制字元、檔案總管會自動處理的捷徑類檔案、``.env``、不是 UTF-8、路徑太長或太深、只差大小寫的重複；
- 名單：連結、子模組；
- 磁碟：連結、硬連結（可能是別處的檔案）、不是一般檔案、不見了、讀不到。

上限是模組常數，計時從讀名單開始；名單上每個名稱的檢查與比對只和它的長度成正比（路徑最長
``MAX_ARCHIVE_PATH_CHARS`` 字、最深 ``MAX_PATH_DEPTH`` 層），報告每個原因只列前
``MAX_REPORT_LINES`` 個。被拒時丟 ``ToukaException``（422 名單或形狀的問題、413 太大、429 同時
只做一個）。zip 做在系統暫存資料夾，被拒或請求取消時刪掉；送出後由回應刪掉（``TempFileResponse``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.utils.no_follow import LinkRefusedError, WalkReport, zip_open_file
from memory_server.utils.zone_paths import ZonePathRequiredError

from src.core.exceptions import ErrorCode, ToukaException
from src.services.isolation.project_space import PROJECT_LINKED
from src.services.resource_manager import resource_manager
from src.utils import archive_names, git_index
from src.utils.time_utils import get_monotonic_ms
from src.utils.touka_dirs import PROTECTED_LOCATION

if TYPE_CHECKING:
    from memory_server.utils.no_follow import WalkEntry

logger = logging.getLogger(__name__)

# 上限
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_INDEX_ENTRIES = 200_000
MAX_DOWNLOAD_FILES = 50_000
MAX_DOWNLOAD_DIRS = 50_000  # 要放的檔案所在的資料夾（不同的）
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 依開檔時 fstat 的大小累計
MAX_WALK_ENTRIES = 1_000_000  # 走訪到的項目（含名單以外、在名單資料夾裡的）
MAX_WALK_SECONDS = 300  # 檢查名單、走訪加打包
MAX_ARCHIVE_PATH_CHARS = 200  # zip 裡的路徑（含專案資料夾那一段）
MAX_PATH_DEPTH = 32  # 路徑最多幾段
MAX_FOLDER_CHARS = 64
MAX_REPORT_LINES = 1_000  # 報告裡每個原因最多列幾條
REPORT_NAME = "TOUKA_DOWNLOAD_REPORT.txt"
_FALLBACK_FOLDER = "project"
_MAX_UTF8_BYTES = 4  # 一個字最多幾個位元組：名稱超過上限的 4 倍位元組，不用解碼就知道太長

# 被拒的原因（422／413；index 的原因在 git_index）
NOT_DEDICATED_SPACE = "not_dedicated_space"
TOO_MANY_FILES = "too_many_files"
TOO_LARGE = "too_large"
WALK_TOO_LARGE = "walk_too_large"

# 略過的原因（報告；名稱的原因在 archive_names）
SKIP_NOT_UTF8 = "not_utf8"
SKIP_TOO_LONG = "too_long"
SKIP_CASE_DUPLICATE = "case_duplicate"
SKIP_LINK = "link"
SKIP_SUBMODULE = "submodule"
SKIP_HARDLINK = "hardlink"
SKIP_NOT_A_FILE = "not_a_file"
SKIP_MISSING = "missing"
SKIP_UNREADABLE = "unreadable"

_REASON_TEXT = {
    archive_names.GIT_FOLDER: "Git's own folder, or a name Windows or macOS treats as it. / "
    "git 自己的資料夾，或 Windows、macOS 會當成它的名稱。",
    archive_names.VCS_METADATA: "Could make its folder act as a version-control repository after "
    "unzipping (a file named HEAD, or a Mercurial, Subversion, Bazaar, Darcs or Jujutsu folder). / "
    "解壓後可能讓資料夾變成版本控制的 repository（名為 HEAD 的檔案，或 Mercurial、Subversion、"
    "Bazaar、Darcs、Jujutsu 的資料夾）。",
    archive_names.BAD_NAME: "A name Windows cannot use (reserved name, : * ? etc., control "
    "characters, or a trailing dot or space). / Windows 不能用的名稱（保留名稱、: * ? 等字元、"
    "控制字元、結尾是點或空白）。",
    archive_names.BIDI: "The name has bidirectional control characters that can disguise it. / "
    "名稱裡有可以偽裝檔名的雙向控制字元。",
    archive_names.SHORTCUT: "A shortcut-like file a file manager acts on when it opens the "
    "folder. / 檔案總管打開資料夾時會自動處理的捷徑類檔案。",
    archive_names.ENV_FILE: "An environment file (.env) that may hold secrets. / "
    "環境變數檔（.env），可能有密碼或金鑰。",
    SKIP_NOT_UTF8: "The name is not UTF-8. / 名稱不是 UTF-8。",
    SKIP_TOO_LONG: f"The path is longer than {MAX_ARCHIVE_PATH_CHARS} characters or more than "
    f"{MAX_PATH_DEPTH} levels deep. / 路徑超過 {MAX_ARCHIVE_PATH_CHARS} 個字或超過 "
    f"{MAX_PATH_DEPTH} 層。",
    SKIP_CASE_DUPLICATE: "Same name as an earlier file except for letter case. / "
    "和前面的檔案只差大小寫。",
    SKIP_LINK: "A symbolic link, or under one; links are not followed. / "
    "連結或在連結底下；不跟隨連結。",
    SKIP_SUBMODULE: "A git submodule. / git 子模組。",
    SKIP_HARDLINK: "A hard link (it may be a file from somewhere else). / "
    "硬連結（可能是別處的檔案）。",
    SKIP_NOT_A_FILE: "Not a regular file on disk. / 磁碟上不是一般檔案。",
    SKIP_MISSING: "Listed by git but not on disk. / git 有列但磁碟上沒有。",
    SKIP_UNREADABLE: "Could not be read. / 讀不到。",
}
_REPORT_HEAD = (
    "Touka left these files tracked by git out of this download.",
    "Touka 沒有把下面這些 git 追蹤的檔案放進這次下載。",
)

# 同時只做一個下載
_DOWNLOAD_SLOT = asyncio.Lock()


@dataclass(frozen=True)
class ProjectArchive:
    """做好的 zip：暫存檔與統計（稽核紀錄用）."""

    path: Path
    files: int
    skipped: int
    size: int


@dataclass
class _Plan:
    folder: str
    started: int  # 開始的時間（毫秒）
    wanted: dict[tuple[str, ...], str] = field(default_factory=dict)  # 段落 → 相對路徑
    dirs: set[tuple[str, ...]] = field(default_factory=set)
    skipped: dict[str, list[str]] = field(default_factory=dict)  # 原因 → 前幾個的顯示用名稱
    counts: Counter[str] = field(default_factory=Counter)  # 原因 → 略過幾個

    def skip(self, reason: str, name: str | bytes) -> None:
        """記一個略過的檔案；報告每個原因只列前 ``MAX_REPORT_LINES`` 個，其餘只計數."""
        self.counts[reason] += 1
        if self.counts[reason] <= MAX_REPORT_LINES:
            shown = (
                archive_names.shown_raw(name)
                if isinstance(name, bytes)
                else archive_names.shown(name)
            )
            self.skipped.setdefault(reason, []).append(shown)

    def check_time(self) -> None:
        if get_monotonic_ms() - self.started > MAX_WALK_SECONDS * 1000:
            raise _too_large(WALK_TOO_LARGE)


def _refused(reason: str) -> ToukaException:
    return ToukaException(ErrorCode.VALIDATION_ERROR, reason=reason)


def _too_large(reason: str) -> ToukaException:
    return ToukaException(ErrorCode.REQUEST_TOO_LARGE, reason=reason)


async def download_project_zip(folder: str, root: Path) -> ProjectArchive:
    """做 zip（執行緒裡跑）；同時只做一個，請求取消時讓打包做完再刪掉 zip."""
    if _DOWNLOAD_SLOT.locked():
        raise ToukaException(ErrorCode.PROJECT_DOWNLOAD_BUSY)
    await _DOWNLOAD_SLOT.acquire()
    job = asyncio.ensure_future(asyncio.to_thread(build_project_zip, folder, root))
    job.add_done_callback(_release_slot)
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        job.add_done_callback(_discard)
        raise


def _release_slot(_job: asyncio.Future[ProjectArchive]) -> None:
    _DOWNLOAD_SLOT.release()


def _discard(job: asyncio.Future[ProjectArchive]) -> None:
    """請求已取消：做好的 zip 沒人拿，刪掉（被拒的在打包裡就刪了）."""
    if not (job.cancelled() or job.exception() is not None):
        _remove_quietly(job.result().path)


def _remove_quietly(path: Path) -> None:
    """刪暫存的 zip；刪不掉只記 log（不蓋掉原本的錯誤）."""
    try:
        resource_manager.remove_file(path)
    except OSError:
        logger.warning("Project download left a temporary zip behind", exc_info=True)


def build_project_zip(folder: str, root: Path) -> ProjectArchive:
    """在系統暫存資料夾做 zip；被拒時丟 ``ToukaException``，做一半的 zip 刪掉."""
    started = get_monotonic_ms()
    plan = _plan(_Plan(_safe_folder(folder), started), _index_entries(root))
    fd, name = tempfile.mkstemp(prefix="touka_project_", suffix=".zip")
    os.close(fd)  # ZipFile 以路徑自己開；失敗時才刪得掉（Windows）
    path = Path(name)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            packed, size = _pack(zf, plan, root)
            if plan.counts:
                zf.writestr(REPORT_NAME, _report_text(plan))
    except BaseException:
        _remove_quietly(path)
        raise
    return ProjectArchive(path, files=packed, skipped=plan.counts.total(), size=size)


def _index_entries(root: Path) -> git_index.IndexEntries:
    try:
        status, data = resource_manager.read_git_index(root, MAX_INDEX_BYTES)
    except LinkRefusedError as exc:
        raise _refused(PROJECT_LINKED) from exc
    except ZonePathRequiredError as exc:
        raise _refused(PROTECTED_LOCATION) from exc
    except OSError as exc:
        raise _refused(git_index.INDEX_INVALID) from exc
    if status == git_index.INDEX_TOO_LARGE:
        raise _too_large(status)
    if status != git_index.INDEX_OK:
        raise _refused(status)
    try:
        entries = git_index.read_index(data, max_entries=MAX_INDEX_ENTRIES)
    except git_index.GitIndexError as exc:
        if exc.reason == git_index.INDEX_TOO_LARGE:
            raise _too_large(exc.reason) from exc
        raise _refused(exc.reason) from exc
    if not (entries.files or entries.links or entries.submodules):
        raise _refused(git_index.NO_INDEX)  # git 還沒追蹤任何檔案：不給空的 zip
    return entries


# --- 名稱 ---------------------------------------------------------------------------------


def _safe_folder(folder: str) -> str:
    """專案資料夾在 zip 裡的名稱；不適合時用 ``project``."""
    if not folder or len(folder) > MAX_FOLDER_CHARS or archive_names.name_problem([folder]):
        return _FALLBACK_FOLDER
    return unicodedata.normalize("NFC", folder)


def _collides(parts: list[str], file_keys: set[str], dir_keys: set[str]) -> bool:
    """解壓到不分大小寫的磁碟時會撞到前面的檔案（同名檔案、或檔案和資料夾同名）."""
    keys = [archive_names.case_key("/".join(parts[:i])) for i in range(1, len(parts) + 1)]
    full, prefixes = keys[-1], keys[:-1]
    if full in file_keys or full in dir_keys or any(key in file_keys for key in prefixes):
        return True
    file_keys.add(full)
    dir_keys.update(prefixes)
    return False


def _name_problem(raw: bytes, folder: str) -> tuple[str | None, str, list[str]]:
    """（略過的原因, 名稱, 段落）；名稱太長的不解碼、不拆段."""
    if len(raw) > MAX_ARCHIVE_PATH_CHARS * _MAX_UTF8_BYTES:
        return SKIP_TOO_LONG, "", []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return SKIP_NOT_UTF8, "", []
    parts = text.split("/")
    if len(folder) + 1 + len(text) > MAX_ARCHIVE_PATH_CHARS or len(parts) > MAX_PATH_DEPTH:
        return SKIP_TOO_LONG, text, parts
    return archive_names.name_problem(parts), text, parts


def _plan(plan: _Plan, entries: git_index.IndexEntries) -> _Plan:
    """名單上的哪些檔案要放：名稱的檢查都在走訪之前."""
    file_keys: set[str] = set()
    dir_keys: set[str] = set()
    for raw in entries.files:
        plan.check_time()
        problem, text, parts = _name_problem(raw, plan.folder)
        if problem is None and _collides(parts, file_keys, dir_keys):
            problem = SKIP_CASE_DUPLICATE
        if problem is not None:
            plan.skip(problem, text or raw)
            continue
        plan.wanted[tuple(parts)] = text
        plan.dirs.update(tuple(parts[:i]) for i in range(1, len(parts)))
        if len(plan.wanted) > MAX_DOWNLOAD_FILES or len(plan.dirs) > MAX_DOWNLOAD_DIRS:
            raise _too_large(TOO_MANY_FILES)
    for raw in entries.links:
        plan.skip(SKIP_LINK, raw)
    for raw in entries.submodules:
        plan.skip(SKIP_SUBMODULE, raw)
    return plan


# --- 走訪與打包 ---------------------------------------------------------------------------


@dataclass
class _Seen:
    files: set[tuple[str, ...]] = field(default_factory=set)
    links: set[tuple[str, ...]] = field(default_factory=set)
    not_files: set[tuple[str, ...]] = field(default_factory=set)


def _pack(zf: zipfile.ZipFile, plan: _Plan, root: Path) -> tuple[int, int]:
    """走訪專案、把名單上的檔案寫進 zip；回（放了幾個, 累計位元組）."""
    report, seen = WalkReport(), _Seen()

    def skip_dir(parts: tuple[str, ...]) -> bool:
        if parts in plan.dirs:
            return False
        if parts in plan.wanted:
            seen.not_files.add(parts)  # 名單說是檔案，磁碟上是資料夾
        return True

    def want(entry: WalkEntry) -> bool:
        return entry.parts in plan.wanted

    walked, packed, size = 0, 0, 0
    try:
        walk = resource_manager.project_walk(root, report, want=want, skip_dir=skip_dir)
        with contextlib.closing(walk):
            for entry, handle, info in walk:
                walked += 1
                if walked > MAX_WALK_ENTRIES:
                    raise _too_large(WALK_TOO_LARGE)
                plan.check_time()
                if entry.kind == "link":
                    seen.links.add(entry.parts)
                if entry.parts not in plan.wanted:
                    continue
                seen.files.add(entry.parts)
                text = plan.wanted[entry.parts]
                if handle is None or info is None:
                    plan.skip(_on_disk_reason(entry.kind), text)
                    continue
                if info.st_nlink > 1:
                    plan.skip(SKIP_HARDLINK, text)
                    continue
                size += info.st_size
                if size > MAX_DOWNLOAD_BYTES:
                    raise _too_large(TOO_LARGE)
                try:
                    zip_open_file(zf, f"{plan.folder}/{text}", handle, info)
                except OSError:
                    logger.warning("Project download failed while packing %r", text, exc_info=True)
                    raise
                packed += 1
    except LinkRefusedError as exc:  # 根或上層變成連結
        raise _refused(PROJECT_LINKED) from exc
    except ZonePathRequiredError as exc:
        raise _refused(PROTECTED_LOCATION) from exc
    _unseen(plan, seen, report)
    return packed, size


def _on_disk_reason(kind: str) -> str:
    return {"link": SKIP_LINK, "unreadable": SKIP_UNREADABLE}.get(kind, SKIP_NOT_A_FILE)


def _unseen(plan: _Plan, seen: _Seen, report: WalkReport) -> None:
    """名單上但走訪沒遇到的：在連結底下、讀不到（本身或上層）、磁碟上是資料夾，或不見了."""
    errors = {tuple(rel.split("/")) if rel else () for rel, _code in report.errors}
    for parts, text in plan.wanted.items():
        if parts in seen.files:
            continue
        path_and_above = [parts[:i] for i in range(len(parts) + 1)]
        if parts in seen.not_files:
            reason = SKIP_NOT_A_FILE
        elif any(prefix in seen.links for prefix in path_and_above):
            reason = SKIP_LINK
        elif any(prefix in errors for prefix in path_and_above):
            reason = SKIP_UNREADABLE
        else:
            reason = SKIP_MISSING
        plan.skip(reason, text)


def _report_text(plan: _Plan) -> str:
    lines = [*_REPORT_HEAD, ""]
    for reason, text in _REASON_TEXT.items():
        names = plan.skipped.get(reason)
        if not names:
            continue
        lines.append(f"[{reason}] {text}")
        lines.extend(f"  {name}" for name in names)
        more = plan.counts[reason] - len(names)
        if more:
            lines.append(f"... and {more} more / 另外還有 {more} 個")
        lines.append("")
    return "\n".join(lines)

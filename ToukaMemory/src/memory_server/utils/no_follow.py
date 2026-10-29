"""不跟隨連結的檔案操作：代理人的檔案動詞用（ADR-0292 決定 12）。

代理人的檔案動詞先把路徑解析、確認在根底下，才開檔。這中間代理人可以把路上某一段換成連結
（symlink 或 junction），開檔就會跟著連結寫到根外面；走訪時也會跟著代理人自己建的連結讀到根
外面（例如設定資料夾裡的 ``hooks.json``）。這裡的函式拿**已解析的絕對路徑**，路上任何一段
（含最後一段）是連結就丟 ``LinkRefusedError``；走訪時不進入、也不列出連結。

- POSIX：從檔案系統的根起逐段以 ``O_NOFOLLOW`` 開啟（``dir_fd``；Linux 用 ``O_PATH``，只能
  通過、不能列的資料夾也開得了），開檔時換成連結的那一段打不開；寫入、改名與刪除都在開好的
  上一層資料夾裡做。開檔帶 ``O_NONBLOCK``，換成 FIFO 時不會卡住，之後也只認一般檔案。
  失敗時例外的 ``filename`` 換成那一段的絕對路徑（``dir_fd`` 呼叫原本只帶相對名稱），
  ResourceManager 的權限自癒才認得要修哪一條路（ADR-0272 U2n）。
- Windows 沒有 ``dir_fd``：開檔後檢查路上沒有 symlink 或 junction（OneDrive 之類的雲端
  檔案不算），而且開到的就是路徑現在指的那個檔案（比對檔案識別碼：開檔那一刻是連結、檢查前
  又換回來時擋下）；會建立或寫入檔案時開檔前也先檢查。檢查和開檔不是同一個動作，仍有很短的
  時間差（見 ADR-0292「已知的界線」）。Windows 本機的代理人終端不經隔離、每條指令都要人核准。

只用標準函式庫；ToukaProxy 的 ResourceManager 也用這裡的函式。

代理人資料夾（區，``zone_paths``）另有走訪與刪樹（ADR-0293）：都收已解析的絕對路徑，從檔案系統
的根起不跟隨；走訪不進入、不列出連結，刪樹遇到連結只刪連結本身。同一套演算法在 POSIX 上以開好的
資料夾 fd 做（``_FdTree``），在 Windows 上以路徑做（``_PathTree``，檢查與動作之間有時間差）。
"""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, NamedTuple, Protocol, override

from memory_server.utils.zone_paths import (
    ENFORCED_ANCHORS,
    ZoneEvent,
    ZoneMarkers,
    ZoneSegmentError,
    current_roots,
    is_zone_segment,
    report,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from memory_server.utils.zone_paths import Anchor, MarkerState, ZoneRoots

_POSIX_DIR_FD = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | _DIRECTORY | _NOFOLLOW | _CLOEXEC
# 要列出內容的資料夾不能用 O_PATH 開
_LIST_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC | _BINARY
# 開到連結時各系統回的錯誤：Linux 的 ELOOP（加 O_DIRECTORY 時是 ENOTDIR）、FreeBSD 的 EMLINK
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})
# junction 的 reparse tag：只有 Windows 有（其他系統的 stat 沒有這個常數）
_JUNCTION_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
_WINDOWS_DIRECTORY_ATTR = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_WINDOWS_READONLY_ATTR = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)

# 走訪最深幾層（超過記成錯誤，不進入）
MAX_ZONE_WALK_DEPTH = 256
# 刪樹時同時開著的資料夾上限：更深的資料夾先搬到樹根底下再刪，所以不會因為太深而失敗
MAX_REMOVE_OPEN_DIRS = 64
# 同一項在刪除途中一直被換掉、或刪完又長出新東西時，最多重試幾次
MAX_REMOVE_SWAP_RETRIES = 8
# 走訪錯誤清單裡「太深」與「走訪途中被換掉」的代碼
WALK_TOO_DEEP = errno.ENAMETOOLONG
WALK_CHANGED = errno.ESTALE


# 回給代理人的說明（不含絕對路徑）
LINK_REFUSED_MESSAGE = "the path goes through a symbolic link or junction; links are not followed."
LINK_NOT_FOLLOWED = "(link, not followed)"
# 刪樹重試到上限時的固定原因
TREE_KEPT_CHANGING_MESSAGE = "the folder kept changing while it was being deleted"


class LinkRefusedError(OSError):
    """路上有 symlink 或 junction：不跟隨，拒絕這次操作。"""


class ZoneLinkRefusedError(LinkRefusedError):
    """代理人資料夾（區）裡路上或最後一段有連結（ADR-0293 R12）。

    訊息固定；``filename`` 是 ``ZonePath.display()``，絕對路徑放在 ``zone_abs``（只有權限自癒
    讀，Z8 檢查）。建立時通知 ``zone_paths`` 的觀察者，所以被產品程式接住也看得到。
    """

    def __init__(self, display: str | None, absolute: str | None = None) -> None:
        """``display`` 是區內路徑的顯示形式，``absolute`` 是那一段的絕對路徑。"""
        super().__init__(errno.ELOOP, LINK_REFUSED_MESSAGE, display)
        self.zone_abs = absolute
        report(ZoneEvent(type(self), display))


@contextlib.contextmanager
def zone_errors(display: str) -> Iterator[None]:
    """區內動作的例外改成只帶顯示路徑（R12）.

    連結換成 ``ZoneLinkRefusedError``；其他帶路徑的 ``OSError`` 換成同型別、同 errno 的新例外，
    ``filename`` 是 ``display``、原本的絕對路徑放在 ``zone_abs``（只有權限自癒讀）。
    """
    try:
        yield
    except ZoneLinkRefusedError:
        raise
    except LinkRefusedError as exc:
        raise ZoneLinkRefusedError(display, exc.filename) from None
    except OSError as exc:
        if hasattr(exc, "zone_abs") or (exc.filename is None and exc.filename2 is None):
            raise
        # 另建新例外：原本的 filename2 清不掉（設成 None 訊息會多出「-> None」）
        shown = type(exc)(exc.errno, exc.strerror, display)
        shown.zone_abs = exc.filename
        raise shown from None


def _lstat(path: Path) -> os.stat_result | None:
    """不跟隨的 stat；不存在回 None（讀不到狀態照原樣丟，``filename`` 是絕對路徑）。"""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_link_stat(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return _JUNCTION_TAG is not None and getattr(info, "st_reparse_tag", 0) == _JUNCTION_TAG


def is_link(path: Path) -> bool:
    """這一段本身是 symlink 或 junction（不跟隨；不存在時回 False）。"""
    info = _lstat(path)
    return info is not None and _is_link_stat(info)


def _refused(path: Path) -> LinkRefusedError:
    return LinkRefusedError(errno.ELOOP, "Path goes through a symbolic link or junction", str(path))


def _walk_no_links(p: Path) -> os.stat_result | None:
    """從根往下逐段 lstat，是連結就拒絕；回最後一段的狀態（有一段不存在時回 None、不再往下）。"""
    current = Path(p.anchor)
    info = None
    for name in p.parts[1:]:
        current = current / name
        info = _lstat(current)
        if info is None:
            return None
        if _is_link_stat(info):
            raise _refused(current)
    return info


def ensure_no_links(path: Path | str) -> None:
    """從根往下，現存的每一段都不能是 symlink 或 junction.

    走訪前用；Windows 開檔、改名、刪除前與開檔失敗時也用。
    每段一次 lstat；遇到不存在的那一段就停（更下層也不存在）。Windows 開檔後由
    ``_check_opened`` 再查一次並比對檔案識別碼。
    """
    _walk_no_links(Path(path))


def _check_opened(fd: int, path: Path) -> None:
    """Windows 開檔後：路上沒有連結，而且 ``fd`` 就是路徑現在指的那個檔案（呼叫端負責關閉）。

    開檔那一刻路上是連結、檢查前又換回來時，路上看不到連結，``fd`` 卻開在連結另一端：
    比對檔案識別碼擋下。
    """
    info = _walk_no_links(path)
    if info is None or not os.path.samestat(os.fstat(fd), info):
        raise _refused(path)


def _with_paths(exc: OSError, path: Path, path2: Path | None = None) -> OSError:
    """POSIX 的 ``dir_fd`` 呼叫失敗時例外只帶相對名稱：換成絕對路徑（權限自癒靠它）。"""
    exc.filename = str(path)
    exc.filename2 = None if path2 is None else str(path2)
    return exc


def _link_error(exc: OSError, name: str, parent: int, shown: Path) -> OSError:
    """POSIX 開不起來時：那一段是連結就換成 ``LinkRefusedError``，其餘改帶絕對路徑照原樣丟。"""
    if exc.errno in _LINK_ERRNOS:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                return _refused(shown)
    return _with_paths(exc, shown)


def _open_parent(path: Path, *, make_parents: bool) -> int:
    """POSIX：從檔案系統的根逐段開到 ``path`` 的上一層，回它的 fd（呼叫端負責關閉）。"""
    fd = os.open(path.anchor, _DIR_FLAGS & ~_NOFOLLOW)
    shown = Path(path.anchor)
    try:
        for name in path.parts[1:-1]:
            shown = shown / name
            try:
                child = _open_dir(name, fd, make_parents=make_parents)
            except OSError as exc:
                raise _link_error(exc, name, fd, shown) from None
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dir(name: str, parent: int, *, make_parents: bool) -> int:
    """POSIX：在 ``parent`` 裡不跟隨地開一層資料夾；不存在且 ``make_parents`` 時先建。"""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not make_parents:
            raise
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o777, dir_fd=parent)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def open_no_follow(
    path: Path | str, flags: int, mode: int = 0o666, *, make_parents: bool = False
) -> int:
    """開啟已解析的絕對路徑，回 fd；路上任何一段（含最後一段）是連結就拒絕。

    ``flags`` 不要帶 ``O_TRUNC``：Windows 要等開檔後的檢查通過才能截斷
    （見 ``write_text_no_follow``）。
    """
    p = Path(path)
    if _POSIX_DIR_FD:
        parent = _open_parent(p, make_parents=make_parents)
        try:
            return os.open(p.name, flags | _NOFOLLOW | _CLOEXEC, mode, dir_fd=parent)
        except OSError as exc:
            raise _link_error(exc, p.name, parent, p) from None
        finally:
            os.close(parent)
    # 只讀時開檔不改任何東西，開檔後的檢查就夠；會建立或寫入時開檔前也先擋掉現成的連結
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        ensure_no_links(p)
    if make_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
        ensure_no_links(p)
    try:
        fd = os.open(p, flags | _BINARY, mode)
    except OSError:
        # 開到指向資料夾的 junction 等也會失敗：是連結時報連結
        ensure_no_links(p)
        raise
    try:
        _check_opened(fd, p)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_regular(fd: int, path: Path | str) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise IsADirectoryError(errno.EISDIR, "Not a regular file", str(path))


def read_text_no_follow(path: Path | str, encoding: str = "utf-8", errors: str = "strict") -> str:
    """讀文字檔（換行和 ``Path.read_text`` 一樣統一）；路上有連結就拒絕。"""
    fd = open_no_follow(path, os.O_RDONLY | _NONBLOCK)
    with os.fdopen(fd, "r", encoding=encoding, errors=errors) as f:
        _require_regular(f.fileno(), path)
        return f.read()


def read_bytes_no_follow(path: Path | str, size: int = -1) -> bytes:
    """讀二進位檔（``size`` 給了就只讀開頭這麼多）；路上有連結就拒絕。"""
    fd = open_no_follow(path, os.O_RDONLY | _NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        _require_regular(f.fileno(), path)
        return f.read(size)


def open_read_no_follow(path: Path | str) -> tuple[IO[bytes], os.stat_result]:
    """開一般檔案給串流讀，回（檔案, fstat）；路上有連結就拒絕。呼叫端負責關閉。"""
    fd = open_no_follow(path, os.O_RDONLY | _NONBLOCK)
    try:
        info = os.fstat(fd)
        _require_regular(fd, path)
        if _POSIX_DIR_FD:
            os.set_blocking(fd, True)
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return handle, info


def _kind_of(info: os.stat_result) -> str:
    if _is_link_stat(info):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "file" if stat.S_ISREG(info.st_mode) else "other"


def kind_no_follow(path: Path | str) -> str:
    """最後一段是什麼：``missing``／``file``／``dir``／``link``／``other``（不跟隨）。

    上層路上有連結就拒絕；上層某一段不存在（或不是資料夾）回 ``missing``。
    """
    p = Path(path)
    if _POSIX_DIR_FD:
        try:
            parent = _open_parent(p, make_parents=False)
        except (FileNotFoundError, NotADirectoryError):
            return "missing"
        try:
            info = os.stat(p.name, dir_fd=parent, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return "missing"
        except OSError as exc:
            raise _with_paths(exc, p) from None
        finally:
            os.close(parent)
        return _kind_of(info)
    if _walk_no_links(p.parent) is None:
        return "missing"
    info = _lstat(p)
    return "missing" if info is None else _kind_of(info)


def write_bytes_no_follow(path: Path | str, data: bytes) -> None:
    """就地寫入二進位（需要時建上層資料夾）；路上有連結就拒絕，不截斷任何別的檔案。"""
    fd = open_no_follow(path, os.O_WRONLY | os.O_CREAT | _NONBLOCK, make_parents=True)
    with os.fdopen(fd, "wb") as f:
        _require_regular(f.fileno(), path)
        f.truncate(0)
        f.write(data)


def make_dirs_no_follow(path: Path | str) -> None:
    """建資料夾（含上層；已存在沒關係）；路上任何一段（含最後一段）是連結就拒絕。"""
    p = Path(path)
    if _POSIX_DIR_FD:
        parent = _open_parent(p, make_parents=True)
        try:
            os.close(_open_dir(p.name, parent, make_parents=True))
        except OSError as exc:
            raise _link_error(exc, p.name, parent, p) from None
        finally:
            os.close(parent)
        return
    ensure_no_links(p)
    p.mkdir(parents=True, exist_ok=True)
    ensure_no_links(p)


def rename_entry_no_follow(parent: Path | str, old: str, new: str) -> None:
    """在同一個資料夾裡把 ``old`` 改名成 ``new``（只改名、不複製；``old`` 是連結就拒絕）。

    ``new`` 已存在時丟 ``FileExistsError``；只差大小寫而且是同一個檔案（大小寫不分的檔案系統）
    時照常改名。兩個名稱都要是單段。
    """
    if not (is_zone_segment(old) and is_zone_segment(new)):
        raise ZoneSegmentError
    base = Path(parent)
    if _POSIX_DIR_FD:
        _rename_entry(_FdTree(), base, old, new)
    else:
        _rename_entry(_PathTree(), base, old, new)


def write_text_no_follow(
    path: Path | str, text: str, encoding: str = "utf-8", *, append: bool = False
) -> None:
    """就地寫入或追加文字（需要時建上層資料夾）；路上有連結就拒絕，不截斷任何檔案。"""
    flags = os.O_WRONLY | os.O_CREAT | _NONBLOCK | (os.O_APPEND if append else 0)
    fd = open_no_follow(path, flags, make_parents=True)
    with os.fdopen(fd, "a" if append else "w", encoding=encoding) as f:
        _require_regular(f.fileno(), path)
        if not append:
            f.truncate(0)
        f.write(text)


def replace_text_no_follow(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """原子寫入：同資料夾的暫存檔寫完、fsync 後改名蓋過去；路上有連結就拒絕。

    和 ``ResourceManager.write_file_text`` 一樣，寫出的檔案權限是 0600。
    """
    p = Path(path)
    tmp_name = f".{p.name}.{secrets.token_hex(6)}.tmp"
    if _POSIX_DIR_FD:
        tmp = p.parent / tmp_name
        parent = _open_parent(p, make_parents=True)
        try:
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
            except OSError as exc:
                raise _with_paths(exc, tmp) from None
            try:
                _write_synced(fd, text, encoding)
                os.replace(tmp_name, p.name, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException as exc:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=parent)
                if isinstance(exc, OSError):
                    raise _with_paths(exc, tmp, p) from None
                raise
        finally:
            os.close(parent)
        return
    ensure_no_links(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / tmp_name
    # 暫存檔開檔前後都查：開在連結另一端時寫入內容前就擋下
    fd = open_no_follow(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_synced(fd, text, encoding)
        ensure_no_links(p)
        tmp.replace(p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _write_synced(fd: int, text: str, encoding: str) -> None:
    with os.fdopen(fd, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def remove_no_follow(path: Path | str) -> None:
    """刪除檔案；上層路上有連結就拒絕（最後一段是連結時刪的是連結本身）。"""
    p = Path(path)
    if _POSIX_DIR_FD:
        parent = _open_parent(p, make_parents=False)
        try:
            os.unlink(p.name, dir_fd=parent)
        except OSError as exc:
            raise _with_paths(exc, p) from None
        finally:
            os.close(parent)
        return
    ensure_no_links(p.parent)
    p.unlink()


# --- 走訪與刪樹（ADR-0293 Z4） ---------------------------------------------
# 同一套演算法跑在兩種「容器」上：POSIX 是開好的資料夾 fd、Windows 是路徑。``shown`` 是同一項
# 的絕對路徑，只用來放進例外（權限自癒靠它）。


def _missing(shown: Path) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(shown))


def _changed(shown: Path) -> OSError:
    return OSError(WALK_CHANGED, "Changed while it was being read", str(shown))


def _busy(shown: Path) -> OSError:
    return OSError(errno.EBUSY, TREE_KEPT_CHANGING_MESSAGE, str(shown))


@functools.cache
def _owner_repair_supported() -> bool:
    """刪樹時的權限自癒要 ``O_PATH`` 與 ``/proc/self/fd``（只有 Linux）；第一次用到時探測一次。"""
    return sys.platform == "linux" and hasattr(os, "O_PATH") and Path("/proc/self/fd").is_dir()


class _TreeOps[H](Protocol):
    """走訪與刪樹用到的容器操作；``H`` 是容器的型別。"""

    def open_parent(self, path: Path) -> H:
        """開到 ``path`` 的上一層（只做 ``*at`` 動作，不列內容）。"""
        ...

    def open_root(self, path: Path) -> tuple[H, os.stat_result] | None:
        """開到 ``path`` 本身供列出內容；不存在回 None，路上或本身是連結就拒絕。"""
        ...

    def names(self, holder: H, shown: Path) -> list[str]:
        """容器裡的名稱，依名稱排序。"""
        ...

    def lstat(self, holder: H, name: str, shown: Path) -> os.stat_result | None:
        """不跟隨的 stat；不存在回 None。"""
        ...

    def open_dir(self, holder: H, name: str, info: os.stat_result, shown: Path) -> H:
        """開子資料夾；是連結就拒絕，和 ``info`` 不是同一個就丟 ``WALK_CHANGED``。"""
        ...

    def open_file(self, holder: H, name: str, info: os.stat_result, shown: Path) -> int:
        """開一般檔案讀取，回 fd；是連結就拒絕，和 ``info`` 不是同一個就丟 ``WALK_CHANGED``。"""
        ...

    def unlink(self, holder: H, name: str, info: os.stat_result, shown: Path) -> None:
        """刪檔案或連結本身（``info`` 是剛才的 lstat）。"""
        ...

    def rmdir(self, holder: H, name: str, shown: Path) -> None:
        """刪空資料夾。"""
        ...

    def rename(self, src: H, name: str, dst: H, new: str, shown: Path) -> None:
        """改名或搬到另一個容器（同一個檔案系統）。"""
        ...

    def close(self, holder: H) -> None:
        """關掉容器。"""
        ...

    def repair_dir(self, holder: H, name: str) -> bool:
        """補自己擁有的資料夾 ``name`` 的擁有者位元；有補回 True。"""
        ...


class _FdTree(_TreeOps[int]):
    """POSIX：容器是不跟隨開好的資料夾 fd，傳給 ``*at`` 的名稱一律單段。"""

    @override
    def open_parent(self, path: Path) -> int:
        return _open_parent(path, make_parents=False)

    @override
    def open_root(self, path: Path) -> tuple[int, os.stat_result] | None:
        try:
            parent = _open_parent(path, make_parents=False)
        except (FileNotFoundError, NotADirectoryError):
            return None
        try:
            holder = os.open(path.name, _LIST_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _link_error(exc, path.name, parent, path) from None
        finally:
            os.close(parent)
        return holder, os.fstat(holder)

    @override
    def names(self, holder: int, shown: Path) -> list[str]:
        try:
            with os.scandir(holder) as entries:
                return sorted(entry.name for entry in entries)
        except OSError as exc:
            raise _with_paths(exc, shown) from None

    @override
    def lstat(self, holder: int, name: str, shown: Path) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=holder, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _with_paths(exc, shown) from None

    @override
    def open_dir(self, holder: int, name: str, info: os.stat_result, shown: Path) -> int:
        try:
            child = os.open(name, _LIST_FLAGS, dir_fd=holder)
        except OSError as exc:
            raise _link_error(exc, name, holder, shown) from None
        if not os.path.samestat(os.fstat(child), info):
            os.close(child)
            raise _changed(shown)
        return child

    @override
    def open_file(self, holder: int, name: str, info: os.stat_result, shown: Path) -> int:
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=holder)
        except OSError as exc:
            raise _link_error(exc, name, holder, shown) from None
        try:
            opened = os.fstat(fd)
            same = stat.S_ISREG(opened.st_mode) and os.path.samestat(opened, info)
            if same:
                os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        if not same:
            os.close(fd)
            raise _changed(shown)
        return fd

    @override
    def unlink(self, holder: int, name: str, info: os.stat_result, shown: Path) -> None:
        try:
            os.unlink(name, dir_fd=holder)
        except OSError as exc:
            raise _with_paths(exc, shown) from None

    @override
    def rmdir(self, holder: int, name: str, shown: Path) -> None:
        try:
            os.rmdir(name, dir_fd=holder)
        except OSError as exc:
            raise _with_paths(exc, shown) from None

    @override
    def rename(self, src: int, name: str, dst: int, new: str, shown: Path) -> None:
        try:
            os.rename(name, new, src_dir_fd=src, dst_dir_fd=dst)
        except OSError as exc:
            raise _with_paths(exc, shown) from None

    @override
    def close(self, holder: int) -> None:
        os.close(holder)

    @override
    def repair_dir(self, holder: int, name: str) -> bool:
        # 以 O_PATH 不跟隨地開，再經 /proc/self/fd 改權限：改的是剛核對過的那個 inode
        if not _owner_repair_supported():
            return False
        fd = os.open(name, _DIR_FLAGS, dir_fd=holder)
        try:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            wanted = (mode & ~(stat.S_ISUID | stat.S_ISGID)) | stat.S_IRWXU
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or wanted == mode:
                return False
            Path(f"/proc/self/fd/{fd}").chmod(wanted)
        finally:
            os.close(fd)
        return True


class _PathTree(_TreeOps[Path]):
    """Windows：沒有 ``dir_fd``，容器是路徑；每一步前先 lstat 確認不是連結（仍有時間差）。"""

    @override
    def open_parent(self, path: Path) -> Path:
        info = _walk_no_links(path.parent)
        if info is None:
            raise _missing(path.parent)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path.parent))
        return path.parent

    @override
    def open_root(self, path: Path) -> tuple[Path, os.stat_result] | None:
        info = _walk_no_links(path)
        if info is None:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path))
        return path, info

    @override
    def names(self, holder: Path, shown: Path) -> list[str]:
        if is_link(holder):
            raise _refused(shown)
        with os.scandir(holder) as entries:
            return sorted(entry.name for entry in entries)

    @override
    def lstat(self, holder: Path, name: str, shown: Path) -> os.stat_result | None:
        return _lstat(holder / name)

    @override
    def open_dir(self, holder: Path, name: str, info: os.stat_result, shown: Path) -> Path:
        now = _lstat(holder / name)
        if now is not None and _is_link_stat(now):
            raise _refused(shown)
        if now is None or not os.path.samestat(now, info):
            raise _changed(shown)
        return holder / name

    @override
    def open_file(self, holder: Path, name: str, info: os.stat_result, shown: Path) -> int:
        fd = open_no_follow(holder / name, os.O_RDONLY)
        try:
            opened = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        if not (stat.S_ISREG(opened.st_mode) and os.path.samestat(opened, info)):
            os.close(fd)
            raise _changed(shown)
        return fd

    @override
    def unlink(self, holder: Path, name: str, info: os.stat_result, shown: Path) -> None:
        target = holder / name
        attrs = getattr(info, "st_file_attributes", 0)
        if _is_link_stat(info):
            if attrs & _WINDOWS_DIRECTORY_ATTR:
                target.rmdir()  # 指向資料夾的 symlink 或 junction：刪的是連結本身
            else:
                target.unlink()
            return
        if attrs & _WINDOWS_READONLY_ATTR:
            target.chmod(stat.S_IREAD | stat.S_IWRITE)  # 唯讀檔（例如 .git/objects 裡的）
        target.unlink()

    @override
    def rmdir(self, holder: Path, name: str, shown: Path) -> None:
        target = holder / name
        try:
            target.rmdir()
        except PermissionError:
            info = _lstat(target)
            if info is None or _is_link_stat(info):
                raise
            if not getattr(info, "st_file_attributes", 0) & _WINDOWS_READONLY_ATTR:
                raise
            target.chmod(stat.S_IREAD | stat.S_IWRITE)
            target.rmdir()

    @override
    def rename(self, src: Path, name: str, dst: Path, new: str, shown: Path) -> None:
        (src / name).rename(dst / new)

    @override
    def close(self, holder: Path) -> None:
        return

    @override
    def repair_dir(self, holder: Path, name: str) -> bool:
        return False


def _rename_entry[H](ops: _TreeOps[H], base: Path, old: str, new: str) -> None:
    holder = ops.open_parent(base / old)
    try:
        info = ops.lstat(holder, old, base / old)
        if info is None:
            raise _missing(base / old)
        if _is_link_stat(info):
            raise _refused(base / old)
        existing = ops.lstat(holder, new, base / new)
        if existing is not None and not os.path.samestat(existing, info):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(base / new))
        ops.rename(holder, old, holder, new, base / old)
    finally:
        ops.close(holder)


@dataclass
class WalkReport:
    """走訪結束後的統計（略過的不算錯誤）。

    ``links``：略過的連結；``bad_names``：不能當區裡一段的名稱（只有區內走訪檢查）；
    ``errors``：（相對 POSIX 路徑, errno）——讀不到、太深（``WALK_TOO_DEEP``）、
    走訪途中被換掉（``WALK_CHANGED``）、換了檔案系統（``EXDEV``）；
    ``pruned``：``walk_pruned`` 剪掉的區根（錨點, 段落；整個錨點時段落是空的）。
    """

    links: int = 0
    bad_names: int = 0
    errors: list[tuple[str, int]] = field(default_factory=list)
    pruned: list[tuple[Anchor, tuple[str, ...]]] = field(default_factory=list)


class WalkEntry(NamedTuple):
    """走訪到的一項：相對根的段落、種類（``file``／``dir``／``other``）、lstat。"""

    parts: tuple[str, ...]
    kind: str
    info: os.stat_result


@dataclass
class _WalkSettings:
    report: WalkReport
    check_names: bool
    same_device: bool
    skip_dir: Callable[[tuple[str, ...]], bool] | None = None
    markers: ZoneMarkers | None = None
    root_dev: int = 0


def _walk(
    root: Path, settings: _WalkSettings
) -> Iterator[tuple[WalkEntry, Callable[[], int] | None]]:
    """深度優先、同層依名稱排序；每個資料夾先給檔案，再依序進入子資料夾（和 ``os.walk`` 同序）。

    一般檔案附一個開檔函式：在列出它的同一個資料夾裡不跟隨地開、回 fd（呼叫端負責關閉），
    只在拿到下一項之前有效。根不存在時什麼都不給；根或上層路上有連結就拒絕。
    """
    start = None
    if settings.markers is not None:
        start = settings.markers.start(str(root))
        if start is not None and start.kind == "zone":
            settings.report.pruned.append((start.anchor, start.parts))
            return
    if _POSIX_DIR_FD:
        yield from _walk_tree(_FdTree(), root, start, settings)
    else:
        yield from _walk_tree(_PathTree(), root, start, settings)


def _walk_tree[H](
    ops: _TreeOps[H], root: Path, start: MarkerState | None, settings: _WalkSettings
) -> Iterator[tuple[WalkEntry, Callable[[], int] | None]]:
    opened = ops.open_root(root)
    if opened is None:
        return
    holder, info = opened
    settings.root_dev = info.st_dev
    try:
        yield from _walk_dir(ops, holder, root, (), start, settings)
    finally:
        ops.close(holder)


def _walk_dir[H](
    ops: _TreeOps[H],
    holder: H,
    shown: Path,
    parts: tuple[str, ...],
    state: MarkerState | None,
    settings: _WalkSettings,
) -> Iterator[tuple[WalkEntry, Callable[[], int] | None]]:
    found = settings.report
    try:
        names = ops.names(holder, shown)
    except OSError as exc:
        found.errors.append(("/".join(parts), exc.errno or 0))
        return
    dirs: list[tuple[str, os.stat_result]] = []
    for name in names:
        if settings.check_names and not is_zone_segment(name):
            found.bad_names += 1
            continue
        try:
            info = ops.lstat(holder, name, shown / name)
        except OSError as exc:
            found.errors.append(("/".join((*parts, name)), exc.errno or 0))
            continue
        if info is None:
            continue
        if _is_link_stat(info):
            found.links += 1
        elif stat.S_ISDIR(info.st_mode):
            dirs.append((name, info))
        elif stat.S_ISREG(info.st_mode):
            opener = functools.partial(ops.open_file, holder, name, info, shown / name)
            yield WalkEntry((*parts, name), "file", info), opener
        else:
            yield WalkEntry((*parts, name), "other", info), None
    for name, info in dirs:
        child_parts = (*parts, name)
        opened = _open_child(ops, holder, shown / name, child_parts, info, state, settings)
        if opened is None:
            continue
        child, child_state = opened
        try:
            yield WalkEntry(child_parts, "dir", info), None
            yield from _walk_dir(ops, child, shown / name, child_parts, child_state, settings)
        finally:
            ops.close(child)


def _open_child[H](
    ops: _TreeOps[H],
    holder: H,
    shown: Path,
    parts: tuple[str, ...],
    info: os.stat_result,
    state: MarkerState | None,
    settings: _WalkSettings,
) -> tuple[H, MarkerState | None] | None:
    """要不要進這個子資料夾：不進的原因記進統計；要進就回（開好的容器, 位置）。"""
    found, rel = settings.report, "/".join(parts)
    if settings.skip_dir is not None and settings.skip_dir(parts):
        return None
    child_state = None
    if settings.markers is not None:
        child_state = settings.markers.child(state, str(shown), parts[-1], info)
        if child_state is not None and child_state.kind == "zone":
            found.pruned.append((child_state.anchor, child_state.parts))
            return None
    if settings.same_device and info.st_dev != settings.root_dev:
        found.errors.append((rel, errno.EXDEV))
        return None
    if len(parts) > MAX_ZONE_WALK_DEPTH:
        found.errors.append((rel, WALK_TOO_DEEP))
        return None
    try:
        child = ops.open_dir(holder, parts[-1], info, shown)
    except LinkRefusedError:
        found.links += 1
        return None
    except OSError as exc:
        found.errors.append((rel, exc.errno or 0))
        return None
    return child, child_state


def walk_no_follow(root: Path | str, report: WalkReport) -> Iterator[WalkEntry]:
    """走訪代理人資料夾（區）裡的一棵樹：不進入、不列出連結（計數），名稱不合格的略過並計數。

    根不存在時什麼都不給；根或上層路上有連結就拒絕。不跨檔案系統（記成錯誤）。
    """
    settings = _WalkSettings(report, check_names=True, same_device=True)
    for entry, _opener in _walk(Path(root), settings):
        yield entry


def walk_open_no_follow(
    root: Path | str, report: WalkReport
) -> Iterator[tuple[tuple[str, ...], IO[bytes], os.stat_result]]:
    """同 ``walk_no_follow``，但只給一般檔案，已經不跟隨地開好（相對段落, 檔案, fstat）。

    開檔和走訪在同一個資料夾上；拿下一個之前由這裡關掉上一個。開檔前被換成連結的計入
    ``links``，被換成別的東西的記成 ``WALK_CHANGED``。
    """
    settings = _WalkSettings(report, check_names=True, same_device=True)
    for entry, opener in _walk(Path(root), settings):
        if opener is None:
            continue
        try:
            fd = opener()
        except LinkRefusedError:
            report.links += 1
            continue
        except OSError as exc:
            report.errors.append(("/".join(entry.parts), exc.errno or 0))
            continue
        with os.fdopen(fd, "rb") as handle:
            yield entry.parts, handle, os.fstat(handle.fileno())


def walk_pruned(
    root: Path | str,
    roots: ZoneRoots,
    report: WalkReport,
    *,
    anchors: Iterable[Anchor] = ENFORCED_ANCHORS,
    skip_dir: Callable[[tuple[str, ...]], bool] | None = None,
) -> Iterator[WalkEntry]:
    """走訪代理人資料夾以外的一棵樹（專案根、備份的 data 根）：不跟隨連結，遇到區根剪掉（R5）。

    剪掉的區根記在 ``report.pruned``，由呼叫端決定略過並計數（列、找、統計）或整個拒絕（刪、
    複製、打包）。名稱不檢查、可以跨檔案系統（專案的 find、grep 行為不變）。``skip_dir`` 收
    子資料夾的相對段落，回真就不進入。
    """
    settings = _WalkSettings(
        report,
        check_names=False,
        same_device=False,
        skip_dir=skip_dir,
        markers=ZoneMarkers(roots, anchors),
    )
    for entry, _opener in _walk(Path(root), settings):
        yield entry


def iter_regular_files(
    root: Path, exclude_names: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Path]]:
    """遞迴列出 ``root`` 底下的一般檔案（相對 POSIX 路徑, 路徑），同層依名稱排序。

    建在 ``walk_pruned`` 上：不進入、也不列出 symlink 與 junction，讀不到的資料夾（含根）不進入，
    代理人資料夾剪掉；``exclude_names`` 依資料夾名稱在任何深度剪除。根或上層是連結照樣拒絕。
    """
    walk = walk_pruned(
        root, current_roots(), WalkReport(), skip_dir=lambda parts: parts[-1] in exclude_names
    )
    try:
        for entry in walk:
            if entry.kind == "file":
                yield "/".join(entry.parts), root.joinpath(*entry.parts)
    except LinkRefusedError:
        raise
    except OSError:
        return  # 只有根會丟（其他資料夾記進統計）：照舊什麼都不給


def remove_tree_no_follow(path: Path | str) -> None:
    """刪掉 ``path``：整個資料夾、檔案或連結本身；不跟隨任何連結，不因為太深而失敗。

    不存在時丟 ``FileNotFoundError``；上層路上有連結就拒絕。樹裡的連結只刪本身；樹裡的資料夾
    讀不到或刪不掉（EACCES）時補自己擁有的資料夾的擁有者位元再試一次（只有 Linux）。不跨檔案
    系統（``EXDEV``）；一直被換掉、或刪完又長出新東西時以固定原因（``EBUSY``）失敗。
    """
    p = Path(path)
    if _POSIX_DIR_FD:
        _remove_tree(_FdTree(), p)
    else:
        _remove_tree(_PathTree(), p)


@dataclass
class _Frame[H]:
    """刪樹時開著的一層資料夾；``outer`` 是補它自己權限時用的（上一層的容器, 名稱）。"""

    holder: H
    shown: Path
    name: str
    outer: tuple[H, str]
    pending: list[str] | None = None
    rescans: int = 0
    swaps: int = 0


def _repairing[H, T](ops: _TreeOps[H], action: Callable[[], T], target: tuple[H, str]) -> T:
    """``action`` 遇到 EACCES 時補 ``target``（上一層的容器, 名稱）的擁有者位元，再試一次。"""
    try:
        return action()
    except PermissionError as exc:
        if exc.errno != errno.EACCES or not ops.repair_dir(*target):
            raise
    return action()


def _remove_tree[H](ops: _TreeOps[H], path: Path) -> None:
    parent = ops.open_parent(path)
    try:
        for _attempt in range(MAX_REMOVE_SWAP_RETRIES):
            if _remove_once(ops, parent, path):
                return
        raise _busy(path)
    finally:
        ops.close(parent)


def _remove_once[H](ops: _TreeOps[H], parent: H, path: Path) -> bool:
    """刪一次；途中被換掉（檔案變資料夾、資料夾變連結、刪之前又長出東西）時回 False 重來。"""
    name = path.name
    info = ops.lstat(parent, name, path)
    if info is None:
        raise _missing(path)
    if _is_link_stat(info) or not stat.S_ISDIR(info.st_mode):
        try:
            ops.unlink(parent, name, info, path)
        except IsADirectoryError:
            return False
        return True
    try:
        child = _repairing(
            ops, functools.partial(ops.open_dir, parent, name, info, path), (parent, name)
        )
    except LinkRefusedError:
        return False
    except OSError as exc:
        if exc.errno != WALK_CHANGED:
            raise
        return False
    try:
        _empty_tree(ops, _Frame(child, path, name, (parent, name)), info.st_dev)
    finally:
        ops.close(child)
    try:
        ops.rmdir(parent, name, path)
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
        return False
    return True


def _empty_tree[H](ops: _TreeOps[H], root: _Frame[H], root_dev: int) -> None:
    """清空 ``root``（呼叫端負責關閉並刪掉它本身）；明確的堆疊，同時開著的資料夾有上限。"""
    frames = [root]
    try:
        while True:
            frame = frames[-1]
            if not frame.pending:
                names = ops.names(frame.holder, frame.shown)
                if names and frame.pending is not None:
                    frame.rescans += 1  # 清完之後又長出東西
                    if frame.rescans > MAX_REMOVE_SWAP_RETRIES:
                        raise _busy(frame.shown)
                frame.pending = names[::-1]
                if not names:
                    if len(frames) == 1:
                        return
                    _finish_dir(ops, frames)
                    continue
            _remove_child(ops, frames, frame.pending.pop(), root_dev)
    finally:
        for frame in frames[1:]:
            ops.close(frame.holder)


def _retry_later[H](frame: _Frame[H], name: str) -> None:
    frame.swaps += 1
    if frame.swaps > MAX_REMOVE_SWAP_RETRIES:
        raise _busy(frame.shown / name)
    frame.pending = [*(frame.pending or []), name]


def _finish_dir[H](ops: _TreeOps[H], frames: list[_Frame[H]]) -> None:
    """最上面的資料夾已經空了：關掉、從上一層刪掉；刪之前又長出東西時交回上一層重來。"""
    done = frames.pop()
    ops.close(done.holder)
    above = frames[-1]
    try:
        _repairing(
            ops, functools.partial(ops.rmdir, above.holder, done.name, done.shown), above.outer
        )
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
        _retry_later(above, done.name)


def _remove_child[H](ops: _TreeOps[H], frames: list[_Frame[H]], name: str, root_dev: int) -> None:
    frame = frames[-1]
    shown = frame.shown / name
    info = _repairing(ops, functools.partial(ops.lstat, frame.holder, name, shown), frame.outer)
    if info is None:
        return
    if _is_link_stat(info) or not stat.S_ISDIR(info.st_mode):
        try:
            _repairing(
                ops, functools.partial(ops.unlink, frame.holder, name, info, shown), frame.outer
            )
        except FileNotFoundError:
            return
        except IsADirectoryError:
            _retry_later(frame, name)  # 刪之前被換成資料夾
        return
    if info.st_dev != root_dev:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(shown))
    if len(frames) >= MAX_REMOVE_OPEN_DIRS:
        _move_to_root(ops, frames, name)
        return
    target = (frame.holder, name)
    try:
        child = _repairing(
            ops, functools.partial(ops.open_dir, frame.holder, name, info, shown), target
        )
    except LinkRefusedError:
        _retry_later(frame, name)  # 換成連結：下一輪刪連結本身
        return
    except OSError as exc:
        if exc.errno != WALK_CHANGED:
            raise
        _retry_later(frame, name)
        return
    frames.append(_Frame(child, shown, name, target))


def _move_to_root[H](ops: _TreeOps[H], frames: list[_Frame[H]], name: str) -> None:
    """太深：把子資料夾搬到樹根底下一個猜不到的名稱，之後從樹根再刪（同一個檔案系統）。"""
    frame, root = frames[-1], frames[0]
    shown = frame.shown / name
    for _attempt in range(MAX_REMOVE_SWAP_RETRIES):
        new = f".touka-remove-{secrets.token_hex(16)}"
        if ops.lstat(root.holder, new, root.shown / new) is not None:
            continue
        try:
            ops.rename(frame.holder, name, root.holder, new, shown)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
                raise
            continue
        root.pending = [*(root.pending or []), new]
        return
    raise _busy(shown)

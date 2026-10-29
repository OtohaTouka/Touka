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
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_POSIX_DIR_FD = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | _DIRECTORY | _NOFOLLOW | _CLOEXEC
# 開到連結時各系統回的錯誤：Linux 的 ELOOP（加 O_DIRECTORY 時是 ENOTDIR）、FreeBSD 的 EMLINK
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})
# junction 的 reparse tag：只有 Windows 有（其他系統的 stat 沒有這個常數）
_JUNCTION_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


# 回給代理人的說明（不含絕對路徑）
LINK_REFUSED_MESSAGE = "the path goes through a symbolic link or junction; links are not followed."
LINK_NOT_FOLLOWED = "(link, not followed)"


class LinkRefusedError(OSError):
    """路上有 symlink 或 junction：不跟隨，拒絕這次操作。"""


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
    """從根往下，現存的每一段都不能是 symlink 或 junction（走訪前、Windows 開檔前與開檔失敗時用）。

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


def read_bytes_no_follow(path: Path | str) -> bytes:
    """讀二進位檔；路上有連結就拒絕。"""
    fd = open_no_follow(path, os.O_RDONLY | _NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        _require_regular(f.fileno(), path)
        return f.read()


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


def _enterable(path: Path) -> bool:
    """走訪時進不進這個資料夾：不是連結才進；讀不到狀態（例如上層沒有執行權限）時不進。"""
    try:
        return not is_link(path)
    except OSError:
        return False


def iter_regular_files(
    root: Path, exclude_names: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Path]]:
    """遞迴列出 ``root`` 底下的一般檔案（相對 POSIX 路徑, 路徑），同層依名稱排序。

    不進入、也不列出 symlink 與 junction（只收 lstat 為一般檔案者），讀不到狀態的資料夾也
    不進入；``exclude_names`` 依資料夾名稱在任何深度剪除。
    """
    for current, dirnames, filenames in root.walk(follow_symlinks=False):
        rel_dir = current.relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames if d not in exclude_names and _enterable(current / d)
        )
        for name in sorted(filenames):
            try:
                info = (current / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield (rel_dir / name).as_posix(), current / name

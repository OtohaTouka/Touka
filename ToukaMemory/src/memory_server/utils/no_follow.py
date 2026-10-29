"""不跟隨連結的檔案操作：代理人的檔案動詞用（ADR-0292 決定 12）。

代理人的檔案動詞先把路徑解析、確認在根底下，才開檔。這中間代理人可以把路上某一段換成連結
（symlink 或 junction），開檔就會跟著連結寫到根外面；走訪時也會跟著代理人自己建的連結讀到根
外面（例如設定資料夾裡的 ``hooks.json``）。這裡的函式拿**已解析的絕對路徑**，路上任何一段
（含最後一段）是連結就丟 ``LinkRefusedError``；走訪時不進入、也不列出連結。

- POSIX：從檔案系統的根起逐段以 ``O_NOFOLLOW`` 開啟（``dir_fd``；Linux 用 ``O_PATH``，只能
  通過、不能列的資料夾也開得了），開檔時換成連結的那一段打不開；寫入、改名與刪除都在開好的
  上一層資料夾裡做。開檔帶 ``O_NONBLOCK``，換成 FIFO 時不會卡住，之後也只認一般檔案。
- Windows 沒有 ``dir_fd``：開檔前後各檢查一次路上有沒有 symlink 或 junction（OneDrive 之類的
  雲端檔案不算），兩次檢查之間仍有很短的時間差。Windows 本機的代理人終端不經隔離、每條指令
  都要人核准，代理人要建連結也得先經人核准。

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


# 回給代理人的說明（不含絕對路徑）
LINK_REFUSED_MESSAGE = "the path goes through a symbolic link or junction; links are not followed."
LINK_NOT_FOLLOWED = "(link, not followed)"


class LinkRefusedError(OSError):
    """路上有 symlink 或 junction：不跟隨，拒絕這次操作。"""


def is_link(path: Path) -> bool:
    """這一段本身是 symlink 或 junction（不跟隨；不存在時回 False）。"""
    return path.is_symlink() or path.is_junction()


def _refused(path: Path) -> LinkRefusedError:
    return LinkRefusedError(errno.ELOOP, "Path goes through a symbolic link or junction", str(path))


def ensure_no_links(path: Path | str) -> None:
    """從根往下，現存的每一段都不能是 symlink 或 junction（Windows 開檔前後與走訪前用）。"""
    p = Path(path)
    current = Path(p.anchor)
    for name in p.parts[1:]:
        current = current / name
        if is_link(current):
            raise _refused(current)


def _link_error(exc: OSError, name: str, parent: int, shown: Path) -> OSError:
    """POSIX 開不起來時：那一段是連結就換成 ``LinkRefusedError``，其餘照原樣。"""
    if exc.errno in _LINK_ERRNOS:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                return _refused(shown)
    return exc


def _open_parent(path: Path, *, make_parents: bool) -> int:
    """POSIX：從檔案系統的根逐段開到 ``path`` 的上一層，回它的 fd（呼叫端負責關閉）。"""
    fd = os.open(path.anchor, _DIR_FLAGS & ~_NOFOLLOW)
    shown = Path(path.anchor)
    try:
        for name in path.parts[1:-1]:
            shown = shown / name
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not make_parents:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o777, dir_fd=fd)
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise _link_error(exc, name, fd, shown) from None
            except OSError as exc:
                raise _link_error(exc, name, fd, shown) from None
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


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
    ensure_no_links(p)
    if make_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
        ensure_no_links(p)
    fd = os.open(p, flags | _BINARY, mode)
    try:
        ensure_no_links(p)
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
        parent = _open_parent(p, make_parents=True)
        try:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                         0o600, dir_fd=parent)
            try:
                _write_synced(fd, text, encoding)
                os.replace(tmp_name, p.name, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=parent)
                raise
        finally:
            os.close(parent)
        return
    ensure_no_links(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    ensure_no_links(p.parent)
    tmp = p.parent / tmp_name
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
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
        finally:
            os.close(parent)
        return
    ensure_no_links(p.parent)
    p.unlink()


def iter_regular_files(
    root: Path, exclude_names: frozenset[str] = frozenset()
) -> Iterator[tuple[str, Path]]:
    """遞迴列出 ``root`` 底下的一般檔案（相對 POSIX 路徑, 路徑），同層依名稱排序。

    不進入、也不列出 symlink 與 junction（只收 lstat 為一般檔案者）；``exclude_names`` 依
    資料夾名稱在任何深度剪除。
    """
    for current, dirnames, filenames in root.walk(follow_symlinks=False):
        rel_dir = current.relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames if d not in exclude_names and not is_link(current / d)
        )
        for name in sorted(filenames):
            try:
                info = (current / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield (rel_dir / name).as_posix(), current / name

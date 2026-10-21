"""擁有者位元自癒（只用標準函式庫；proxy 端的自癒服務與執行器伺服器共用）.

``repair_chain(root, target)`` **只修一條路徑鏈、不遞迴**：從白名單根 ``root`` 沿著 ``target`` 的
原路徑（正規化、不 resolve；``target`` 必須等於 ``root`` 或在它底下）逐段開到目標，路上每個
被鎖的 inode 和目標本身都修：

- 每一段用上一段的 dir fd 以 ``O_PATH|O_NOFOLLOW`` 開啟，fstat 後才決定：符號連結（9p 上的
  junction 也是）、換了檔案系統、不是自己 uid 擁有、``st_nlink > 1`` 的一般檔、FIFO／裝置一律停下；
- 只補擁有者位元（資料夾 u+rwx；一般檔與 socket u+rw，原本有任何 x 再加 u+x），順便拿掉
  setuid／setgid；群組與其他人的位元不動、不 chown；
- 對 ``/proc/self/fd/N`` 做 chmod：綁在已開啟、已核對的同一個 inode 上，沒有 TOCTOU；
  下一段從修好的資料夾往下開；
- 目標最後幾段還不存在（例如在被鎖的資料夾裡建新檔）時，修到最後存在的一段，其餘當作完成；
- 只有權限位元真的有改才算有修（``changed``）；位元本來就對時（EACCES 來自 LSM）回「沒有改」；
- 只在 ``sys.platform == "linux"`` 執行（macOS 沒有 ``O_PATH`` 與 ``/proc``）；任何內部錯誤都變成
  「修不了」加原因，不往外丟。白名單、errno 判斷、受保護路徑與頻率限制由呼叫端負責。

``repair_private(root, target)`` 走同一條鏈、停在同樣的地方，另外拿掉群組與其他人的位元：只給
執行器伺服器自己的私有資料夾與檔案（W6：state、ipc 與 ipc 裡的 socket、權杖、alive）用。
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

UNSUPPORTED, OUTSIDE, SYMLINK = "unsupported_platform", "outside", "symlink"
NOT_OWNER, HARDLINK, SPECIAL = "not_owner", "hardlink", "special"
MISSING_ROOT, DENIED, ERROR = "missing_root", "denied", "error"

_OPEN_FLAGS = getattr(os, "O_PATH", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_SETID = stat.S_ISUID | stat.S_ISGID
_OWNER_DIR, _OWNER_FILE, _OWNER_X, _ANY_X = 0o700, 0o600, 0o100, 0o111
_GROUP_OTHER = 0o077
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM})


@dataclass(frozen=True)
class RepairOutcome:
    """``changed``：權限位元真的改了的 inode 數；``stopped``：停下的原因（None＝走完）."""

    changed: int = 0
    stopped: str | None = None
    missing_tail: bool = False

    @property
    def repaired(self) -> bool:
        """有沒有真的改到權限位元（呼叫端只在這時重試）。"""
        return self.changed > 0


class FsOps(Protocol):
    """走訪用的檔案系統操作（真實作是 ``_LinuxOps``，測試用假的）。"""

    def open_root(self, path: str) -> int:
        """開白名單根（``O_PATH|O_NOFOLLOW``）。"""
        ...

    def open_child(self, dir_fd: int, name: str) -> int:
        """從 dir fd 開下一段（``O_PATH|O_NOFOLLOW``）。"""
        ...

    def fstat(self, fd: int) -> os.stat_result:
        """已開啟 inode 的 stat。"""
        ...

    def chmod(self, fd: int, mode: int) -> None:
        """改這個 inode 的權限位元。"""
        ...

    def close(self, fd: int) -> None:
        """關掉 fd。"""
        ...

    def uid(self) -> int:
        """自己的有效 uid。"""
        ...


class _LinuxOps:
    def open_root(self, path: str) -> int:
        return os.open(path, _OPEN_FLAGS)

    def open_child(self, dir_fd: int, name: str) -> int:
        return os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def chmod(self, fd: int, mode: int) -> None:
        # O_PATH fd 不能 fchmod；經 magic link 改的是同一個已核對的 inode
        Path(f"/proc/self/fd/{fd}").chmod(mode)

    def close(self, fd: int) -> None:
        os.close(fd)

    def uid(self) -> int:
        return os.geteuid()


def owner_mode(mode: int) -> int | None:
    """補好擁有者位元、拿掉 setuid／setgid 後的權限位元；不是資料夾、一般檔或 socket 回 None。"""
    perm = stat.S_IMODE(mode) & ~_SETID
    if stat.S_ISDIR(mode):
        return perm | _OWNER_DIR
    if stat.S_ISREG(mode) or stat.S_ISSOCK(mode):
        return perm | _OWNER_FILE | (_OWNER_X if perm & _ANY_X else 0)
    return None


def private_mode(mode: int) -> int | None:
    """``owner_mode`` 之後再拿掉群組與其他人的位元（伺服器自己的私有 inode）。"""
    wanted = owner_mode(mode)
    return None if wanted is None else wanted & ~_GROUP_OTHER


def chain_parts(root: str, target: str) -> tuple[str, ...] | None:
    """``target`` 在 ``root`` 底下的各段（正規化、不 resolve）；不在底下或含 ``..`` 回 None。"""
    root_path, target_path = PurePosixPath(root), PurePosixPath(target)
    if not (root_path.is_absolute() and target_path.is_absolute()):
        return None
    if ".." in root_path.parts or ".." in target_path.parts:
        return None
    if not target_path.is_relative_to(root_path):
        return None
    return target_path.relative_to(root_path).parts


class _Walk:
    """一次走訪；手上最多一個 fd，``close`` 一定會關。"""

    def __init__(self, ops: FsOps, wanted: Callable[[int], int | None]) -> None:
        self.ops, self.wanted = ops, wanted
        self.changed = 0
        self.fd: int | None = None

    def _hold(self, fd: int | None) -> None:
        if self.fd is not None:
            self.ops.close(self.fd)
        self.fd = fd

    def close(self) -> None:
        self._hold(None)

    def _stop_reason(self, info: os.stat_result, uid: int, root_dev: int) -> str | None:
        if stat.S_ISLNK(info.st_mode):
            return SYMLINK
        if info.st_dev != root_dev:
            return OUTSIDE
        if info.st_uid != uid:
            return NOT_OWNER
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return HARDLINK
        return SPECIAL if owner_mode(info.st_mode) is None else None

    def run(self, root: str, parts: tuple[str, ...]) -> RepairOutcome:
        try:
            self._hold(self.ops.open_root(root))
        except OSError as exc:
            return RepairOutcome(stopped=MISSING_ROOT if exc.errno == errno.ENOENT else DENIED)
        uid, root_dev = self.ops.uid(), None
        for index in range(len(parts) + 1):
            if index:
                try:
                    self._hold(self.ops.open_child(self.fd, parts[index - 1]))
                except OSError as exc:
                    if exc.errno == errno.ENOENT:  # 後面還不存在：修到最後存在的一段
                        return RepairOutcome(self.changed, missing_tail=True)
                    reason = DENIED if exc.errno in _DENIED_ERRNOS else ERROR
                    return RepairOutcome(self.changed, reason)
            info = self.ops.fstat(self.fd)
            root_dev = info.st_dev if root_dev is None else root_dev
            reason = self._stop_reason(info, uid, root_dev)
            if reason:
                return RepairOutcome(self.changed, reason)
            wanted = self.wanted(info.st_mode)
            if wanted != stat.S_IMODE(info.st_mode):
                self.ops.chmod(self.fd, wanted)
                self.changed += 1
        return RepairOutcome(self.changed)


def repair_chain(root: str, target: str, *, ops: FsOps | None = None) -> RepairOutcome:
    """修 ``root`` 到 ``target`` 這條鏈上被鎖的擁有者位元；規則見模組說明，不往外丟例外。"""
    return _repair(root, target, ops, owner_mode)


def repair_private(root: str, target: str, *, ops: FsOps | None = None) -> RepairOutcome:
    """同 ``repair_chain``，另外拿掉群組與其他人的位元（只給執行器伺服器自己的私有 inode）。"""
    return _repair(root, target, ops, private_mode)


def _repair(
    root: str, target: str, ops: FsOps | None, wanted: Callable[[int], int | None]
) -> RepairOutcome:
    if ops is None:
        if sys.platform != "linux":
            return RepairOutcome(stopped=UNSUPPORTED)
        ops = _LinuxOps()
    parts = chain_parts(root, target)
    if parts is None:
        return RepairOutcome(stopped=OUTSIDE)
    walk = _Walk(ops, wanted)
    try:
        return walk.run(root, parts)
    except Exception:  # 內部錯誤一律當成修不了；原本的 PermissionError 由呼叫端照樣往上丟
        return RepairOutcome(walk.changed, ERROR)
    finally:
        with contextlib.suppress(Exception):
            walk.close()

"""擁有者位元自癒的 kernel 函式（``sandbox_kernel/owner_repair.py``）：純函式與假檔案系統.

``repair_chain(root, target)`` 只修一條路徑鏈（從白名單根逐段走到目標，不遞迴）；只補擁有者位元、
拿掉 setuid／setgid；遇到連結、別人擁有、硬連結、換了檔案系統、特殊檔就停；目標最後幾段還不存在
時修到最後存在的一段；只有權限位元真的有改才算有修；任何內部錯誤都變成「修不了」，不往外丟。
``repair_private`` 走同一條鏈，另外拿掉群組與其他人的位元（只給執行器伺服器自己的私有 inode）。
真核心（O_PATH＋``/proc/self/fd``）的案例在 ``tests/integration/test_isolation_linux.py``。
"""

from __future__ import annotations

import errno
import os
import stat
import sys

import pytest
from src.sandbox_kernel import owner_repair as orp

DIR, REG, LNK, FIFO, SOCK = stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK
ME, OTHER = 1001, 0


class FakeFs:
    """路徑 → (類型, 權限, uid, nlink, dev)；開子項目時照核心規則要求上層有擁有者 x 位元."""

    def __init__(self, nodes: dict[str, tuple]) -> None:
        self.nodes = {path: list(node) for path, node in nodes.items()}
        self.fds: dict[int, str] = {}
        self.closed: list[int] = []
        self.chmods: list[tuple[str, int]] = []
        self.opened: list[str] = []
        self._next = 10

    def _open(self, path: str) -> int:
        if path not in self.nodes:
            raise FileNotFoundError(errno.ENOENT, path)
        self.opened.append(path)
        self._next += 1
        self.fds[self._next] = path
        return self._next

    def open_root(self, path: str) -> int:
        return self._open(path)

    def open_child(self, dir_fd: int, name: str) -> int:
        parent = self.nodes[self.fds[dir_fd]]
        if parent[0] != DIR:
            raise NotADirectoryError(errno.ENOTDIR, name)
        if not parent[1] & 0o100:
            raise PermissionError(errno.EACCES, name)  # 上層沒有 x：查不到子項目
        return self._open(f"{self.fds[dir_fd]}/{name}")

    def fstat(self, fd: int) -> os.stat_result:
        kind, mode, uid, nlink, dev = self.nodes[self.fds[fd]]
        return os.stat_result((kind | mode, 1, dev, nlink, uid, uid, 0, 0, 0, 0))

    def chmod(self, fd: int, mode: int) -> None:
        path = self.fds[fd]
        self.chmods.append((path, mode))
        self.nodes[path][1] = mode

    def close(self, fd: int) -> None:
        self.closed.append(fd)

    def uid(self) -> int:
        return ME

    def mode(self, path: str) -> int:
        return self.nodes[path][1]

    def all_closed(self) -> bool:
        return sorted(self.closed) == sorted(self.fds)


def _node(kind=DIR, mode=0o755, uid=ME, nlink=1, dev=7) -> tuple:
    return (kind, mode, uid, nlink, dev)


def _repair(fs: FakeFs, root: str, target: str) -> orp.RepairOutcome:
    return orp.repair_chain(root, target, ops=fs)


# ── 權限位元：只補擁有者、拿掉 setuid／setgid、群組與其他人不動 ─────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (DIR | 0o000, 0o700),
        (DIR | 0o055, 0o755),
        (DIR | 0o2750, 0o750),  # setgid 拿掉
        (DIR | 0o1000, 0o1700),  # sticky 不動
        (REG | 0o000, 0o600),
        (REG | 0o044, 0o644),  # 群組與其他人的讀位元不動，也不新增
        (REG | 0o010, 0o710),  # 原本有任何 x 才加 u+x
        (REG | 0o001, 0o701),
        (REG | 0o4755, 0o755),  # setuid 拿掉
        (REG | 0o6000, 0o600),
        (SOCK | 0o000, 0o600),  # 執行器的 ctl.sock
        (FIFO | 0o000, None),
        (LNK | 0o777, None),
    ],
)
def test_owner_mode_only_adds_owner_bits_and_drops_setid(mode, expected):
    assert orp.owner_mode(mode) == expected


@pytest.mark.parametrize(
    ("root", "target", "parts"),
    [
        ("/r", "/r", ()),
        ("/r", "/r/a/b", ("a", "b")),
        ("/r", "/r//a/./b", ("a", "b")),  # 只做正規化，不 resolve
        ("/r", "/rx/a", None),  # 名稱前綴相同的兄弟資料夾
        ("/r", "/r/../x", None),
        ("/r", "/r/a/..", None),
        ("/r", "r/a", None),
        ("/r", "/", None),
        ("r", "r/a", None),
    ],
)
def test_chain_parts_stay_under_the_root_without_resolving(root, target, parts):
    assert orp.chain_parts(root, target) == parts


# ── 一條鏈、不遞迴 ─────────────────────────────────────────────────────────────────


def _locked_tree() -> FakeFs:
    return FakeFs(
        {
            "/r": _node(mode=0o755),
            "/r/a": _node(mode=0o000),
            "/r/a/b": _node(mode=0o000),
            "/r/a/b/child": _node(mode=0o000),
            "/r/a/b/f.txt": _node(REG, 0o000),
            "/r/a/sibling": _node(mode=0o000),
        }
    )


def test_repairs_every_locked_inode_on_the_chain_and_nothing_else():
    fs = _locked_tree()
    outcome = _repair(fs, "/r", "/r/a/b/f.txt")
    assert outcome == orp.RepairOutcome(changed=3)
    assert outcome.repaired is True
    assert fs.chmods == [("/r/a", 0o700), ("/r/a/b", 0o700), ("/r/a/b/f.txt", 0o600)]
    assert fs.mode("/r/a/sibling") == fs.mode("/r/a/b/child") == 0o000  # 不遞迴
    assert fs.all_closed()


def test_folder_target_does_not_reach_into_its_children():
    fs = _locked_tree()
    assert _repair(fs, "/r", "/r/a/b") == orp.RepairOutcome(changed=2)
    assert fs.opened == ["/r", "/r/a", "/r/a/b"]  # 沒有列目錄、沒有往下開
    assert fs.mode("/r/a/b/child") == fs.mode("/r/a/b/f.txt") == 0o000


def test_target_equal_to_root_repairs_only_the_root():
    fs = FakeFs({"/r": _node(mode=0o000), "/r/a": _node(mode=0o000)})
    assert _repair(fs, "/r", "/r") == orp.RepairOutcome(changed=1)
    assert fs.chmods == [("/r", 0o700)] and fs.opened == ["/r"]


@pytest.mark.parametrize("target", ["/r/a/new.txt", "/r/a/.x.tmp", "/r/a/new/deeper/f"])
def test_missing_tail_repairs_up_to_the_last_existing_segment(target):
    fs = FakeFs({"/r": _node(), "/r/a": _node(mode=0o000)})
    outcome = _repair(fs, "/r", target)
    assert outcome == orp.RepairOutcome(changed=1, missing_tail=True)
    assert fs.chmods == [("/r/a", 0o700)]
    assert fs.all_closed()


def test_nothing_to_change_is_not_a_repair():
    """EACCES 其實來自 AppArmor／SELinux／Landlock 時，位元本來就對：回「沒有改」，呼叫端不重試."""
    fs = FakeFs({"/r": _node(), "/r/a": _node(), "/r/a/f": _node(REG, 0o644)})
    outcome = _repair(fs, "/r", "/r/a/f")
    assert outcome == orp.RepairOutcome() and outcome.repaired is False
    assert fs.chmods == []


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (_node(LNK, 0o777), orp.SYMLINK),
        (_node(mode=0o000, uid=OTHER), orp.NOT_OWNER),
        (_node(REG, 0o000, nlink=2), orp.HARDLINK),
        (_node(mode=0o000, dev=8), orp.OUTSIDE),  # 換了檔案系統（掛載點）
        (_node(FIFO, 0o000), orp.SPECIAL),
    ],
    ids=["symlink", "not-owner", "hardlink", "other-device", "fifo"],
)
def test_stops_at_inodes_it_must_not_touch(bad, reason):
    fs = FakeFs(
        {
            "/r": _node(),
            "/r/a": _node(mode=0o000),
            "/r/a/x": bad,
            "/r/a/x/y": _node(mode=0o000),
        }
    )
    outcome = _repair(fs, "/r", "/r/a/x/y")
    assert outcome == orp.RepairOutcome(changed=1, stopped=reason)
    assert fs.chmods == [("/r/a", 0o700)]  # 停下的那個與之後的都不動
    assert fs.mode("/r/a/x/y") == 0o000
    assert fs.all_closed()


def test_root_owned_by_someone_else_is_not_touched():
    fs = FakeFs({"/r": _node(mode=0o000, uid=OTHER)})
    assert _repair(fs, "/r", "/r/a") == orp.RepairOutcome(stopped=orp.NOT_OWNER)
    assert fs.chmods == []


def test_target_outside_the_root_opens_nothing():
    fs = _locked_tree()
    assert _repair(fs, "/r", "/rx/a") == orp.RepairOutcome(stopped=orp.OUTSIDE)
    assert _repair(fs, "/r", "/r/a/../../etc") == orp.RepairOutcome(stopped=orp.OUTSIDE)
    assert fs.opened == [] and fs.chmods == []


# ── 不往外丟例外：一律變成「修不了」加原因 ─────────────────────────────────────────────


class _LsmDenied(FakeFs):
    """位元都對，查子項目仍被 LSM（AppArmor／SELinux／Landlock）拒絕."""

    def open_child(self, dir_fd: int, name: str) -> int:
        raise PermissionError(errno.EACCES, name)


def test_missing_root_and_denied_lookups_are_outcomes():
    fs = FakeFs({"/r": _node()})
    assert _repair(fs, "/gone", "/gone/a") == orp.RepairOutcome(stopped=orp.MISSING_ROOT)
    denied = _LsmDenied({"/r": _node(), "/r/a": _node()})
    outcome = _repair(denied, "/r", "/r/a")
    assert outcome == orp.RepairOutcome(stopped=orp.DENIED) and outcome.repaired is False
    assert denied.all_closed()


class _Boom(FakeFs):
    def chmod(self, fd: int, mode: int) -> None:
        if self.fds[fd] == "/r/a/b":
            raise RuntimeError("unexpected")
        super().chmod(fd, mode)


def test_internal_errors_become_cannot_repair_and_keep_the_count():
    fs = _Boom(_locked_tree().nodes)
    outcome = _repair(fs, "/r", "/r/a/b/f.txt")
    assert outcome == orp.RepairOutcome(changed=1, stopped=orp.ERROR)
    assert fs.all_closed()


@pytest.mark.parametrize("platform", ["darwin", "win32", "freebsd14"])
def test_only_linux_runs_the_real_repair(monkeypatch, tmp_path, platform):
    """macOS 雖然是 posix，但沒有 ``os.O_PATH`` 與 ``/proc``：一律回「修不了」、不丟例外."""
    monkeypatch.setattr(sys, "platform", platform)
    locked = tmp_path / "locked"
    locked.mkdir()
    outcome = orp.repair_chain(str(tmp_path), str(locked))
    assert outcome == orp.RepairOutcome(stopped=orp.UNSUPPORTED)


def test_reasons_are_short_codes():
    reasons = {
        orp.UNSUPPORTED, orp.OUTSIDE, orp.SYMLINK, orp.NOT_OWNER, orp.HARDLINK, orp.SPECIAL,
        orp.MISSING_ROOT, orp.DENIED, orp.ERROR,
    }
    assert len(reasons) == 9
    assert all(reason.isidentifier() and reason.islower() for reason in reasons)


# ── 執行器自己的私有 inode（W6）：另外拿掉群組與其他人的位元（第 3 輪 F1）──────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (DIR | 0o777, 0o700),
        (DIR | 0o000, 0o700),
        (DIR | 0o2775, 0o700),  # setgid 也拿掉
        (REG | 0o644, 0o600),
        (REG | 0o755, 0o700),  # 擁有者的 x 留著
        (SOCK | 0o777, 0o700),
        (FIFO | 0o600, None),
        (LNK | 0o777, None),
    ],
)
def test_private_mode_also_drops_group_and_other_bits(mode, expected):
    assert orp.private_mode(mode) == expected


def test_repair_private_closes_every_inode_on_the_chain_to_its_owner():
    fs = FakeFs({"/ipc": _node(mode=0o777), "/ipc/token": _node(REG, 0o644)})
    outcome = orp.repair_private("/ipc", "/ipc/token", ops=fs)
    assert outcome == orp.RepairOutcome(changed=2)
    assert fs.chmods == [("/ipc", 0o700), ("/ipc/token", 0o600)]
    assert orp.repair_private("/ipc", "/ipc/token", ops=fs) == orp.RepairOutcome()  # 已經私有
    assert fs.all_closed()


@pytest.mark.parametrize(
    ("root", "reason"),
    [(_node(LNK, 0o777), orp.SYMLINK), (_node(mode=0o777, uid=OTHER), orp.NOT_OWNER)],
    ids=["symlink", "not-owner"],
)
def test_repair_private_does_not_touch_a_root_it_must_not(root, reason):
    fs = FakeFs({"/ipc": root})
    assert orp.repair_private("/ipc", "/ipc", ops=fs) == orp.RepairOutcome(stopped=reason)
    assert fs.chmods == [] and fs.all_closed()


def test_repair_private_runs_only_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert orp.repair_private(str(tmp_path), str(tmp_path)) == orp.RepairOutcome(
        stopped=orp.UNSUPPORTED
    )

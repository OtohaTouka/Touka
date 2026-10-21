"""Landlock 系統呼叫與權利遮罩（隔離啟動器的核心；只用標準函式庫）.

任何平台都能 import：libc 只在建立 ``LinuxLandlock`` 時才取用。ABI 超過 ``VERIFIED_ABI_MAX``
一律當 7（未驗證的權利不用）。C2-1 由啟動器每條指令用 ``build_net_ruleset`` 自建網路規則集
（``--net-ruleset-fd`` 共用規則集的介面保留，proxy 不提供）；``restrict_self`` 只在
``launcher.py`` 呼叫。
"""

from __future__ import annotations

import ctypes
import errno
import os
from typing import Protocol

# 三個 syscall 在所有使用統一編號表的架構上相同
SYS_CREATE_RULESET = 444
SYS_ADD_RULE = 445
SYS_RESTRICT_SELF = 446
CREATE_RULESET_VERSION = 1 << 0
CREATE_RULESET_ERRATA = 1 << 1
RULE_PATH_BENEATH = 1
RULE_NET_PORT = 2
PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38
SIGKILL = 9

# 檔案系統權利（ABI 1：EXECUTE..MAKE_SYM；2：REFER；3：TRUNCATE；5：IOCTL_DEV）
EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13
TRUNCATE = 1 << 14
IOCTL_DEV = 1 << 15
# 網路（ABI 4）與 scope（ABI 6）；網路規則集同時處理 bind 與 connect（搶不到也連不到封鎖埠）
BIND_TCP = 1 << 0
CONNECT_TCP = 1 << 1
NET_TCP = BIND_TCP | CONNECT_TCP
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1

VERIFIED_ABI_MAX = 7
MIN_ABI = 3
MIN_NET_ABI = 4
_ABI_REFER = 2
_ABI_TRUNCATE = 3
_ABI_IOCTL_DEV = 5
_ABI_SCOPES = 6

# 一般檔與裝置只能帶這些權利（其餘是資料夾權利）
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
DEVICE_RIGHTS = READ_FILE | WRITE_FILE | IOCTL_DEV
_RW = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_SYM
ACCESS_RIGHTS: dict[str, int] = {
    "ro": READ_FILE | READ_DIR,
    "rox": EXECUTE | READ_FILE | READ_DIR,
    "rwx": EXECUTE | READ_FILE | READ_DIR | _RW | REFER | TRUNCATE,  # 不給 MAKE_CHAR／MAKE_BLOCK
    "dev_rw": DEVICE_RIGHTS,
}

_ATTR_SIZE_FS = 8
_ATTR_SIZE_NET = 16
_ATTR_SIZE_SCOPED = 24


def effective_abi(abi: int) -> int:
    """超過已驗證上限時當上限處理。"""
    return min(abi, VERIFIED_ABI_MAX)


def handled_fs(abi: int) -> int:
    """這個 ABI 能處理的檔案系統權利。"""
    abi = effective_abi(abi)
    mask = (MAKE_SYM << 1) - 1
    if abi >= _ABI_REFER:
        mask |= REFER
    if abi >= _ABI_TRUNCATE:
        mask |= TRUNCATE
    if abi >= _ABI_IOCTL_DEV:
        mask |= IOCTL_DEV
    return mask


def handled_net(abi: int) -> int:
    """這個 ABI 能處理的網路權利（BIND_TCP 與 CONNECT_TCP）。"""
    return NET_TCP if effective_abi(abi) >= MIN_NET_ABI else 0


def handled_scopes(abi: int) -> int:
    """ABI 6 起：信號與抽象 unix socket 不能跨出 domain。"""
    return SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL if effective_abi(abi) >= _ABI_SCOPES else 0


def ruleset_attr_size(net: int, scoped: int) -> int:
    """結構只傳到用得到的欄位為止（fs 一定在），舊核心也收得下。"""
    if scoped:
        return _ATTR_SIZE_SCOPED
    return _ATTR_SIZE_NET if net else _ATTR_SIZE_FS


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


class _NetPortAttr(ctypes.Structure):
    _fields_ = (("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64))


class LandlockApi(Protocol):
    """啟動器需要的 Landlock 操作（真實作是 ``LinuxLandlock``，測試用假的）。"""

    def create_ruleset(self, fs: int, net: int, scoped: int) -> int:
        """建規則集，回 fd。"""
        ...

    def add_path_rule(self, ruleset_fd: int, allowed: int, parent_fd: int) -> None:
        """加一條路徑規則。"""
        ...

    def add_port_rule(self, ruleset_fd: int, allowed: int, port: int) -> None:
        """加一條 TCP 埠規則。"""
        ...

    def restrict_self(self, ruleset_fd: int) -> None:
        """把規則集套到目前行程（不可逆）。"""
        ...

    def close(self, fd: int) -> None:
        """關掉規則集 fd。"""
        ...


class LinuxLandlock:
    """以 ctypes 呼叫 libc ``syscall``／``prctl``；只在 Linux 建立。"""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.syscall.restype = ctypes.c_long
        self._libc.prctl.restype = ctypes.c_int
        self._libc.prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )

    def _syscall(self, number: int, *args: object) -> int:
        result = self._libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return int(result)

    def _query(self, flag: int) -> int:
        return self._syscall(
            SYS_CREATE_RULESET, ctypes.c_void_p(None), ctypes.c_size_t(0), ctypes.c_uint32(flag)
        )

    def abi(self) -> int:
        """核心的 Landlock ABI；沒有 Landlock（ENOSYS／EOPNOTSUPP）回 0。"""
        try:
            return self._query(CREATE_RULESET_VERSION)
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP}:
                return 0
            raise

    def errata(self) -> int:
        """已修補的 errata 位元；核心不支援時回 0。"""
        try:
            return self._query(CREATE_RULESET_ERRATA)
        except OSError:
            return 0

    def prctl(self, option: int, value: int) -> None:
        """``prctl(option, value, 0, 0, 0)``，失敗拋 OSError。"""
        if self._libc.prctl(option, value, 0, 0, 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def create_ruleset(self, fs: int, net: int, scoped: int) -> int:
        """建規則集，回 fd（不可繼承由呼叫端決定）。"""
        attr = _RulesetAttr(fs, net, scoped)
        size = ruleset_attr_size(net, scoped)
        return self._syscall(
            SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)
        )

    def add_path_rule(self, ruleset_fd: int, allowed: int, parent_fd: int) -> None:
        """``LANDLOCK_RULE_PATH_BENEATH``。"""
        attr = _PathBeneathAttr(allowed, parent_fd)
        self._syscall(
            SYS_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        )

    def add_port_rule(self, ruleset_fd: int, allowed: int, port: int) -> None:
        """``LANDLOCK_RULE_NET_PORT``。"""
        attr = _NetPortAttr(allowed, port)
        self._syscall(
            SYS_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(RULE_NET_PORT),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        )

    def restrict_self(self, ruleset_fd: int) -> None:
        """套用規則集；之後的子孫都繼承。"""
        self._syscall(SYS_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))

    def close(self, fd: int) -> None:
        """關掉 fd。"""
        os.close(fd)


def build_net_ruleset(api: LandlockApi, block_ports: tuple[int, ...]) -> int:
    """「BIND_TCP＋CONNECT_TCP：允許 0～65535 扣掉封鎖埠」的規則集；回 fd，呼叫端負責關。

    埠 0 一定允許（bind 暫時埠）。需要 ABI≥4（呼叫端先判斷）。建置約 0.1 秒（U1a 量測），
    proxy 在執行緒裡建。
    """
    blocked = set(block_ports)
    ruleset_fd = api.create_ruleset(0, NET_TCP, 0)
    try:
        for port in range(65536):
            if port not in blocked:
                api.add_port_rule(ruleset_fd, NET_TCP, port)
    except BaseException:
        api.close(ruleset_fd)
        raise
    return ruleset_fd

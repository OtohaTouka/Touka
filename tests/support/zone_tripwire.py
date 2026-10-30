"""測試期間的執行期絆線（ADR-0293 Z9）：產品程式對代理人資料夾直接做 I/O 就記一筆、讓測試紅.

以 ``sys.addaudithook`` 裝一次，全測試期間都開；只記錄、不在 hook 裡丟例外。

- 路徑：不按事件名稱篩選，每個事件的 str、bytes、PathLike 參數（含 tuple、list 裡一層）都當路徑，
  相對的以 cwd 補成絕對路徑。Linux 上 fd 經 ``/proc/self/fd`` 還原：帶 ``dir_fd`` 的事件照
  ``_DIR_FDS`` 把相對名稱接在它後面，第一個參數可以是 fd 的事件照 ``_FD_FIRST``；其他 int 是
  mode、flags 一類，不看。子行程的事件只看執行檔與 cwd（參數與 env 是交給子行程的資料）；
  ``open`` 只看第一個參數（之後是 mode、flags）；socket 事件只看 AF_UNIX 的字串位址（網路位址是
  tuple，名稱查詢的參數是主機名稱與連接埠）。工作目錄在區裡時，這些字串不會被補成區內路徑。
- 呼叫者：往上第一個不是標準函式庫、也不是 site-packages 的 frame（「模組:函式」）。檔案在
  ``tests/`` 底下的放行（測試自己建資料），不必再判斷區；``ALLOWED`` 明列的放行。
- 判定：``zone_of`` 不是 None；遞迴的 shutil 事件與 ``os.fwalk`` 另外看 ``contains_zone``。只用
  ``zone_paths`` 的函式（``use_identity=False``），根是當下的 ``current_roots()`` 加上 session
  沙盒的根。刪除與改名的最後一段是連結時只看連結本身的位置（``_LAST_NOT_FOLLOWED``）。
- 交接（R7）：交接登記表裡還沒取消的字串，只在該種類允許的事件放行。

紀錄經 ``zone_records``（種類 ``tripwire``）在測試結束時讓測試紅；``expect_zone_refusal`` 收不掉。
CI 的全量測試（``TOUKA_Z9_FULL_RUN=1``）另外要求 ``ALLOWED`` 每項都命中過（用不到的放行要刪；
``LINUX_ONLY_ALLOWED`` 只在 Linux 要求）。
"""

from __future__ import annotations

import contextlib
import os
import sys
import sysconfig
import threading
from typing import TYPE_CHECKING

from memory_server.utils.zone_paths import (
    Anchor,
    HandoffKind,
    ZonePath,
    absolute_text,
    contains_zone,
    current_roots,
    is_handed_off,
    zone_of,
)

from tests.support import zone_records

if TYPE_CHECKING:
    from memory_server.utils.zone_paths import ZoneRoots

KIND = "tripwire"
FULL_RUN_ENV = "TOUKA_Z9_FULL_RUN"
# 放行的「模組:函式」（``*`` 是整個模組）：每項的理由
ALLOWED: dict[str, str] = {
    "memory_server.utils.no_follow:*": "不跟隨連結的原語（R2、Z4）",
    "src.sandbox_kernel.owner_repair:_LinuxOps.chmod": (
        "權限自癒（禁令 66 ⑨）：逐段以 O_PATH|O_NOFOLLOW 開、核對後經 /proc/self/fd 改同一個 inode"
    ),
}
# 只在 Linux 跑得到的放行：其他平台的全量檢查不要求命中
LINUX_ONLY_ALLOWED = frozenset({"src.sandbox_kernel.owner_repair:_LinuxOps.chmod"})
_ON_LINUX = sys.platform == "linux"
# 正式環境在別的行程裡跑、不載入 RM 的模組：Linux 測試在同一個行程裡起它，堆疊裡有它的 I/O 不是
# proxy 的，不記（不是放行，全量檢查不要求命中）。proxy 只從它匯入什麼由 test_zone_tripwire 釘住
OTHER_PROCESS_MODULES: dict[str, str] = {
    "src.services.isolation.executor_server": (
        "執行器伺服器（ADR-0272 C2-2）：正式環境在 touka-executor 容器，"
        "規則路徑由 kernel 逐段不跟隨地開"
    ),
}
# 參數確定不是檔案系統路徑的事件（其他事件照舊全部檢查）：每項的理由
NOT_PATH_EVENTS: dict[str, str] = {
    "object.__getattr__": "屬性存取：參數是物件與屬性名稱",
    "object.__setattr__": "屬性存取：參數是物件與屬性名稱",
    "object.__delattr__": "屬性存取：參數是物件與屬性名稱",
    "os.putenv": "環境變數的值是資料，不是 I/O",
    "os.unsetenv": "環境變數名稱",
    "import": "模組名稱與搜尋路徑；真的讀檔另外發 open",
    "compile": "原始碼與它的標籤",
    "exec": "程式碼物件",
}
_TREE_EVENTS = frozenset(
    {"shutil.rmtree", "shutil.copytree", "shutil.make_archive", "shutil.move", "os.fwalk"}
)
# 不跟隨最後一段的事件：最後一段是連結時，刪掉或改名的是連結本身，不是它指的地方
_LAST_NOT_FOLLOWED = frozenset({"os.remove", "os.rmdir", "os.rename"})
# Windows 裝置命名空間（具名管道等）不會是區內路徑；連 lstat 一次都會佔掉管道唯一的連線，
# 讓真正的 CreateFile 失敗（WinError 231），所以一律不看
_DEVICE_PREFIXES = ("\\\\.\\", "//./")
# 子行程：執行檔與 cwd 在參數裡的位置
_SPAWN_PATHS: dict[str, tuple[int, ...]] = {
    "subprocess.Popen": (0, 2),
    "os.posix_spawn": (0,),
    "os.exec": (0,),
    "os.spawn": (1,),
    # Windows 的 subprocess.Popen 之後還有一個：（程式, 命令列, 工作目錄）
    "_winapi.CreateProcess": (0, 2),
}
# 只有這個位置是路徑的事件：{事件: 位置}（之後的參數是 mode、flags）
_PATH_ONLY_AT: dict[str, int] = {"open": 0}
# 會帶 AF_UNIX 路徑的 socket 事件（位址是第 2 個參數）；其他 socket 事件不帶路徑
_SOCKET_PREFIX = "socket."
_SOCKET_ADDRESS_EVENTS = frozenset({"socket.bind", "socket.connect", "socket.sendto"})
# 帶 dir_fd 的事件：{名稱的位置: dir_fd 的位置}
_DIR_FDS: dict[str, dict[int, int]] = {
    "os.mkdir": {0: 2},
    "os.remove": {0: 1},
    "os.rmdir": {0: 1},
    "os.chmod": {0: 2},
    "os.chown": {0: 3},
    "os.utime": {0: 3},
    "os.symlink": {1: 2},
    "os.mkfifo": {0: 2},
    "os.mknod": {0: 3},
    "os.rename": {0: 2, 1: 3},
    "os.link": {0: 2, 1: 3},
    "os.fwalk": {0: 4},
    "shutil.rmtree": {0: 1},
}
# 第一個參數可以是已開的 fd（開檔、列目錄、改屬性）
_FD_FIRST = frozenset(
    {
        "open",
        "os.listdir",
        "os.scandir",
        "os.truncate",
        "os.chdir",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.listxattr",
        "os.getxattr",
        "os.setxattr",
        "os.removexattr",
    }
)
_MAX_FD = 1 << 16
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

_local = threading.local()
_lock = threading.Lock()
_hits: set[str] = set()
_extra_roots: list[ZoneRoots] = []
_installed = False


def _library_dirs() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    names = ("stdlib", "platstdlib", "purelib", "platlib")
    return tuple(os.path.normcase(paths[n]) + os.sep for n in names if paths.get(n))


_LIBRARY_DIRS = _library_dirs()
# 測試檔以檔案位置判斷：pytest 載入的測試模組名稱不一定帶 ``tests.``
_TESTS_DIR = os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep


def install() -> None:
    """裝一次（conftest 載入時）。"""
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def add_roots(roots: ZoneRoots) -> None:
    """補充的根（session 沙盒）：``current_roots()`` 換掉之後也照樣判斷。"""
    with _lock:
        _extra_roots.append(roots)


def _roots_key(roots: ZoneRoots) -> tuple[str | None, ...]:
    return tuple(roots.literal(anchor) for anchor in Anchor)


def _roots() -> list[ZoneRoots]:
    """當下的根加上補充的根（和當下相同的不重複判斷）。"""
    current = current_roots()
    seen = {_roots_key(current)}
    found = [current]
    for roots in _extra_roots:
        key = _roots_key(roots)
        if key not in seen:
            seen.add(key)
            found.append(roots)
    return found


def missing_hits() -> list[str]:
    """沒命中過的放行項目（CI 全量測試時要是空的）。"""
    with _lock:
        unused = set(ALLOWED) - _hits
    if not _ON_LINUX:
        unused -= LINUX_ONLY_ALLOWED
    return sorted(unused)


def take_records_for_self_test() -> list[zone_records.Record]:
    """Z9 自己的測試取走這個測試的絆線紀錄（Z8 斷言只有 Z9 的測試檔能呼叫）。"""
    return zone_records.take(KIND)


def _hook(event: str, args: tuple[object, ...]) -> None:
    if getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        _check(event, args)
    except Exception as exc:  # hook 自己的錯誤也要看得到，不能讓絆線悄悄失效
        zone_records.add(KIND, "hook_error", None, f"{event}: {exc!r}")
    finally:
        _local.busy = False


def _check(event: str, args: tuple[object, ...]) -> None:
    if event in NOT_PATH_EVENTS:
        return
    paths = _paths(event, args)
    if not paths:
        return
    found = _caller()
    if found is not None and found[1].startswith(_TESTS_DIR):
        return
    caller = None if found is None else found[0]
    allowed = None if caller is None else _allowed_key(caller)
    if allowed is not None and allowed in _hits:
        return
    roots = _roots()
    for path in paths:
        if not _in_zone(event, path, roots) or _handed_off(event, path, args):
            continue
        if allowed is not None:
            with _lock:
                _hits.add(allowed)
            return
        if _from_another_process():
            return
        zone_records.add(KIND, event, path, caller or "(no product caller)")


def _in_zone(event: str, path: str, roots: list[ZoneRoots]) -> bool:
    if event in _LAST_NOT_FOLLOWED and _is_link(path):
        return any(_link_in_zone(path, zone_roots) for zone_roots in roots)
    for zone_roots in roots:
        if zone_of(path, zone_roots, use_identity=False) is not None:
            return True
        if event in _TREE_EVENTS and contains_zone(path, zone_roots):
            return True
    return False


def _is_link(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def _link_in_zone(path: str, roots: ZoneRoots) -> bool:
    """動的是連結本身：連結的字面位置在區裡，或它所在的資料夾在區裡（指去哪裡不算）。"""
    parent = os.path.dirname(absolute_text(path))
    return (
        zone_of(path, roots, use_identity=False, follow=False) is not None
        or zone_of(parent, roots, use_identity=False) is not None
    )


def _paths(event: str, args: tuple[object, ...]) -> list[str]:
    positions = _SPAWN_PATHS.get(event)
    if positions is not None:
        return [text for i in positions if i < len(args) for text in _texts(args[i])]
    if event.startswith(_SOCKET_PREFIX):
        return _socket_paths(event, args)
    only = _PATH_ONLY_AT.get(event)
    dir_fds = _DIR_FDS.get(event, {})
    skip = set(dir_fds.values())
    found: list[str] = []
    for i, value in enumerate(args):
        if i in skip or (only is not None and i != only):
            continue
        if i == 0 and event in _FD_FIRST and _is_fd(value):
            base = _fd_path(value)  # type: ignore[arg-type]
            if base is not None:
                found.append(base)
            continue
        fd = args[dir_fds[i]] if i in dir_fds and dir_fds[i] < len(args) else None
        base = _fd_path(fd) if _is_fd(fd) else None  # type: ignore[arg-type]
        for text in _texts(value):
            found.append(text if base is None or os.path.isabs(text) else os.path.join(base, text))
    return found


def _socket_paths(event: str, args: tuple[object, ...]) -> list[str]:
    """AF_UNIX 的位址是字串路徑；網路位址是 tuple，不是路徑."""
    if event not in _SOCKET_ADDRESS_EVENTS or len(args) < 2 or isinstance(args[1], (tuple, list)):
        return []
    return _texts(args[1])


def _is_fd(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _texts(value: object) -> list[str]:
    items = value if isinstance(value, (tuple, list)) else (value,)
    return [text for item in items if (text := _text(item)) is not None]


def _text(value: object) -> str | None:
    if isinstance(value, ZonePath) or not isinstance(value, (str, bytes, os.PathLike)):
        return None  # ZonePath 轉字串會先通知觀察者再丟錯，這裡不碰
    with contextlib.suppress(TypeError, ValueError):
        text = os.fsdecode(os.fspath(value))
        if text and "\x00" not in text and "\n" not in text:
            return None if text.startswith(_DEVICE_PREFIXES) else text
    return None


def _fd_path(fd: int) -> str | None:
    if sys.platform != "linux" or not 0 <= fd < _MAX_FD:
        return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _handed_off(event: str, path: str, args: tuple[object, ...]) -> bool:
    if event in _SPAWN_PATHS:
        return is_handed_off(path, HandoffKind.SUBPROCESS_CWD)
    if event == "open" and _read_only(args):
        return is_handed_off(path, HandoffKind.PROC_FD) or is_handed_off(
            path, HandoffKind.WINDOWS_FILE_RESPONSE
        )
    return False


def _read_only(args: tuple[object, ...]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str):
        return not set(mode) & set("wax+")
    return isinstance(flags, int) and not flags & _WRITE_FLAGS


def _caller() -> tuple[str, str] | None:
    """第一個不是標準函式庫、也不是 site-packages 的 frame：（「模組:函式」, 檔名）."""
    frame = sys._getframe(3)
    while frame is not None:
        filename = os.path.normcase(frame.f_code.co_filename)
        if not filename.startswith("<") and not filename.startswith(_LIBRARY_DIRS):
            module = frame.f_globals.get("__name__", "?")
            return f"{module}:{frame.f_code.co_qualname}", filename
        frame = frame.f_back
    return None


def _from_another_process() -> bool:
    """堆疊裡有 ``OTHER_PROCESS_MODULES`` 的 frame（只在要記一筆之前才走整個堆疊）."""
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get("__name__") in OTHER_PROCESS_MODULES:
            return True
        frame = frame.f_back
    return False


def _allowed_key(caller: str) -> str | None:
    module = caller.partition(":")[0]
    for key in (caller, f"{module}:*"):
        if key in ALLOWED:
            return key
    return None

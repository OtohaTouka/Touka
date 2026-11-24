"""Windows 系統呼叫（ADR-0312）。整個小幫手只有這個檔案可以用 ctypes。"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Any, Final

_DRIVE: Final = re.compile(r"[A-Za-z]:")

# shellscalingapi.h：整個程式用系統縮放比例（Tk 8.6 不處理各螢幕不同的縮放）
PROCESS_SYSTEM_DPI_AWARE: Final = 1

FO_DELETE: Final = 3
FOF_SILENT: Final = 0x0004
FOF_NOCONFIRMATION: Final = 0x0010
FOF_ALLOWUNDO: Final = 0x0040
FOF_NOERRORUI: Final = 0x0400
# 不然丟 a.html 會連 a_files 一起丟，清單上卻沒列
FOF_NO_CONNECTED_ELEMENTS: Final = 0x2000
FOF_WANTNUKEWARNING: Final = 0x4000
DELETE_FLAGS: Final = (
    FOF_ALLOWUNDO
    | FOF_NOCONFIRMATION
    | FOF_NOERRORUI
    | FOF_WANTNUKEWARNING
    | FOF_SILENT
    | FOF_NO_CONNECTED_ELEMENTS
)
EXPECTED_STRUCTURE_SIZE: Final = 56

_DRIVE_KINDS: Final = {2: "removable", 3: "fixed", 4: "remote", 5: "cdrom", 6: "ramdisk"}
_ERROR_ALREADY_EXISTS: Final = 183
_MUTEX_NAME: Final = "Local\\ToukaLocalHelper"
# 第二個小幫手用它叫出第一個的主視窗（ADR-0312 修訂，2026-11）；同一個登入工作階段才看得到
SHOW_EVENT_NAME: Final = "Local\\ToukaLocalHelperShow"
EVENT_MODIFY_STATE: Final = 0x0002
WAIT_OBJECT_0: Final = 0
ASFW_ANY: Final = 0xFFFFFFFF


class SHFILEOPSTRUCTW(ctypes.Structure):
    """``shellapi.h`` 的結構，x64 為 56 位元組。"""

    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    )


def structure_size() -> int:
    """結構的實際大小。"""
    return ctypes.sizeof(SHFILEOPSTRUCTW)


def build_from_buffer(path: str) -> str:
    """API 要完整路徑、不接受長路徑前綴，結尾要兩個 NUL。"""
    pure = PureWindowsPath(path)
    if not path or "\0" in path or _DRIVE.fullmatch(pure.drive) is None or pure.root != "\\":
        msg = "unsafe path"
        raise ValueError(msg)
    return f"{path}\0\0"


def interpret_delete(code: int, aborted: int) -> str:
    """回傳 0 仍可能被中止，兩個都要看。"""
    if code != 0:
        return "failed"
    return "aborted" if aborted else "ok"


def drive_kind_name(code: int) -> str:
    """``GetDriveTypeW`` 的回傳值換成名稱。"""
    return _DRIVE_KINDS.get(code, "unknown")


def drive_type(anchor: str) -> str:
    """磁碟的種類。"""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return drive_kind_name(kernel32.GetDriveTypeW(anchor))


class RecycleBin:
    """丟到回收桶，一次一項。"""

    def send(self, path: str) -> str:
        """回傳 ``ok``、``aborted`` 或 ``failed``。"""
        if structure_size() != EXPECTED_STRUCTURE_SIZE:
            return "failed"
        try:
            buffer = ctypes.create_unicode_buffer(build_from_buffer(path))
        except ValueError:
            return "failed"
        operation = SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=FO_DELETE,
            pFrom=ctypes.cast(buffer, wintypes.LPCWSTR),
            pTo=None,
            fFlags=DELETE_FLAGS,
        )
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = shell32.SHFileOperationW(ctypes.byref(operation))
        return interpret_delete(code, operation.fAnyOperationsAborted)


def _load_dpi_libraries() -> tuple[Any | None, Any] | None:
    """縮放用的兩個系統程式庫；不是 Windows 時回 None，舊版 Windows 沒有 shcore。"""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32")
    except OSError:
        return None
    try:
        shcore = ctypes.WinDLL("shcore")
    except OSError:
        shcore = None
    return shcore, user32


def enable_dpi_awareness() -> bool:
    """告訴 Windows 小幫手會自己處理縮放，字才不會被當成圖片放大而變糊。

    要在建立任何視窗之前呼叫。做不到也只是字比較糊，不影響服務，所以不丟例外。
    """
    libraries = _load_dpi_libraries()
    if libraries is None:
        return False
    shcore, user32 = libraries
    if shcore is not None:
        try:
            shcore.SetProcessDpiAwareness(PROCESS_SYSTEM_DPI_AWARE)
        except (OSError, AttributeError):
            pass
        else:
            return True
    try:
        user32.SetProcessDPIAware()
    except (OSError, AttributeError):
        return False
    return True


def acquire_single_instance(name: str = _MUTEX_NAME) -> bool:
    """同一個帳號只能開一個小幫手。把 handle 留到行程結束。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, name)
    return bool(handle) and ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def _event_kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def create_show_event(name: str = SHOW_EVENT_NAME) -> int | None:
    """第一個小幫手建立「叫出主視窗」的事件，handle 留到行程結束。

    已經有同名的事件就不是自己的（別的程式先建了），不用它：回 None，主視窗照舊顯示。
    """
    if sys.platform != "win32":
        return None
    kernel32 = _event_kernel32()
    # 自動重設：連續幾次通知只會叫出一次
    handle = kernel32.CreateEventW(None, False, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def signal_show_event(name: str = SHOW_EVENT_NAME) -> bool:
    """第二個小幫手請第一個叫出主視窗；打不開事件（例如舊版在跑）回 False。"""
    if sys.platform != "win32":
        return False
    kernel32 = _event_kernel32()
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        # 打開事件之後才讓出前景，第一個小幫手才能把主視窗帶到最前面
        user32 = ctypes.WinDLL("user32")
        user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow(ASFW_ANY)
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def consume_show_event(handle: int | None) -> bool:
    """有沒有人請小幫手叫出主視窗；不會等。"""
    if handle is None or sys.platform != "win32":
        return False
    kernel32 = _event_kernel32()
    return kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0

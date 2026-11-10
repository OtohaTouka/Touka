"""Windows 系統呼叫（ADR-0311）。整個小幫手只有這個檔案可以用 ctypes。"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Final

_DRIVE: Final = re.compile(r"[A-Za-z]:")

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


def acquire_single_instance() -> bool:
    """同一個帳號只能開一個小幫手。把 handle 留到行程結束。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    return bool(handle) and ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

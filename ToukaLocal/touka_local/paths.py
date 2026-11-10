"""路徑判斷的純函式（ADR-0312）：一律照 Windows 的規則，任何系統上都測得到。"""

from __future__ import annotations

import ntpath
import re
import unicodedata
from pathlib import PureWindowsPath
from typing import Final

from touka_local.errors import Refused

MAX_PATH_CHARS: Final = 259
MAX_REQUEST_PATH_CHARS: Final = 1024
MAX_SEGMENT_CHARS: Final = 255

_FORBIDDEN_CHARS: Final = frozenset('<>:"|?*/\\')
_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
# Unicode 的 Default_Ignorable_Code_Point：字型通常什麼都不畫
_DEFAULT_IGNORABLE: Final = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# 變體選擇符和零寬連接字：Windows 表情面板打出的 emoji 常帶著，既有檔名要能用，新名稱不行
_EMOJI_PARTS: Final = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x200D, 0x200D))
# 長得像路徑分隔線的字元：全形斜線、數學符號、方框線
_LOOKALIKE_SEPARATORS: Final = frozenset("／＼⁄∕⧸⧹∖﹨｜∣ǀ❘￨╱╲")
# 逐層檢查時會在路徑後面接 \.git，要留位置
_GIT_PROBE_CHARS: Final = len("\\.git")
_NUMBERED_SUFFIXES: Final = "0123456789¹²³"
_RESERVED_NAMES: Final = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{suffix}" for suffix in _NUMBERED_SUFFIXES),
        *(f"lpt{suffix}" for suffix in _NUMBERED_SUFFIXES),
    }
)
_SHORT_NAME: Final = re.compile(r"~[0-9]+(\.[^.]*)?\Z")
# 名稱結尾是 .{CLSID} 的資料夾，檔案總管會當成別的東西顯示
_SHELL_FOLDER_SUFFIX: Final = re.compile(r"\.\{[^.]*\}\Z")
_DRIVE: Final = re.compile(r"[A-Za-z]:")


def is_hidden_character(character: str) -> bool:
    """控制字元、雙向文字控制、看不見的格式字元。"""
    code = ord(character)
    return unicodedata.category(character) in _HIDDEN_CATEGORIES or any(
        low <= code <= high for low, high in _DEFAULT_IGNORABLE
    )


def _blocks_existing_name(character: str) -> bool:
    code = ord(character)
    return is_hidden_character(character) and not any(
        low <= code <= high for low, high in _EMOJI_PARTS
    )


def is_misleading_character(character: str) -> bool:
    """長得像路徑分隔線或一般空白，人看路徑時會看錯。"""
    return character in _LOOKALIKE_SEPARATORS or (
        character != " " and unicodedata.category(character) == "Zs"
    )


def is_too_long(path: str) -> bool:
    """Windows 算的是 UTF-16 單位，emoji 佔 2。"""
    units = len(path.encode("utf-16-le", "surrogatepass")) // 2
    return units + _GIT_PROBE_CHARS > MAX_PATH_CHARS


def check_segment_syntax(name: str) -> None:
    """一段名稱在 Windows 上要有唯一的意思，有歧義就拒絕。"""
    if (
        not name
        or len(name) > MAX_SEGMENT_CHARS
        or name[0] == " "
        or name[-1] in " ."
        or any(c in _FORBIDDEN_CHARS or _blocks_existing_name(c) for c in name)
        or name.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED_NAMES
        or _SHORT_NAME.search(name) is not None
        or _SHELL_FOLDER_SUFFIX.search(name) is not None
    ):
        raise Refused("bad_path")


def split_request_path(raw: object) -> tuple[str, tuple[str, ...]]:
    """把「開放名稱/相對路徑」拆成開放名稱和各段。"""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_REQUEST_PATH_CHARS:
        raise Refused("bad_path")
    segments = raw.split("/")
    for segment in segments:
        check_segment_syntax(segment)
    return segments[0], tuple(segments[1:])


def is_plain_absolute(path: object) -> bool:
    """像磁碟代號開頭的本機絕對路徑；網路路徑、長路徑前綴、沒有根的都不算。"""
    if not isinstance(path, str) or not path or "\0" in path:
        return False
    pure = PureWindowsPath(path)
    return _DRIVE.fullmatch(pure.drive) is not None and pure.root == "\\"


def normalize_folder(path: object) -> str:
    """使用者選的資料夾：要是本機絕對路徑，每一段都要合規則。"""
    if not is_plain_absolute(path):
        raise Refused("bad_path")
    pure = PureWindowsPath(str(path))
    for part in pure.parts[1:]:
        check_segment_syntax(part)
    return str(pure)


def join(base: str, *parts: str) -> str:
    """組 Windows 路徑。"""
    return ntpath.join(base, *parts)


def parts_of(path: str) -> tuple[str, ...]:
    """路徑各段，第一段是磁碟根。"""
    return PureWindowsPath(path).parts


def anchor_of(path: str) -> str:
    """路徑所在的磁碟根。"""
    return PureWindowsPath(path).anchor


def folded_key(path: str) -> tuple[str, ...]:
    """比對用的 key：各段轉成不分大小寫，前綴相同就是上下層關係。"""
    return tuple(part.casefold() for part in parts_of(path))


def same_path(first: str, second: str) -> bool:
    """同一個路徑（不分大小寫）。"""
    return folded_key(first) == folded_key(second)


def is_same_or_inside(path: str, parent: str) -> bool:
    """``path`` 等於 ``parent`` 或在它底下。"""
    return PureWindowsPath(path.casefold()).is_relative_to(PureWindowsPath(parent.casefold()))


def related(first: str, second: str) -> bool:
    """其中一個等於另一個、在它底下或是它的上層。"""
    return is_same_or_inside(first, second) or is_same_or_inside(second, first)

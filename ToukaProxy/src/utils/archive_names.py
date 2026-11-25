"""zip 裡的名稱：哪些不能放、怎麼比大小寫、報告裡怎麼顯示（ADR-0332 PR B；禁令 18 的純函式）.

給下載專案用：名稱來自代理人寫得到的 git 名單，解壓的人可能用 Windows、macOS 或 Linux。

- ``name_problem``：不能放的原因（代碼），可以放回 ``None``。
- ``case_key``：解壓到不分大小寫的磁碟時，名稱比對用的鍵。
- ``shown``／``shown_raw``：報告裡顯示的名稱（跳脫看不見或會騙人的字元、截短）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from src.utils.hard_floor import is_env_file_name

if TYPE_CHECKING:
    from collections.abc import Sequence

# 不能放的原因
GIT_FOLDER = "git_folder"
VCS_METADATA = "vcs_metadata"
BAD_NAME = "bad_name"
BIDI = "bidi"
SHORTCUT = "shortcut"
ENV_FILE = "env_file"

MAX_SHOWN_CHARS = 300  # 報告裡一個名稱最多顯示幾個字（再加跳脫碼）

_GIT_NAME = re.compile(r"\.git|git~\d+")
# HFS+ 比對名稱時忽略的字元（和 git 的 is_hfs_dotgit 同一份）
_HFS_IGNORED = frozenset(
    "‌‍‎‏‪‫‬‭‮"
    "⁪⁫⁬⁭⁮⁯﻿"
)
# 解壓後會讓資料夾變成 repository 的：git 找 repository 一定要有 HEAD；其他版本控制工具的資料夾與
# checkout 檔（含 Windows 的短名稱，解壓蓋到既有的 checkout 時會寫進去）
_REPOSITORY_FILE = "head"
_VCS_NAME = re.compile(
    r"\.(?:hg|svn|bzr|jj|sl|pijul|fslckout)|_darcs|_fossil_|(?:hg|svn|bzr|jj|sl|pijul|fslcko)~\d+"
)
_WINDOWS_BAD_CHARS = frozenset('<>:"|?*\\')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789¹²³"}
)
_BIDI = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
_SHORTCUT_NAMES = frozenset({"desktop.ini", ".directory"})
_SHORTCUT_SUFFIXES = (
    ".lnk", ".url", ".scf", ".library-ms", ".searchconnector-ms", ".theme", ".themepack",
    ".deskthemepack", ".website", ".desktop",
)
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
_SURROGATE_BYTES = range(0xDC80, 0xDD00)  # surrogateescape 解不開的位元組


def _folded(part: str) -> str:
    """Windows、macOS 當成同一個的寫法：NFC、去掉 HFS+ 忽略的字元、不分大小寫、去掉結尾點與空白."""
    text = "".join(ch for ch in unicodedata.normalize("NFC", part) if ch not in _HFS_IGNORED)
    return text.casefold().rstrip(" .")


def _is_bad_part(part: str) -> bool:
    if part in {"", ".", ".."} or part[-1] in " .":
        return True
    if any(ch in _WINDOWS_BAD_CHARS or unicodedata.category(ch) == "Cc" for ch in part):
        return True
    return part.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES


def name_problem(parts: Sequence[str]) -> str | None:
    """``/`` 分開的路徑不能放進 zip 的原因；可以放回 ``None``."""
    folded = [_folded(part) for part in parts]
    if any(_GIT_NAME.fullmatch(part) for part in folded):
        return GIT_FOLDER
    if folded[-1] == _REPOSITORY_FILE or any(_VCS_NAME.fullmatch(part) for part in folded):
        return VCS_METADATA
    if any(_is_bad_part(part) for part in parts):
        return BAD_NAME
    if any(ch in _BIDI for part in parts for ch in part):
        return BIDI
    name = parts[-1].casefold()
    if name in _SHORTCUT_NAMES or name.endswith(_SHORTCUT_SUFFIXES):
        return SHORTCUT
    if is_env_file_name(name):
        return ENV_FILE
    return None


def case_key(text: str) -> str:
    """解壓到不分大小寫的磁碟時比對用的鍵."""
    return unicodedata.normalize("NFC", text).casefold()


def shown(text: str) -> str:
    """報告裡顯示的名稱：截短；控制、格式、分隔字元與解不開的位元組都寫成跳脫碼."""
    cut = len(text) > MAX_SHOWN_CHARS
    out: list[str] = []
    for ch in text[:MAX_SHOWN_CHARS]:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif code in _SURROGATE_BYTES:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(ch)
    return "".join(out) + ("..." if cut else "")


def shown_raw(raw: bytes) -> str:
    """不一定是 UTF-8 的名稱：解不開的位元組寫成跳脫碼（反斜線、x、兩位十六進位）."""
    return shown(raw.decode("utf-8", "surrogateescape"))

"""檔名規則（ADR-0312）：受保護的資料夾、能新建什麼、搬移時不能改什麼。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from touka_local.errors import Refused
from touka_local.paths import is_hidden_character, is_misleading_character

if TYPE_CHECKING:
    from collections.abc import Sequence

GIT_FOLDER: Final = ".git"

_PROTECTED_NAMES: Final = frozenset(
    {
        GIT_FOLDER,
        ".github",
        ".ssh",
        ".aws",
        ".gnupg",
        ".vscode",
        ".idea",
        ".claude",
        ".codex",
        ".agents",
        ".kube",
        ".docker",
    }
)
_CREATABLE_EXTENSIONS: Final = frozenset({".txt", ".md", ".log"})
# AI 工具會自動讀的指示檔，不讓代理人寫進去
_FORBIDDEN_FILE_NAMES: Final = frozenset(
    {
        "claude.md",
        "claude.local.md",
        "agents.md",
        "agents.override.md",
        "agent.md",
        "gemini.md",
        "skill.md",
        "cmakelists.txt",  # 建置工具會當程式執行
    }
)
# 只減少誤讀，不是邊界：邊界是使用者開放了哪些資料夾
_SECRET_NAMES: Final = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        "_netrc",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "wallet.dat",
    }
)
_SECRET_EXTENSIONS: Final = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".kdbx", ".ppk", ".jks", ".keystore", ".ovpn", ".env"}
)
_ENV_PREFIX: Final = ".env."
_ENV_EXAMPLES: Final = frozenset({".env.example", ".env.sample", ".env.template"})
_UNUSUAL_EXTENSIONS: Final = frozenset(
    {
        ".exe",
        ".bat",
        ".cmd",
        ".com",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".msi",
        ".msp",
        ".lnk",
        ".scr",
        ".cpl",
        ".pif",
        ".jar",
        ".reg",
        ".dll",
        ".py",
        ".pyw",
        ".url",
        ".html",
        ".htm",
        ".chm",
        ".msc",
        ".inf",
        ".docm",
        ".xlsm",
        ".ocx",
        ".sys",
        ".appinstaller",
        ".library-ms",
    }
)


def _extension(name: str) -> str:
    """比照 Windows：最後一個點起算，名稱開頭的點也算（``.exe`` 的副檔名就是 ``.exe``）。"""
    _, dot, suffix = name.rpartition(".")
    return f".{suffix}".casefold() if dot else ""


def is_protected_name(name: str) -> bool:
    """這個名稱的資料夾不進入、不搬、不刪。"""
    return name.casefold() in _PROTECTED_NAMES


def check_segment_allowed(name: str) -> None:
    """路徑裡的每一段都不能是受保護的名稱。"""
    if is_protected_name(name):
        raise Refused("protected")


def check_new_segments(parts: Sequence[str], *, kept_name: str | None = None) -> None:
    """新建或搬到的路徑。

    點開頭的常是工具自動讀的設定，整條路徑都不行。形似分隔線、空白或看不見的字元會讓人看錯路徑，
    只擋真的新取的名稱：上層資料夾早就存在，搬移時沿用原檔名也不算新名稱。
    """
    if any(name.startswith(".") for name in parts):
        raise Refused("file_name")
    last = parts[-1]
    if last != kept_name and any(
        is_misleading_character(c) or is_hidden_character(c) for c in last
    ):
        raise Refused("file_name")


def check_created_file_name(name: str) -> None:
    """新建的檔案只能是純文字類，也不能是 AI 工具會讀的指示檔。"""
    if name.casefold() in _FORBIDDEN_FILE_NAMES:
        raise Refused("file_name")
    if _extension(name) not in _CREATABLE_EXTENSIONS:
        raise Refused("file_type")


def check_move_names(source_name: str, destination_name: str) -> None:
    """搬移不能改副檔名，目的地也不能叫指示檔的名字。"""
    if destination_name.casefold() in _FORBIDDEN_FILE_NAMES:
        raise Refused("file_name")
    if _extension(source_name) != _extension(destination_name):
        raise Refused("extension_changed")


def is_secret_name(name: str) -> bool:
    """看起來是金鑰或密碼檔。"""
    folded = name.casefold()
    if folded in _ENV_EXAMPLES:
        return False
    return (
        folded in _SECRET_NAMES
        or folded.startswith(_ENV_PREFIX)
        or _extension(name) in _SECRET_EXTENSIONS
    )


def is_unusual_file(name: str) -> bool:
    """可執行或腳本類的副檔名，核准時要特別標出來。"""
    return _extension(name) in _UNUSUAL_EXTENSIONS

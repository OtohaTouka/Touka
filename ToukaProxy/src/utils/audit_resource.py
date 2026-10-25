"""政策稽核帳本的 resource 欄寫法（ADR-0283）。

``stats/decisions.csv`` 每次權限判斷記一列。終端指令只記程式名稱和長度，參數和值一律
不記：指令裡常帶金鑰，要靠樣式認出所有寫法做不到。其他動作記的是工具名稱；不像工具名稱
的內容只記長度。權限判斷本身照舊看完整原文，這裡只決定寫進檔案的那一份。

認出金鑰樣式的 SSOT 仍是 ``utils/secrets.py``；這裡不另寫樣式，只在要照記的名稱上再過
一次它的遮罩。
"""

from __future__ import annotations

import re

from src.utils.secrets import redact_secrets_from_content

TERMINAL_ACTION = "terminal_execute"
COMMAND_UNAVAILABLE = "[command unavailable]"
RESOURCE_UNAVAILABLE = "[resource unavailable]"
UNKNOWN_PROGRAM = "?"

# 只照 shell 的方式切段：ASCII 空白、tab、換行。str.split() 會在 Unicode 空白與 \r
# 等控制字元切開，設定值的後段就可能被當成程式名稱；這些字元留在段落裡，整段比對失敗。
_SEPARATOR_RE = re.compile(r"[ \t\n]+")
_SEPARATOR_CHARS = " \t\n"
# 開頭的 POSIX 環境變數設定：值只收不會跨段的字元，其他寫法（引號、反斜線、$ 等）就停。
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[A-Za-z0-9_./:@%+,-]*")
# 程式段落整段的字元集（不含 =，不合法的設定也停在這裡），以及取出的程式名稱；
# 都用明確的 ASCII 範圍，不用 \w。
_PROGRAM_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\:+-]+")
_PATH_SEPARATOR_RE = re.compile(r"[/\\]")
_PROGRAM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,200}")


def _program_name(command: str) -> str:
    for token in _SEPARATOR_RE.split(command.strip(_SEPARATOR_CHARS)):
        if _ASSIGNMENT_RE.fullmatch(token):
            # `NAME= 值 程式` 會把「值」當程式執行，那一段常常就是手誤的祕密。
            if token.endswith("="):
                return UNKNOWN_PROGRAM
            continue
        if not _PROGRAM_TOKEN_RE.fullmatch(token):
            return UNKNOWN_PROGRAM
        name = _PATH_SEPARATOR_RE.split(token)[-1]
        if not _PROGRAM_NAME_RE.fullmatch(name):
            return UNKNOWN_PROGRAM
        _, redacted = redact_secrets_from_content(name)
        return UNKNOWN_PROGRAM if redacted else name
    return UNKNOWN_PROGRAM


def summarize_command_for_audit(command: str) -> str:
    """終端指令只留程式名稱和長度：``[command program=<名稱或?> chars=<數字>]``。"""
    try:
        return f"[command program={_program_name(command)} chars={len(command)}]"
    except Exception:  # 任何失敗都不讓原文落地
        return COMMAND_UNAVAILABLE


def audit_resource_for(action: str, resource: str) -> str:
    """依動作挑寫進帳本的 resource；不改傳入的值。"""
    if action == TERMINAL_ACTION:
        return summarize_command_for_audit(resource)
    try:
        if not _TOOL_NAME_RE.fullmatch(resource):
            return f"[resource chars={len(resource)}]"
        redacted, _ = redact_secrets_from_content(resource)
    except Exception:  # 任何失敗都不讓原文落地
        return RESOURCE_UNAVAILABLE
    return redacted

"""代理人撰寫指令的純函式安全檢查（終端與互審蒐證共用）。"""

from __future__ import annotations

import re
import sys

# C5 fix: detect shell chaining/injection metacharacters
# Matches: ; | ` \n \r && || $(...) <(...) >(...)
CHAIN_METACHAR_RE = re.compile(r"[;|`\n\r]|&&|\|\||[$][(]|[<>][(]")

# 指令政策的合法執行結果；其餘（DENY、未知值）一律視為拒絕（fail-closed）。
RUNNABLE_STRATEGIES = frozenset({"ALLOW", "CONFIRM"})
_WINDOWS_SHELLS = frozenset({"powershell", "wsl"})  # Windows 上 execute_command 的 env

# Auto 模式下只有人能核准的動態 shell 寫法（決定 4 丙，ADR-0246）：讓「看到的」和
# 「實際跑的」不一樣的變數、子指令、PowerShell 編碼參數、eval，以及把輸出寫進檔案。
# 引號、簡單管線、萬用字元不在此列（之後交給審查員）。盡力而為、寧嚴勿鬆。
DYNAMIC_SHELL_RE = re.compile(
    r"\$[\w{(?$@*#!]"  # $VAR ${VAR} $(cmd) $env:X $? $@ …
    r"|%\w+%"  # cmd 的 %VAR%
    r"|`"  # 反引號子指令（PowerShell 的跳脫字元也一併算）
    r"|[<>]\("  # 程序替換 <(cmd) >(cmd)
    r"|>(?!&|\s*(?:/dev/null|nul)(?:$|[\s;&|)]))"  # 寫進檔案的 > >>（2>&1、/dev/null 不算）
    r"|(?:^|[\s;&|(])(?:eval|invoke-expression|iex)(?=$|[\s;&|)])"
    r"|\b(?:powershell|pwsh)(?:\.exe)?\b.*?\s[-/]{1,2}e(?:c|n[a-z]*)?(?=$|\s)",
    re.IGNORECASE,
)


def effective_shell(env: object = None) -> str:
    """實際跑指令的 shell（terminal 與 Auto 審查員共用）：非 Windows 一律 bash，Windows 照 env。"""
    if sys.platform != "win32":
        return "bash"
    shell = str(env or "powershell")
    return shell if shell in _WINDOWS_SHELLS else f"unsupported:{shell}"


def has_chaining_metacharacters(command: str) -> bool:
    """Return True if `command` contains shell chaining/injection metacharacters.

    These characters allow injecting additional commands after an otherwise
    safe-looking prefix (e.g., `ls; rm -rf /`). Their presence in a command
    that the policy would auto-approve is a prompt-injection red flag.
    """
    return bool(CHAIN_METACHAR_RE.search(command))


def has_dynamic_shell_syntax(command: str) -> bool:
    """Auto 模式下只有人能核准的動態 shell 寫法（見 ``DYNAMIC_SHELL_RE``）。"""
    return bool(DYNAMIC_SHELL_RE.search(command))

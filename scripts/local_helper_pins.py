"""本機小幫手一行指令安裝的釘住值（ADR-0329）。

印出 ``ToukaLocal/install.ps1`` 該有的雜湊清單，以及 README 那一行指令。測試對不上時，
照這裡印出來的改。只讀檔、跑唯讀的 git；用 AST 讀 ``ALLOWED_FILES``，不匯入小幫手的程式。

    python scripts/local_helper_pins.py              印 install.ps1 的雜湊清單
    python scripts/local_helper_pins.py --commit C   另外印指向 commit C 的那一行
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
LOCAL: Final = ROOT / "ToukaLocal"
INSTALLER: Final = LOCAL / "install.py"
BOOTSTRAP: Final = LOCAL / "install.ps1"
BOOTSTRAP_IN_REPO: Final = "ToukaLocal/install.ps1"
RAW_BASE: Final = "https://raw.githubusercontent.com/OtohaTouka/Touka"
# README 那一行的樣板：只有 <C>（40 字 commit）與 <H>（install.ps1 的 sha256）能變
ONE_LINE_TEMPLATE: Final = (
    '$d=Join-Path $env:TEMP ("touka-helper-"+[guid]::NewGuid()); '
    "New-Item -ItemType Directory $d | Out-Null; "
    "$f=Join-Path $d 'install.ps1'; "
    "[Net.ServicePointManager]::SecurityProtocol="
    "[Net.ServicePointManager]::SecurityProtocol -bor 3072; "
    f"Invoke-WebRequest -UseBasicParsing '{RAW_BASE}/<C>/{BOOTSTRAP_IN_REPO}' -OutFile $f; "
    "if ((Get-FileHash $f -Algorithm SHA256).Hash -eq '<H>') "
    "{ powershell -NoProfile -ExecutionPolicy Bypass -File $f -Commit '<C>' -WorkDir $d } "
    "else { Remove-Item -Recurse $d; Write-Error 'install.ps1 does not match' }"
)


def allowed_files() -> tuple[str, ...]:
    """install.py 的 ALLOWED_FILES（用 AST 讀，不執行它）。"""
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "ALLOWED_FILES" and node.value:
            return tuple(ast.literal_eval(node.value))
    msg = "install.py 裡找不到 ALLOWED_FILES"
    raise SystemExit(msg)


def bootstrap_files() -> tuple[str, ...]:
    """一行指令要下載的小幫手檔案：程式本身加上安裝程式。"""
    return (*allowed_files(), "install.py")


def lf_sha256(data: bytes) -> str:
    """GitHub 上的檔案是 LF；Windows 的工作目錄可能是 CRLF，先換回 LF 再算。"""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest().upper()


def file_sha256(relative: str) -> str:
    """ToukaLocal 底下一個檔目前內容的 sha256。"""
    return lf_sha256((LOCAL / relative).read_bytes())


def hash_block() -> str:
    """install.ps1 裡 $HelperFiles 那一段該有的樣子。"""
    lines = ["$HelperFiles = [ordered]@{"]
    lines += [f"    '{relative}' = '{file_sha256(relative)}'" for relative in bootstrap_files()]
    lines.append("}")
    return "\n".join(lines)


def one_line(commit: str, sha256: str) -> str:
    """README 裡的那一行。"""
    return ONE_LINE_TEMPLATE.replace("<C>", commit).replace("<H>", sha256)


def blob_at(commit: str, path: str) -> bytes:
    """某個 commit 上某個檔的內容（git blob，GitHub raw 給的就是它）。"""
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def main(argv: list[str] | None = None) -> int:
    """印出釘住值。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", help="README 要指向的 40 字 commit")
    arguments = parser.parse_args(argv)
    print(hash_block())
    if arguments.commit:
        sha256 = hashlib.sha256(blob_at(arguments.commit, BOOTSTRAP_IN_REPO)).hexdigest().upper()
        print()
        print(one_line(arguments.commit, sha256))
    return 0


if __name__ == "__main__":
    sys.exit(main())

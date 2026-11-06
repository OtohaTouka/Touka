"""ToukaChat 的開發模式不再啟動終端伺服器（ADR-0294）.

舊的 ``npm run dev`` 會一起啟動 ``src/lib/terminal_server.mjs``：不認證、接受任何網站的連線、
開在所有網路介面上，任何網頁或同網段的電腦都能叫它開終端執行指令。產品程式沒有用到它。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_ROOT = REPO_ROOT / "ToukaChat"
PACKAGE_JSON = CHAT_ROOT / "package.json"

# dev 指令只准這幾段（以 && 串起）：先產生 API 型別與 artifact runtime，最後只開 Next 開發伺服器
ALLOWED_DEV_STEPS = (
    "npm run sync-api",
    "npm run gen:artifact-runtime",
    "next dev -p 3001",
)
# 會開伺服器或終端的套件；src 裡的程式不得匯入
SERVER_MODULES = ("socket.io", "node-pty")
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def _scripts() -> dict[str, str]:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]


def test_dev_script_only_starts_next_dev():
    steps = tuple(step.strip() for step in _scripts()["dev"].split("&&"))
    assert steps == ALLOWED_DEV_STEPS


def test_no_script_starts_the_terminal_server():
    for name, command in _scripts().items():
        assert "terminal_server" not in command, name
        assert "concurrently" not in command, name


def test_terminal_server_file_is_gone():
    assert not (CHAT_ROOT / "src" / "lib" / "terminal_server.mjs").exists()


def test_chat_source_does_not_import_server_modules():
    pattern = re.compile(
        r"""(?:from\s+|require\(\s*|import\(\s*)["'](%s)["']""" % "|".join(map(re.escape, SERVER_MODULES))
    )
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (CHAT_ROOT / "src").rglob("*")
        if path.suffix in SOURCE_SUFFIXES and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []

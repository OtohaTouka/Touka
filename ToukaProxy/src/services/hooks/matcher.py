"""Hook 比對規則（照 Claude Code，ADR-0292）.

- 空字串或 ``*``：全部符合。
- 只有字母、數字、``_``、``-``、空白、``,``、``|``：精確字串，或用 ``|``／``,`` 分隔的清單。
- 其他：Python 正規表示式，用 ``re.search``（不錨定）、區分大小寫。

``values`` 是同一個工具的所有名稱（例如 MCP 工具同時有 Touka 名稱與 ``mcp__server__tool``），
任一個符合就算符合。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_PLAIN = re.compile(r"[A-Za-z0-9_\-\s,|]*")
_SPLIT = re.compile(r"[|,]")


def matcher_matches(matcher: str, values: Iterable[str]) -> bool:
    """``matcher`` 是否符合 ``values`` 中任一個名稱。"""
    names = [value for value in values if value]
    if matcher in ("", "*"):
        return True
    if not names:
        return False
    if _PLAIN.fullmatch(matcher):
        wanted = {part.strip() for part in _SPLIT.split(matcher) if part.strip()}
        return any(name in wanted for name in names)
    pattern = re.compile(matcher)
    return any(pattern.search(name) for name in names)

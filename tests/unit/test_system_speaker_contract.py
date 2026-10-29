"""系統寫進對話的 user 訊息標記（雙端）：心跳寫入、後端判斷、前端導覽列都用同一個名字。

心跳的「請繼續」nudge 以 role="user"、name=SYSTEM_SPEAKER 寫進對話；後端 user_intent 用它判斷
「不是人話」，前端 ToukaChat/src/lib/chat/messageNavigation.ts 用它把 nudge 排除在訊息導覽列外。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.utils import user_intent

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "ToukaChat" / "src" / "lib" / "chat" / "messageNavigation.ts"
_HEARTBEAT = _ROOT / "ToukaProxy" / "src" / "routers" / "heartbeat.py"


def test_frontend_constant_matches_backend() -> None:
    source = _FRONTEND.read_text(encoding="utf-8")
    match = re.search(r'export const SYSTEM_SPEAKER = "([^"]*)";', source)
    assert match is not None
    assert match.group(1) == user_intent.SYSTEM_SPEAKER


def _user_append_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append_node"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "user"
        ):
            calls.append(node)
    return calls


def test_heartbeat_writes_nudges_with_the_shared_name() -> None:
    calls = _user_append_calls(ast.parse(_HEARTBEAT.read_text(encoding="utf-8")))
    assert len(calls) == 2  # 脈衝補給與自主行動階段完成兩種 nudge
    for call in calls:
        names = [kw.value for kw in call.keywords if kw.arg == "name"]
        assert len(names) == 1
        value = names[0]
        assert isinstance(value, ast.Attribute)
        assert isinstance(value.value, ast.Name)
        assert (value.value.id, value.attr) == ("user_intent", "SYSTEM_SPEAKER")

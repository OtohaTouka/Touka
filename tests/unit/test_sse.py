"""P1-D：SSE 傳輸序列化 SSOT helper——TDD.

``sse_line`` 收斂 handler 遍地重複的 ``f"data: {json.dumps(ev)}\\n\\n"``。以
``ensure_ascii=False`` 輸出（傳輸為 UTF-8、前端 JSON.parse 與轉義無關、省 payload），
純函式無副作用。
"""

from __future__ import annotations

import json

from src.utils.sse import sse_line


def test_sse_line_format_roundtrip():
    line = sse_line({"type": "x", "n": 1})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = line[len("data: ") :].rstrip("\n")
    assert json.loads(payload) == {"type": "x", "n": 1}


def test_sse_line_non_ascii_not_escaped():
    line = sse_line({"msg": "工具執行失敗"})
    # ensure_ascii=False：中文不轉 \uXXXX
    assert "工具執行失敗" in line
    # 仍是合法 JSON
    payload = line[len("data: ") :].rstrip("\n")
    assert json.loads(payload)["msg"] == "工具執行失敗"

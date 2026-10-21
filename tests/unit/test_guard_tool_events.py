"""P1-C：Tool-Event Envelope Guard（第 38 道守衛）自測——TDD.

守衛強制 SSE 工具事件（tool_start/tool_result/tool_stream）走 tool_events 封套；
判別器須能區分「SSE 事件（帶字面 id）」與「Anthropic tool_result 訊息區塊
（帶 tool_use_id）」，避免誤判。並回歸驗證實際 src 樹已零違規（P1-B 已全轉封套）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GUARD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "guard_tool_events.py"
_spec = importlib.util.spec_from_file_location("guard_tool_events", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
# 先註冊進 sys.modules，@dataclass 才能解析字串註解（from __future__ import annotations）
sys.modules["guard_tool_events"] = guard
_spec.loader.exec_module(guard)


def test_flags_raw_sse_tool_result_literal():
    src = 'x = {"type": "tool_result", "id": cid, "result": r, "status": "success"}'
    violations = guard.find_tool_event_literals(src)
    assert len(violations) == 1
    assert violations[0].event_type == "tool_result"


def test_flags_tool_start_and_stream_literals():
    src = (
        'a = {"type": "tool_start", "id": c, "tool": n, "args": {}}\n'
        'b = {"type": "tool_stream", "id": c, "chunk": t}\n'
    )
    types = {v.event_type for v in guard.find_tool_event_literals(src)}
    assert types == {"tool_start", "tool_stream"}


def test_does_not_flag_anthropic_tool_result_block():
    """Anthropic 訊息區塊：tool_use_id + content、無 id → 不得誤判。"""
    src = '{"type": "tool_result", "tool_use_id": tid, "content": str(c)}'
    assert guard.find_tool_event_literals(src) == []


def test_does_not_flag_builder_usage():
    src = "await emit(tool_stream_event(call_id=cid, chunk=text))"
    assert guard.find_tool_event_literals(src) == []


def test_real_source_tree_is_clean():
    """實際 src 樹（排除 SSOT 封套）應零違規——P1-B 已全數轉封套。"""
    assert guard.run_all() == []

"""進度事件純函式建構器 (progress_events) 單元測試。

P1 結構化進度頻道：驗證各 kind 的事件外層一致 (type=touka_progress)、
欄位正確，且為純函式（同輸入同輸出、每次回傳新 dict、無共享狀態）。
"""
from __future__ import annotations

from src.utils.progress_events import (
    PROGRESS_EVENT_TYPE,
    creation_stage_event,
    group_event,
    react_slots_event,
    repair_event,
    token_usage_event,
    tool_retry_event,
)


def test_all_events_share_outer_envelope():
    events = [
        react_slots_event(1, 5),
        repair_event(1, 3),
        group_event(1, 3, "Alice"),
        creation_stage_event("VALIDATE", "running"),
        tool_retry_event("research", 1, 4),
        token_usage_event(1200, 0.003),
    ]
    for ev in events:
        assert ev["type"] == PROGRESS_EVENT_TYPE == "touka_progress"
        assert isinstance(ev["kind"], str) and ev["kind"]


def test_react_slots_event_shape():
    ev = react_slots_event(current=2, total=5)
    assert ev == {"type": "touka_progress", "kind": "react_slots", "current": 2, "total": 5}


def test_repair_event_shape():
    ev = repair_event(attempt=2, max_attempts=3)
    assert ev == {"type": "touka_progress", "kind": "repair", "attempt": 2, "max": 3}


def test_group_event_shape():
    ev = group_event(index=2, total=3, agent="Bob")
    assert ev == {
        "type": "touka_progress",
        "kind": "group",
        "index": 2,
        "total": 3,
        "agent": "Bob",
    }


def test_creation_stage_event_shape():
    ev = creation_stage_event(stage="SMOKE", status="ok", target_kind="skill")
    assert ev == {
        "type": "touka_progress",
        "kind": "creation_stage",
        "stage": "SMOKE",
        "status": "ok",
        "target_kind": "skill",
    }


def test_tool_retry_event_shape():
    ev = tool_retry_event(tool="research", attempt=3, max_attempts=4)
    assert ev == {
        "type": "touka_progress",
        "kind": "tool_retry",
        "tool": "research",
        "attempt": 3,
        "max": 4,
    }


def test_token_usage_event_shape():
    ev = token_usage_event(total_tokens=1200, cost_usd=0.0036)
    assert ev == {
        "type": "touka_progress",
        "kind": "token_usage",
        "total": 1200,
        "cost": 0.0036,
    }


def test_events_are_pure_new_dict_each_call():
    a = react_slots_event(1, 5)
    b = react_slots_event(1, 5)
    assert a == b
    assert a is not b  # 每次回傳新 dict，無共享參考

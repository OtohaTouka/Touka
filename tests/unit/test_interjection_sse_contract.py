"""插話 SSE 契約（ADR-0249 第 4a 段）：後端常數、事件形狀與前端常數同步。

`interjection_delivered`（interjection_id、step、urgent）與 `interjection_undelivered`
（interjection_ids）的名稱與欄位只定義一次：後端在 services/interjection_queue.py，前端在
ToukaChat/src/lib/chat/interjections.ts；U4a-2 照這份契約送事件。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.services import interjection_queue as queue_mod

_FRONTEND = (
    Path(__file__).resolve().parents[2] / "ToukaChat" / "src" / "lib" / "chat" / "interjections.ts"
)


def test_backend_event_constants():
    assert queue_mod.INTERJECTION_DELIVERED_EVENT == "interjection_delivered"
    assert queue_mod.INTERJECTION_UNDELIVERED_EVENT == "interjection_undelivered"


def test_event_builders_shape():
    assert queue_mod.interjection_delivered_event(
        interjection_id="i1", step=3, urgent=True, text="改用 B 方案"
    ) == {
        "type": "interjection_delivered",
        "interjection_id": "i1",
        "step": 3,
        "urgent": True,
        "text": "改用 B 方案",
    }
    assert queue_mod.interjection_undelivered_event(interjection_ids=["a", "b"]) == {
        "type": "interjection_undelivered",
        "interjection_ids": ["a", "b"],
    }


def test_frontend_constants_match():
    source = _FRONTEND.read_text(encoding="utf-8")
    for literal in (
        "INTERJECTION_DELIVERED_EVENT = 'interjection_delivered'",
        "INTERJECTION_UNDELIVERED_EVENT = 'interjection_undelivered'",
        "id: 'interjection_id'",
        "step: 'step'",
        "urgent: 'urgent'",
        "ids: 'interjection_ids'",
        "text: 'text'",
    ):
        assert literal in source, literal


def test_frontend_event_names_equal_backend_constants():
    """前端事件常數的值（純文字讀出，不執行前端）和後端常數相同；含第 4b 段的打斷事件。"""
    source = _FRONTEND.read_text(encoding="utf-8")
    pairs = {
        "INTERJECTION_DELIVERED_EVENT": queue_mod.INTERJECTION_DELIVERED_EVENT,
        "INTERJECTION_UNDELIVERED_EVENT": queue_mod.INTERJECTION_UNDELIVERED_EVENT,
        "ROUND_INTERRUPTED_EVENT": queue_mod.ROUND_INTERRUPTED_BY_USER_EVENT,
    }
    for name, backend in pairs.items():
        match = re.search(rf"export const {name} = '([^']*)';", source)
        assert match is not None, name
        assert match.group(1) == backend, name
    assert queue_mod.ROUND_INTERRUPTED_BY_USER_EVENT == "round_interrupted_by_user"

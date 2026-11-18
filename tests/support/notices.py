"""從串流裡讀出畫面上的小提示（ADR-0322 PR 2b）.

系統說明不再寫在回覆文字裡，測試改看 ``turn_notice`` 事件。
"""

from __future__ import annotations

import json
from typing import Any

from src.utils.turn_notices import TURN_NOTICE_EVENT


def notice_events(chunks: list[Any]) -> list[dict[str, Any]]:
    """串流裡的小提示事件，照出現順序."""
    events = []
    for chunk in chunks:
        for line in str(chunk).split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: "):])
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == TURN_NOTICE_EVENT:
                events.append(event)
    return events


def notice_kinds(chunks: list[Any]) -> list[str]:
    """串流裡小提示的種類，照出現順序."""
    return [event.get("kind") for event in notice_events(chunks)]

"""三家模型的串流黃金檔：系統說明只以小提示事件出現，不在回覆文字裡（ADR-0322 PR 2b）.

工具計時、工具輪額度、中斷、拒答與各種警告，以前是混在回覆文字裡的一行；現在是
``turn_notice`` 事件。逐一檢查所有黃金檔：文字裡沒有舊日誌行的開頭、沒有 ``log`` 事件；
會產生小提示的情境要看得到對應種類。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from src.utils.turn_notices import (
    LEGACY_LOG_LINES,
    NOTICE_HONESTY,
    NOTICE_INTERRUPTED,
    NOTICE_MEMORY_HALLUCINATION,
    NOTICE_REFUSAL,
    NOTICE_ROUNDS_EXHAUSTED,
    NOTICE_SAFETY,
    NOTICE_TOOL_FINISHED,
    NOTICE_TOOL_HALLUCINATION,
    TURN_NOTICE_EVENT,
)

_GOLDENS = Path(__file__).parent / "goldens"
# 情境 → 至少要有其中一種小提示（OpenAI、Gemini 的拒答情境是廠商安全過濾）
_EXPECTED = {
    "interrupt_pre_call": {NOTICE_INTERRUPTED},
    "interrupt_during_run": {NOTICE_INTERRUPTED},
    "budget_exhaustion": {NOTICE_ROUNDS_EXHAUSTED},
    "hallucination_warning": {NOTICE_TOOL_HALLUCINATION},
    "memory_warning": {NOTICE_MEMORY_HALLUCINATION},
    "honesty_warning": {NOTICE_HONESTY},
    "refusal": {NOTICE_REFUSAL, NOTICE_SAFETY},
    "single_tool_round": {NOTICE_TOOL_FINISHED},
}


def _events(chunks: list[str]) -> list[dict[str, Any]]:
    events = []
    for chunk in chunks:
        for line in chunk.split("\n"):
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                data = json.loads(line[len("data: "):])
            except ValueError:
                continue
            if isinstance(data, dict):
                events.append(data)
    return events


def _text(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            parts.append((choices[0].get("delta") or {}).get("content") or "")
        elif event.get("type") == "content_block_delta":
            parts.append((event.get("delta") or {}).get("text") or "")
    return "".join(parts)


@pytest.mark.parametrize("path", sorted(_GOLDENS.glob("*.json")), ids=lambda path: path.stem)
def test_system_lines_appear_only_as_notices(path: Path) -> None:
    events = _events(json.loads(path.read_text(encoding="utf-8")))
    text = _text(events)
    for start, _end in LEGACY_LOG_LINES:
        assert start not in text
    assert not any(event.get("type") == "log" for event in events)
    expected = _EXPECTED.get(path.stem.split("__", 1)[1])
    if expected is not None:
        kinds = {event.get("kind") for event in events if event.get("type") == TURN_NOTICE_EVENT}
        assert expected & kinds

"""Tool results end with the Taipei time they finished at, and nothing else.

The executor passes the registered tool name (``memory_tools``), so the old
``save_memory`` reminder could never fire; it is gone. The time suffix stays: it is the
only backend clock that moves forward during a long ReAct run or heartbeat (ADR-0258).
"""

from __future__ import annotations

from typing import Any

import pytest
from src.utils import time_utils

# 2026-05-13 07:30:00 Asia/Taipei, a Wednesday
FINISHED_S = 1_778_628_600.0
SUFFIX = "\n\n[系統自動附加] 執行完畢之台北時間：2026-05-13 07:30:00 (週三)"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_utils, "get_current_epoch", lambda: FINISHED_S)


@pytest.mark.parametrize("tool_name", ["memory_tools", "save_memory", "bash", ""])
def test_text_result_gets_only_the_time(tool_name: str) -> None:
    assert time_utils.inject_time_to_result("ok", tool_name) == "ok" + SUFFIX


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"content": [{"type": "text", "text": "saved"}]},
            {"content": [{"type": "text", "text": "saved" + SUFFIX}]},
        ),
        ({"content": "saved"}, {"content": "saved" + SUFFIX}),
        ({"content": []}, {"content": [{"type": "text", "text": SUFFIX}]}),
    ],
)
def test_structured_result_gets_only_the_time(result: Any, expected: Any) -> None:
    assert time_utils.inject_time_to_result(result, "save_memory") == expected

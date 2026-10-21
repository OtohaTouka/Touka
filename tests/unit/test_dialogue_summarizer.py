"""Unit tests for dialogue_summarizer（ADR-0092 Phase D；ADR-0244 改走 complete_once）。

驗證 fail-safe（空輸入不送出；complete_once 非成功／例外／空回應回 None）與成功路徑
（注入假的 complete_once）。
"""
from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.services.completion import CompletionError, CompletionResult
from src.services.dialogue_summarizer import (
    _format_dialogue,
    summarize_dialogue,
    summary_time_range,
)
from src.utils.time_utils import CONVERSATION_RECORD_TIME_NOTE

_COMPLETE = "src.services.dialogue_summarizer.complete_once"
_OWNER = AgentIdentity(agent_id="DebugAgent")

_MSGS = [
    {"role": "user", "content": "我們約好下週交付。"},
    {"role": "assistant", "content": "好的，我會準備。"},
]


def _result(text: str) -> CompletionResult:
    return CompletionResult(text=text, usage={}, cost_usd=0.0, stop_reason="end", error=None)


class _Fake:
    def __init__(self, result: CompletionResult | Exception) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self, *_args, **_kwargs) -> CompletionResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_summarize_success(monkeypatch):
    monkeypatch.setattr(_COMPLETE, _Fake(_result("  • 約定下週交付\n")))
    out = await summarize_dialogue(_OWNER, _MSGS, 100, "claude-haiku-4-5")
    assert out is not None and out.split("\n", 1)[1] == "• 約定下週交付"  # 第一行是時間範圍


@pytest.mark.parametrize(
    "reason", ["missing_credentials", "over_budget", "timeout", "max_tokens", "refusal"]
)
async def test_summarize_unsuccessful_completion_returns_none(monkeypatch, reason):
    """缺金鑰、超預算、逾時、截斷、拒答：一律降級回 None（即使結果帶文字也不採用）。"""
    failed = CompletionResult(text="不該被用", usage={}, cost_usd=0.0, stop_reason="error",
                              error=CompletionError("upstream_error", reason, "x"))
    monkeypatch.setattr(_COMPLETE, _Fake(failed))
    assert await summarize_dialogue(_OWNER, _MSGS, 100, "claude-haiku-4-5") is None


@pytest.mark.parametrize(
    ("messages", "budget", "model"),
    [([], 100, "claude-haiku-4-5"), (_MSGS, 0, "claude-haiku-4-5"), (_MSGS, 100, ""),
     ([{"role": "system", "content": "只有系統訊息"}], 100, "claude-haiku-4-5")],
)
async def test_summarize_nothing_to_send_skips_call(monkeypatch, messages, budget, model):
    """空對話、零預算、沒指定模型、沒有可摘要的內容：不呼叫 complete_once（不花錢）。"""
    fake = _Fake(_result("不該被用"))
    monkeypatch.setattr(_COMPLETE, fake)
    assert await summarize_dialogue(_OWNER, messages, budget, model) is None
    assert fake.calls == 0


async def test_summarize_exception_returns_none(monkeypatch):
    monkeypatch.setattr(_COMPLETE, _Fake(RuntimeError("boom")))
    assert await summarize_dialogue(_OWNER, _MSGS, 100, "claude-haiku-4-5") is None


async def test_summarize_empty_response_returns_none(monkeypatch):
    monkeypatch.setattr(_COMPLETE, _Fake(_result("   ")))
    assert await summarize_dialogue(_OWNER, _MSGS, 100, "claude-haiku-4-5") is None


def test_format_dialogue_flattens_blocks():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "分析"}]},
        {"role": "assistant", "content": "好"},
        {"role": "system", "content": "略過"},  # 非 user/assistant 跳過
    ]
    out = _format_dialogue(msgs)
    assert "user: 分析" in out
    assert "assistant: 好" in out
    assert "略過" not in out


# ── 對話時間（摘要時間範圍、每行時間、相對時間換絕對日期）──────────────────────────

_T0 = 1_760_486_400_000  # 2025-10-15 08:00 Asia/Taipei（毫秒）
_HOUR_MS = 3_600_000
_NOW_S = 1_760_572_800.0  # 2025-10-16 08:00 Asia/Taipei（秒）
_NOTE = CONVERSATION_RECORD_TIME_NOTE


def _timed(*stamps):
    """user／assistant 交替的訊息；stamps 給每則的 timestamp（None＝沒有）。"""
    roles = ("user", "assistant")
    out = []
    for i, stamp in enumerate(stamps):
        message = {"role": roles[i % 2], "content": f"line-{i}"}
        if stamp is not None:
            message["timestamp"] = stamp
        out.append(message)
    return out


def test_format_dialogue_prefixes_taipei_time_per_line():
    """送摘要員的每行前面帶台北時間；秒也認得；沒有時間照實寫 unknown time。"""
    out = _format_dialogue(_timed(_T0, (_T0 + 3 * _HOUR_MS) // 1000, None)).splitlines()
    assert out == [
        "[2025-10-15 08:00 Asia/Taipei] user: line-0",
        "[2025-10-15 11:00 Asia/Taipei] assistant: line-1",
        "[unknown time] user: line-2",
    ]


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        (  # 全部有時間：最早到最晚（順序亂也一樣）
            _timed(_T0 + 3 * _HOUR_MS, _T0),
            "[Summary of earlier messages from 2025-10-15 08:00 Asia/Taipei to "
            f"2025-10-15 11:00 Asia/Taipei {_NOTE}; summarized 2025-10-16 08:00 Asia/Taipei]",
        ),
        (  # 部分沒有：範圍照有的算，並寫出幾則沒有
            _timed(_T0, None, _T0 + _HOUR_MS),
            "[Summary of earlier messages from 2025-10-15 08:00 Asia/Taipei to "
            f"2025-10-15 09:00 Asia/Taipei {_NOTE}; 1 of 3 messages have no saved time; "
            "summarized 2025-10-16 08:00 Asia/Taipei]",
        ),
        (  # 全部沒有：照實寫不知道
            _timed(None, None),
            "[Summary of earlier messages with no saved message times; "
            "summarized 2025-10-16 08:00 Asia/Taipei]",
        ),
        (  # 非對話角色不算
            [{"role": "system", "content": "x", "timestamp": _T0 - 9 * _HOUR_MS},
             *_timed(_T0, _T0)],
            "[Summary of earlier messages from 2025-10-15 08:00 Asia/Taipei to "
            f"2025-10-15 08:00 Asia/Taipei {_NOTE}; summarized 2025-10-16 08:00 Asia/Taipei]",
        ),
    ],
)
def test_summary_time_range_line(messages, expected):
    assert summary_time_range(messages, _NOW_S) == expected


async def test_summary_starts_with_time_range_and_prompt_asks_absolute_dates(monkeypatch):
    """回傳的摘要第一行是時間範圍（存在 context_summary 文字裡）；送出的提示要求換成絕對日期。"""
    calls = []

    async def _record(*args, **_kwargs):
        calls.append(args)
        return _result("• 約定 2025-10-22 交付")

    monkeypatch.setattr(_COMPLETE, _record)
    monkeypatch.setattr("src.services.dialogue_summarizer.get_current_epoch", lambda: _NOW_S)
    messages = _timed(_T0, _T0 + _HOUR_MS)
    out = await summarize_dialogue(_OWNER, messages, 100, "claude-haiku-4-5")
    assert out == f"{summary_time_range(messages, _NOW_S)}\n• 約定 2025-10-22 交付"
    system, dialogue = calls[0][3], calls[0][4]
    assert "相對時間" in system and "絕對日期" in system and "使用者裝置" in system
    assert dialogue.startswith("[2025-10-15 08:00 Asia/Taipei] user: line-0")

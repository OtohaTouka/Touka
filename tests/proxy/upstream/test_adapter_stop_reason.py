"""ProviderAdapter.stop_reason（ADR-0244，1b-U3）：三家停止原因正規化＋錄製台照轉。

正規化值只有 end／max_tokens／refusal／safety／error。拒答與截斷不得回 end；未知值、缺欄、
形狀異常一律 error（fail-closed）。1a 錄製台以 ``__getattr__`` 照轉，Protocol 新成員自動涵蓋。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.handlers.adapters.base import ProviderAdapter
from src.handlers.adapters.claude_adapter import ClaudeAdapter
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter

from .harness import RecordingAdapter


@pytest.mark.parametrize(("raw_reason", "expected"), [
    ("end_turn", "end"),
    ("stop_sequence", "end"),
    ("tool_use", "end"),
    ("max_tokens", "max_tokens"),
    ("model_context_window_exceeded", "max_tokens"),
    ("refusal", "refusal"),
    ("pause_turn", "error"),
    ("brand_new_reason", "error"),
    (None, "error"),
])
def test_claude_stop_reason(raw_reason, expected):
    assert ClaudeAdapter().stop_reason(None, SimpleNamespace(stop_reason=raw_reason)) == expected


def test_claude_stop_reason_missing_attribute_is_error():
    assert ClaudeAdapter().stop_reason(None, SimpleNamespace()) == "error"


def _openai(finish: object, **message: object) -> dict:
    return {"choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": "x", **message}}]}


@pytest.mark.parametrize(("raw", "expected"), [
    (_openai("stop"), "end"),
    (_openai("tool_calls"), "end"),
    (_openai("length"), "max_tokens"),
    (_openai("content_filter"), "safety"),
    (_openai("stop", refusal="I can't help with that."), "refusal"),
    (_openai("stop", refusal=None), "end"),
    (_openai("insufficient_system_resource"), "error"),
    (_openai(None), "error"),
    ({"choices": []}, "error"),
    ({}, "error"),
])
def test_openai_stop_reason(raw, expected):
    assert OpenAIAdapter().stop_reason(None, raw) == expected


def _gemini(finish: str | None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": [{"text": "x"}]}}
    if finish is not None:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


@pytest.mark.parametrize(("raw", "expected"), [
    (_gemini("STOP"), "end"),
    (_gemini("MAX_TOKENS"), "max_tokens"),
    (_gemini("SAFETY"), "safety"),
    (_gemini("RECITATION"), "safety"),
    (_gemini("PROHIBITED_CONTENT"), "safety"),
    (_gemini("BLOCKLIST"), "safety"),
    (_gemini("OTHER"), "error"),
    (_gemini("MALFORMED_FUNCTION_CALL"), "error"),
    (_gemini(None), "error"),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "safety"),
    ({"candidates": [], "promptFeedback": {"blockReason": "OTHER"}}, "safety"),
    ({"candidates": []}, "error"),
])
def test_gemini_stop_reason(raw, expected):
    assert GeminiAdapter().stop_reason(None, raw) == expected


# ── 成對改動：Protocol 成員＋錄製台照轉 ──────────────────────────────────────────


def test_protocol_declares_stop_reason():
    assert "stop_reason" in vars(ProviderAdapter)


@pytest.mark.parametrize(("adapter", "raw", "expected"), [
    (ClaudeAdapter(), SimpleNamespace(stop_reason="refusal"), "refusal"),
    (OpenAIAdapter(), _openai("length"), "max_tokens"),
    (GeminiAdapter(), _gemini("SAFETY"), "safety"),
])
def test_recording_adapter_forwards_stop_reason(adapter, raw, expected):
    recorder = RecordingAdapter(adapter, [])
    assert recorder.stop_reason == adapter.stop_reason
    assert recorder.stop_reason(None, raw) == expected

"""gemini_handler 特徵化 golden（react-core-extraction E0）。

共通 8 情境＋gemini 專屬：retry_503（指數退避＝touka_status 進度事件）、
recache_mid_loop（長迴圈、從不帶 cachedContent；ADR-0243 全停顯式快取）、honesty_warning（迴圈中
誠實守衛；ADR-0157 K2 後機制三家共用、本情境沿用）、thought_parts_split
（ADR-0157 K3 新行為＝對新碼錄製）。asyncio.sleep 以**模組命名空間代理**釘住
（避免全域 patch 影響事件迴圈）。

3b-U3（ADR-0248）：主迴圈改走自寫 SSE 串流，每個情境跑三種上游切法（1 個 chunk／語意多 chunk／
位元組亂切），輸出都必須和同一份 golden 完全相同。串流以 finishReason 判定送完，所以假回應都帶
``finishReason``（非串流時沒有這欄，下游輸出不變）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.gemini_handler import handle_gemini_request
from src.services.rhythm_service import rhythm_service

from tests.helpers.stream_transport import MODES, FakeUpstream, gemini_stream_reply, status_reply

from .harness import (
    UPSTREAM_S,
    assert_golden,
    drain,
    install_clock,
    make_clock,
    make_ctx,
    make_mock_executor,
    msg,
)

# E2 起 gemini_handler＝薄殼；時鐘在 react_core（harness 已涵蓋）、wire 靶點在 adapter。
_MODULE = "src.handlers.react_core"


def _tool_resp(*calls: tuple[str, dict]) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": name, "args": args}} for name, args in calls
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


def _text_resp(text: str) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    }


def _request(messages=None, tools=None):
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.pulse_slots = None
    request.safety_settings = None
    request.messages = messages or [msg("user", "hi")]
    request.tools = tools or []
    return request


async def _drive(monkeypatch, mode: str, scenario: str, responses: list, *,
                 request=None,
                 ctx_extra: dict | None = None,
                 tool_registry: dict | None = None,
                 status_codes: list[int] | None = None,
                 interrupt_on_call: int | None = None,
                 interrupt_before: bool = False) -> list[str]:
    clock = make_clock()
    install_clock(monkeypatch, _MODULE, clock)
    # asyncio.sleep 代理（503 退避不真睡）：只換 gemini 模組視角的 asyncio 名字。
    fake_asyncio = SimpleNamespace(
        Queue=asyncio.Queue, create_task=asyncio.create_task,
        sleep=AsyncMock(return_value=None),
    )
    monkeypatch.setattr("src.handlers.adapters.gemini_adapter.asyncio", fake_asyncio)
    monkeypatch.setattr("src.handlers.react_core._retry_sleep", AsyncMock(return_value=None))

    sid = f"sess-gg-{scenario}"
    rid = f"req-gg-{scenario}"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)
    if interrupt_before:
        rhythm_service.interrupt_session(sid)

    def on_request(n: int) -> None:
        clock["now"] += UPSTREAM_S
        if interrupt_on_call is not None and n == interrupt_on_call:
            rhythm_service.interrupt_session(sid)

    # 第 i 次請求：狀態碼照 status_codes（預設 200）；回應照舊每次消費一份（含非 200 那次）。
    codes, queue = list(status_codes or []), [*responses, _text_resp("Done!")]
    codes += [200] * (len(queue) - len(codes))
    replies = [gemini_stream_reply(body, mode) if code == 200 else status_reply(code)
               for code, body in zip(codes, queue, strict=False)]
    upstream = FakeUpstream(replies, on_request=on_request).install(monkeypatch)

    ctx = make_ctx(rid, sid, **(ctx_extra or {}))
    if tool_registry is not None:
        ctx["tool_registry"] = tool_registry

    try:
        with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
            MockExecutor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            chunks = await drain(handle_gemini_request(request or _request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)
    stream_url = "/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    assert all(str(r.url).endswith(stream_url) for r in upstream.requests)  # 確實走主迴圈串流
    upstream.assert_all_closed()
    return chunks


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_pure_text(monkeypatch, mode):
    chunks = await _drive(monkeypatch, mode, "pure_text", [_text_resp("你好，我是 Sumi。")])
    assert_golden("gemini", "pure_text", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_single_tool_round(monkeypatch, mode):
    chunks = await _drive(monkeypatch, mode, "single_tool_round", [
        _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
        _text_resp("清單如上。"),
    ])
    assert_golden("gemini", "single_tool_round", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_multi_tool_round(monkeypatch, mode):
    chunks = await _drive(monkeypatch, mode, "multi_tool_round", [
        _tool_resp(
            ("vault_tools", {"vault": "personal", "action": "list"}),
            ("memory_tools", {"action": "query", "query": "紅茶"}),
        ),
        _text_resp("兩件事都查好了。"),
    ])
    assert_golden("gemini", "multi_tool_round", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_budget_exhaustion_wrap_up(monkeypatch, mode):
    chunks = await _drive(
        monkeypatch, mode, "budget_exhaustion",
        [
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _text_resp("總結：已完成一輪查詢。"),
        ],
        ctx_extra={"enable_autonomy": True, "pulse_rounds": 1},
    )
    assert_golden("gemini", "budget_exhaustion", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_interrupt_pre_call(monkeypatch, mode):
    chunks = await _drive(
        monkeypatch, mode, "interrupt_pre_call", [_text_resp("不該到這")], interrupt_before=True
    )
    assert "已由使用者中斷" in "".join(chunks)
    assert_golden("gemini", "interrupt_pre_call", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_interrupt_during_run(monkeypatch, mode):
    chunks = await _drive(
        monkeypatch, mode, "interrupt_during_run",
        [
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _tool_resp(("vault_tools", {"vault": "personal", "action": "list"})),
            _text_resp("不該到這"),
        ],
        interrupt_on_call=2,
    )
    assert_golden("gemini", "interrupt_during_run", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_hallucination_warning(monkeypatch, mode):
    chunks = await _drive(
        monkeypatch, mode, "hallucination_warning",
        [_text_resp("查詢結果：\n> research ✅\n以上。")],
        tool_registry={"research": object()},
    )
    assert_golden("gemini", "hallucination_warning", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_memory_warning(monkeypatch, mode):
    chunks = await _drive(
        monkeypatch, mode, "memory_warning", [_text_resp("好的，我幫你記下來了！")]
    )
    assert_golden("gemini", "memory_warning", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_retry_503(monkeypatch, mode):
    """gemini 專屬：首攻 503 → touka_status 重試事件 → 第二攻 200。"""
    chunks = await _drive(
        monkeypatch, mode, "retry_503",
        [_text_resp("等到了。"), _text_resp("等到了。")],  # 503 那次的 json 不被消費，防禦性給兩份
        status_codes=[503, 200],
    )
    assert "retrying" in "".join(chunks)
    assert_golden("gemini", "retry_503", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_long_loop_never_cached(monkeypatch, mode):
    """gemini 專屬：長迴圈越過 contents>10（舊的重快取門檻），從不帶 cachedContent。

    顯式快取已全停（ADR-0243）；golden 檔名 ``recache_mid_loop`` 沿用舊名，SSE 逐字不變。
    """
    request = _request(
        messages=[
            msg("system", "You are Sumi."),
            msg("user", "hi"), msg("assistant", "hello"), msg("user", "do things"),
        ],
        tools=[{
            "type": "function",
            "function": {"name": "vault_tools", "description": "vault ops",
                         "parameters": {"type": "object", "properties": {}}},
        }],
    )
    chunks = await _drive(
        monkeypatch, mode, "recache_mid_loop",
        [_tool_resp(("vault_tools", {"vault": "personal", "action": "list"}))] * 5
        + [_text_resp("長迴圈完成。")],
        request=request,
    )
    assert_golden("gemini", "recache_mid_loop", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_honesty_warning_mid_loop(monkeypatch, mode):
    """gemini 專屬情境（ADR-0157 K2 後三家共用機制）：文字宣稱過去記憶但零查證
    → 誠實守衛注入矯正輪。"""
    chunks = await _drive(
        monkeypatch, mode, "honesty_warning",
        [
            _text_resp("根據過去的記憶系統內容，你上次說喜歡紅茶。"),
            _text_resp("我查證後再回答：您喜歡紅茶。"),
        ],
    )
    assert_golden("gemini", "honesty_warning", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_thought_parts_split(monkeypatch, mode):
    """K3（ADR-0157 新行為）：`thought: true` 思考摘要 → reasoning 頻道分頻。"""
    resp = {
        "candidates": [{"content": {"role": "model", "parts": [
            {"text": "使用者偏好……讓我想想。", "thought": True},
            {"text": "答案是紅茶。"},
        ]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2},
    }
    chunks = await _drive(monkeypatch, mode, "thought_parts_split", [resp])
    joined = "".join(chunks)
    assert "reasoning_content" in joined  # 思考走獨立頻道
    assert_golden("gemini", "thought_parts_split", chunks)


# ── model-p1a U4：上游錯誤／截斷／拒答（3b-U5a 起 finish_reason 照實回報）──


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_upstream_error(monkeypatch, mode):
    """上游 400（非 503 不重試）→ `API Error (400)`＋回應文字的單一 delta＋[DONE]。

    回應文字沿用 status_reply 的固定值 "server busy"；本情境凍結的是錯誤格式。
    """
    chunks = await _drive(monkeypatch, mode, "upstream_error", [], status_codes=[400])
    assert "API Error (400)" in "".join(chunks)  # 確認走到 RoundError 路徑
    assert_golden("gemini", "upstream_error", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_truncated(monkeypatch, mode):
    """finishReason=MAX_TOKENS：半句正文照送，終端 finish_reason 是 length（3b-U5a）。"""
    resp = {
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "紅茶的沖泡步驟：第一步，把水煮到"}]},
            "finishReason": "MAX_TOKENS",
        }],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 100},
    }
    chunks = await _drive(monkeypatch, mode, "truncated", [resp])
    assert_golden("gemini", "truncated", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_refusal(monkeypatch, mode):
    """finishReason=SAFETY 且沒有 content：只有一則安全過濾提示，終端是 content_filter。"""
    resp = {
        "candidates": [{
            "finishReason": "SAFETY",
            "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                               "probability": "HIGH", "blocked": True}],
        }],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 0},
    }
    chunks = await _drive(monkeypatch, mode, "refusal", [resp])
    assert_golden("gemini", "refusal", chunks)

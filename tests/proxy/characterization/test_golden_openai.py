"""openai_handler 特徵化 golden（react-core-extraction E0）。

情境矩陣（共通 8＋openai 專屬 reasoning_split）。degraded 注入影響的是**上游請求**
（system 拼接）而非下游 chunk，故不入 golden——由 E1 起的 adapter 契約測試
（prepare_request）把關。

3b-U2（ADR-0248 附註 14）：主迴圈改走自寫 SSE 串流，每個情境跑三種上游切法
（1 個 chunk／語意多 chunk／位元組亂切），輸出都必須和同一份 golden 完全相同。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.openai_handler import handle_openai_request
from src.services.rhythm_service import rhythm_service

from tests.helpers.stream_transport import MODES, FakeUpstream, json_reply, stream_reply

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

# E1 起 openai_handler＝薄殼（無時鐘/執行器）；時鐘與 ToolExecutor 靶點在 react_core。
_MODULE = "src.handlers.react_core"


def _tool_resp(*calls: tuple[str, str]) -> dict:
    """一輪工具呼叫回應；calls=[(name, arguments_json)]。"""
    return {
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": f"c{i+1}", "type": "function",
             "function": {"name": name, "arguments": args}}
            for i, (name, args) in enumerate(calls)
        ]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_resp(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


def _request():
    request = MagicMock()
    request.model = "gpt-4"
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "gpt-4", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [msg("user", "hi")]
    return request


async def _drive(monkeypatch, scenario: str, responses: list[dict], mode: str = "one", *,
                 ctx_extra: dict | None = None,
                 tool_registry: dict | None = None,
                 interrupt_on_call: int | None = None,
                 interrupt_before: bool = False) -> list[str]:
    clock = make_clock()
    install_clock(monkeypatch, _MODULE, clock)
    sid = f"sess-go-{scenario}"
    rid = f"req-go-{scenario}"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)
    if interrupt_before:  # 呼叫前中斷情境（須在 clear 之後設）
        rhythm_service.interrupt_session(sid)

    # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
    ctx = make_ctx(rid, sid, custom_connection=False, **(ctx_extra or {}))
    if tool_registry is not None:
        ctx["tool_registry"] = tool_registry

    def on_request(n: int) -> None:
        clock["now"] += UPSTREAM_S
        if interrupt_on_call is not None and n == interrupt_on_call:
            rhythm_service.interrupt_session(sid)

    replies = [stream_reply(r, mode) for r in [*responses, _text_resp("Done!")]]
    upstream = FakeUpstream(replies, on_request=on_request).install(monkeypatch)
    try:
        # E1 起 ToolExecutor 建構點在 react_core（openai_handler＝薄殼）；patch 靶點隨遷。
        with patch("src.handlers.react_core.ToolExecutor") as MockExecutor:
            MockExecutor.return_value.execute_to_queue = AsyncMock(
                side_effect=make_mock_executor(clock)
            )
            chunks = await drain(handle_openai_request(_request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)
    assert all(body["stream"] is True for body in upstream.sent)  # 確實走主迴圈串流
    upstream.assert_all_closed()
    return chunks


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_pure_text(monkeypatch, mode):
    chunks = await _drive(monkeypatch, "pure_text", [_text_resp("你好，我是 Sumi。")], mode=mode)
    assert_golden("openai", "pure_text", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_single_tool_round(monkeypatch, mode):
    chunks = await _drive(monkeypatch, "single_tool_round", [
        _tool_resp(("vault_tools", '{"vault": "personal", "action": "list"}')),
        _text_resp("清單如上。"),
    ], mode=mode)
    assert_golden("openai", "single_tool_round", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_multi_tool_round(monkeypatch, mode):
    chunks = await _drive(monkeypatch, "multi_tool_round", [
        _tool_resp(
            ("vault_tools", '{"vault": "personal", "action": "list"}'),
            ("memory_tools", '{"action": "query", "query": "紅茶"}'),
        ),
        _text_resp("兩件事都查好了。"),
    ], mode=mode)
    assert_golden("openai", "multi_tool_round", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_budget_exhaustion_wrap_up(monkeypatch, mode):
    """headless 保險絲=1 輪：第 2 輪工具請求跳閘 → denied＋WRAP_UP＋tool_choice=none 收尾。"""
    chunks = await _drive(
        monkeypatch, "budget_exhaustion",
        [
            _tool_resp(("vault_tools", '{"vault": "personal", "action": "list"}')),
            _tool_resp(("vault_tools", '{"vault": "personal", "action": "list"}')),
            _text_resp("總結：已完成一輪查詢。"),
        ],
        ctx_extra={"enable_autonomy": True, "pulse_rounds": 1},
        mode=mode,
    )
    assert_golden("openai", "budget_exhaustion", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_interrupt_pre_call(monkeypatch, mode):
    """呼叫前中斷：只有中斷訊息＋[DONE]，**不發遙測**（刻意不對稱，凍結之）。"""
    chunks = await _drive(
        monkeypatch, "interrupt_pre_call", [_text_resp("不該到這")], interrupt_before=True,
        mode=mode,
    )
    assert "已由使用者中斷" in "".join(chunks)  # 防再犯：確認真的走到中斷路徑
    assert_golden("openai", "interrupt_pre_call", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_interrupt_during_run(monkeypatch, mode):
    """第 2 次上游呼叫時觸發中斷：凍結 handler 現行的中斷退出路徑（含遙測與否）。"""
    chunks = await _drive(
        monkeypatch, "interrupt_during_run",
        [
            _tool_resp(("vault_tools", '{"vault": "personal", "action": "list"}')),
            _tool_resp(("vault_tools", '{"vault": "personal", "action": "list"}')),
            _text_resp("不該到這"),
        ],
        interrupt_on_call=2,
        mode=mode,
    )
    assert_golden("openai", "interrupt_during_run", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_hallucination_warning(monkeypatch, mode):
    """最終文字模擬未呼叫工具的日誌特徵 → guard 警告 delta。"""
    chunks = await _drive(
        monkeypatch, "hallucination_warning",
        [_text_resp("查詢結果：\n> research ✅\n以上。")],
        tool_registry={"research": object()},
        mode=mode,
    )
    assert_golden("openai", "hallucination_warning", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_memory_warning(monkeypatch, mode):
    """承諾記錄但未呼叫 memory_tools(save) → 記憶幻覺警告 delta。"""
    chunks = await _drive(
        monkeypatch, "memory_warning", [_text_resp("好的，我幫你記下來了！")], mode=mode
    )
    assert_golden("openai", "memory_warning", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_reasoning_split(monkeypatch, mode):
    """openai 專屬：message.reasoning_content 分頻——reasoning chunk 先於正文（ADR-0152）。"""
    resp = {
        "choices": [{"message": {
            "content": "答案是紅茶。",
            "reasoning_content": "使用者上次提過偏好，讓我想想……",
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    chunks = await _drive(monkeypatch, "reasoning_split", [resp], mode=mode)
    assert_golden("openai", "reasoning_split", chunks)


# ── model-p1a U4：上游錯誤／截斷／拒答（3b-U5a 起 finish_reason 照實回報）──
# 非 200 處理在 send_round（_post_once）內，故與 _drive 相同、在 httpx 邊界攔截。


async def _drive_http_error(monkeypatch, scenario: str, status: int, body: str) -> list[str]:
    """上游回非 200（_drive 的假回應固定 200，故另立最小驅動）。"""
    clock = make_clock()
    install_clock(monkeypatch, _MODULE, clock)
    sid = f"sess-go-{scenario}"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)

    def on_request(_n: int) -> None:
        clock["now"] += UPSTREAM_S

    ctx = make_ctx(f"req-go-{scenario}", sid, custom_connection=False)
    replies = [json_reply(body, status)]
    upstream = FakeUpstream(replies, on_request=on_request).install(monkeypatch)
    try:
        with patch("src.handlers.react_core.ToolExecutor"):
            chunks = await drain(handle_openai_request(_request(), ctx))
    finally:
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)
    upstream.assert_all_closed()
    return chunks


@pytest.mark.asyncio
async def test_upstream_error(monkeypatch):
    """上游 400 → `OpenAI API Error (400)`＋原始 body 的單一 delta＋[DONE]。"""
    body = (
        '{"error": {"message": "Invalid value for \'messages\'.", '
        '"type": "invalid_request_error", "param": "messages", "code": null}}'
    )
    chunks = await _drive_http_error(monkeypatch, "upstream_error", 400, body)
    assert "API Error (400)" in "".join(chunks)  # 確認走到 RoundError 路徑
    assert_golden("openai", "upstream_error", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_truncated(monkeypatch, mode):
    """finish_reason=length：半句正文照送，終端 finish_reason 照實是 length（3b-U5a）。"""
    resp = {
        "choices": [{"message": {"content": "紅茶的沖泡步驟：第一步，把水煮到"},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 100},
    }
    chunks = await _drive(monkeypatch, "truncated", [resp], mode=mode)
    assert_golden("openai", "truncated", chunks)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.asyncio
async def test_refusal(monkeypatch, mode):
    """finish_reason=content_filter、content=null：只有一則安全過濾提示，終端照實回報。"""
    resp = {
        "choices": [{"message": {"content": None}, "finish_reason": "content_filter"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0},
    }
    chunks = await _drive(monkeypatch, "refusal", [resp], mode=mode)
    assert_golden("openai", "refusal", chunks)

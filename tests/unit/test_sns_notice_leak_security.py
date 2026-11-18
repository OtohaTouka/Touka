"""SNS 不再把工具名稱、執行時間與工具例外原文送到外部平台（ADR-0322 PR 2b；SECURITY.md）.

以前工具執行的日誌行由 ``react_core`` 接進回覆文字，SNS 原樣送到 Discord／Telegram：外人看得到
用了哪些工具、什麼時候跑的，工具出例外時連錯誤原文都看得到。現在工具執行器送的是小提示事件，
``react_core`` 只把它當事件轉出去、不接進文字；SNS 只送模型寫的文字。存進對話的小提示裡，
例外原文先消毒。

真的 ``react_core`` 迴圈（OpenAI 方言、假的上游）接真的 SNS 收回覆；全部離線。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from src.utils.turn_notices import NOTICES_FIELD, tool_exception_notice

from tests.proxy.characterization import test_golden_openai as golden
from tests.unit.test_tool_step_collector import _assistant_call, _sns

LEAKY_DETAIL = "RuntimeError: Bearer abc123"


def _leaky_executor(_clock: dict) -> Any:
    """工具出例外：舊的日誌行與新的小提示各送一則，兩種都不能被外人看到."""

    async def execute(name: str, _args: Any, _req_id: str, _ctx: Any, queue: Any,
                      **_kwargs: Any) -> str:
        await queue.put({"type": "log", "content": f"❌ 執行發生例外：{LEAKY_DETAIL}\n"})
        await queue.put(tool_exception_notice(name, LEAKY_DETAIL))
        await queue.put(None)
        return "Tool Success"

    return execute


@pytest.mark.asyncio
async def test_tool_lines_never_reach_the_outside_platform(monkeypatch) -> None:
    monkeypatch.setattr(golden, "make_mock_executor", _leaky_executor)
    chunks = await golden._drive(
        monkeypatch, "sns_notice_leak",
        [golden._tool_resp(("secret_tool", "{}")), golden._text_resp("查好了。")],
    )
    text, append, send = await _sns(*chunks)

    sent = send.await_args.args[1].text
    assert sent == text == "查好了。"
    call = _assistant_call(append)
    assert call.args[2] == "查好了。"
    stored = json.dumps(call.kwargs["extra_fields"][NOTICES_FIELD], ensure_ascii=False)
    assert "RuntimeError" in stored  # 小提示照樣存下來給網頁看
    assert "abc123" not in stored  # 例外原文先消毒
    for leaked in ("secret_tool", "RuntimeError", "abc123", "執行發生例外"):
        assert leaked not in sent

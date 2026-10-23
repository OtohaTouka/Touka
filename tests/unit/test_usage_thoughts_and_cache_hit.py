"""ADR-0250 A6／A7：Gemini 思考 token 算進輸出、DeepSeek 快取命中照實（ADR-0244 剩餘風險）。

- A6：Gemini 輸出＝``candidatesTokenCount``＋``thoughtsTokenCount``（Google 把思考 token 當輸出
  計費）；串流沒完成時的部分用量、回給客戶端的 ``completion_tokens`` 同一個換算。成本上限因此
  在同一次 run 更早觸發。
- A7：OpenAI 相容的快取讀＝``prompt_tokens_details.cached_tokens``，沒有才用 DeepSeek 頂層的
  ``prompt_cache_hit_tokens``；兩者都在只取前者、不相加。快取命中照快取價記帳。
全部 mock，不送任何廠商請求；期望值一律寫字面值。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.gemini_to_openai import convert_gemini_to_openai
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.handlers import react_core
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.providers import pricing
from src.services.resource_manager import resource_manager
from src.services.telemetry import accumulate_token_usage
from src.utils.tool_usage import record_tool_executed, record_tool_succeeded, record_tool_usage

from tests.helpers.stream_transport import FakeUpstream, Reply, sse

_GUARD_S = 5.0
_PRICING = {
    "_default": {"input": 1.0, "output": 2.0},
    "pricing": {
        "gemini-2.5-flash": {"input": 1.0, "output": 2.0},
        "deepseek-test": {"input": 1.0, "output": 2.0, "cache_read": 0.1},
    },
}


@pytest.fixture(autouse=True)
def _test_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    """只換 model_pricing.json 的讀取；其他設定檔照常讀。"""
    real_read = pricing.config_utils.read_config_json
    monkeypatch.setattr(
        pricing.config_utils,
        "read_config_json",
        lambda name: _PRICING if name == "model_pricing.json" else real_read(name),
    )


def _expected(prompt: int, cache_read: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": 0,
        "completion_tokens": completion,
    }


def _stats(ident: AgentIdentity) -> dict:
    path = Path(resource_manager.get_stats_path(ident)) / "token_usage.json"
    return resource_manager.read_json(path)


# ── A6：Gemini 思考 token ───────────────────────────────────────────────────


def test_gemini_thoughts_count_as_output() -> None:
    raw = {"usageMetadata": {
        "promptTokenCount": 100, "candidatesTokenCount": 6, "thoughtsTokenCount": 37,
    }}
    assert GeminiAdapter().extract_usage(None, raw) == (_expected(100, 0, 43), 100, 43)


def test_gemini_thoughts_with_implicit_cache() -> None:
    raw = {"usageMetadata": {
        "promptTokenCount": 5000, "cachedContentTokenCount": 4096,
        "candidatesTokenCount": 30, "thoughtsTokenCount": 70,
    }}
    assert GeminiAdapter().extract_usage(None, raw) == (_expected(904, 4096, 100), 5000, 100)


def test_gemini_to_openai_completion_tokens_include_thoughts() -> None:
    response = {
        "candidates": [{"content": {"parts": [{"text": "好"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 6,
                          "thoughtsTokenCount": 37, "totalTokenCount": 143},
    }
    assert convert_gemini_to_openai(response)["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 43, "total_tokens": 143,
    }


def _request() -> OpenAIChatRequest:
    return OpenAIChatRequest(
        model="gemini-2.5-flash", messages=[OpenAIMessage(role="user", content="hi")]
    )


def _loop_ctx(rid: str, agent_id: str) -> dict[str, Any]:
    return {
        "gemini_api_key": "fake-key", "agent_id": agent_id,
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": rid, "t0": 0.0, "identity": AgentIdentity.for_role(agent_id, "Owner"),
    }


async def test_gemini_unfinished_round_books_thoughts_as_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串流沒完成（沒有 finishReason）但已回報用量：部分用量走同一個換算。"""
    booked: list[dict] = []

    def book(_identity: Any, _provider: Any, usage: dict, *_rest: Any, **_kw: Any) -> None:
        if usage:
            booked.append(usage)

    monkeypatch.setattr(react_core, "accumulate_token_usage", book)
    usage = {"promptTokenCount": 30, "candidatesTokenCount": 4, "thoughtsTokenCount": 9}
    chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": "好"}]}}],
             "usageMetadata": usage}
    FakeUpstream([Reply(pieces=[sse(chunk, done=False)])]).install(monkeypatch)
    loop = react_core.run_react_loop(
        GeminiAdapter(stream=True), _request(), _loop_ctx("req-thoughts", "Sumi")
    )
    async with asyncio.timeout(_GUARD_S):
        _ = [c async for c in loop]
    assert booked == [_expected(30, 0, 13)]


def _gemini_round(thoughts: int, *, tool: bool) -> Reply:
    part = {"functionCall": {"name": "vault_tools", "args": {}}} if tool else {"text": "好了。"}
    body = {
        "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 0,
                          "thoughtsTokenCount": thoughts},
    }
    return Reply(pieces=[sse(body, done=False)])


async def _run_gemini(agent_id: str, thoughts: int) -> list[str]:
    executed: list[str] = []

    async def execute(name: str, args: Any, _rid: Any, _ctx: Any, queue: Any, **kw: Any) -> str:
        executed.append(name)
        used = kw.get("actually_used_tools")
        record_tool_usage(used, name, args)
        record_tool_executed(used, name, args)
        record_tool_succeeded(used, name, args)
        await queue.put({"type": "log", "content": f"[{name} done]"})
        await queue.put(None)
        return "ok"

    replies = [_gemini_round(thoughts, tool=True), _gemini_round(thoughts, tool=True),
               _gemini_round(0, tool=False), _gemini_round(0, tool=False)]
    with pytest.MonkeyPatch.context() as monkeypatch, patch(
        "src.handlers.react_core.ToolExecutor"
    ) as executor_cls:
        FakeUpstream(replies).install(monkeypatch)
        executor_cls.return_value.execute_to_queue = AsyncMock(side_effect=execute)
        loop = react_core.run_react_loop(
            GeminiAdapter(stream=True), _request(), _loop_ctx(f"req-{agent_id}", agent_id)
        )
        async with asyncio.timeout(_GUARD_S):
            _ = [c async for c in loop]
    return executed


async def test_gemini_thoughts_make_the_cost_cap_trigger_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上限 $0.0015：每輪輸入 $0.001；思考 300 token＝$0.0006 → 第 1 輪就到上限、工具不執行。

    對照組沒有思考 token：第 1 輪的工具照常執行，第 2 輪才到上限。
    """
    monkeypatch.setitem(
        autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": 0.0015}
    )
    assert await _run_gemini("ThoughtsCapControl", thoughts=0) == ["vault_tools"]
    assert await _run_gemini("ThoughtsCapEarly", thoughts=300) == []


# ── A7：DeepSeek 快取命中 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 100, "completion_tokens": 5,
          "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}, (20, 80)),
        ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": None,
          "prompt_cache_hit_tokens": 80}, (20, 80)),
        ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80,
          "prompt_tokens_details": {"cached_tokens": 80}}, (20, 80)),
        ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 50,
          "prompt_tokens_details": {"cached_tokens": 80}}, (20, 80)),
    ],
    ids=["top-level-only", "null-details", "both-equal-not-added", "details-wins"],
)
def test_deepseek_cache_hit_is_read(usage: dict, expected: tuple[int, int]) -> None:
    uncached, cache_read = expected
    assert OpenAIAdapter().extract_usage(None, {"usage": usage}) == (
        _expected(uncached, cache_read, 5), 100, 5,
    )


def test_deepseek_cache_hit_is_billed_at_the_cache_price() -> None:
    """100 萬輸入、命中 80 萬：未快取 20 萬×$1＋快取 80 萬×$0.1＝$0.28（以前整筆照輸入價 $1）。"""
    ident = AgentIdentity.for_role(agent_id="DeepSeekCacheHit", role="Owner")
    raw = {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                     "prompt_cache_hit_tokens": 800_000, "prompt_cache_miss_tokens": 200_000}}
    usage, _pt, _ct = OpenAIAdapter().extract_usage(None, raw)
    accumulate_token_usage(ident, "DeepSeek", usage, model="deepseek-test")
    stats = _stats(ident)["DeepSeek"]
    assert stats["prompt_tokens"] == 200_000
    assert stats["cache_read_input_tokens"] == 800_000
    assert stats["total_cost_usd"] == 0.28

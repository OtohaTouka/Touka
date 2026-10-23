"""每輪記帳（ADR-0244；model-p1b U2）。

- react_core 每輪 parse 後就把該輪用量寫進 token_usage.json，成本閘因此看得到本次 run 的花費；
- E-Index 歷史整次 run 只記一筆；
- 稽核 CSV 每次 run 一列、欄位不變，``cost_usd`` 含快取；
- 畫面 token_usage 事件＝整次 run 加總（含快取），``usage_pct`` 用最後一輪的總輸入；
- 原生 Anthropic 出口照常記帳，但不送 token_usage 事件。

期望值一律寫字面值；入帳檔走 conftest 沙盒。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.handlers.openai_handler import handle_openai_request
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.providers import pricing
from src.services.resource_manager import resource_manager
from src.services.rhythm_service import rhythm_service
from src.services.tool_executor import record_tool_usage
from src.utils.tool_usage import record_tool_executed, record_tool_succeeded

from tests.helpers.claude_stream import delegate_stream_to_create
from tests.helpers.stream_transport import FakeUpstream, stream_reply

# 費率（USD／1M tokens）：輸入 1、輸出 2、快取讀 0.5、快取寫 4。OpenAI 模型不設快取價＝照輸入價。
_PRICING = {
    "_default": {"input": 1.0, "output": 2.0},
    "pricing": {
        "acct-gpt": {"input": 1.0, "output": 2.0},
        "claude-3-5-sonnet": {
            "input": 1.0, "output": 2.0, "cache_read": 0.5, "cache_write": 4.0,
        },
    },
}
_AUDIT_HEADERS = [
    "timestamp", "request_id", "agent_id", "provider",
    "total_ms", "local_ms", "network_ms", "pure_gen_ms",
    "e_index", "input_tokens", "output_tokens", "cost_usd", "rate_source",
]


@pytest.fixture(autouse=True)
def _test_pricing(monkeypatch):
    """只換 model_pricing.json 的讀取；其他設定檔照常讀。"""
    real_read = pricing.config_utils.read_config_json
    monkeypatch.setattr(
        pricing.config_utils,
        "read_config_json",
        lambda name: _PRICING if name == "model_pricing.json" else real_read(name),
    )


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id, "Owner", capabilities=["*"])


def _stats_dir(identity: AgentIdentity) -> Path:
    return Path(resource_manager.get_stats_path(identity))


def _token_usage(identity: AgentIdentity) -> dict | None:
    return resource_manager.read_json(_stats_dir(identity) / "token_usage.json")


def _audit_rows(identity: AgentIdentity) -> list[list[str]]:
    text = (_stats_dir(identity) / "usage_audit.csv").read_text(encoding="utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _sse_objects(chunks: list[str]) -> list[dict]:
    objs: list[dict] = []
    for chunk in chunks:
        payload = chunk[len("data: "):].strip() if chunk.startswith("data: ") else ""
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _usage_events(chunks: list[str]) -> list[dict]:
    return [o for o in _sse_objects(chunks) if o.get("kind") == "token_usage"]


def _text(chunks: list[str]) -> str:
    return "".join(
        (o.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
        for o in _sse_objects(chunks)
    )


def _ctx(agent_id: str, **extra) -> dict:
    ctx = {
        "api_key": "fake-key", "url": "http://fake-api.test", "provider": "OpenAI",
        "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {}, "memory_tool_registry": {}, "plugin_registry": {},
        "request_id": f"req-acct-{agent_id}", "t0": 0.0, "agent_id": agent_id,
        "identity": _identity(agent_id), "is_toukasoul": True,
        "session_id": f"sess-acct-{agent_id}",
    }
    ctx.update(extra)
    return ctx


def _executor(executed: list[str]):
    async def execute(name, args, _req_id, _ctx, queue, **kwargs):
        executed.append(name)
        used = kwargs.get("actually_used_tools")
        record_tool_usage(used, name, args)
        record_tool_executed(used, name, args)
        record_tool_succeeded(used, name, args)
        await queue.put({"type": "log", "content": f"[{name} done]"})
        await queue.put(None)
        return "ok"

    return execute


# ── OpenAI 相容（prompt_tokens 含 cached_tokens）────────────────────────────


def _openai_round(prompt: int, cached: int, completion: int, *, tool: str | None = None,
                  args: str = "{}") -> dict:
    message: dict = {"content": "好了。"}
    if tool is not None:
        message = {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": tool, "arguments": args}},
        ]}
    return {
        "choices": [{"message": message}],
        "usage": {
            "prompt_tokens": prompt, "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


async def _run_openai(agent_id: str, responses: list[dict]) -> SimpleNamespace:
    """跑一次 OpenAI run；``seen_at_send[n]``＝第 n+1 次送出當下的 token_usage.json。"""
    ctx = _ctx(agent_id)
    request = MagicMock()
    request.model = "acct-gpt"
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "acct-gpt", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    seen_at_send: list[dict | None] = []
    executed: list[str] = []
    # 3b-U2：主迴圈走自寫 SSE 串流（共用假傳輸）；用量照廠商慣例放在最後的 choices:[] chunk
    upstream = FakeUpstream(
        [stream_reply(r, "semantic") for r in responses],
        on_request=lambda _n: seen_at_send.append(_token_usage(ctx["identity"])),
    )

    rhythm_service.clear_session(ctx["session_id"])
    rhythm_service.clear_interrupt(ctx["session_id"])
    try:
        with pytest.MonkeyPatch.context() as monkeypatch, patch(
            "src.handlers.react_core.ToolExecutor"
        ) as executor_cls:
            upstream.install(monkeypatch)
            executor_cls.return_value.execute_to_queue = AsyncMock(
                side_effect=_executor(executed)
            )
            chunks = [c async for c in handle_openai_request(request, ctx)]
    finally:
        rhythm_service.clear_session(ctx["session_id"])
    return SimpleNamespace(
        chunks=chunks, identity=ctx["identity"], seen_at_send=seen_at_send, executed=executed,
    )


@pytest.mark.asyncio
async def test_each_round_is_written_before_the_next_round_is_sent():
    """第 1 輪用量在第 2 輪送出前就已寫進 token_usage.json；run 結束時兩輪都在。"""
    run = await _run_openai("AcctPerRound", [
        _openai_round(1200, 200, 50, tool="vault_tools"),
        _openai_round(1500, 1000, 30),
    ])
    assert run.seen_at_send[0] is None  # 新身分，第 1 輪送出前沒有帳
    at_second_send = run.seen_at_send[1]["OpenAI"]
    assert at_second_send["prompt_tokens"] == 1000
    assert at_second_send["cache_read_input_tokens"] == 200
    assert at_second_send["completion_tokens"] == 50
    assert at_second_send["total_cost_usd"] == 0.0013

    final = _token_usage(run.identity)["OpenAI"]
    assert final["prompt_tokens"] == 1500
    assert final["cache_read_input_tokens"] == 1200
    assert final["cache_creation_input_tokens"] == 0
    assert final["completion_tokens"] == 80
    assert final["total_cost_usd"] == 0.00286


@pytest.mark.asyncio
async def test_cost_cap_sees_spend_of_the_current_run(monkeypatch):
    """上限 $0.0015、每輪 $0.001：第 2 輪工具請求時累計已達上限 → 不執行、改走收尾。"""
    monkeypatch.setitem(
        autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": 0.0015}
    )
    run = await _run_openai("AcctCostCap", [
        _openai_round(1000, 0, 0, tool="vault_tools"),
        _openai_round(1000, 0, 0, tool="vault_tools"),
        _openai_round(1000, 0, 0),
    ])
    assert run.executed == ["vault_tools"]  # 只有第 1 輪的工具有執行
    assert len(run.seen_at_send) == 3  # 第 3 次＝無工具收尾
    assert "已達總量上限" in _text(run.chunks)


@pytest.mark.asyncio
async def test_e_index_history_gets_one_entry_per_run():
    """三輪的 run：用量三輪都入帳，E-Index 歷史只多一筆，值＝本次 run 遙測的 e_index。"""
    run = await _run_openai("AcctEIndex", [
        _openai_round(100, 0, 10, tool="memory_tools", args='{"action": "query"}'),
        _openai_round(100, 0, 10, tool="vault_tools"),
        _openai_round(100, 0, 10),
    ])
    stats = _token_usage(run.identity)["OpenAI"]
    assert stats["prompt_tokens"] == 300
    history = stats["e_index_history"]
    assert len(history) == 1
    telemetry = next(o["telemetry"] for o in _sse_objects(run.chunks)
                     if o.get("type") == "touka_status" and "telemetry" in o)
    assert history[0]["e_index"] == telemetry["e_index"]
    assert history[0]["e_index"] > 0  # 有 memory_tools.query＝非零，證明不是預設值


@pytest.mark.asyncio
async def test_audit_csv_one_row_per_run_same_columns_and_cost_with_cache():
    """兩輪的 run：CSV 只有一列、欄位不變；token 數為兩輪加總，cost_usd 含快取讀。"""
    run = await _run_openai("AcctAudit", [
        _openai_round(1200, 200, 50, tool="vault_tools"),
        _openai_round(1500, 1000, 30),
    ])
    rows = _audit_rows(run.identity)
    assert rows[0] == _AUDIT_HEADERS
    assert len(rows) == 2
    row = dict(zip(_AUDIT_HEADERS, rows[1], strict=True))
    assert row["request_id"] == "req-acct-AcctAudit"
    assert row["input_tokens"] == "1500"  # 未快取輸入 1000＋500
    assert row["output_tokens"] == "80"
    assert row["cost_usd"] == "0.002860"  # 1500×1＋快取讀 1200×1＋80×2（每百萬）


# ── Claude（input_tokens 不含快取；快取讀寫另列）────────────────────────────


def _claude_round(uncached: int, cache_read: int, cache_write: int, output: int, *,
                  tool: bool = False) -> MagicMock:
    block = MagicMock()
    if tool:
        block.type, block.id, block.name, block.input = "tool_use", "c1", "vault_tools", {}
        dump = [{"type": "tool_use", "id": "c1", "name": "vault_tools", "input": {}}]
    else:
        block.type, block.text = "text", "好了。"
        dump = [{"type": "text", "text": "好了。"}]
    usage = {
        "input_tokens": uncached, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write, "output_tokens": output,
    }
    resp = MagicMock()
    resp.content = [block]
    resp.usage.model_dump.return_value = usage
    resp.model_dump.return_value = {
        "id": "msg_acct", "model": "claude-3-5-sonnet", "role": "assistant",
        "content": dump, "usage": usage,
    }
    return resp


class FakeAnthropicRequest:
    """型別名含 anthropic → 走原生 Anthropic 出口。"""

    model = "claude-3-5-sonnet"
    pulse_slots = None
    messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]

    def model_dump(self, **_k) -> dict:
        return {
            "model": "claude-3-5-sonnet", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }


def _openai_shaped_claude_request() -> MagicMock:
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    request.messages = [SimpleNamespace(role="user", content="hi", tool_calls=None)]
    return request


async def _run_claude(agent_id: str, request, responses: list) -> SimpleNamespace:
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    delegate_stream_to_create(client)  # 3b-U1：主迴圈走 messages.stream()
    ctx = _ctx(agent_id, anthropic_client=client)
    rhythm_service.clear_session(ctx["session_id"])
    rhythm_service.clear_interrupt(ctx["session_id"])
    try:
        with patch("src.handlers.react_core.ToolExecutor") as executor_cls:
            executor_cls.return_value.execute_to_queue = AsyncMock(side_effect=_executor([]))
            chunks = [c async for c in handle_claude_request(request, ctx)]
    finally:
        rhythm_service.clear_session(ctx["session_id"])
    return SimpleNamespace(chunks=chunks, identity=ctx["identity"])


@pytest.mark.asyncio
async def test_usage_event_is_run_total_with_cache_and_usage_pct_uses_last_round():
    """事件 total／cost＝兩輪加總含快取；usage_pct 與分解總數用最後一輪的總輸入（2 萬／20 萬）。"""
    run = await _run_claude("AcctEvent", _openai_shaped_claude_request(), [
        _claude_round(60_000, 0, 40_000, 100, tool=True),  # 本輪總輸入 10 萬＝50%
        _claude_round(2_000, 18_000, 0, 200),  # 本輪總輸入 2 萬＝10%
    ])
    events = _usage_events(run.chunks)
    assert len(events) == 1
    event = events[0]
    assert event["total"] == 120_300
    assert event["cost"] == 0.2316  # 62000×1＋快取讀 18000×0.5＋快取寫 40000×4＋300×2（每百萬）
    assert event["usage_pct"] == 10.0
    assert event["context_breakdown"]["total_tokens"] == 20_000
    assert event["context_breakdown"]["window"] == 200_000


@pytest.mark.asyncio
async def test_native_anthropic_egress_records_usage_but_sends_no_usage_event():
    """原生出口：token_usage.json 與稽核 CSV 照常入帳（含快取），串流裡沒有 token_usage 事件。"""
    run = await _run_claude("AcctNative", FakeAnthropicRequest(), [
        _claude_round(1000, 2000, 500, 100),
    ])
    assert _usage_events(run.chunks) == []
    stats = _token_usage(run.identity)["Anthropic"]
    assert stats["prompt_tokens"] == 1000
    assert stats["cache_read_input_tokens"] == 2000
    assert stats["cache_creation_input_tokens"] == 500
    assert stats["completion_tokens"] == 100
    assert stats["total_cost_usd"] == 0.0042
    rows = _audit_rows(run.identity)
    assert len(rows) == 2
    assert dict(zip(_AUDIT_HEADERS, rows[1], strict=True))["cost_usd"] == "0.004200"

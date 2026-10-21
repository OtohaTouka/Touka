"""ReAct 跨輪記憶動作偵測回歸測試。

歷史根因（本檔誕生時）：claude_handler 的「防幻覺打臉機制」與「湧現指標」曾在
迴圈結束後依賴殘留的最後一輪 `tool_use_blocks` 推導 save/query，較早回合的記憶
動作會偵測遺漏（誤發記憶幻覺警告／e_index 被算成 0）。

漂移修復批 C2 之後：訊號 SSOT ＝ `tool_executor.record_tool_usage` 產生的限定名
（`memory_tools.save`/`memory_tools.query`，記入 `actually_used_tools`、天然跨輪
累積），三 handler 同源消費。本檔守的**跨輪語意不變**——mock 執行器改呼叫真的
`record_tool_usage`，使測試持續追蹤生產訊號機制：

- save 在較早回合 → 最終宣稱存檔不得誤發「記憶幻覺警告」。
- query 在較早回合 → 湧現指標 e_index 必須 > 0。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.rhythm_service import rhythm_service
from src.services.tool_executor import record_tool_usage

from tests.helpers.claude_stream import delegate_stream_to_create


def _block(btype: str, *, text=None, cid=None, name=None, inp=None):
    """構造一個 Anthropic content block 的輕量替身。"""
    b = MagicMock()
    b.type = btype
    b.text = text
    b.id = cid
    b.name = name
    b.input = inp
    return b


def _response(blocks, dump_content):
    """構造一個 Anthropic messages.create() 回應替身（含 usage / model_dump）。"""
    r = MagicMock()
    r.content = blocks
    r.usage = MagicMock()
    r.usage.model_dump.return_value = {"input_tokens": 5, "output_tokens": 5}
    r.model_dump.return_value = {
        "id": "msg_test",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": dump_content,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    return r


def _ctx(sid: str, rid: str):
    return {
        "anthropic_client": None,  # 由各測試覆寫
        "protocol_manager": MagicMock(),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": rid,
        "t0": 0.0,
        "agent_id": "MemAgent",
        "identity": AgentIdentity.for_role(agent_id="MemAgent", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": sid,
        "model": "claude-3-5-sonnet",
    }


def _request():
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [],
    }
    request.messages = [MagicMock(content="幫我記住這件重要的事情", role="user")]
    return request


def _decode_texts(chunks: list[str]) -> str:
    """把 SSE chunk 裡的 delta.content 解碼合併（JSON 會把中文 escape）。"""
    out = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: "):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
        if delta:
            out.append(delta)
    return "".join(out)


def _e_index_values(chunks: list[str]) -> list[float]:
    """蒐集 telemetry 裡出現過的 e_index（湧現指標）。"""
    vals = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: "):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        tele = obj.get("telemetry")
        if isinstance(tele, dict) and "e_index" in tele:
            vals.append(tele["e_index"])
    return vals


async def _drive(request, context) -> list[str]:
    chunks: list[str] = []
    async for c in handle_claude_request(request, context):
        chunks.append(c)
        if len(chunks) > 80:  # 安全閥，防止測試卡死在無限迴圈
            break
    return chunks


def _mock_executor(monkeypatch):
    """Patch ToolExecutor，以真的 record_tool_usage（SSOT）登記 actually_used_tools。

    C2 後訊號＝限定名（memory_tools.save/query）；mock 呼叫生產同款函式，
    測試才持續追蹤真實訊號機制（而非複製一份會漂移的 add(name)）。
    """

    async def mock_execute(name, args, req_id, ctx, queue, **kwargs):
        record_tool_usage(kwargs.get("actually_used_tools"), name, args)
        await queue.put(None)
        return "OK"

    p = patch("src.handlers.react_core.ToolExecutor")
    MockExecutor = p.start()
    MockExecutor.return_value.execute_to_queue = AsyncMock(side_effect=mock_execute)
    return p


@pytest.fixture
def isolate_rhythm(monkeypatch):
    """不寫入配額檔案（ADR-0146 後純文字輪天然收尾，毋須再壓 nudge）。"""
    monkeypatch.setattr(autonomy_service, "consume_quota", lambda *a, **k: None)
    yield


@pytest.mark.asyncio
async def test_save_in_earlier_round_no_false_memory_warning(monkeypatch, isolate_rhythm):
    """第 1 輪 memory_tools save、最後一輪純文字宣稱已存檔 → 不應誤發記憶幻覺警告。

    舊碼會從最後一輪空的 tool_use_blocks 推導 has_saved=False，導致誤判。
    """
    sid, rid = "sess-mem-save-1", "req-mem-save-1"
    rhythm_service.clear_session(sid)

    save_block = _block(
        "tool_use", cid="c1", name="memory_tools", inp={"action": "save", "content": "x"}
    )
    round1 = _response(
        [_block("text", text="我來幫你存檔。"), save_block],
        [
            {"type": "text", "text": "我來幫你存檔。"},
            {"type": "tool_use", "id": "c1", "name": "memory_tools", "input": {"action": "save"}},
        ],
    )
    round2 = _response(
        [_block("text", text="好的，我已經存檔了，任務結束。")],
        [{"type": "text", "text": "好的，我已經存檔了，任務結束。"}],
    )

    context = _ctx(sid, rid)
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[round1, round2])
    context["anthropic_client"] = delegate_stream_to_create(client)  # 3b-U1：走 stream()

    p = _mock_executor(monkeypatch)
    try:
        chunks = await _drive(_request(), context)
    finally:
        p.stop()
        rhythm_service.clear_session(sid)

    texts = _decode_texts(chunks)
    assert "已經存檔" in texts  # 確認最終回覆確實宣稱了存檔（觸發偵測前提）
    assert "記憶幻覺警告" not in texts, (
        "save 發生在較早回合，最終卻誤發記憶幻覺警告（跨輪偵測遺漏）"
    )


@pytest.mark.asyncio
async def test_query_in_earlier_round_keeps_emergence_index(monkeypatch, isolate_rhythm):
    """第 1 輪 memory_tools query、最後一輪純文字 → 湧現指標 e_index 應 > 0。

    舊碼從最後一輪空的 tool_use_blocks 推導 has_queried=False，使 e_index 被算成 0。
    """
    sid, rid = "sess-mem-query-1", "req-mem-query-1"
    rhythm_service.clear_session(sid)

    query_block = _block(
        "tool_use", cid="c1", name="memory_tools", inp={"action": "query", "query": "x"}
    )
    round1 = _response(
        [_block("text", text="我先查一下記憶。"), query_block],
        [
            {"type": "text", "text": "我先查一下記憶。"},
            {"type": "tool_use", "id": "c1", "name": "memory_tools", "input": {"action": "query"}},
        ],
    )
    round2 = _response(
        [_block("text", text="我查到了相關的記憶，任務結束。")],
        [{"type": "text", "text": "我查到了相關的記憶，任務結束。"}],
    )

    context = _ctx(sid, rid)
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=[round1, round2])
    context["anthropic_client"] = delegate_stream_to_create(client)  # 3b-U1：走 stream()

    p = _mock_executor(monkeypatch)
    try:
        chunks = await _drive(_request(), context)
    finally:
        p.stop()
        rhythm_service.clear_session(sid)

    e_vals = _e_index_values(chunks)
    assert e_vals, "未取得任何 telemetry e_index"
    assert max(e_vals) > 0, (
        "query 發生在較早回合，has_queried 偵測遺漏導致 e_index=0（跨輪偵測遺漏）"
    )


@pytest.mark.asyncio
async def test_interrupt_after_save_preserves_detection(monkeypatch, isolate_rhythm):
    """中斷情境：save 在第 1 輪、第 2 輪換別的工具、第 3 輪被中斷 break。

    中斷在 LLM 回應後、執行工具前 break，此時 tool_use_blocks 殘留為第 2 輪
    （非記憶）的值。舊碼據此推導 has_saved=False → 對宣稱存檔的最終文字誤發警告。
    """
    sid, rid = "sess-mem-int-1", "req-mem-int-1"
    rhythm_service.clear_session(sid)
    rhythm_service.clear_interrupt(sid)

    save_block = _block(
        "tool_use", cid="c1", name="memory_tools", inp={"action": "save", "content": "x"}
    )
    round1 = _response(
        [save_block],
        [{"type": "tool_use", "id": "c1", "name": "memory_tools", "input": {"action": "save"}}],
    )
    other_block = _block(
        "tool_use", cid="c2", name="vault_tools", inp={"vault": "personal", "action": "list"}
    )
    round2 = _response(
        [other_block],
        [{"type": "tool_use", "id": "c2", "name": "vault_tools", "input": {"action": "list"}}],
    )
    round3 = _response(
        [_block("text", text="我已經存檔，謝謝。")],
        [{"type": "text", "text": "我已經存檔，謝謝。"}],
    )

    state = {"n": 0}

    async def _create(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return round1
        if state["n"] == 2:
            return round2
        # 第 3 輪：在 LLM 回應「完成」之際觸發中斷 → handler 於執行工具前 break
        rhythm_service.interrupt_session(sid)
        return round3

    context = _ctx(sid, rid)
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    context["anthropic_client"] = delegate_stream_to_create(client)  # 3b-U1：走 stream()

    p = _mock_executor(monkeypatch)
    try:
        chunks = await _drive(_request(), context)
    finally:
        p.stop()
        rhythm_service.clear_session(sid)
        rhythm_service.clear_interrupt(sid)

    texts = _decode_texts(chunks)
    assert "已由使用者中斷" in texts  # 確認確實走到中斷 break 路徑
    assert "記憶幻覺警告" not in texts, (
        "save 發生在中斷前較早回合，中斷後卻誤發記憶幻覺警告（殘留 tool_use_blocks 誤判）"
    )

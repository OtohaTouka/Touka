"""Gemini 顯式快取全停（ADR-0243；ADR-0241 決策 4 原本只停無痕，現改為全停）。

一般與無痕對話、初次與迴圈中，每一輪送出的 payload 都不含 ``cachedContent``、都帶
``system_instruction`` 與 ``tools``、contents 不塌縮；傳輸層只看得到主迴圈串流端點
``:streamGenerateContent?alt=sse``（3b-U3），沒有任何對 ``cachedContents`` 的連線
（``httpx.AsyncClient.send`` 在類別層級攔截全部請求，其餘連線由 conftest 網路封鎖抓）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.adapters.gemini_adapter import GeminiAdapter
from src.handlers.gemini_handler import handle_gemini_request
from src.models.identity import AgentIdentity

from tests.helpers.stream_transport import FakeUpstream, gemini_stream_reply

_URL = ("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash"
        ":streamGenerateContent?alt=sse")
# httpx 自己加的標頭；扣掉後，產品程式碼送的標頭只能有金鑰這一個
_HTTPX_HEADERS = {"host", "accept", "accept-encoding", "connection", "user-agent",
                  "content-length", "content-type"}
_SYSTEM = {"parts": [{"text": "You are Sumi, a helpful soul."}]}
_TOOLS = [{"functionDeclarations": [{
    "name": "vault_tools", "description": "vault ops",
    "parameters": {"type": "OBJECT", "properties": {}},
}]}]
_MODES = [("incognito", {"incognito": True}), ("normal", {"incognito": False}), ("absent", {})]
_MODE_IDS = [tag for tag, _ in _MODES]


def _msg(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, tool_calls=None)


def _request() -> MagicMock:
    request = MagicMock()
    request.model = "gemini-2.5-flash"
    request.messages = [
        _msg("system", "You are Sumi, a helpful soul."),
        _msg("user", "hi"),
        _msg("assistant", "hello"),
        _msg("user", "do things"),
    ]
    request.tools = [{"type": "function", "function": {
        "name": "vault_tools", "description": "vault ops",
        "parameters": {"type": "object", "properties": {}},
    }}]
    request.pulse_slots = None
    request.safety_settings = None
    return request


def _ctx(tag: str, **extra) -> dict:
    ctx = {
        "gemini_api_key": "fake-key",
        "protocol_manager": MagicMock(get_all_tools=AsyncMock(return_value=[])),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": f"req-nocache-{tag}",
        "t0": 0.0,
        "agent_id": "Sumi",
        "identity": AgentIdentity.for_role("Sumi", "Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": f"sess-nocache-{tag}",
    }
    ctx.update(extra)
    return ctx


def _tool_resp(action: str) -> dict:
    call = {"name": "vault_tools", "args": {"action": action}}
    return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": call}]},
                            "finishReason": "STOP"}]}


def _text_resp(text: str) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": "STOP"}]}


async def _run(responses: list[dict], ctx: dict) -> tuple[list[dict], str]:
    """跑完整迴圈；回傳每一次請求的 (url, 產品送的標頭, payload) 與下游 SSE。"""
    replies = [gemini_stream_reply(r, "semantic") for r in [*responses, _text_resp("Done!")]]
    upstream = FakeUpstream(replies)

    async def fake_execute(name, args, req_id, c, q, **kwargs):
        await q.put({"type": "log", "content": f"[{name} done]"})
        await q.put(None)
        return "Tool Success"

    with pytest.MonkeyPatch.context() as monkeypatch, patch(
        "src.handlers.react_core.ToolExecutor"
    ) as executor:
        upstream.install(monkeypatch)
        executor.return_value.execute_to_queue = AsyncMock(side_effect=fake_execute)
        chunks = []
        async for chunk in handle_gemini_request(_request(), ctx):
            chunks.append(chunk)
            if len(chunks) > 300:  # 防呆
                break
    upstream.assert_all_closed()
    posts = [{"url": str(request.url), "payload": payload,
              "headers": {k: v for k, v in request.headers.items() if k not in _HTTPX_HEADERS}}
             for request, payload in zip(upstream.requests, upstream.sent, strict=True)]
    return posts, "".join(chunks)


def _assert_only_generate_content(posts: list[dict], rounds: int) -> None:
    assert len(posts) == rounds, "每輪恰一次請求，不得多出任何快取連線"
    for post in posts:
        assert post["url"] == _URL
        assert "cachedContents" not in post["url"]
        assert post["headers"] == {"x-goog-api-key": "fake-key"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("tag", "mode"), _MODES, ids=_MODE_IDS)
async def test_initial_payload_is_complete_without_cached_content(tag, mode):
    adapter = GeminiAdapter()
    state = await adapter.prepare_request(_request(), _ctx(f"init-{tag}", **mode))
    try:
        payload = state.payload
        assert sorted(payload) == ["contents", "system_instruction", "tools"]
        assert payload["system_instruction"] == _SYSTEM
        assert payload["tools"] == _TOOLS
        assert [c["role"] for c in payload["contents"]] == ["user", "model", "user"]
    finally:
        await adapter.close(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tag", "mode"), _MODES, ids=_MODE_IDS)
async def test_long_loop_never_sends_cached_content(tag, mode):
    """5 個工具輪讓 contents 越過 10 則（舊的重快取門檻），第 6 輪回文字收尾。"""
    responses = [_tool_resp(str(i)) for i in range(5)] + [_text_resp("長迴圈完成。")]
    posts, sse = await _run(responses, _ctx(f"loop-{tag}", **mode))

    _assert_only_generate_content(posts, 6)
    payloads = [p["payload"] for p in posts]
    for payload in payloads:
        assert "cachedContent" not in payload
        assert payload["system_instruction"] == _SYSTEM
        assert payload["tools"] == _TOOLS
        assert "tool_config" not in payload
    assert [len(p["contents"]) for p in payloads] == [3, 5, 7, 9, 11, 13]
    assert payloads[-1]["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert "長迴圈完成。" in sse


@pytest.mark.asyncio
@pytest.mark.parametrize(("tag", "mode"), _MODES, ids=_MODE_IDS)
async def test_tool_config_rounds_still_carry_system_and_tools(tag, mode):
    """矯正輪（ANY）與之後的工具輪（AUTO）：tool_config 與 system／tools 同送、不帶快取。"""
    responses = [
        _text_resp("根據過去的記憶系統，你喜歡紅茶。"),
        _tool_resp("read"),
        _text_resp("查到了。"),
    ]
    posts, _sse = await _run(responses, _ctx(f"tc-{tag}", **mode))

    _assert_only_generate_content(posts, 3)
    payloads = [p["payload"] for p in posts]
    assert [p.get("tool_config") for p in payloads] == [
        None,
        {"function_calling_config": {"mode": "ANY"}},
        {"function_calling_config": {"mode": "AUTO"}},
    ]
    for payload in payloads:
        assert "cachedContent" not in payload
        assert payload["system_instruction"] == _SYSTEM
        assert payload["tools"] == _TOOLS

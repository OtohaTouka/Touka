"""TDD — root-cause-B：聊天供應商金鑰免重啟即時生效（dispatcher factory 層）。

背景：`registry.get_key` 是即時金鑰 SSOT（以 getattr 讀 src.config 模組常數，apply_runtime_env
會更新之）。但兩條路徑繞過它、於「進程啟動時」快照金鑰，導致精靈寫入新金鑰後、不重啟就無法對話：
  1. Gemini：`factory.py` 用 `from src.config import GEMINI_API_KEY`（from-import 綁定，永不隨
     src.config 更新）。
  2. Anthropic：`main.py` 於匯入時以當時 os.environ 建好 AsyncAnthropic client，claude_handler
     沿用該 baked-key client。

本測試以 monkeypatch 模擬「執行期金鑰改變」（等同 apply_runtime_env 更新 src.config 常數），
驗證 factory 產出的 context 帶即時金鑰。teardown 由 monkeypatch 自動還原、不洩漏全域狀態。
"""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.identity import AgentIdentity
from src.services.dispatchers import factory
from src.services.models.manager import ModelManager

# 測試身分：一律 DebugAgent（不動其他 agent 資料）。
_IDENTITY = AgentIdentity.for_role(
    agent_id="DebugAgent", role="admin", is_human=True, capabilities=[]
)


async def _noop_stream(_req, _ctx):  # pragma: no cover - 僅為可呼叫的 async generator 佔位
    if False:
        yield ""


def _openai_req(model: str) -> OpenAIChatRequest:
    return OpenAIChatRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_factory_gemini_uses_live_key(monkeypatch):
    """執行期寫入新 GEMINI_API_KEY 後，factory 的 context['gemini_api_key'] 應為即時值。"""
    # 模擬執行期金鑰改變（apply_runtime_env 等效：更新 src.config 常數）。
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "live-gemini", raising=False)
    monkeypatch.setattr(factory, "handle_gemini_request", _noop_stream)
    # 避免動態覆寫干擾；patch 類別方法（勿 patch 單例實例的繼承方法，會殘留污染）。
    monkeypatch.setattr(ModelManager, "get_models", lambda self: [])

    ctx: dict = {}
    await factory.get_stream_generator(
        "gemini-1.5-pro", _openai_req("gemini-1.5-pro"), _IDENTITY, "rid", 0.0, ctx
    )
    assert ctx["gemini_api_key"] == "live-gemini"


@pytest.mark.asyncio
async def test_factory_anthropic_client_uses_live_key(monkeypatch):
    """執行期寫入新 ANTHROPIC_API_KEY 後，factory 應把即時金鑰套用到 claude client。"""
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "sk-live-anthropic", raising=False)
    monkeypatch.setattr(factory, "handle_claude_request", _noop_stream)

    # 啟動時的金鑰快照 client（空金鑰）——即 main.py 於匯入時建立者的等價物。
    stale_client = AsyncAnthropic(api_key="")
    ctx: dict = {"anthropic_client": stale_client}
    try:
        await factory.get_stream_generator(
            "claude-3-5-sonnet", _openai_req("claude-3-5-sonnet"), _IDENTITY, "rid", 0.0, ctx
        )
        # context 中的 client 已套用即時金鑰（免重啟）。
        assert ctx["anthropic_client"].api_key == "sk-live-anthropic"
        # with_options 回傳副本：原快照 client 不被污染。
        assert stale_client.api_key == ""
    finally:
        await ctx["anthropic_client"].close()


@pytest.mark.asyncio
async def test_factory_live_key_swap_keeps_no_redirect_http_client(monkeypatch):
    """3a-U2（禁令 22）：換即時金鑰後仍沿用啟動時注入的 no-redirect http client（不重建）。"""
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-live-key", raising=False)
    monkeypatch.setattr(factory, "handle_claude_request", _noop_stream)

    http_client = DefaultAsyncHttpxClient(follow_redirects=False)  # 同 main.py 建構點
    boot_client = AsyncAnthropic(api_key="", http_client=http_client)
    ctx: dict = {"anthropic_client": boot_client}
    try:
        await factory.get_stream_generator(
            "claude-opus-5-5", _openai_req("claude-opus-5-5"), _IDENTITY, "rid", 0.0, ctx
        )
        swapped = ctx["anthropic_client"]
        assert swapped is not boot_client and swapped.api_key == "fake-live-key"
        assert swapped._client is http_client
        assert swapped._client.follow_redirects is False
    finally:
        await boot_client.close()


@pytest.mark.asyncio
async def test_factory_anthropic_no_key_keeps_client(monkeypatch):
    """無即時金鑰時不更動 client（避免以空金鑰覆寫；主 bug 情境為空→有）。"""
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr(factory, "handle_claude_request", _noop_stream)

    stale_client = AsyncAnthropic(api_key="boot-key")
    ctx: dict = {"anthropic_client": stale_client}
    try:
        await factory.get_stream_generator(
            "claude-3-5-sonnet", _openai_req("claude-3-5-sonnet"), _IDENTITY, "rid", 0.0, ctx
        )
        assert ctx["anthropic_client"] is stale_client  # 未替換
    finally:
        await ctx["anthropic_client"].close()

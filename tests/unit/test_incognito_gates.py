"""無痕模式後端閘門單元測試（HAX G17，ADR-0086）。

逐一證明每個持久化/記憶寫入點在 incognito 時關閉：dispatcher 落盤、ToolLayer 記憶工具注入；
並確認旗標貫穿 schema 與 PipelineContext。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import AnthropicRequest, OpenAIChatRequest
from src.middleware.layers import soul as soul_mod
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import dispatcher
from src.services.soul_service import soul_service

_OWNER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=[])


async def _gen(chunks):
    for chunk in chunks:
        yield chunk


def _patch_buffer(monkeypatch):
    monkeypatch.setattr(dispatcher, "_extract_delta_text", lambda _c: "hi")
    monkeypatch.setattr(dispatcher.stream_buffer_service, "append", AsyncMock())
    monkeypatch.setattr(dispatcher.stream_buffer_service, "finish", AsyncMock())
    database_read = AsyncMock(return_value=None)
    monkeypatch.setattr(dispatcher.session_service, "get_session_by_id", database_read)
    trim = AsyncMock()
    monkeypatch.setattr(dispatcher, "_maybe_trim_after_chat", trim)
    spawn = dispatcher.spawn_logged
    tasks = []

    def capture(coro, *args, **kwargs):
        task = spawn(coro, *args, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(dispatcher, "spawn_logged", capture)
    return tasks, database_read, trim


@pytest.mark.asyncio
async def test_dispatcher_skips_persist_when_incognito(monkeypatch):
    persist = AsyncMock()
    monkeypatch.setattr(dispatcher, "_persist_assistant_turn", persist)
    tasks, database_read, trim = _patch_buffer(monkeypatch)
    await dispatcher._pump_stream_to_buffer(_gen(["x"]), "s1", "m", _OWNER, incognito=True)
    await asyncio.gather(*tasks)
    persist.assert_not_awaited()
    database_read.assert_not_awaited()
    trim.assert_not_awaited()
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_dispatcher_persists_when_not_incognito(monkeypatch):
    persist = AsyncMock()
    monkeypatch.setattr(dispatcher, "_persist_assistant_turn", persist)
    tasks, database_read, trim = _patch_buffer(monkeypatch)
    await dispatcher._pump_stream_to_buffer(_gen(["x"]), "s1", "m", _OWNER, incognito=False)
    await asyncio.gather(*tasks)
    persist.assert_awaited_once()
    database_read.assert_not_awaited()
    trim.assert_awaited_once_with("s1")  # 剪裁身分由家對話歸屬決定，不傳請求者
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_toollayer_skips_memory_tools_when_incognito():
    ctx = PipelineContext(identity=_OWNER)
    ctx["agent_id"] = _OWNER.agent_id
    ctx["is_custom"] = False
    ctx["is_toukasoul"] = False
    ctx["headers"] = {}
    ctx["memory_tools_schema"] = [
        {
            "type": "function",
            "function": {"name": "memory_tools", "description": "m", "parameters": {}},
        }
    ]
    ctx["incognito"] = True
    req = MagicMock()
    req.tools = []
    req.model = "gemini-2.5-flash"
    req.project_id = None
    await ToolLayer().process(req, ctx)
    names = [t.get("function", {}).get("name") for t in (req.tools or [])]
    assert "memory_tools" not in names


def test_request_schema_accepts_incognito():
    oai = OpenAIChatRequest(model="m", messages=[{"role": "user", "content": "hi"}], incognito=True)
    anth = AnthropicRequest(model="m", messages=[{"role": "user", "content": "hi"}], incognito=True)
    assert oai.incognito is True
    assert anth.incognito is True


def test_pipeline_context_carries_incognito():
    ctx = PipelineContext()
    ctx["incognito"] = True
    assert ctx.get("incognito") is True


@pytest.mark.asyncio
async def test_soullayer_skips_home_messages_when_incognito(monkeypatch):
    """完全失憶：無痕時 SoulLayer 不注入家對話（記憶錨點），但保留人格。"""
    inject = AsyncMock()
    monkeypatch.setattr(soul_service, "inject_home_messages", inject)
    monkeypatch.setattr(soul_service, "apply_soul_to_request", MagicMock())
    monkeypatch.setattr(soul_service, "get_soul_data", MagicMock(return_value=("soul", {})))
    monkeypatch.setattr(soul_mod, "get_active_brain", MagicMock(return_value="gemini-2.5-flash"))

    ctx = PipelineContext(identity=_OWNER)
    ctx["is_toukasoul"] = True
    ctx["agent_id"] = "DebugAgent"
    ctx["incognito"] = True
    req = MagicMock()
    req.model = "ToukaSoul-X"
    req.messages = []

    await soul_mod.SoulLayer().process(req, ctx)
    inject.assert_not_awaited()

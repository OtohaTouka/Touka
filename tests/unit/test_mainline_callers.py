"""主線的呼叫端（3d PR 1，ADR-0305；審查 F5）：真的走過心跳脈衝與群聊派送.

- 心跳脈衝（在子對話）：送出的請求帶前情，不帶家原文（全文太貴）。
- 群聊：無痕時交給主線的是無痕模式（前情與家原文都不帶），一般時是群聊模式。

主線本身的對照表在 ``test_mainline_service``；這裡確認呼叫端接對了。
全部離線：SQLite 只在測試沙盒；模型派送換成假的，只攔下送出的請求。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.session_persistence import SavedResult
from src.models.sliding_window import SlidingWindowConfig
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services import continuity_service, mainline_service
from src.services.dispatchers import group
from src.services.soul_service import SoulService
from src.utils import async_db
from src.utils import soul_prelude_db as db_mod
from src.utils.async_db import SessionSnapshot
from src.utils.soul_prelude_db import PreludeSegment

from tests.support.session_scope import A, seed
from tests.support.session_scope import scope_db as scope_db

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("scope_db")]

HOME_TREE = {
    "messages": {
        "u": {"id": "u", "role": "user", "content": "家裡的問題", "parentId": None},
        "a": {"id": "a", "role": "assistant", "content": "家裡的回答", "parentId": "u"},
    },
    "currentId": "a",
}


@pytest.fixture(autouse=True)
async def _home_and_prelude(scope_db, monkeypatch):
    _ = scope_db  # 先建好沙盒資料庫
    monkeypatch.setattr(mainline_service, "load_sliding_window_config", SlidingWindowConfig)
    await seed("home", tree=HOME_TREE)
    assert await async_db.session_service.set_home("home", A, True)
    await db_mod.insert_segment(
        PreludeSegment(
            agent=A.agent_id,
            kind="home_trim",
            source_session="home",
            dedupe_key="m0",
            content="前情的一段",
            created_at=1,
        )
    )


def _text(message: Any) -> str:
    return str(message.content)


async def _no_chunks() -> Any:
    for _ in ():
        yield ""


async def test_heartbeat_pulse_sends_the_prelude_but_not_the_home(monkeypatch):
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    pulse_session = MagicMock(
        read_execution_snapshot=AsyncMock(
            return_value=SessionSnapshot(
                {"id": "child", "title": "t", "messages": [{"role": "user", "content": "hi"}]},
                "rev",
            )
        ),
        conditional_save_session=AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="next")
        ),
    )
    monkeypatch.setattr(heartbeat, "session_service", pulse_session)
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    monkeypatch.setattr(continuity_service, "_last_request_at", OrderedDict())

    await heartbeat._execute_single_pulse(
        "child", A, "claude-sonnet-4-6", PipelineContext(), 1, 0.0, ""
    )

    sent = captured["request"].messages
    system = "".join(_text(m) for m in sent if m.role == "system")
    assert "<soul_prelude>" in system and "前情的一段" in system
    assert all("家裡的問題" not in _text(m) for m in sent)


@pytest.mark.parametrize(("incognito", "mode"), [(True, "incognito"), (False, "group")])
async def test_group_hands_the_right_mode_to_the_mainline(monkeypatch, incognito, mode):
    modes: list[str] = []

    async def spy(_request: Any, _identity: Any, *, mode: str, **_kwargs: Any) -> None:
        modes.append(mode)

    async def _fake_stream(*_args: Any) -> Any:
        async def _gen() -> Any:
            yield 'data: {"choices": [{"delta": {"content": "嗨"}, "index": 0}]}\n\n'
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(group, "inject_mainline", spy)
    monkeypatch.setattr(group, "get_stream_generator", _fake_stream)
    monkeypatch.setattr(group, "get_active_brain", lambda _identity: "Brain-X")
    monkeypatch.setattr(group, "record_working_brain", lambda *_a: None)
    monkeypatch.setattr(group, "soul_service", MagicMock())
    monkeypatch.setattr(
        group, "parse_group_roster", lambda *_a: [{"name": "小愛", "id": A.agent_id}]
    )
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content="@小愛 早")]
    )
    ctx = PipelineContext(incognito=incognito)
    async for _chunk in group.dispatch_group_stream(request, "req-g", 0.0, ctx):
        pass
    assert modes == [mode]

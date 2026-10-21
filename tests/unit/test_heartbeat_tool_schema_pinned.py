"""model-p1a U6 → ADR-0246 U2c-5：心跳送出的請求帶工具（KF-4 已修）。

原釘子 ``test_kf4_heartbeat_request_has_no_tools``（心跳繞過 ToolLayer、請求沒有工具）照
docstring「修正時改這個測試」改成新期望並改名：心跳在 ``dispatch_request`` 前跑 ToolLayer。
比照 test_no_sampling_on_wire.py 攔下 dispatch_request 收到的請求。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services.soul_service import SoulService
from src.utils.async_db import SessionSnapshot


async def _no_chunks():
    for _ in ():
        yield ""


async def test_kf4_heartbeat_request_carries_tools(monkeypatch):
    """KF-4 已修（ADR-0242 → ADR-0246）：心跳交給 dispatch_request 的請求帶工具 schema。"""
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)  # 不污染 singleton
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    session = {"id": "s", "title": "t", "messages": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr(
        heartbeat, "session_service",
        MagicMock(read_execution_snapshot=AsyncMock(return_value=SessionSnapshot(session, "rev"))),
    )
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    identity = AgentIdentity.for_role(agent_id="Alice", role="Owner")

    await heartbeat._execute_single_pulse(
        "s", identity, "claude-sonnet-4-6", PipelineContext(), 1, 0.0, ""
    )

    assert captured["context"].get("enable_autonomy") is True  # 前提：確實是無人心跳 run
    assert captured["context"].auth_level == "Manual"  # 沒有設定時決定的模式 → 最嚴
    request = captured["request"]
    assert request.model == "claude-sonnet-4-6"
    assert [m.role for m in request.messages] == ["system", "user"]
    names = {(t.get("function") or {}).get("name") for t in request.tools or []}
    assert "write" in names

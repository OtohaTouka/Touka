"""ADR-0250 A3：沒設主要模型時，群聊與心跳回清楚的錯誤、不當掉。

- 場景 9：群聊點名 @A、@B，B 沒有主要模型 → 整輪不派發，串流裡點名 B（HTTP 200，和其他群聊
  錯誤同形），伺服器不丟 AttributeError；同時 warning 記錯誤碼名稱與成員 ID。名字模糊（A5）同。
- 場景 10：心跳 ``/awaken`` 沒有 ``model_override``、靈魂沒有主要模型 → 409
  ``AGENT_BRAIN_NOT_SET``，不啟動背景迴圈。
- 背景迴圈拿到解析不到的模型（喚醒後 brain 被清掉、或經 ``/retrigger`` 進來）→ 記 log、結束
  這次迴圈，不丟例外、不寫錯誤訊息進對話、不送出請求。
全部 mock，不送任何廠商請求。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers import heartbeat
from src.services.auth_service import AuthService
from src.services.dispatchers import group
from src.services.models import model_manager
from src.services.models.routes import (
    CustomModelRoute,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_AWAKEN_KEY = "test_heartbeat_token_for_ci"
_BRAINS = {"SoulA": "claude-a", "SoulB": None}


def _ambiguous_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    route = CustomModelRoute(connection_id="c", endpoint="http://127.0.0.1:1", api_key="",
                             provider="X")
    snapshot = ModelRouteSnapshot.create(
        generation=1, config_fingerprint="sandbox", active_routes={"Foo": route, "foo": route},
        state=ModelRouteSnapshotState.READY,
    )
    monkeypatch.setattr(model_manager, "_route_snapshot", snapshot)


# ── 場景 9：群聊 ────────────────────────────────────────────────────────────


@pytest.fixture
def group_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def _fake_stream(model: str, *_args: Any) -> Any:
        calls.append(model)

        async def _gen() -> Any:
            yield "data: [DONE]\n\n"

        return _gen()

    roster = [{"name": "A", "id": "SoulA"}, {"name": "B", "id": "SoulB"}]
    monkeypatch.setattr(group, "parse_group_roster", lambda *_a: roster)
    monkeypatch.setattr(group, "get_active_brain", lambda who: _BRAINS[who.agent_id])
    monkeypatch.setattr(group, "get_stream_generator", _fake_stream)
    monkeypatch.setattr(group, "record_working_brain", lambda *_a: None)
    monkeypatch.setattr(group, "soul_service", MagicMock(inject_home_messages=AsyncMock()))
    return calls


async def _group_text() -> str:
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content="@A @B 早")]
    )
    out: list[str] = []
    async for chunk in group.dispatch_group_stream(request, "req-g", 0.0, PipelineContext()):
        payload = chunk.removeprefix("data: ").strip()
        if payload.startswith("{"):
            delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
            out.append(delta.get("content", ""))
    return "".join(out)


async def test_group_member_without_brain_stops_the_whole_round(
    group_calls: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="src.services.dispatchers.group"):
        text = await _group_text()
    assert "『@B』還沒設定主要模型" in text
    assert group_calls == []  # A 也不派發
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AGENT_BRAIN_NOT_SET" in m and "SoulB" in m for m in warned), warned


async def test_group_member_with_ambiguous_brain_stops_the_whole_round(
    group_calls: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _ambiguous_routes(monkeypatch)
    monkeypatch.setitem(_BRAINS, "SoulB", "FOO")
    with caplog.at_level(logging.WARNING, logger="src.services.dispatchers.group"):
        text = await _group_text()
    assert "『@B』" in text and "只差大小寫" in text
    assert group_calls == []
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("MODEL_NAME_AMBIGUOUS" in m and "SoulB" in m for m in warned), warned


async def test_group_members_with_brains_are_dispatched(
    group_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """釘住：每位成員都有主要模型時照常依序派發。"""
    monkeypatch.setitem(_BRAINS, "SoulB", "Claude-B")
    text = await _group_text()
    assert "【A】" in text and "【B】" in text
    assert group_calls == ["claude-a", "claude-b"]


# ── 場景 10：/awaken ────────────────────────────────────────────────────────


@pytest.fixture
def awaken(monkeypatch: pytest.MonkeyPatch, mock_identity: Any) -> AsyncMock:
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", _AWAKEN_KEY)
    loop = AsyncMock(return_value=None)
    monkeypatch.setattr(heartbeat, "_process_heartbeat_loop", loop)
    soul = mock_identity(agent_id="HbNoBrain", capabilities=["heartbeat_awakening"])
    monkeypatch.setattr(heartbeat.identity_service, "get_identity", lambda _x: soul)
    monkeypatch.setattr(heartbeat.permission_service, "can_awaken", lambda _x: True)
    owner = User(id="o", email="o@test.com", hashed_password="...",
                 owned_agents=["HbNoBrain"], refresh_token_exp=9999999999)
    monkeypatch.setattr(AuthService, "get_all_users", lambda _self: [owner])
    monkeypatch.setattr(heartbeat.session_service, "get_home_session_id", AsyncMock())
    monkeypatch.setattr(heartbeat.session_service, "get_session_by_id", AsyncMock())
    monkeypatch.setattr(heartbeat.session_service, "upsert_session", AsyncMock())
    monkeypatch.setattr(heartbeat.session_service, "append_message_to_session", AsyncMock())
    return loop


def _awaken(client: TestClient, **extra: Any) -> Any:
    body = {"token": _AWAKEN_KEY, "agent_id": "HbNoBrain", "pulse_count": 1, **extra}
    return client.post("/v1/heartbeat/awaken", json=body)


def test_awaken_without_brain_or_override_is_409(auth_client: TestClient, awaken: AsyncMock):
    resp = _awaken(auth_client)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_BRAIN_NOT_SET"
    awaken.assert_not_called()


def test_awaken_with_override_still_runs(auth_client: TestClient, awaken: AsyncMock):
    """釘住：排程有指定模型時不看主要模型。"""
    resp = _awaken(auth_client, model_override="claude-x")
    assert resp.status_code == 202, resp.text
    awaken.assert_called_once()


def test_awaken_with_brain_still_runs(
    auth_client: TestClient, awaken: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: "claude-x")
    resp = _awaken(auth_client)
    assert resp.status_code == 202, resp.text
    awaken.assert_called_once()


# ── 背景迴圈 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("brain", "code"),
    [(None, "AGENT_BRAIN_NOT_SET"), ("FOO", "MODEL_NAME_AMBIGUOUS")],
    ids=["no-brain", "ambiguous"],
)
async def test_loop_without_a_usable_model_logs_and_ends(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    brain: str | None,
    code: str,
) -> None:
    _ambiguous_routes(monkeypatch)
    identity = AgentIdentity.for_role(agent_id="HbLoop", role="Owner")
    session = {"id": "s", "title": "t", "messages": [{"role": "user", "content": "hi"}]}
    sessions = MagicMock(
        get_session_by_id=AsyncMock(return_value=session),
        upsert_session=AsyncMock(),
        append_message_to_session=AsyncMock(),
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(heartbeat, "session_service", sessions)
    monkeypatch.setattr(heartbeat, "dispatch_request", dispatch)
    monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: brain)
    monkeypatch.setattr(heartbeat.model_manager, "check_models_health", AsyncMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", AsyncMock())

    with caplog.at_level(logging.WARNING, logger="src.routers.heartbeat"):
        await heartbeat._process_heartbeat_loop(
            final_session_id="s", identity=identity, model_override=None,
            ctx=PipelineContext(), pulse_count=3, pulse_slots=1, t0=0.0, init_prompt="",
        )

    dispatch.assert_not_called()
    sessions.append_message_to_session.assert_not_called()  # 沒有「多脈衝任務執行失敗」
    sessions.upsert_session.assert_not_called()  # 沒有補 nudge
    warned = [r.getMessage() for r in caplog.records if code in r.getMessage()]
    assert len(warned) == 1, warned  # 結束這次迴圈：三個脈衝只試一次
    assert "HbLoop" in warned[0]

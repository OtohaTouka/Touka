"""前情摘要與回覆寫入要看 session 歸屬（U2-SESSION；R-2a ① 再審第 2 次問題 2；禁令 20）。

修補前：靈魂對話的前情摘要（`context_summary`）只用 session_id 查、不看 session 屬於誰；
沒帶 `ui_persistence` 的舊串流路徑（`resumable_stream`）也只用 session_id 把回覆寫回去。
擁有 Bob 的人送「model／標頭 Bob＋別人 session 的 session_id」，就會讀到對方的前情摘要，
回覆還會寫進對方的 session（連 session 綁的 agent 都被改成 Bob）。SNS 的 session ID 可預測。

修補後：session 綁的 agent（正式 ID）等於這次要注入的靈魂、而且呼叫者能驅動那個靈魂，
才注入前情摘要、才在舊串流路徑寫回；不符合就略過（不注入、不寫入）。

全部離線：上游以假的串流取代；session 只寫在測試沙盒的資料庫。
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.middleware.layers.soul import SoulLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.services.identity_router import get_verified_identity
from src.services.sns_gateway import SNSGatewayService
from src.services.soul_service import soul_service
from src.utils.async_db import get_async_db, session_service
from src.utils.history_manager import TreeMessageManager

ALICE = "U2xAlice"
BOB = "U2xBob"
SOULS = {ALICE, BOB}
REPLY = "BOB-REPLY-TEXT"
_PATHS = ["/v1/messages", "/v1/chat/completions"]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """ALICE／BOB 被認成靈魂；上游換成假串流，記錄送出的請求。"""
    seen: dict[str, Any] = {"requests": []}

    async def _no_home(*_args: Any) -> None:
        return None

    async def _fake_stream(_model: str, request: Any, *_args: Any, **_kwargs: Any) -> Any:
        seen["requests"].append(request.model_dump_json())

        async def _gen() -> Any:
            chunk = {"choices": [{"delta": {"content": REPLY}, "index": 0}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(
        soul_service, "is_toukasoul", lambda who: getattr(who, "agent_id", who) in SOULS
    )
    monkeypatch.setattr(soul_service, "get_soul_data", lambda _agent_id: ("PERSONA", {}))
    monkeypatch.setattr(soul_service, "apply_soul_to_request", lambda *_args: None)
    monkeypatch.setattr(soul_service, "inject_home_messages", _no_home)
    monkeypatch.setattr(
        "src.middleware.layers.soul.get_active_brain", lambda _identity: "claude-probe"
    )
    monkeypatch.setattr("src.services.dispatcher.get_stream_generator", _fake_stream)
    return seen


@pytest.fixture
def client() -> Any:
    owner_of_bob = AgentIdentity.for_role(
        "u2x-bob-owner", "normal", is_human=True, owned_agents=[BOB]
    )
    app.dependency_overrides[get_verified_identity] = lambda: owner_of_bob
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


async def _make_session(sid: str, soul: str, summary: str) -> None:
    initial_id = f"legacy-seed-{sid}" if sid.startswith("sns_") else sid
    await session_service.upsert_session(
        initial_id,
        "u2x probe",
        TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity.for_role(soul, "Owner"),
        context_summary=summary,
    )
    if initial_id != sid:
        # 模擬升級前已存在的共享頻道；受管建立已不接受退休 ID。
        async with get_async_db() as db:
            await db.execute("UPDATE user_sessions SET id = ? WHERE id = ?", (sid, initial_id))
            await db.commit()


def _chat(client: TestClient, path: str, sid: str) -> Any:
    body: dict[str, Any] = {
        "model": BOB,
        "session_id": sid,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    return client.post(path, json=body, headers={"X-Touka-Soul-ID": BOB})


def _victim_sid(kind: str) -> str:
    # SNS 的 session ID 可預測：sns_{platform}_{channel}
    suffix = uuid.uuid4().hex[:8]
    return f"sns_telegram_u2x{suffix}" if kind == "sns" else f"u2x-alice-{suffix}"


# --------------------------------------------------------------------------- #
# 修補前會紅：別人的 session
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["ui", "sns"])
@pytest.mark.parametrize("path", _PATHS)
async def test_other_souls_session_summary_is_not_injected(
    path: str, kind: str, probe: dict[str, Any], client: TestClient
) -> None:
    """擁有 Bob 的人帶 Alice 的 session_id → 不注入 Alice 的前情摘要。"""
    sid = _victim_sid(kind)
    await _make_session(sid, ALICE, "ALICE-SECRET-SUMMARY")
    resp = _chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    assert len(probe["requests"]) == 1
    assert "ALICE-SECRET-SUMMARY" not in probe["requests"][0]


@pytest.mark.parametrize("kind", ["ui", "sns"])
@pytest.mark.parametrize("path", _PATHS)
async def test_reply_is_not_written_into_other_souls_session(
    path: str, kind: str, probe: dict[str, Any], client: TestClient
) -> None:
    """同樣情境：回覆不寫進 Alice 的 session，session 綁的 agent 也不被改成 Bob。"""
    sid = _victim_sid(kind)
    await _make_session(sid, ALICE, "ALICE-SECRET-SUMMARY")
    resp = _chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    assert REPLY in resp.text
    after = await session_service.get_session_by_id(sid)
    assert after is not None
    assert after["agent_id"] == ALICE
    assert REPLY not in json.dumps(after["messages"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 照舊可以：自己的靈魂的 session
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _PATHS)
async def test_own_souls_session_still_gets_summary_and_reply(
    path: str, probe: dict[str, Any], client: TestClient
) -> None:
    """Bob 自己的 session → 前情摘要照注入、回覆照寫回。"""
    sid = f"u2x-bob-{uuid.uuid4().hex[:8]}"
    await _make_session(sid, BOB, "BOB-OWN-SUMMARY")
    resp = _chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    assert "BOB-OWN-SUMMARY" in probe["requests"][0]
    after = await session_service.get_session_by_id(sid)
    assert after is not None
    assert after["agent_id"] == BOB
    assert REPLY in json.dumps(after["messages"], ensure_ascii=False)


async def test_legacy_prefixed_binding_is_compared_by_canonical_id(
    probe: dict[str, Any], client: TestClient
) -> None:
    """session 存的是舊前綴 ToukaSoul<Bob> → 正規化成 Bob 再比，照舊注入。"""
    sid = f"u2x-bobprefix-{uuid.uuid4().hex[:8]}"
    await session_service.upsert_session(
        sid,
        "u2x probe",
        TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=f"ToukaSoul{BOB}"),
        context_summary="BOB-PREFIX-SUMMARY",
    )
    resp = _chat(client, "/v1/chat/completions", sid)
    assert resp.status_code == 200, resp.text
    assert "BOB-PREFIX-SUMMARY" in probe["requests"][0]


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        (AgentIdentity.for_role(BOB, "Owner", is_human=False), False),
        (AgentIdentity.for_role(ALICE, "Owner", is_human=False), True),
        (None, True),
    ],
    ids=["other_soul_agent", "the_soul_itself", "internal_no_identity"],
)
async def test_session_binding_also_requires_the_caller_to_drive_the_soul(
    caller: AgentIdentity | None, expected: bool, probe: dict[str, Any], client: TestClient
) -> None:
    """session 綁在 Alice 也不夠：呼叫者還要能驅動 Alice（代理只能是 Alice 本身）。"""
    sid = f"u2x-alicebind-{uuid.uuid4().hex[:8]}"
    await _make_session(sid, ALICE, "ALICE-SECRET-SUMMARY")
    ctx = PipelineContext(identity=caller, session_id=sid)
    assert await SoulLayer()._session_bound(ctx, AgentIdentity(agent_id=ALICE)) is expected


# --------------------------------------------------------------------------- #
# 一般模型：舊串流路徑寫回前也要能存取 session（擁有者或 admin）
# --------------------------------------------------------------------------- #

PLAIN = "u2x-plain-model"


async def _make_plain_session(sid: str, owner: str) -> None:
    await session_service.upsert_session(
        sid,
        "u2x plain",
        TreeMessageManager.create_empty_tree(),
        identity=AgentIdentity(agent_id=PLAIN),
        owner_user_id=owner,
    )


def _plain_chat(client: TestClient, path: str, sid: str) -> Any:
    body: dict[str, Any] = {
        "model": PLAIN,
        "session_id": sid,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    return client.post(path, json=body)


@pytest.mark.parametrize("path", _PATHS)
async def test_plain_model_reply_is_not_written_into_someone_elses_session(
    path: str, probe: dict[str, Any], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """normal 使用者用一般模型、帶別人的 session_id → 回覆不寫進去，也不觸發剪裁。"""
    trim = AsyncMock()
    monkeypatch.setattr("src.services.dispatcher._maybe_trim_after_chat", trim)
    sid = f"u2x-plain-other-{uuid.uuid4().hex[:8]}"
    await _make_plain_session(sid, "u2x-someone-else")
    resp = _plain_chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    assert REPLY in resp.text
    after = await session_service.get_session_by_id(sid)
    assert after is not None
    assert REPLY not in json.dumps(after["messages"], ensure_ascii=False)
    assert trim.called is False


@pytest.mark.parametrize("path", _PATHS)
async def test_plain_model_reply_is_still_written_into_own_session(
    path: str, probe: dict[str, Any], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自己的一般模型對話（owner_user_id 是自己）→ 照舊寫回並觸發剪裁檢查。"""
    trim = AsyncMock()
    monkeypatch.setattr("src.services.dispatcher._maybe_trim_after_chat", trim)
    sid = f"u2x-plain-own-{uuid.uuid4().hex[:8]}"
    await _make_plain_session(sid, "u2x-bob-owner")
    resp = _plain_chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    after = await session_service.get_session_by_id(sid)
    assert after is not None
    assert REPLY in json.dumps(after["messages"], ensure_ascii=False)
    assert trim.called is True


# --------------------------------------------------------------------------- #
# SNS 釘子：程式內部路徑（沒有身分）照舊注入自己 session 的前情摘要
# --------------------------------------------------------------------------- #


async def test_sns_flow_still_replies_on_its_soul(
    probe: dict[str, Any], client: TestClient
) -> None:
    """SNS adapter 綁 Bob，以每靈魂頻道走靈魂路徑並保存回覆。"""
    channel = f"u2x{uuid.uuid4().hex[:8]}"
    sid = await session_service.open_channel_session(
        "sns", AgentIdentity(agent_id=BOB), "telegram", channel, title="SNS"
    )
    await _make_session(sid, BOB, "SNS-OWN-SUMMARY")
    adapter = MagicMock(agent_id=BOB, show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(
        platform="telegram", sender_id="u1", channel_id=channel, content="ping"
    )

    text = await SNSGatewayService().on_event(adapter, event)

    assert text == REPLY
    assert len(probe["requests"]) == 1
    assert '"model":"claude-probe"' in probe["requests"][0]
    adapter.send_message.assert_awaited_once()
    after = await session_service.get_session_by_id(sid)
    assert after is not None
    assert after["agent_id"] == BOB
    assert REPLY in json.dumps(after["messages"], ensure_ascii=False)

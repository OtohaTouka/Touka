"""家知道去過哪、子對話知道自己的編號（ADR-0323 PR 3）.

- 家-4：一般群聊每位說完的成員記一筆 ``group_turn``（主題是對話編號的雜湊，沒有對話編號的 API 群聊
  用固定字）；SNS 頻道與 SNS 群聊在回覆送出後記 ``sns_turn``（群聊照分發器記下的名單一人一筆）。
  兩種都會重複發生，同一段對話說兩次就是兩筆。失敗、沒送到、無痕都不記；SNS 單人在家模式不記，
  SNS 群聊在家模式照記；一般群聊經 SNS 時不記 ``group_turn``。家（不是 SNS 觸發）的 ``<continuity>``
  多一行最近 24 小時的計數與最後時間；都是 0 不加。內容一律不記、不帶。
- 家-5：子對話與心跳頻道的 ``<continuity>`` 寫出這段對話的編號，對得上前情裡「Report from
  conversation …」的標頭；家、SNS、無痕、不回報的內部頻道不寫。

全部離線、在 conftest 沙盒內：活動事件寫測試沙盒的資料庫，群聊成員與 SNS 都用假的模型與平台。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.services import continuity_events as ce
from src.services import continuity_service as cs
from src.services import dispatcher, proxy_service
from src.services.dispatchers import group
from src.services.home_handover import Unreported
from src.services.sns_gateway import SNSGatewayService
from src.utils.async_db import SessionSnapshot, init_db_async
from src.utils.continuity_events_db import EventRow, insert_event, read_snapshot
from src.utils.session_scope import channel_session_id
from src.utils.time_utils import format_epoch_to_taipei

if TYPE_CHECKING:
    from pathlib import Path

NOW = 1_760_500_000.0
SOUL = "PR3HomeSoul"
HOUR = 3600


def _soul(agent_id: str = SOUL) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner", is_human=False)


def _ms(epoch_s: float) -> int:
    return int(epoch_s * 1000)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str | None]]:
    """記錄器收到的（靈魂, 種類, 主題, 對話編號）；不寫資料庫。"""
    calls: list[tuple[str, str, str, str | None]] = []

    def fake(identity: Any, kind: str, subject: str, metadata: Any = None, *,
             session_id: str | None = None) -> None:
        calls.append((identity.agent_id, kind, subject, session_id))

    monkeypatch.setattr(ce.recorder, "record", fake)
    return calls


# ── 事件種類 ─────────────────────────────────────────────────────────────────


def test_group_and_sns_turns_are_repeatable_kinds() -> None:
    for kind in ("group_turn", "sns_turn"):
        assert kind in ce.EVENT_KINDS
        assert kind in ce.REPEATABLE_KINDS
        assert ce.event_id(kind, SOUL, "s", 1) != ce.event_id(kind, SOUL, "s", 2)


def test_the_sns_subject_is_always_a_hash() -> None:
    subject = ce.hashed_ref("telegram:ch1")
    assert subject.startswith("h:")
    assert "ch1" not in subject
    assert ce.safe_ref("a" * 40) == ce.hashed_ref("a" * 40)


@pytest.fixture
async def sandbox_db(tmp_path: Path) -> Any:
    with patch("src.utils.async_db.get_db_path", return_value=tmp_path / "sessions.db"):
        await init_db_async()
        yield


async def _event(kind: str, at_s: float, *, agent: str = SOUL, subject: str = "s1") -> None:
    occurred = _ms(at_s)
    assert await insert_event(
        EventRow(
            event_id=ce.event_id(kind, agent, subject, occurred),
            agent=agent,
            kind=kind,
            occurred_at=occurred,
            subject=subject,
        )
    )


async def test_the_snapshot_counts_turns_elsewhere_in_the_window(sandbox_db: Any) -> None:
    await _event("group_turn", NOW - 3 * HOUR)
    await _event("group_turn", NOW - 600)  # 同一段群聊說第二次：兩筆都留
    await _event("sns_turn", NOW - 2 * HOUR, subject="h:abc")
    await _event("group_turn", NOW - 30 * HOUR)  # 超過 24 小時
    await _event("group_turn", NOW - 60, agent="PR3Other")  # 別的靈魂
    snapshot = await read_snapshot(
        _soul(), session_id=None, exclude_message_id=None, since_ms=0, limit=3,
        elsewhere_since_ms=_ms(NOW - 24 * HOUR),
    )
    assert snapshot.elsewhere == (
        ("group_turn", 2, _ms(NOW - 600)),
        ("sns_turn", 1, _ms(NOW - 2 * HOUR)),
    )
    # 不混進其他行
    assert snapshot.recent == ()
    assert snapshot.last_run is None
    assert snapshot.last_human_ms is None


# ── 群聊分發器：說完的成員 ───────────────────────────────────────────────────

ROSTER = [{"name": "A", "id": "Alice"}, {"name": "B", "id": "Bob"}]


def _install(monkeypatch: pytest.MonkeyPatch, *, fail: set[str] = frozenset()) -> None:
    monkeypatch.setattr(group, "parse_group_roster", lambda *_a: ROSTER)
    monkeypatch.setattr(group.resource_manager, "read_json", lambda *_a: {})
    monkeypatch.setattr(group, "record_working_brain", lambda *_a, **_k: None)
    monkeypatch.setattr(group, "get_active_brain", lambda _i: "m-1")
    monkeypatch.setattr(group, "model_manager", MagicMock(request_model_name=lambda b: b))
    monkeypatch.setattr(group, "soul_service", MagicMock())
    monkeypatch.setattr(group, "inject_mainline", AsyncMock())

    async def member_round(
        _m: str, _request: Any, identity: Any, _rid: str, _t0: float, nested: Any, **_k: Any
    ) -> Any:
        async def gen() -> Any:
            payload = {"choices": [{"delta": {"content": "好"}, "index": 0}]}
            yield f"data: {json.dumps(payload)}\n\n"
            if identity.agent_id in fail:
                nested["model_failed"] = True
                nested["model_failure_text"] = "壞了"
            yield "data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(group, "get_stream_generator", member_round)


async def _run_group(ctx: PipelineContext) -> None:
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content="@A @B 早")]
    )
    async for _chunk in group.dispatch_group_stream(request, "r", 0.0, ctx):
        pass


async def test_a_plain_group_records_each_member_who_spoke(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _install(monkeypatch, fail={"Bob"})
    await _run_group(PipelineContext(session_id="sess-g"))
    await _run_group(PipelineContext(session_id="sess-g"))
    # 對話編號是用戶端送的：只存雜湊，不存原字串（審查 L2）
    assert recorded == [("Alice", "group_turn", ce.hashed_ref("sess-g"), None)] * 2


async def test_an_api_group_without_a_conversation_uses_a_fixed_subject(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _install(monkeypatch)
    await _run_group(PipelineContext())
    assert recorded == [
        ("Alice", "group_turn", ce.GROUP_NO_SESSION_SUBJECT, None),
        ("Bob", "group_turn", ce.GROUP_NO_SESSION_SUBJECT, None),
    ]


async def test_an_incognito_group_records_nothing(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _install(monkeypatch)
    await _run_group(PipelineContext(session_id="sess-g", incognito=True))
    assert recorded == []


async def test_an_sns_group_leaves_the_members_who_spoke_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    _install(monkeypatch, fail={"Bob"})
    ctx = PipelineContext(sns_source=True)
    await _run_group(ctx)
    assert recorded == []  # 經 SNS 的群聊不記 group_turn
    assert [soul.agent_id for soul in ce.group_spoken_souls(ctx)] == ["Alice"]


# ── SNS 入口：送出後才記 ─────────────────────────────────────────────────────


class _Adapter:
    def __init__(self, agent_id: str = "Alice", *, delivered: bool = True) -> None:
        self.agent_id = agent_id
        self.delivered = delivered

    async def send_message(self, _channel_id: str, _response: Any) -> bool:
        return self.delivered

    async def show_typing(self, _channel_id: str) -> None:
        return None


def _reply(text: str) -> MagicMock:
    async def body() -> Any:
        yield f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    response = MagicMock()
    response.body_iterator = body()
    return response


async def _sns(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str = "Hello",
    mode: str = "independent",
    adapter: _Adapter | None = None,
    spoken: list[str] | None = None,
    failed: bool = False,
    real_entry: bool = False,
) -> None:
    monkeypatch.setattr(
        "src.services.sns_gateway.agent_service.identify_agent",
        lambda value: {"agent_id": value, "is_toukasoul": True},
    )
    monkeypatch.setattr(
        "src.services.sns_gateway.AuthService.get_all_users",
        lambda _self: [MagicMock(owned_agents=["Alice", "Bob"])],
    )
    identities = MagicMock()
    identities.get_identity.side_effect = lambda value: AgentIdentity.for_role(value, "Owner")
    sessions = MagicMock()
    sessions.read_home_snapshot = AsyncMock(return_value=SessionSnapshot({"id": "home"}, "r"))
    sessions.open_channel_session = AsyncMock(
        side_effect=lambda kind, identity, *coords, **_k: channel_session_id(
            kind, identity.agent_id, *coords
        )
    )
    sessions.append_message_to_session = AsyncMock(return_value=True)
    sessions.read_execution_snapshot = AsyncMock(
        return_value=SessionSnapshot({"id": "x"}, "r")
    )

    async def proxy(_request: Any, ctx: Any, **_k: Any) -> Any:
        if spoken is not None:
            ctx[ce.GROUP_SPOKEN_SOULS] = list(spoken)
        if failed:
            ctx["model_failed"] = True
        return _reply("回覆")

    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="ch1", content=content)
    with (
        patch("src.services.sns_gateway.detect_target_agent", return_value=None),
        patch("src.services.sns_gateway.identity_service", identities),
        patch("src.services.sns_gateway.session_service", sessions),
        patch("src.services.sns_gateway.process_proxy_request",
              new=proxy_service.process_proxy_request if real_entry else proxy),
    ):
        await SNSGatewayService().on_event(adapter or _Adapter(), event, session_mode=mode)


SNS_SUBJECT = ce.hashed_ref("telegram:ch1")


async def test_an_sns_group_member_called_twice_is_recorded_once(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    await _sns(monkeypatch, content="@Alice @Alice @Bob 早", spoken=["Alice", "Alice", "Bob"])
    assert recorded == [
        ("Alice", "sns_turn", SNS_SUBJECT, None),
        ("Bob", "sns_turn", SNS_SUBJECT, None),
    ]


async def test_the_sns_group_list_travels_through_the_real_entry(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    """接線：SNS 入口建的 ctx 經真的 ``process_proxy_request``、``dispatch_request`` 到群聊分發器.

    分發器記下的名單要回到同一個 ctx；中間哪一層改成傳副本，SNS 群聊就一筆都不記，這裡會紅。
    """

    async def group_stream(_request: Any, _rid: str, _t0: float, context: Any) -> Any:
        for name in ("Alice", "Bob"):
            ce.note_group_member_spoke(context, AgentIdentity.for_role(name, "Owner"))
        yield f"data: {json.dumps({'choices': [{'delta': {'content': '好'}}]})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(proxy_service, "_refuse_old_home", AsyncMock())
    monkeypatch.setattr(proxy_service, "pipeline", MagicMock(execute=AsyncMock()))
    monkeypatch.setattr(proxy_service, "log_payload", MagicMock())
    monkeypatch.setattr(proxy_service, "record_working_brain", MagicMock())
    monkeypatch.setattr(dispatcher, "dispatch_group_stream", group_stream)
    monkeypatch.setattr(dispatcher, "record_working_brain", MagicMock())
    await _sns(monkeypatch, content="@Alice @Bob 早", real_entry=True)
    assert recorded == [
        ("Alice", "sns_turn", SNS_SUBJECT, None),
        ("Bob", "sns_turn", SNS_SUBJECT, None),
    ]


async def test_an_sns_turn_is_recorded_after_the_reply_is_sent(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    await _sns(monkeypatch)
    channel = channel_session_id("sns", "Alice", "telegram", "ch1")
    assert recorded == [("Alice", "sns_turn", SNS_SUBJECT, channel)]


async def test_an_sns_turn_in_home_mode_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    await _sns(monkeypatch, mode="home")  # 回合本來就在家裡
    assert recorded == []


async def test_an_sns_group_records_each_member_who_spoke_even_in_home_mode(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any]
) -> None:
    await _sns(monkeypatch, content="@Alice @Bob 早", mode="home", spoken=["Alice", "Bob"])
    assert recorded == [
        ("Alice", "sns_turn", SNS_SUBJECT, None),
        ("Bob", "sns_turn", SNS_SUBJECT, None),
    ]


@pytest.mark.parametrize("case", ["failed", "undelivered", "group_nobody"])
async def test_a_failed_or_undelivered_sns_turn_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, recorded: list[Any], case: str
) -> None:
    if case == "failed":
        await _sns(monkeypatch, failed=True)
    elif case == "undelivered":
        await _sns(monkeypatch, adapter=_Adapter(delivered=False))
    else:
        await _sns(monkeypatch, content="@Alice @Bob 早", spoken=[])
    assert recorded == []


# ── 家裡的 <continuity> ──────────────────────────────────────────────────────


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "_last_request_at", OrderedDict())
    monkeypatch.setattr(cs, "get_current_epoch", lambda: NOW)
    monkeypatch.setattr(cs, "_mission_line", AsyncMock(return_value="- Mission board: n/a"))
    monkeypatch.setattr(cs, "unreported_children", AsyncMock(return_value=Unreported(0, None)))
    monkeypatch.setattr(cs.session_service, "home_session_ids", AsyncMock(return_value=["home-1"]))


async def _block(sid: str, **kwargs: Any) -> str:
    kwargs.setdefault("incognito", False)
    return await cs.build_continuity_block(soul=_soul(), session_id=sid, identity=_soul(), **kwargs)


def _when(at_s: float) -> str:
    ago = cs.format_elapsed(NOW - at_s)
    return f"last {ago} ago, {format_epoch_to_taipei(at_s)[0][:16]}"


async def test_the_home_hears_where_else_it_spoke(sandbox_db: Any, frozen: None) -> None:
    await _event("group_turn", NOW - 30 * HOUR)  # 超過 24 小時：不算
    await _event("group_turn", NOW - 3 * HOUR)
    await _event("group_turn", NOW - 600)
    await _event("sns_turn", NOW - 2 * HOUR)
    line = (
        f"- {cs.ELSEWHERE_LABEL} group chats (2 turns, {_when(NOW - 600)}) and "
        f"SNS channels (1 turn, {_when(NOW - 2 * HOUR)}); {cs.ELSEWHERE_NOT_HERE} "
        f"{cs.ACTIVITY_RECORD}"
    )
    assert line in await _block("home-1")
    assert cs.ELSEWHERE_LABEL not in await _block("child-1")
    assert cs.ELSEWHERE_LABEL not in await _block("home-1", sns=True)
    assert cs.ELSEWHERE_LABEL not in await _block("home-1", incognito=True)
    assert "24 hours" in cs.ELSEWHERE_LABEL


async def test_only_the_places_it_spoke_are_listed(sandbox_db: Any, frozen: None) -> None:
    assert cs.ELSEWHERE_LABEL not in await _block("home-1")  # 都是 0
    await _event("group_turn", NOW - 600)
    block = await _block("home-1")
    assert f"- {cs.ELSEWHERE_LABEL} group chats (1 turn, {_when(NOW - 600)}); " in block
    assert "SNS channels" not in block


# ── 子對話知道自己的編號 ──────────────────────────────────────────────────────


def _id_line(sid: str) -> str:
    return f"- {cs.CONVERSATION_ID_TEMPLATE.format(id=sid)}"


async def test_a_child_or_heartbeat_channel_knows_its_own_id(
    sandbox_db: Any, frozen: None
) -> None:
    channel = channel_session_id("heartbeat", SOUL, "independent")
    child = await _block("child-1")
    assert _id_line("child-1") in child
    assert child.index(f"- {cs.CONVERSATION_LABEL}:") < child.index(_id_line("child-1"))
    assert _id_line(channel) in await _block(channel, heartbeat=True)
    assert 'marked "Report from conversation child-1"' in _id_line("child-1")
    for sid, kwargs in (
        ("home-1", {}),
        ("child-1", {"sns": True}),
        ("child-1", {"incognito": True}),
        ("touka.internal", {}),
    ):
        assert "This conversation's id" not in await _block(sid, **kwargs), (sid, kwargs)

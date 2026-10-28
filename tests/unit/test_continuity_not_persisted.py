"""<continuity> 不存進任何紀錄、審查員看不到；心跳也注入（只在送出的請求）。

它只附在送給模型的那一次請求、最後一則 user 訊息尾端。存下的對話樹（含那則使用者訊息）、
前情摘要欄位與摘要員的輸入、Auto 審查員看的使用者原意裡都不能有。心跳由脈衝自己注入
（心跳模式）：只在送出的請求，存回的樹與 nudge 都沒有它，nudge 和區塊用同一個時間。

全部離線：上游以假的串流取代；session 只寫在測試沙盒的資料庫。
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import OrderedDict
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import SavedResult
from src.models.souls import SoulConfig
from src.routers import heartbeat
from src.services import continuity_service, dialogue_summarizer
from src.services.auto_reviewer import _intent_payload
from src.services.identity_router import get_verified_identity
from src.services.prompt_blocks import CONTINUITY
from src.services.soul_service import SoulService, soul_service
from src.utils import user_intent
from src.utils.async_db import SessionSnapshot, session_service
from src.utils.history_manager import TreeMessageManager
from src.utils.time_utils import format_epoch_to_taipei

OPEN = f"<{CONTINUITY}>"
SOUL = "U6PersistSoul"
REPLY = "U6-REPLY-TEXT"
SUMMARY = "U6-OLD-SUMMARY"
NUDGE_EPOCH = 1_760_000_000
OWNER = AgentIdentity.for_role("u6-soul-owner", "normal", is_human=True, owned_agents=[SOUL])
_PATHS = ["/v1/messages", "/v1/chat/completions"]
# 前端送出前先存好的使用者這一輪（沒有後端時間戳）
USER_TREE = {
    "messages": {
        "user": {"id": "user", "role": "user", "content": "hi", "parentId": None, "childrenIds": []}
    },
    "currentId": "user",
}


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """SOUL 被認成靈魂；上游換成假串流，記錄送出的請求與 pipeline context。"""
    seen: dict[str, Any] = {"requests": [], "contexts": []}

    async def _no_home(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _fake_stream(_model: str, request: Any, *args: Any, **_kwargs: Any) -> Any:
        seen["requests"].append(json.loads(request.model_dump_json()))
        seen["contexts"].append(args[3])  # (identity, request_id, start_time, context)

        async def _gen() -> Any:
            chunk = {"choices": [{"delta": {"content": REPLY}, "index": 0}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return _gen()

    monkeypatch.setattr(
        soul_service, "is_toukasoul", lambda who: getattr(who, "agent_id", who) == SOUL
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
    app.dependency_overrides[get_verified_identity] = lambda: OWNER
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


async def _seed(sid: str, *, auth_level: str | None = None) -> None:
    assert await session_service.upsert_session(
        sid,
        "u6 probe",
        copy.deepcopy(USER_TREE),
        identity=AgentIdentity.for_role(SOUL, "Owner"),
        owner_user_id=OWNER.agent_id,
        context_summary=SUMMARY,
        auth_level=auth_level,
    )


def _chat(client: TestClient, path: str, sid: str, **extra: Any) -> Any:
    body: dict[str, Any] = {
        "model": SOUL,
        "session_id": sid,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    return client.post(path, json=body, headers={"X-Touka-Soul-ID": SOUL})


def _sent_last_user_text(seen: dict[str, Any]) -> str:
    assert len(seen["requests"]) == 1
    content = seen["requests"][0]["messages"][-1]["content"]
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


async def _assert_nothing_saved_has_continuity(
    sid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = await session_service.get_session_by_id(sid)
    assert saved is not None
    assert REPLY in json.dumps(saved["messages"], ensure_ascii=False)  # 這一輪確實存了
    assert saved["messages"]["messages"]["user"]["content"] == "hi"  # 那則使用者訊息原樣
    assert OPEN not in json.dumps(saved, ensure_ascii=False, default=str)
    assert saved["context_summary"] == SUMMARY

    # 前情摘要的輸入：摘要員拿到的對話文字（不連線，攔下 complete_once 的參數）
    complete = AsyncMock(return_value=MagicMock(ok=False, error=MagicMock(reason="probe")))
    monkeypatch.setattr(dialogue_summarizer, "complete_once", complete)
    linear = TreeMessageManager.get_linear_path(saved["messages"])
    await dialogue_summarizer.summarize_dialogue(
        AgentIdentity.for_role(SOUL, "Owner"), linear, 500, "probe-model"
    )
    dialogue = complete.await_args.args[4]
    assert "hi" in dialogue and REPLY in dialogue
    assert OPEN not in dialogue


@pytest.mark.parametrize("path", _PATHS)
async def test_continuity_not_persisted(
    path: str, probe: dict[str, Any], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """舊串流路徑：送出的最後一則 user 訊息尾端有它；存下的對話樹與摘要輸入都沒有。"""
    sid = f"u6-legacy-{uuid.uuid4().hex[:8]}"
    await _seed(sid)
    resp = _chat(client, path, sid)
    assert resp.status_code == 200, resp.text
    sent = _sent_last_user_text(probe)
    assert sent.startswith("hi") and OPEN in sent
    await _assert_nothing_saved_has_continuity(sid, monkeypatch)


async def test_ui_persisted_turn_excludes_continuity(
    probe: dict[str, Any], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明示 UI 保存路徑（前端先存這一輪）：存下的使用者訊息與回覆都不含它。"""
    sid = f"u6-ui-{uuid.uuid4().hex[:8]}"
    await _seed(sid)
    snapshot = await session_service.read_persistence_snapshot(sid, OWNER)
    admission = {
        "mode": "persistent",
        "revision": snapshot.revision,
        "user_parent_id": "user",
        "assistant_node_id": "assistant",
    }
    resp = _chat(client, "/v1/chat/completions", sid, ui_persistence=admission)
    assert resp.status_code == 200, resp.text
    assert OPEN in _sent_last_user_text(probe)
    await _assert_nothing_saved_has_continuity(sid, monkeypatch)


async def test_reviewer_user_intent_excludes_continuity(
    probe: dict[str, Any], client: TestClient
) -> None:
    """Auto 對話：審查員看的使用者原意只有人類訊息，沒有系統附加的 <continuity>。"""
    sid = f"u6-auto-{uuid.uuid4().hex[:8]}"
    await _seed(sid, auth_level="Auto")
    resp = _chat(client, "/v1/chat/completions", sid)
    assert resp.status_code == 200, resp.text
    assert OPEN in _sent_last_user_text(probe)
    ctx = probe["contexts"][0]
    assert ctx.auth_level == "Auto"  # 前提：這個對話會送審查員
    payload = _intent_payload(ctx.get("user_intent"))
    assert payload is not None and payload["messages"] == ["hi"]
    assert OPEN not in json.dumps(payload, ensure_ascii=False)


async def _no_chunks() -> Any:
    for _ in ():
        yield ""


def _pulse_doubles(
    monkeypatch: pytest.MonkeyPatch, messages: Any, *, is_soul: bool = True
) -> tuple[dict[str, Any], MagicMock]:
    """心跳單一脈衝的替身：攔下送出的請求與存回的樹；人格不讀磁碟、不污染 singleton。"""
    captured: dict[str, Any] = {}

    async def _dispatch(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return MagicMock(body_iterator=_no_chunks())

    soul = object.__new__(SoulService)
    soul.get_soul_data = lambda _identity: ("靈魂", SoulConfig().model_dump())
    if not is_soul:
        soul.is_toukasoul = lambda _identity: False
    service = MagicMock(
        read_execution_snapshot=AsyncMock(
            return_value=SessionSnapshot({"id": "s", "title": "t", "messages": messages}, "rev")
        ),
        conditional_save_session=AsyncMock(
            return_value=SavedResult(status="saved", representation="full", revision="next")
        ),
    )
    monkeypatch.setattr(heartbeat, "session_service", service)
    monkeypatch.setattr(heartbeat, "dispatch_request", _dispatch)
    monkeypatch.setattr(heartbeat, "autonomy_service", MagicMock())
    monkeypatch.setattr(heartbeat, "rhythm_service", MagicMock())
    monkeypatch.setattr(heartbeat, "soul_service", soul)
    monkeypatch.setattr(continuity_service, "_last_request_at", OrderedDict())
    return captured, service


async def _pulse(ctx: PipelineContext | None = None) -> None:
    identity = AgentIdentity.for_role(agent_id=SOUL, role="Owner")
    await heartbeat._execute_single_pulse(
        "s", identity, "claude-sonnet-4-6", ctx or PipelineContext(), 1, 0.0, ""
    )


async def test_heartbeat_gets_continuity_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳也注入（心跳模式）：只附在送出的最後一則 user，不進 system、不寫回對話樹；
    不算「這段對話上一輪」，也不動記憶體紀錄（家對話和人類共用 session id）。"""
    captured, service = _pulse_doubles(monkeypatch, [{"role": "user", "content": "hi"}])
    ctx = PipelineContext()
    ctx["user_intent"] = user_intent.heartbeat_intent("整理檔案")

    await _pulse(ctx)

    request = captured["request"]
    assert [m.role for m in request.messages] == ["system", "user"]
    assert OPEN not in request.messages[0].content
    last = request.messages[-1].content
    assert last.startswith("hi\n\n" + OPEN) and last.count(OPEN) == 1
    assert continuity_service.HEARTBEAT_LINE in last
    assert continuity_service.RUN_LABEL_HEARTBEAT in last
    assert continuity_service.PREVIOUS_TURN_LABEL not in last
    assert not continuity_service._last_request_at
    service.conditional_save_session.assert_not_called()  # 最後一則是 user：不補 nudge、不存檔
    payload = _intent_payload(ctx.get("user_intent"))  # 審查員的原意不含它
    assert payload is not None and OPEN not in json.dumps(payload, ensure_ascii=False)


async def test_heartbeat_nudge_and_continuity_share_one_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """補 nudge 的脈衝：nudge 前綴格式逐字不變，時間與區塊的 Current time 是同一個值。"""
    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "hi")
    tree = TreeMessageManager.append_node(tree, "assistant", "done", name=SOUL)
    captured, service = _pulse_doubles(monkeypatch, tree)
    ticks = iter(float(NUDGE_EPOCH + step) for step in range(100))

    def clock() -> float:
        return next(ticks)  # 每次取時間都前進一秒：取兩次就會對不上

    monkeypatch.setattr(heartbeat, "get_current_epoch", clock)
    monkeypatch.setattr(continuity_service, "get_current_epoch", clock)

    await _pulse()

    time_str, weekday = format_epoch_to_taipei(NUDGE_EPOCH)
    saved_tree = service.conditional_save_session.await_args.args[2].messages
    nudge = TreeMessageManager.get_linear_path(saved_tree)[-1]
    assert nudge["role"] == "user"
    assert nudge["content"] == (
        f"{user_intent.PULSE_NUDGE_PREFIX}{time_str}】\n\n請繼續執行您的任務或檢查進度。"
    )
    assert OPEN not in json.dumps(saved_tree, ensure_ascii=False)
    sent = captured["request"].messages[-1].content
    assert sent.startswith(nudge["content"] + "\n\n" + OPEN)
    assert f"- Current time (Asia/Taipei): {time_str} ({weekday})" in sent


async def test_non_soul_heartbeat_gets_no_continuity(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, _service = _pulse_doubles(
        monkeypatch, [{"role": "user", "content": "hi"}], is_soul=False
    )
    await _pulse()
    assert OPEN not in captured["request"].model_dump_json()

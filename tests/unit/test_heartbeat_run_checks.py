"""心跳先檢查再寫提示、提示說明、精簡 continuity、請求編號、不再切段（ADR-0323 PR 2）.

- ``/awaken``：寫初始提示之前先檢查模型（和脈衝同一套解析）、每日配額與花費上限；跑不了就不寫提示、
  記一筆略過原因，回真正的錯誤碼（配額與花費共用 ``AUTONOMY_LIMIT_REACHED``）。探針照舊先寫再回。
- 初始提示在開頭那行之後多一行說明「這是排程送出的預設提示」。
- 靈魂的心跳 ``<continuity>`` 寫明不帶技能與專案；不是靈魂的代理人也拿到精簡版（時間、沒人在場、
  第幾個脈衝、被誰叫醒）。
- 每個脈衝有自己的請求編號，Claude 那條路也帶著，兩個心跳的工具輪計數不再共用。
- 模型寫出「--- … 自主行動」不再被切成兩則、插一句「請繼續」。
全部離線：模型、配額與對話儲存都換成假的。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers import heartbeat
from src.routers.heartbeat import _execute_single_pulse
from src.services import continuity_service
from src.services.auth_service import AuthService
from src.services.continuity_service import HeartbeatWake
from src.services.dispatchers import factory
from src.services.dispatchers.route_family import ANTHROPIC_FAMILY, ModelRoute
from src.services.models import model_manager
from src.services.models.routes import (
    CustomModelRoute,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)
from src.services.prompt_blocks import CONTINUITY
from src.utils import user_intent
from src.utils.async_db import SavedResult, SessionSnapshot
from src.utils.history_manager import TreeMessageManager

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_AWAKEN_KEY = "test_heartbeat_token_for_ci"
_SOUL = AgentIdentity.for_role(agent_id="HbSoul", role="Owner")
_OTHER = AgentIdentity.for_role(agent_id="DebugAgent", role="Owner")


# ── /awaken：先檢查再寫提示 ───────────────────────────────────────────


def _ambiguous_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    route = CustomModelRoute(
        connection_id="c", endpoint="http://127.0.0.1:1", api_key="", provider="X"
    )
    snapshot = ModelRouteSnapshot.create(
        generation=1,
        config_fingerprint="sandbox",
        active_routes={"Foo": route, "foo": route},
        state=ModelRouteSnapshotState.READY,
    )
    monkeypatch.setattr(model_manager, "_route_snapshot", snapshot)


@pytest.fixture
def awaken(monkeypatch: pytest.MonkeyPatch, mock_identity: Any) -> SimpleNamespace:
    """``/awaken`` 的替身：背景迴圈、寫提示、記略過都換成假的；預設有大腦、配額與花費都夠。"""
    monkeypatch.setattr(heartbeat, "HEARTBEAT_TOKEN", _AWAKEN_KEY)
    loop = AsyncMock(return_value=None)
    monkeypatch.setattr(heartbeat, "_process_heartbeat_loop", loop)
    agents = ["HbChecks", "heartbeat_probe"]

    def identity_for(agent_id: str) -> Any:
        return mock_identity(agent_id=agent_id, capabilities=["heartbeat_awakening"])

    monkeypatch.setattr(heartbeat.identity_service, "get_identity", identity_for)
    monkeypatch.setattr(heartbeat.permission_service, "can_awaken", lambda _x: True)
    owner = User(
        id="o",
        email="o@test.com",
        hashed_password="...",
        owned_agents=agents,
        refresh_token_exp=9999999999,
    )
    monkeypatch.setattr(AuthService, "get_all_users", lambda _self: [owner])
    append = AsyncMock(return_value=True)
    monkeypatch.setattr(heartbeat.session_service, "append_message_to_session", append)
    skipped = Mock()
    monkeypatch.setattr(heartbeat, "record_heartbeat_skipped", skipped)
    monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: "claude-x")
    monkeypatch.setattr(heartbeat.autonomy_service, "check_quota", lambda _identity: True)
    monkeypatch.setattr(heartbeat.autonomy_service, "is_over_budget", lambda _identity: False)
    return SimpleNamespace(loop=loop, append=append, skipped=skipped)


def _awaken(client: TestClient, agent: str = "HbChecks", **extra: Any) -> Any:
    body = {"token": _AWAKEN_KEY, "agent_id": agent, "pulse_count": 1, **extra}
    return client.post("/v1/heartbeat/awaken", json=body)


def _refused(resp: Any, status: int, code: str, awaken: SimpleNamespace, reason: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code
    awaken.append.assert_not_called()  # 提示沒寫進對話
    awaken.loop.assert_not_called()
    (identity, recorded), kwargs = awaken.skipped.call_args
    assert (identity.agent_id, recorded) == ("HbChecks", reason)
    assert kwargs["session_id"]


def test_no_brain_is_refused_before_the_prompt_is_written(
    auth_client: TestClient, awaken: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: None)
    _refused(_awaken(auth_client), 409, "AGENT_BRAIN_NOT_SET", awaken, "no_brain")


@pytest.mark.parametrize("where", ["brain", "override"])
def test_an_ambiguous_model_name_is_refused_with_its_own_code(
    auth_client: TestClient,
    awaken: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    _ambiguous_routes(monkeypatch)
    if where == "brain":
        monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: "FOO")
        resp = _awaken(auth_client)
    else:
        resp = _awaken(auth_client, model_override="FOO")
    _refused(resp, 409, "MODEL_NAME_AMBIGUOUS", awaken, "no_model")


@pytest.mark.parametrize(
    ("quota_left", "over_budget", "reason"),
    [(False, False, "quota"), (True, True, "budget")],
)
def test_an_exhausted_quota_or_budget_is_refused(
    auth_client: TestClient,
    awaken: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    quota_left: bool,
    over_budget: bool,
    reason: str,
) -> None:
    monkeypatch.setattr(heartbeat.autonomy_service, "check_quota", lambda _identity: quota_left)
    monkeypatch.setattr(heartbeat.autonomy_service, "is_over_budget", lambda _identity: over_budget)
    _refused(_awaken(auth_client), 429, "AUTONOMY_LIMIT_REACHED", awaken, reason)


def test_a_runnable_heartbeat_writes_the_prompt_with_the_note(
    auth_client: TestClient, awaken: SimpleNamespace
) -> None:
    resp = _awaken(auth_client, init_prompt="整理今天的筆記")
    assert resp.status_code == 202, resp.text
    awaken.loop.assert_called_once()
    awaken.skipped.assert_not_called()
    (_sid, role, content), _kwargs = awaken.append.call_args
    assert role == "user"
    head, note, blank, prompt = content.split("\n")
    assert head.startswith(user_intent.HEARTBEAT_INIT_PREFIX)
    assert note == user_intent.HEARTBEAT_INIT_NOTE
    assert (blank, prompt) == ("", "整理今天的筆記")


def test_the_probe_still_writes_first_without_the_checks(
    auth_client: TestClient, awaken: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heartbeat, "get_active_brain", lambda _identity: None)
    monkeypatch.setattr(heartbeat.autonomy_service, "check_quota", lambda _identity: False)
    resp = _awaken(auth_client, agent="heartbeat_probe")
    assert resp.status_code == 202, resp.text
    assert resp.json()["message"] == "Probe persisted."
    awaken.append.assert_called_once()
    awaken.skipped.assert_not_called()


def test_the_init_note_is_a_bracketed_system_line() -> None:
    assert user_intent.HEARTBEAT_INIT_NOTE == (
        "〔這是排程心跳的預設提示，由系統送出，不是人當下說的話〕"
    )


# ── <continuity> ────────────────────────────────────────────────────


async def test_a_soul_heartbeat_is_told_skills_and_project_are_not_loaded() -> None:
    block = await continuity_service.build_continuity_block(
        soul=_SOUL,
        session_id=None,
        identity=_SOUL,
        incognito=False,
        heartbeat=True,
        wake=HeartbeatWake(pulse=1, total=2, trigger="schedule"),
        now_s=0.0,
    )
    assert f"- {continuity_service.HEARTBEAT_SCOPE_SOUL}" in block
    assert continuity_service.HEARTBEAT_SCOPE_OTHER not in block
    assert continuity_service.HEARTBEAT_SCOPE_SOUL == (
        "Heartbeat runs do not load chat skills or the conversation's project context; "
        "only your persona, prelude and the listed tools."
    )


async def test_a_chat_turn_does_not_get_the_heartbeat_scope_line() -> None:
    block = await continuity_service.build_continuity_block(
        soul=_SOUL, session_id=None, identity=_SOUL, incognito=False, now_s=0.0
    )
    assert continuity_service.HEARTBEAT_SCOPE_SOUL not in block


def test_a_non_soul_heartbeat_gets_the_minimal_block() -> None:
    block = continuity_service.build_heartbeat_minimal_continuity(
        HeartbeatWake(pulse=2, total=3, trigger="manual"), now_s=0.0
    )
    assert block.startswith(f"<{CONTINUITY}>")
    assert "- Current time (Asia/Taipei): 1970-01-01 08:00:00" in block
    assert continuity_service.HEARTBEAT_LINE in block
    assert "- Pulse 2 of 3:" in block
    assert f"- {continuity_service.WOKEN_LABEL}:" in block
    assert f"- {continuity_service.HEARTBEAT_SCOPE_OTHER}" in block
    assert continuity_service.HEARTBEAT_SCOPE_OTHER == (
        "Heartbeat runs do not load chat skills or the conversation's project context; "
        "only the listed tools."
    )
    for absent in ("Mission board", "Last message from a human", "Last autonomous run"):
        assert absent not in block


# ── 脈衝：continuity、請求編號、不切段 ─────────────────────────────────


class _Pulses:
    """跑脈衝用的替身：模型層記下每段對話送出的參數，存檔照對話編號分開。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.sent: dict[str, dict[str, Any]] = {}

    async def dispatch(self, **kwargs: Any) -> Any:
        sid = kwargs["context"]["session_id"]
        self.sent[sid] = {**kwargs, "context_request_id": kwargs["context"].get("request_id")}
        await asyncio.sleep(0)  # 讓同時跑的另一個脈衝插進來

        async def body() -> Any:
            for text in self.texts:
                payload = json.dumps({"choices": [{"delta": {"content": text}}]})
                yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"

        return SimpleNamespace(body_iterator=body())


def _snapshot(sid: str, _identity: Any = None) -> SessionSnapshot:
    tree = TreeMessageManager.append_node(TreeMessageManager.create_empty_tree(), "user", "開始")
    return SessionSnapshot({"id": sid, "title": "t", "messages": tree}, "rev")


async def _run_pulses(
    runs: list[tuple[AgentIdentity, str]], texts: list[str], *, souls: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, PipelineContext], _Pulses, AsyncMock]:
    """同一組替身裡同時跑幾個脈衝；回傳每段對話最後存下的對話樹、ctx、替身與 continuity 替身。"""
    fake = _Pulses(texts)
    ctxs = {sid: PipelineContext(identity=identity) for identity, sid in runs}
    for ctx in ctxs.values():
        ctx["heartbeat_wake"] = HeartbeatWake(pulse=1, total=1, trigger="schedule")
    save = AsyncMock(
        return_value=SavedResult(status="saved", representation="full", revision="next")
    )
    with (
        patch(
            "src.utils.async_db.SessionService.read_execution_snapshot",
            new_callable=AsyncMock,
            side_effect=lambda sid, identity: _snapshot(sid),
        ),
        patch("src.utils.async_db.SessionService.conditional_save_session", new=save),
        patch("src.routers.heartbeat.dispatch_request", new=fake.dispatch),
        patch("src.routers.heartbeat.autonomy_service"),
        patch("src.routers.heartbeat.get_active_brain", return_value="claude-x"),
        patch("src.routers.heartbeat.inject_continuity", new_callable=AsyncMock) as soul_block,
        patch("src.routers.heartbeat.inject_mainline", new_callable=AsyncMock),
        patch.object(
            heartbeat.soul_service, "is_toukasoul", side_effect=lambda i: i.agent_id in souls
        ),
    ):
        await asyncio.gather(
            *(
                _execute_single_pulse(
                    final_session_id=sid,
                    identity=identity,
                    model_override=None,
                    ctx=ctxs[sid],
                    pulse_slots=1,
                    t0=0.0,
                    init_prompt="test",
                )
                for identity, sid in runs
            )
        )
    trees = {call.args[0]: call.args[2].messages for call in save.await_args_list}
    return trees, ctxs, fake, soul_block


def _last_user_text(request: OpenAIChatRequest) -> str:
    content = request.messages[-1].content
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


async def test_a_non_soul_pulse_gets_the_minimal_continuity() -> None:
    _trees, _ctxs, fake, soul_block = await _run_pulses([(_OTHER, "s1")], ["好"], souls=set())
    text = _last_user_text(fake.sent["s1"]["request"])
    assert f"<{CONTINUITY}>" in text
    assert continuity_service.HEARTBEAT_SCOPE_OTHER in text
    assert "- Pulse 1 of 1:" in text
    soul_block.assert_not_awaited()


async def test_a_soul_pulse_still_gets_the_full_continuity() -> None:
    _trees, _ctxs, fake, soul_block = await _run_pulses(
        [(_SOUL, "s1")], ["好"], souls={_SOUL.agent_id}
    )
    soul_block.assert_awaited_once()
    sent = _last_user_text(fake.sent["s1"]["request"])
    assert continuity_service.HEARTBEAT_SCOPE_OTHER not in sent


async def test_two_pulses_at_once_each_carry_their_own_request_id() -> None:
    _trees, ctxs, fake, _block = await _run_pulses(
        [(_SOUL, "s1"), (_OTHER, "s2")], ["好"], souls={_SOUL.agent_id}
    )
    first, second = fake.sent["s1"]["request_id"], fake.sent["s2"]["request_id"]
    assert first and second and first != second
    assert fake.sent["s1"]["context_request_id"] == first
    assert fake.sent["s2"]["context_request_id"] == second
    assert ctxs["s1"].get("request_id") == first
    assert ctxs["s2"].get("request_id") == second


async def test_the_claude_route_keeps_the_request_id_for_the_round_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = ModelRoute(kind=ANTHROPIC_FAMILY, api_key=None)
    monkeypatch.setattr(factory, "resolve_model_route", lambda *_a, **_k: route)
    captured: dict[str, Any] = {}

    def fake_claude(_request: Any, context: dict) -> str:
        captured.update(context)
        return "gen"

    monkeypatch.setattr(factory, "handle_claude_request", fake_claude)
    request = OpenAIChatRequest(
        model="claude-x", messages=[OpenAIMessage(role="user", content="hi")]
    )
    context: dict[str, Any] = {"session_id": "s"}
    await factory.get_stream_generator("claude-x", request, _SOUL, "req-7", 0.0, context)
    assert captured["request_id"] == "req-7"
    assert captured["session_id"] == "s"  # 只多帶請求編號，不動其他鍵


async def test_a_divider_in_the_reply_no_longer_splits_the_pulse() -> None:
    trees, _ctxs, _fake, _block = await _run_pulses(
        [(_SOUL, "s1")], ["第一步 --- 第 1 次 自主行動", "第二步"], souls={_SOUL.agent_id}
    )
    nodes = list(trees["s1"]["messages"].values())
    replies = [node for node in nodes if node["role"] == "assistant"]
    assert [reply["content"] for reply in replies] == ["第一步 --- 第 1 次 自主行動第二步"]
    assert not any("自主行動階段完成" in str(node.get("content")) for node in nodes)

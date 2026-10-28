"""非擁有者不能指定別人的靈魂（U2-SOUL；ADR-0245 已知殘留的修補；禁令 20 對應安全測試）。

修補前的漏洞（R-2a ① 問題 1）：沒登入已擋下，但已登入的非擁有者（含 `/auth/register`
自助註冊、註冊後立即可用）或別的代理 token，只要把 `model` 或 `X-Touka-Soul-ID` 指向別人的
靈魂，MonitorLayer 認出靈魂後只是「不換身分」，SoulLayer 照樣套人設、注入家對話與前情摘要，
再用伺服器的金鑰呼叫模型。群聊 `@別名` 與子代理 `sub_model=<靈魂>` 是同一個根因。

修補後的規則：
- 人類呼叫者：必須是那個靈魂的擁有者（`owned_agents`）或 admin，否則 403；
- 代理呼叫者：只能是那個靈魂本身或 `DebugAgent`，否則 403（admin 代打保留 admin 權）；
- 群聊：每位被點名的成員做同樣檢查，有一位不行整輪就不派發；
- 子代理（臨時身分 `__subagent_*__`）指定靈魂當 `sub_model` 一律拒絕；
- SNS 不經 HTTP 閘；閘道以可信靈魂身分存檔，pipeline 不帶身分（外人訊息技能與特權維持零）。

全部離線：上游 `dispatch_request`／群聊成員派發以 spy 取代，不打任何 API；帳號寫到 tmp。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ToukaException
from src.main import app
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.routers import auth as auth_router
from src.services import proxy_service
from src.services.dispatchers.group import dispatch_group_stream
from src.services.group_router import parse_group_roster
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.sns_gateway import SNSGatewayService
from src.services.soul_service import soul_service
from src.tools import subagent_tools

PROBE_SOUL = "U2sProbeSoul"
OTHER_SOUL = "U2sOtherSoul"
SOULS = {PROBE_SOUL, OTHER_SOUL}
_PATHS = ["/v1/messages", "/v1/chat/completions"]
REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(path: str, model: str = PROBE_SOUL) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    return body


@pytest.fixture
def soul_probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """讓 PROBE_SOUL／OTHER_SOUL 被認成靈魂，記錄人設、家對話與上游呼叫是否被觸及。"""
    seen: dict[str, Any] = {"persona": [], "home": [], "dispatch": AsyncMock(return_value={})}

    def _apply_persona(_request: Any, agent_id: Any) -> None:
        seen["persona"].append(agent_id)

    async def _inject_home(_request: Any, agent_id: Any, **_kwargs: Any) -> None:
        seen["home"].append(agent_id)

    async def _no_summary(*_args: Any) -> None:
        return None

    monkeypatch.setattr(
        soul_service, "is_toukasoul", lambda who: getattr(who, "agent_id", who) in SOULS
    )
    monkeypatch.setattr(soul_service, "get_soul_data", lambda _agent_id: ("PERSONA", {}))
    monkeypatch.setattr(soul_service, "apply_soul_to_request", _apply_persona)
    monkeypatch.setattr(soul_service, "inject_home_messages", _inject_home)
    monkeypatch.setattr(soul_service, "inject_context_summary", _no_summary)
    monkeypatch.setattr(
        "src.middleware.layers.soul.get_active_brain", lambda _identity: "claude-probe"
    )
    monkeypatch.setattr(proxy_service, "dispatch_request", seen["dispatch"])
    return seen


def _post(
    path: str,
    *,
    identity: AgentIdentity | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    model: str = PROBE_SOUL,
) -> Any:
    if identity is not None:
        app.dependency_overrides[get_verified_identity] = lambda: identity
    sent = dict(headers or {})
    if token:
        sent["Authorization"] = f"Bearer {token}"
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(path, json=_payload(path, model), headers=sent)


def _assert_denied(resp: Any, seen: dict[str, Any]) -> None:
    reached = (
        f"persona={seen['persona']} home={seen['home']} "
        f"dispatch={seen['dispatch'].call_count}"
    )
    assert resp.status_code == 403, f"{resp.status_code} {resp.text} | {reached}"
    assert resp.json()["detail"]["code"] == "AUTH_PERMISSION_DENIED"
    assert seen["persona"] == [], reached
    assert seen["home"] == [], reached
    seen["dispatch"].assert_not_called()


def _assert_reached(resp: Any, seen: dict[str, Any], acting_id: str) -> None:
    assert resp.status_code == 200, resp.text
    assert seen["persona"] == [PROBE_SOUL]
    assert seen["home"] == [PROBE_SOUL]
    seen["dispatch"].assert_awaited_once()
    assert seen["dispatch"].await_args.args[2].agent_id == acting_id


def _human(role: str = "normal", owned: tuple[str, ...] = ()) -> AgentIdentity:
    return AgentIdentity.for_role("u2s-human", role, is_human=True, owned_agents=list(owned))


# --------------------------------------------------------------------------- #
# 修補前會紅：非擁有者、剛註冊的帳號、別的代理
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("owned", [(), (OTHER_SOUL,)])
@pytest.mark.parametrize("path", _PATHS)
def test_normal_user_without_ownership_cannot_summon_soul(
    path: str, owned: tuple[str, ...], soul_probe: dict[str, Any]
) -> None:
    """normal 使用者（沒有靈魂或只擁有別的靈魂）指定別人的靈魂 → 403，人設、家對話、上游都沒碰。"""
    _assert_denied(_post(path, identity=_human(owned=owned)), soul_probe)


@pytest.mark.parametrize("path", _PATHS)
def test_soul_header_from_non_owner_is_also_denied(
    path: str, soul_probe: dict[str, Any]
) -> None:
    """model 不是靈魂、改用 X-Touka-Soul-ID 指向別人的靈魂 → 一樣 403。"""
    resp = _post(
        path, identity=_human(), headers={"X-Touka-Soul-ID": PROBE_SOUL}, model="claude-x"
    )
    _assert_denied(resp, soul_probe)


@pytest.fixture
def fresh_account_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """經公開 /auth/register＋/auth/login 拿到的真 access token（帳號寫到 tmp）。"""
    monkeypatch.setattr("src.services.auth_service.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(auth_router.register_rate_limiter, "is_allowed", lambda *_a: True)
    monkeypatch.setattr(auth_router.login_rate_limiter, "is_allowed", lambda *_a: True)
    account = {"email": "u2s-fresh@touka.io", "password": "strongpassword123"}
    with TestClient(app, raise_server_exceptions=False) as client:
        registered = client.post("/auth/register", json=account)
        assert registered.status_code == 200, registered.text
        assert registered.json()["role"] == "normal"
        login = client.post("/auth/login", json=account)
        assert login.status_code == 200, login.text
    return login.json()["access_token"]


@pytest.mark.parametrize("path", _PATHS)
def test_freshly_registered_account_cannot_summon_soul(
    path: str, soul_probe: dict[str, Any], fresh_account_token: str
) -> None:
    """自助註冊後立即登入的帳號（真 token，人類、沒有擁有任何靈魂）→ 403。"""
    caller = identity_service.verify_token(fresh_account_token)
    assert caller is not None
    assert caller.is_human is True
    assert caller.owned_agents == []
    _assert_denied(_post(path, token=fresh_account_token), soul_probe)


@pytest.mark.parametrize("path", _PATHS)
def test_other_agent_token_cannot_summon_soul(path: str, soul_probe: dict[str, Any]) -> None:
    """別的靈魂的代理 token（真 token，Owner 角色）指定別人的靈魂 → 403。"""
    token = identity_service.issue_token(AgentIdentity(agent_id=OTHER_SOUL))
    caller = identity_service.verify_token(token)
    assert caller is not None
    assert caller.agent_id == OTHER_SOUL
    assert caller.is_human is False
    _assert_denied(_post(path, token=token), soul_probe)


@pytest.mark.parametrize("path", _PATHS)
def test_owner_assuming_own_soul_cannot_reach_another_soul(
    path: str, soul_probe: dict[str, Any]
) -> None:
    """擁有者用 X-Agent-ID 切進自己的靈魂後（代理身分），再指定別的靈魂 → 403。"""
    assumed = AgentIdentity.for_role(OTHER_SOUL, "Owner", is_human=False)
    _assert_denied(_post(path, identity=assumed), soul_probe)


# --------------------------------------------------------------------------- #
# 照舊可以：擁有者、admin、靈魂自己、DebugAgent、admin 代打
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _PATHS)
def test_owner_still_talks_to_own_soul_as_the_soul(path: str, soul_probe: dict[str, Any]) -> None:
    """擁有者 → 照舊放行，前景代打把執行身分換成靈魂（ADR-0154）。"""
    resp = _post(path, identity=_human(owned=(PROBE_SOUL,)))
    _assert_reached(resp, soul_probe, PROBE_SOUL)


@pytest.mark.parametrize("path", _PATHS)
def test_admin_still_talks_to_any_soul(path: str, soul_probe: dict[str, Any]) -> None:
    """人類 admin（不在 owned_agents）→ 照舊放行並代打成靈魂。"""
    _assert_reached(_post(path, identity=_human(role="admin")), soul_probe, PROBE_SOUL)


@pytest.mark.parametrize("path", _PATHS)
def test_soul_itself_still_talks_with_its_own_token(path: str, soul_probe: dict[str, Any]) -> None:
    """靈魂自己的代理 token → 放行。"""
    token = identity_service.issue_token(AgentIdentity(agent_id=PROBE_SOUL))
    _assert_reached(_post(path, token=token), soul_probe, PROBE_SOUL)


@pytest.mark.parametrize("path", _PATHS)
def test_debug_agent_still_talks_to_any_soul(path: str, soul_probe: dict[str, Any]) -> None:
    """DebugAgent 的代理 token → 放行（除錯身分）。"""
    token = identity_service.issue_token(AgentIdentity(agent_id="DebugAgent"))
    _assert_reached(_post(path, token=token), soul_probe, "DebugAgent")


@pytest.mark.parametrize("path", _PATHS)
def test_admin_assumption_keeps_admin_authority(path: str, soul_probe: dict[str, Any]) -> None:
    """admin 經 X-Agent-ID 切進某個靈魂（ADR-0141 §8-1 保留 admin 權）→ 指定別的靈魂仍放行。"""
    assumed = AgentIdentity(agent_id=OTHER_SOUL, human_role="admin", is_human=False)
    _assert_reached(_post(path, identity=assumed), soul_probe, OTHER_SOUL)


# --------------------------------------------------------------------------- #
# 子代理：sub_model=<靈魂>
# --------------------------------------------------------------------------- #


def _sub_identity() -> AgentIdentity:
    return AgentIdentity.for_role("__subagent_u2s0000__", "Subagent")


async def test_subagent_cannot_take_a_soul_as_sub_model(soul_probe: dict[str, Any]) -> None:
    """子代理把靈魂當 sub_model → 組裝時 403，人設與家對話都沒被注入。"""
    raised: ToukaException | None = None
    try:
        await subagent_tools._assemble_subagent_request(
            goal="整理資料", sub_identity=_sub_identity(), base_ctx=PipelineContext(),
            model=PROBE_SOUL,
        )
    except ToukaException as exc:
        raised = exc
    assert soul_probe["persona"] == []
    assert soul_probe["home"] == []
    assert raised is not None
    assert raised.status_code == 403
    assert raised.detail["code"] == "AUTH_PERMISSION_DENIED"


async def test_subagent_with_plain_model_is_unaffected(soul_probe: dict[str, Any]) -> None:
    """子代理用一般模型 → 照舊組裝，不套任何靈魂。"""
    request = await subagent_tools._assemble_subagent_request(
        goal="整理資料", sub_identity=_sub_identity(), base_ctx=PipelineContext(),
        model="claude-x",
    )
    assert request.model == "claude-x"
    assert soul_probe["persona"] == []
    assert soul_probe["home"] == []


# --------------------------------------------------------------------------- #
# 群聊：每位成員都檢查
# --------------------------------------------------------------------------- #

REGISTRY = {"Alice": [], "Bob": []}
WHITELIST = {"ToukaSoul": {"小愛": "Alice", "阿鮑": "Bob"}}


@pytest.fixture
def group_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """tmp 群聊設定；攔截每位成員的派發與靈魂注入，回傳被派發的成員 ID。"""
    (tmp_path / "group_whitelist.json").write_text(
        json.dumps(WHITELIST, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "agents_registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    for mod in ("src.services.group_router", "src.services.dispatchers.group"):
        monkeypatch.setattr(f"{mod}.get_config_dir", lambda: tmp_path)
    souls = SimpleNamespace(is_toukasoul={"Alice", "Bob"}.__contains__)
    monkeypatch.setattr("src.services.group_router.soul_service", souls)
    dispatched: list[str] = []

    async def _fake_gsg(_model, _req, identity, _request_id, _start_time, _ctx):
        dispatched.append(identity.agent_id)

        async def _gen():
            yield 'data: {"choices": [{"delta": {"content": "嗨"}, "index": 0}]}\n\n'
            yield "data: [DONE]\n\n"

        return _gen()

    soul = MagicMock()
    soul.inject_home_messages = AsyncMock()
    monkeypatch.setattr("src.services.dispatchers.group.get_stream_generator", _fake_gsg)
    monkeypatch.setattr("src.services.dispatchers.group.get_active_brain", lambda _i: "Brain-X")
    monkeypatch.setattr("src.services.dispatchers.group.record_working_brain", lambda *_a: None)
    monkeypatch.setattr("src.services.dispatchers.group.soul_service", soul)
    return dispatched


async def _group_text(text: str, caller: AgentIdentity | None) -> str:
    request = OpenAIChatRequest(
        model="ToukaGroup", messages=[OpenAIMessage(role="user", content=text)]
    )
    out = []
    ctx = PipelineContext(identity=caller)
    async for chunk in dispatch_group_stream(request, "req-u2s", 0.0, ctx):
        payload = chunk.removeprefix("data: ").strip()
        if payload.startswith("{"):
            delta = json.loads(payload).get("choices", [{}])[0].get("delta", {})
            out.append(delta.get("content", ""))
    return "".join(out)


async def test_group_mention_of_unowned_soul_is_blocked(group_cfg: list[str]) -> None:
    """normal 使用者 @別人的靈魂 → 不派發，回固定訊息。"""
    text = await _group_text("@小愛 哈囉", _human())
    assert text == "❌ **系統提示**：沒有權限和『@小愛』對話，只限靈魂本身、擁有者或管理員。\n\n"
    assert group_cfg == []


async def test_group_with_one_unowned_member_dispatches_nobody(group_cfg: list[str]) -> None:
    """擁有 Alice 但不擁有 Bob → 整輪不派發（連 Alice 也不先講）。"""
    text = await _group_text("@小愛 @阿鮑 哈囉", _human(owned=("Alice",)))
    assert text == "❌ **系統提示**：沒有權限和『@阿鮑』對話，只限靈魂本身、擁有者或管理員。\n\n"
    assert group_cfg == []


async def test_group_soul_agent_can_only_mention_itself(group_cfg: list[str]) -> None:
    """靈魂 Bob 的代理身分：@小愛 被擋，@阿鮑（自己）放行。"""
    bob = AgentIdentity.for_role("Bob", "Owner", is_human=False)
    blocked = await _group_text("@小愛 哈囉", bob)
    assert blocked == "❌ **系統提示**：沒有權限和『@小愛』對話，只限靈魂本身、擁有者或管理員。\n\n"
    assert group_cfg == []
    assert "【阿鮑】" in await _group_text("@阿鮑 哈囉", bob)
    assert group_cfg == ["Bob"]


async def test_group_owner_and_admin_still_dispatch(group_cfg: list[str]) -> None:
    """擁有者點自己的靈魂、admin 點任何靈魂 → 照舊派發。"""
    assert "【小愛】" in await _group_text("@小愛 哈囉", _human(owned=("Alice",)))
    assert "【阿鮑】" in await _group_text("@阿鮑 哈囉", _human(role="admin"))
    assert group_cfg == ["Alice", "Bob"]


def test_shipped_heartbeat_tester_is_not_a_group_soul(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出貨範本 registry 的 HeartbeatTester（不是靈魂）即使被白名單點名也不能進群聊。"""
    template = REPO_ROOT / "ToukaProxy" / "config" / "agents_registry.json.example"
    registry = json.loads(template.read_text(encoding="utf-8"))
    assert registry == {"HeartbeatTester": ["測試員", "Tester"]}
    (tmp_path / "group_whitelist.json").write_text(
        json.dumps({"ToukaSoul": {"測試員": "HeartbeatTester"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.services.group_router.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "src.services.group_router.soul_service", SimpleNamespace(is_toukasoul=lambda _i: False)
    )
    roster = parse_group_roster([OpenAIMessage(role="user", content="@測試員 你好")], registry)
    assert roster == {"error": "『@測試員』指向的『HeartbeatTester』不是靈魂，群聊成員只限靈魂。"}


# --------------------------------------------------------------------------- #
# SNS 釘子：閘道以可信身分存檔、pipeline 不帶身分，指定靈魂照常回覆
# --------------------------------------------------------------------------- #


def _sse(text: str) -> Any:
    async def _body() -> Any:
        yield f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    response = AsyncMock()
    response.body_iterator = _body()
    return response


async def test_sns_internal_path_still_reaches_soul(
    soul_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNS adapter 綁靈魂：pipeline 不帶身分，套人設與家對話並回覆；閘道以可信身分存檔。"""
    session_svc = AsyncMock()
    session_svc.open_channel_session.return_value = "sns-test-channel"
    session_svc.append_message_to_session.return_value = True
    monkeypatch.setattr("src.services.sns_gateway.session_service", session_svc)
    identity = AgentIdentity.for_role(PROBE_SOUL, "Owner")
    monkeypatch.setattr(identity_service, "get_identity", lambda _agent: identity)
    soul_probe["dispatch"].return_value = _sse("pong")
    adapter = MagicMock(agent_id=PROBE_SOUL, show_typing=AsyncMock(), send_message=AsyncMock())
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="u2s", content="ping")

    text = await SNSGatewayService().on_event(adapter, event)

    assert text == "pong"
    assert soul_probe["persona"] == [PROBE_SOUL]
    assert soul_probe["home"] == [PROBE_SOUL]
    soul_probe["dispatch"].assert_awaited_once()
    ctx = soul_probe["dispatch"].await_args.args[5]
    assert ctx.get("identity") is None
    assert ctx["session_id"] == "sns-test-channel"
    assert soul_probe["dispatch"].await_args.args[1].session_id == ctx["session_id"]
    session_svc.open_channel_session.assert_awaited_once_with(
        "sns", identity, "telegram", "u2s", title="Telegram: u2s"
    )
    assert all(
        call.kwargs["identity"] is identity
        for call in session_svc.append_message_to_session.await_args_list
    )
    assert session_svc.append_message_to_session.await_count == 2
    adapter.send_message.assert_awaited_once()

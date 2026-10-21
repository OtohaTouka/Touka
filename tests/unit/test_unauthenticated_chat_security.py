"""沒登入不能聊天（ADR-0245，模型層 2a U2a-1；禁令 20 對應安全測試）。

修補前的漏洞：`/messages`、`/chat/completions`（含 `/v1`）在沒帶憑證時改用 Guest 情境
照樣跑完整 pipeline——指定靈魂的名字就會套上它的人設、注入家對話，再用伺服器的金鑰
呼叫模型。修補後兩個聊天端點掛授權閘道的 `require_authenticated`，沒有已驗證身分一律
401，請求根本進不了 pipeline。

另釘兩件「照舊」的事：
- 首裝前（系統未認領）的本機領卡（LDH）仍發 DebugAgent 卡並放行；
- SNS 閘道不經 HTTP 認證閘，以可信靈魂身分存檔；pipeline 不帶身分，外人訊息技能維持零。

全部離線：上游 `dispatch_request` 以 spy 取代，不打任何 API。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.protocols.sns_base import BaseSNSAdapter
from src.services import identity_router, proxy_service
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.sns_gateway import SNSGatewayService
from src.services.soul_service import soul_service

PROBE_SOUL = "U2aProbeSoul"
_PAYLOADS = {
    "messages": {
        "model": PROBE_SOUL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    },
    "chat/completions": {
        "model": PROBE_SOUL,
        "messages": [{"role": "user", "content": "hi"}],
    },
}
_CHAT_PATHS = ["/v1/messages", "/v1/chat/completions", "/messages", "/chat/completions"]


def _payload(path: str) -> dict[str, Any]:
    return _PAYLOADS[path.removeprefix("/v1").lstrip("/")]


@pytest.fixture
def soul_probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """讓 PROBE_SOUL 被認成靈魂，並記錄人設、家對話與上游呼叫是否被觸及。"""
    seen: dict[str, Any] = {"persona": [], "home": [], "dispatch": AsyncMock(return_value={})}

    def _apply_persona(_request: Any, agent_id: str) -> None:
        seen["persona"].append(agent_id)

    async def _inject_home(_request: Any, agent_id: str) -> None:
        seen["home"].append(agent_id)

    async def _no_summary(*_args: Any) -> None:
        return None

    monkeypatch.setattr(soul_service, "is_toukasoul", lambda agent_id: agent_id == PROBE_SOUL)
    monkeypatch.setattr(soul_service, "get_soul_data", lambda _agent_id: ("PERSONA", {}))
    monkeypatch.setattr(soul_service, "apply_soul_to_request", _apply_persona)
    monkeypatch.setattr(soul_service, "inject_home_messages", _inject_home)
    monkeypatch.setattr(soul_service, "inject_context_summary", _no_summary)
    monkeypatch.setattr(
        "src.middleware.layers.soul.get_active_brain", lambda _identity: "claude-probe"
    )
    monkeypatch.setattr(proxy_service, "dispatch_request", seen["dispatch"])
    return seen


def _assert_rejected_before_pipeline(resp: Any, seen: dict[str, Any]) -> None:
    dispatched = seen["dispatch"].call_count
    reached = f"persona={seen['persona']} home={seen['home']} dispatch={dispatched}"
    assert resp.status_code == 401, f"{resp.status_code} {resp.text} | {reached}"
    assert resp.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"
    assert seen["persona"] == [], reached
    assert seen["home"] == [], reached
    seen["dispatch"].assert_not_called()


@pytest.mark.parametrize("path", _CHAT_PATHS)
def test_external_request_without_token_cannot_summon_soul(
    path: str, soul_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """外部來源、沒帶憑證，指定靈魂名字 → 401，人設、家對話、上游呼叫都沒被觸及。"""
    monkeypatch.setattr(identity_router, "is_request_from_trusted_local", lambda _request: False)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(path, json=_payload(path), headers={"X-Touka-Soul-ID": PROBE_SOUL})
    _assert_rejected_before_pipeline(resp, soul_probe)


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
def test_local_request_after_claim_cannot_summon_soul(
    path: str, soul_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """本機但系統已認領（LDH 關閉）、沒帶憑證，X-Agent-ID 冒稱靈魂 → 一樣 401。"""
    monkeypatch.setattr(AuthService, "any_claimed_user", lambda _self: True)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(path, json=_payload(path), headers={"X-Agent-ID": PROBE_SOUL})
    _assert_rejected_before_pipeline(resp, soul_probe)


def test_first_install_local_handshake_still_issues_debugagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首裝前（未認領）的本機無卡請求：LDH 仍發 DebugAgent 卡並放行（照舊）。"""
    captured: dict[str, Any] = {}

    async def _spy(_request: Any, ctx: Any, is_anthropic: bool = True) -> dict[str, bool]:
        captured["ctx"] = ctx
        return {"ok": True}

    monkeypatch.setattr(AuthService, "any_claimed_user", lambda _self: False)
    monkeypatch.setattr(identity_router._ldh_limiter, "is_allowed", lambda *_args: True)
    monkeypatch.setattr("src.routers.proxy.process_proxy_request", _spy)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/v1/messages", json=_payload("/v1/messages"))

    assert resp.status_code == 200, resp.text
    assert captured["ctx"]["identity"].agent_id == "DebugAgent"
    issued = identity_service.verify_token(resp.headers["X-New-Auth-Token"])
    assert issued is not None
    assert issued.agent_id == "DebugAgent"


class _Adapter(BaseSNSAdapter):
    """最小 SNS adapter：只收集送出的訊息。"""

    def __init__(self) -> None:
        self.agent_id = PROBE_SOUL
        self.sent: list[Any] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_message(self, _channel_id: str, response: Any) -> bool:
        self.sent.append(response)
        return True

    async def show_typing(self, _channel_id: str) -> None:
        return None


def _sse(text: str) -> Any:
    async def _body() -> Any:
        yield f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    response = AsyncMock()
    response.body_iterator = _body()
    return response


async def test_sns_internal_path_is_not_gated_by_http_auth(
    soul_probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNS 不需 HTTP 憑證：pipeline 不帶身分、技能為零，照常跑完真 pipeline 並回覆。"""
    session_svc = AsyncMock()
    session_svc.open_channel_session.return_value = "sns-test-channel"
    session_svc.append_message_to_session.return_value = True
    monkeypatch.setattr("src.services.sns_gateway.session_service", session_svc)
    identity = AgentIdentity.for_role(PROBE_SOUL, "Owner")
    monkeypatch.setattr(identity_service, "get_identity", lambda _agent: identity)
    dispatch = AsyncMock(return_value=_sse("pong"))
    monkeypatch.setattr(proxy_service, "dispatch_request", dispatch)
    adapter = _Adapter()
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="ch1", content="ping")

    text = await SNSGatewayService().on_event(adapter, event)

    assert text == "pong"
    assert len(adapter.sent) == 1
    dispatch.assert_awaited_once()
    ctx = dispatch.await_args.args[5]
    assert ctx.get("identity") is None
    assert ctx["session_id"] == "sns-test-channel"
    assert dispatch.await_args.args[1].session_id == ctx["session_id"]
    session_svc.open_channel_session.assert_awaited_once_with(
        "sns", identity, "telegram", "ch1", title="Telegram: ch1"
    )
    assert ctx["active_skills"] == []

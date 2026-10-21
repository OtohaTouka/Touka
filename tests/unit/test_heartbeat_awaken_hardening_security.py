"""DA24: harden the heartbeat /awaken endpoint.

The ownership + `heartbeat_awakening` capability gates already existed, but:
  1. `security_level` mapped ANY non-"safe" value (typo / garbage) to FullYOLO —
     `"SafeAuto" if security_level == "safe" else "FullYOLO"`. Fix: an explicit
     allowlist (`_resolve_auth_level`) where only "full" grants FullYOLO and anything
     unknown fails safe (ADR-0246 U2c-5: to Manual, the strictest mode).
  2. The caller-controlled `init_prompt` was injected verbatim as a `user` message into
     the agent's privileged LLM context. Fix: pass it through the H3 content guard
     (`sanitize_memory_content`) first — same boundary as the DA9 dispatch text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.models.user import User
from src.routers.heartbeat import _resolve_auth_level, identity_service, permission_service
from src.services.auth_service import AuthService

_HEARTBEAT_SERVICE_VALUE = "test_heartbeat_token_for_ci"


class TestSecurityLevelMapping:
    """#1: only the explicit 'full' value may escalate to YOLO (formerly FullYOLO)."""

    def test_safe_maps_to_safeauto(self):
        assert _resolve_auth_level("safe") == "Auto"

    def test_full_maps_to_fullyolo(self):
        assert _resolve_auth_level("full") == "YOLO"

    def test_unknown_value_fails_safe_to_manual(self):
        # previously ANY of these escalated straight to FullYOLO; U2c-5: Manual (strictest)
        assert _resolve_auth_level("garbage") == "Manual"
        assert _resolve_auth_level("") == "Manual"
        assert _resolve_auth_level("FULL") == "Manual"  # case-sensitive, not 'full'
        assert _resolve_auth_level("yolo") == "Manual"


def _setup_awaken_gates(monkeypatch, mock_identity):
    """Make the awaken endpoint reach the init-prompt injection for DebugAgent."""
    monkeypatch.setattr("src.routers.heartbeat.HEARTBEAT_TOKEN", _HEARTBEAT_SERVICE_VALUE)
    monkeypatch.setattr(
        "src.routers.heartbeat._process_heartbeat_loop", AsyncMock(return_value=1)
    )
    debug_id = mock_identity(agent_id="DebugAgent", capabilities=["heartbeat_awakening"])
    monkeypatch.setattr(identity_service, "get_identity", lambda x: debug_id)
    monkeypatch.setattr(permission_service, "can_awaken", lambda x: True)
    # ADR-0250 A3：沒有主要模型的靈魂 /awaken 回 409；這裡測的是 init_prompt，先給主要模型
    monkeypatch.setattr("src.routers.heartbeat.get_active_brain", lambda _identity: "claude-x")
    monkeypatch.setattr(
        AuthService,
        "get_all_users",
        lambda self: [
            User(
                id="owner_id",
                email="owner@test.com",
                hashed_password="...",
                owned_agents=["DebugAgent"],
                refresh_token_exp=9999999999,
            )
        ],
    )


def test_awaken_orphan_denied_until_attributed(auth_client, monkeypatch):
    """守衛 B 因果鏈（ADR-0158 自動歸屬所依賴）：孤兒靈魂 awaken 必拒
    HEARTBEAT_ORPHAN_AGENT；寫入 owned_agents（＝create_soul 自動歸屬的那一筆）後，
    同請求須通過守衛 B（改死在後續能力閘＝orphan 閘已放行）。"""
    monkeypatch.setattr("src.routers.heartbeat.HEARTBEAT_TOKEN", _HEARTBEAT_SERVICE_VALUE)
    owner = User(
        id="owner_id",
        email="owner@test.com",
        hashed_password="...",
        owned_agents=[],
        refresh_token_exp=9999999999,
    )
    monkeypatch.setattr(AuthService, "get_all_users", lambda self: [owner])

    payload = {"token": _HEARTBEAT_SERVICE_VALUE, "agent_id": "OrphanSoulX", "pulse_count": 1}
    r1 = auth_client.post("/v1/heartbeat/awaken", json=payload)
    assert r1.json()["detail"]["code"] == "HEARTBEAT_ORPHAN_AGENT"

    owner.owned_agents = ["OrphanSoulX"]  # ＝自動歸屬寫入 users.json 的效果
    monkeypatch.setattr(permission_service, "can_awaken", lambda x: False)  # 停在下一閘
    r2 = auth_client.post("/v1/heartbeat/awaken", json=payload)
    assert r2.json()["detail"]["code"] == "HEARTBEAT_UNAUTHORIZED_AGENT"


def test_awaken_sanitizes_init_prompt(auth_client, mock_identity, monkeypatch):
    """#2: a caller-supplied init_prompt must pass the H3 content guard (truncation)
    before it is injected into the agent's session."""
    _setup_awaken_gates(monkeypatch, mock_identity)

    captured: dict = {}

    async def _capture_append(session_id, role, content, **kwargs):
        captured["content"] = content
        return True

    monkeypatch.setattr(
        "src.routers.heartbeat.session_service.get_session_by_id",
        AsyncMock(return_value={"messages": {}, "title": "x"}),
    )
    monkeypatch.setattr(
        "src.routers.heartbeat.session_service.append_message_to_session", _capture_append
    )

    long_prompt = "A" * 9000  # > content_guard MAX_CONTENT_LENGTH (8000)
    resp = auth_client.post(
        "/v1/heartbeat/awaken",
        json={
            "token": _HEARTBEAT_SERVICE_VALUE,
            "agent_id": "DebugAgent",
            "pulse_count": 1,
            "init_prompt": long_prompt,
        },
    )

    assert resp.status_code == 202
    assert "content" in captured
    # the 9000-char prompt must have been truncated to <= 8000 (+ a short time prefix)
    assert len(captured["content"]) <= 8100

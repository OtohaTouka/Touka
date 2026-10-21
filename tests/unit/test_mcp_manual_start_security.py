"""自建 MCP 在等隔離時，人從設定頁手動啟動（帶同意）的端點規則（ADR-0275 §7）。

``POST /mcp/{name}/restart`` 多一個選填 body ``{isolation_ack, generation, bound}``：

- 沒帶 ``isolation_ack``：照今天重啟（舊前端不送 body）。
- 帶了：人類請求者（函式內呼叫，403）、privileged、CSRF；``generation`` 與 ``bound`` 必須等於
  現在的（422 ``mcp_entry_changed``，設定改過要重新確認）；只對「自建、在等隔離」的那台
  （422 ``mcp_not_waiting_isolation``）；``isolation_ack`` 必須等於伺服器當下算的降級去處
  （422 ``mcp_isolation_ack_mismatch``）；同意綁伺服器名、generation 與 ``bound``，只交給啟動層
  用這一次。

啟動層（U3d 的 ``mcp_launch``、U3e 的接線）還沒合入：這裡用假的 ``McpManualStarter``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
import src.main as main_module
import src.routers.mcp as mcp_router
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.models.identity import AgentIdentity
from src.models.mcp_isolation import McpRestartRequest
from src.protocols.registry import ProtocolServerConfig
from src.services.identity_router import get_verified_identity
from src.services.isolation.consent import Consent, consent_subject
from src.services.isolation.status import FallbackPlan
from src.services.mcp_isolation_store import bound_of

if TYPE_CHECKING:
    from collections.abc import Callable

HUMAN = AgentIdentity.for_role(agent_id="owner@touka.io", role="admin", is_human=True)
ADMIN_ACTING = AgentIdentity(agent_id="Alice", human_role="admin", is_human=False)
AGENT = AgentIdentity.for_role(agent_id="Alice", role="Owner", is_human=False)
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", is_human=False)
_ENTRY = {"command": "python", "args": ["/data/mcp_servers/managed/calc/server.py"]}
_GENERATION = 3


class _Manager:
    """只留重啟端點用到的幾個方法。"""

    def __init__(self) -> None:
        self.registry = self
        self.restart_server = AsyncMock(return_value=True)
        self.config = ProtocolServerConfig(**_ENTRY)

    def get_server(self, name: str) -> ProtocolServerConfig | None:
        return self.config if name == "calc" else None

    def get_installation_generation(self, _name: str) -> int:
        return _GENERATION

    async def get_active_server_info(self) -> list[dict[str, Any]]:
        return [{"name": "calc", "status": "connected"}]


class _Starter:
    """假的啟動層：``plan`` 是這台現在的降級去處（None＝不是自建等待中）。"""

    def __init__(self, plan: FallbackPlan | None) -> None:
        self.plan = plan
        self.started: list[tuple[str, Consent]] = []

    def manual_start_plan(self, name: str) -> FallbackPlan | None:
        return self.plan if name == "calc" else None

    async def start_with_consent(self, name: str, consent: Consent) -> bool:
        self.started.append((name, consent))
        return True


@pytest.fixture
def restart_api(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """回傳 ``call(identity, body=None, starter=None, headers=None) -> (response, manager)``。"""
    client = TestClient(main_module.app)

    def _call(
        identity: AgentIdentity,
        body: dict[str, Any] | None = None,
        starter: _Starter | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, _Manager]:
        manager = _Manager()
        monkeypatch.setattr(main_module, "protocol_manager", manager)
        context = {"mcp_manual_starter": starter} if starter is not None else {}
        monkeypatch.setattr(main_module.app.state, "context", context, raising=False)
        main_module.app.dependency_overrides[get_verified_identity] = lambda: identity
        try:
            response = client.post("/v1/mcp/calc/restart", json=body, headers=headers or {})
        finally:
            main_module.app.dependency_overrides.pop(get_verified_identity, None)
        return response, manager

    return _call


def _ack(fallback: str = "landlock", **overrides: Any) -> dict[str, Any]:
    return {
        "isolation_ack": fallback,
        "generation": _GENERATION,
        "bound": bound_of(_ENTRY),
        "features": ["fs"] if fallback == "landlock" else [],
        **overrides,
    }


def _detail(response: Any) -> str:
    return response.json()["detail"]["message"]


_LANDLOCK = FallbackPlan("landlock", frozenset({"fs"}))


def test_restart_features_has_a_bounded_list():
    assert len(McpRestartRequest(features=["fs"] * 16).features) == 16
    with pytest.raises(ValidationError):
        McpRestartRequest(features=["fs"] * 17)


def test_restart_without_a_body_is_unchanged(restart_api: Callable[..., Any]) -> None:
    """舊前端不送 body：照今天重啟，不需要人類請求者。"""
    response, manager = restart_api(DEBUGGER)

    assert response.status_code == 200
    assert response.json()["status"] == "restarted"
    manager.restart_server.assert_awaited_once_with("calc")


@pytest.mark.parametrize("identity", [AGENT, DEBUGGER])
def test_manual_start_requires_a_human_requester(
    restart_api: Callable[..., Any], identity: AgentIdentity
) -> None:
    starter = _Starter(_LANDLOCK)

    response, manager = restart_api(identity, _ack(), starter)

    assert response.status_code == 403
    assert starter.started == []
    manager.restart_server.assert_not_awaited()


def test_manual_start_accepts_an_admin_acting_as_a_soul(restart_api: Callable[..., Any]) -> None:
    starter = _Starter(_LANDLOCK)

    response, _ = restart_api(ADMIN_ACTING, _ack(), starter)

    assert response.status_code == 200
    assert len(starter.started) == 1


def test_manual_start_rejects_cross_site_requests(restart_api: Callable[..., Any]) -> None:
    starter = _Starter(_LANDLOCK)

    response, _ = restart_api(HUMAN, _ack(), starter, {"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert starter.started == []


@pytest.mark.parametrize(
    "overrides",
    [{"generation": _GENERATION - 1}, {"bound": bound_of({**_ENTRY, "args": ["other.py"]})}],
    ids=["generation", "bound"],
)
def test_manual_start_requires_the_current_generation_and_bound(
    restart_api: Callable[..., Any], overrides: dict[str, Any]
) -> None:
    """確認框打開之後設定變了（重載、刪掉再加回、改了程式）：要重新確認。"""
    starter = _Starter(_LANDLOCK)

    response, manager = restart_api(HUMAN, _ack(**overrides), starter)

    assert response.status_code == 422
    assert _detail(response) == "mcp_entry_changed"
    assert starter.started == []
    manager.restart_server.assert_not_awaited()


def test_manual_start_needs_generation_and_bound(restart_api: Callable[..., Any]) -> None:
    starter = _Starter(_LANDLOCK)

    response, _ = restart_api(HUMAN, {"isolation_ack": "landlock"}, starter)

    assert response.status_code == 422
    assert starter.started == []


@pytest.mark.parametrize("starter", [None, _Starter(None)], ids=["not-wired", "not-waiting"])
def test_manual_start_only_applies_to_self_built_servers_waiting_for_isolation(
    restart_api: Callable[..., Any], starter: _Starter | None
) -> None:
    response, manager = restart_api(HUMAN, _ack(), starter)

    assert response.status_code == 422
    assert _detail(response) == "mcp_not_waiting_isolation"
    manager.restart_server.assert_not_awaited()


@pytest.mark.parametrize("ack", ["executor", "direct"])
def test_manual_start_ack_must_match_the_current_fallback(
    restart_api: Callable[..., Any], ack: str
) -> None:
    """確認框寫的去處和伺服器當下算的不同：不算同意（例如對話框寫 landlock，現在只能 direct）。"""
    starter = _Starter(_LANDLOCK)

    response, _ = restart_api(HUMAN, _ack(ack), starter)

    assert response.status_code == 422
    assert _detail(response) == "mcp_isolation_ack_mismatch"
    assert starter.started == []


def test_manual_start_hands_one_bound_consent_to_the_launch_layer(
    restart_api: Callable[..., Any],
) -> None:
    starter = _Starter(FallbackPlan("direct", frozenset()))

    response, manager = restart_api(HUMAN, _ack("direct"), starter)

    assert response.status_code == 200
    assert response.json()["status"] == "restarted"
    assert response.json()["server"] == {"name": "calc", "status": "connected"}
    subject = consent_subject(
        "mcp_manual_start", {"name": "calc", "generation": _GENERATION, "bound": bound_of(_ENTRY)}
    )
    assert starter.started == [("calc", Consent(subject, "direct", frozenset()))]
    manager.restart_server.assert_not_awaited()


def test_manual_start_builds_the_subject_only_through_mcp_launch(
    restart_api: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSOT：換掉 ``mcp_launch.manual_start_subject`` 的那份，交出去的同意就帶它的結果."""
    calls: list[tuple[str, int, str]] = []

    def _spy(name: str, generation: int, bound: str) -> str:
        calls.append((name, generation, bound))
        return "subject-from-mcp-launch"

    monkeypatch.setattr(mcp_router, "manual_start_subject", _spy)
    starter = _Starter(_LANDLOCK)

    restart_api(HUMAN, _ack(), starter)

    assert calls == [("calc", _GENERATION, bound_of(_ENTRY))]
    assert [consent.subject for _name, consent in starter.started] == ["subject-from-mcp-launch"]


def test_manual_start_passes_the_fallback_features(restart_api: Callable[..., Any]) -> None:
    starter = _Starter(_LANDLOCK)

    restart_api(HUMAN, _ack(), starter)

    (_name, consent), = starter.started
    assert consent.fallback == "landlock"
    assert consent.features == frozenset({"fs"})


@pytest.mark.parametrize("features", [["fs", "tcp"], [], None])
def test_manual_start_rejects_changed_or_missing_features(restart_api, features):
    starter = _Starter(_LANDLOCK)
    body = _ack(features=features)
    if features is None:
        body.pop("features")
    response, _ = restart_api(HUMAN, body, starter)
    assert response.status_code == 422
    assert starter.started == []
    if features is not None:
        assert _detail(response) == "mcp_isolation_ack_mismatch"

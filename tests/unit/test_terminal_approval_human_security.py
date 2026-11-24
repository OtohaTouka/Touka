"""舊的終端核准端點只收人類管理員本人（外部審查第 12 項，2026-11-24）.

``/terminal/approve``、``/terminal/reject`` 解算技能安裝、能力部署與沒人在場的終端指令；
``/terminal/stream``、``/terminal/history`` 會看到所有代理人的指令與輸出。四個端點只收
人類管理員本人；核准與拒絕另外一定要帶信任的 Origin。只用合成 ID，不執行任何指令。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import terminal
from src.services.identity_router import get_verified_identity
from src.services.identity_service import ldh_identity

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Response

ADMIN = AgentIdentity.for_role(agent_id="zz-term-admin", role="admin", is_human=True)
ASSUMED_ADMIN = AgentIdentity.for_role(agent_id="zz-term-assumed", role="admin")  # admin 代打
# 一般使用者帶 X-Agent-ID 代打自己的靈魂，解析出來是靈魂的 Owner（identity_router）。
USER_AS_SOUL = AgentIdentity.for_role(agent_id="zz-term-soul", role="Owner")
DEBUGGER = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
USER = AgentIdentity.for_role(agent_id="zz-term-user", role="normal", is_human=True)
TRUSTED_ORIGIN = "http://localhost:3001"
PAYLOAD_VALUE = "sk-zz-term-not-a-real-key-0123456789"

REFUSED = pytest.mark.parametrize(
    "who",
    ["assumed-admin", "user-as-soul", "debugger", "ldh", "user", "anonymous"],
)
DECISIONS = pytest.mark.parametrize(
    ("action", "status"), [("approve", "approved"), ("reject", "rejected")]
)
ACTIONS = pytest.mark.parametrize("action", ["approve", "reject"])


def _identity(who: str) -> AgentIdentity | None:
    return {
        "admin": ADMIN,
        "assumed-admin": ASSUMED_ADMIN,
        "user-as-soul": USER_AS_SOUL,
        "debugger": DEBUGGER,
        # 首次安裝前沒登入的本機請求拿到的開發卡（沒有類型旗標，是代理人身分）。
        "ldh": ldh_identity("zz-term-ldh"),
        "user": USER,
        "anonymous": None,
    }[who]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # 事件串流本來不會結束；換成立刻結束的回應，被錯放進來時測試才不會卡住。
    monkeypatch.setattr(terminal, "StreamingResponse", lambda *_a, **_k: PlainTextResponse("open"))
    subscribers = list(terminal.sse_subscribers)
    terminal.pending_approvals.clear()
    terminal.approval_results.clear()
    terminal.approval_payloads.clear()
    yield
    terminal.pending_approvals.clear()
    terminal.approval_results.clear()
    terminal.approval_payloads.clear()
    terminal.sse_subscribers[:] = subscribers
    app.dependency_overrides.pop(get_verified_identity, None)


def _as(who: str) -> TestClient:
    identity = _identity(who)
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


def _pending(exec_id: str) -> asyncio.Event:
    event = asyncio.Event()
    terminal.pending_approvals[exec_id] = event
    return event


def _decide(
    client: TestClient, action: str, exec_id: str, origin: str | None = TRUSTED_ORIGIN, **kw: object
) -> Response:
    headers = {"Origin": origin} if origin else {}
    return client.post(f"/terminal/{action}/{exec_id}", headers=headers, **kw)


def _code(response: Response) -> str:
    return response.json()["detail"]["code"]


@DECISIONS
def test_a_human_admin_with_a_trusted_origin_decides(action: str, status: str) -> None:
    event = _pending("zz-exec-ok")
    response = _decide(_as("admin"), action, "zz-exec-ok")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    assert event.is_set()
    assert terminal.approval_results["zz-exec-ok"] == status


@REFUSED
@ACTIONS
def test_everyone_but_a_human_admin_is_refused(who: str, action: str) -> None:
    event = _pending("zz-exec-refused")
    response = _decide(_as(who), action, "zz-exec-refused")
    assert response.status_code == 403, response.text
    assert _code(response) == "AUTH_ADMIN_REQUIRED"
    assert not event.is_set()
    assert "zz-exec-refused" not in terminal.approval_results


@pytest.mark.parametrize("origin", [None, "https://evil.example.test", "null"])
@ACTIONS
def test_a_human_admin_without_a_trusted_origin_is_refused(origin: str | None, action: str) -> None:
    event = _pending("zz-exec-origin")
    response = _decide(_as("admin"), action, "zz-exec-origin", origin=origin)
    assert response.status_code == 403, response.text
    assert not event.is_set()
    assert "zz-exec-origin" not in terminal.approval_results


@ACTIONS
def test_identity_is_checked_before_the_exec_id_exists(action: str) -> None:
    """沒權限的人問不出哪些編號正在等核准：不存在的編號也是 403，不是 404。"""
    response = _decide(_as("user-as-soul"), action, "zz-exec-missing")
    assert response.status_code == 403, response.text
    assert _code(response) == "AUTH_ADMIN_REQUIRED"
    admin = _decide(_as("admin"), action, "zz-exec-missing")
    assert _code(admin) == "TERMINAL_APPROVAL_NOT_FOUND"


def test_the_log_names_the_approver_but_not_the_payload(caplog: pytest.LogCaptureFixture) -> None:
    _pending("zz-exec-log")
    with caplog.at_level(logging.INFO, logger=terminal.__name__):
        body = {"payload": {"env_vars": {"KEY": PAYLOAD_VALUE}}}
        response = _decide(_as("admin"), "approve", "zz-exec-log", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["has_payload"] is True
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "zz-term-admin" in logged
    assert "zz-exec-log" in logged
    assert PAYLOAD_VALUE not in logged


@REFUSED
@pytest.mark.parametrize("path", ["/terminal/stream", "/terminal/history"])
def test_stream_and_history_are_for_a_human_admin_only(who: str, path: str) -> None:
    subscribers = len(terminal.sse_subscribers)
    response = _as(who).get(path)
    assert response.status_code == 403, response.text
    assert _code(response) == "AUTH_ADMIN_REQUIRED"
    assert len(terminal.sse_subscribers) == subscribers


def test_a_human_admin_opens_the_stream_and_reads_the_history() -> None:
    client = _as("admin")
    stream = client.get("/terminal/stream")
    assert stream.status_code == 200, stream.text
    assert stream.text == "open"
    history = client.get("/terminal/history")
    assert history.status_code == 200, history.text
    assert "history" in history.json()

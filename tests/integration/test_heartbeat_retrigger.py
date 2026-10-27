"""P5c bonus：UI 一鍵重新觸發 heartbeat（/v1/heartbeat/retrigger）。

以人類擁有者身分（非 service token）對既有中斷會話派發延續性背景脈衝；
強制 Auto、不新建會話。供「上次有 N 個未完成」通知的一鍵重跑按鈕使用。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import heartbeat as hb
from src.services import destructive_ops
from src.services.autonomy import autonomy_service
from src.services.identity_router import get_verified_identity
from src.utils.async_db import SessionService, SessionSnapshot

# R-2c ③ #2：重跑一律以 Auto 執行，只有 admin 或該靈魂的人類擁有者能按（與 session 寫入的
# 模式授權同一個判斷）；對話必須屬於這個靈魂、呼叫者要能存取那個對話。
HUMAN_OWNER = AgentIdentity.for_role(
    "retrigger-owner", "normal", is_human=True, owned_agents=["DebugAgent"]
)
OWN_SESSION = {
    "id": "sess-x", "is_home": 0, "home_for_agent": None,
    "title": "t", "messages": {}, "agent_id": "DebugAgent",
}


def _who(name: str) -> AgentIdentity:
    return AgentIdentity(agent_id=name)


def _as(identity: AgentIdentity) -> None:
    """auth_client 預設是代理身分；重跑要以人類擁有者呼叫（auth_client 收尾時清掉覆寫）。"""
    app.dependency_overrides[get_verified_identity] = lambda: identity


def _owns(target: AgentIdentity, caller: AgentIdentity) -> bool:
    """沙箱沒有 users.json：擁有權照身分的 owned_agents；照 is_owner 現行規則，Debugger 旁路、
    代理身分存取自己也算（is_owner 不改）。"""
    return (
        caller.is_admin_authority()
        or caller.agent_role == "Debugger"
        or (not caller.is_human and caller.agent_id == target.agent_id)
        or target.agent_id in caller.owned_agents
    )


def test_retrigger_dispatches_loop_for_owner(auth_client, monkeypatch, mock_identity):
    """擁有者重新觸發既有會話 → 202、派發背景脈衝、並自未完成清單移除。"""
    _as(HUMAN_OWNER)
    monkeypatch.setattr(hb.resource_manager, "is_owner", lambda _t, _c: True)
    monkeypatch.setattr(
        hb.identity_service, "get_identity",
        lambda _aid: mock_identity(agent_id="DebugAgent"),
    )
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _x: True)
    monkeypatch.setattr(
        SessionService, "read_persistence_snapshot",
        AsyncMock(return_value=SessionSnapshot(dict(OWN_SESSION), "revision")),
    )
    monkeypatch.setattr(hb, "_process_heartbeat_loop", AsyncMock(return_value=None))
    # 預先放一筆「上次未完成」，驗證重新觸發後會被清除
    autonomy_service._interrupted_runs = [
        {"session_id": "sess-x", "agent": "DebugAgent", "total": 3}
    ]

    resp = auth_client.post(
        "/v1/heartbeat/retrigger",
        json={"agent_id": "DebugAgent", "session_id": "sess-x"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["session_id"] == "sess-x"
    # 已自未完成清單移除
    assert all(
        r.get("session_id") != "sess-x"
        for r in autonomy_service.get_interrupted_runs()
    )


def test_retrigger_forces_safeauto_on_dispatched_ctx(auth_client, monkeypatch, mock_identity):
    """E39（ADR-0176）：捕捉實際交給 _process_heartbeat_loop 的 ctx，斷言 auth_level
    被強制為 Auto（UI 重跑永不升級自主權；DA24 精神；ADR-0246 由 SafeAuto 改名）。

    上一個測試把背景迴圈整支 AsyncMock、只驗 202/清單——heartbeat.py 把
    `ctx.auth_level = "Auto"` 改 YOLO（或刪行沿用預設）全綠＝擁有者按鈕
    讓背景迴圈以完全自主權執行工具、繞過審批＝提權 fails open。
    """
    _as(HUMAN_OWNER)
    monkeypatch.setattr(hb.resource_manager, "is_owner", lambda _t, _c: True)
    target = mock_identity(agent_id="DebugAgent")
    monkeypatch.setattr(hb.identity_service, "get_identity", lambda _aid: target)
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _x: True)
    monkeypatch.setattr(
        SessionService, "read_persistence_snapshot",
        AsyncMock(return_value=SessionSnapshot(dict(OWN_SESSION), "revision")),
    )
    loop_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(hb, "_process_heartbeat_loop", loop_spy)

    resp = auth_client.post(
        "/v1/heartbeat/retrigger",
        json={"agent_id": "DebugAgent", "session_id": "sess-x"},
    )
    assert resp.status_code == 202

    # TestClient 於回應週期內同步執行 BackgroundTasks → spy 已收到真實引數
    loop_spy.assert_awaited_once()
    dispatched = loop_spy.await_args.args
    ctx = dispatched[3]  # (session_id, identity, model_override, ctx, pulse_count, ...)
    assert ctx.auth_level == "Auto"  # 載重斷言：強制安全、永不升級
    assert dispatched[1] is target  # 背景迴圈以 target 身分執行
    assert ctx.agent_id == "DebugAgent"


def test_retrigger_denied_for_non_owner(auth_client, monkeypatch):
    """非擁有者重新觸發 → 403。"""
    monkeypatch.setattr(hb.resource_manager, "is_owner", lambda _t, _c: False)
    resp = auth_client.post(
        "/v1/heartbeat/retrigger",
        json={"agent_id": "DebugAgent", "session_id": "sess-x"},
    )
    assert resp.status_code == 403


def test_retrigger_404_when_session_missing(auth_client, monkeypatch, mock_identity):
    """會話不存在 → 404（重新觸發只針對既有中斷會話）。"""
    _as(HUMAN_OWNER)
    monkeypatch.setattr(hb.resource_manager, "is_owner", lambda _t, _c: True)
    monkeypatch.setattr(
        hb.identity_service, "get_identity",
        lambda _aid: mock_identity(agent_id="DebugAgent"),
    )
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _x: True)
    monkeypatch.setattr(
        SessionService, "read_persistence_snapshot",
        AsyncMock(return_value=SessionSnapshot(None, "revision")),
    )
    resp = auth_client.post(
        "/v1/heartbeat/retrigger",
        json={"agent_id": "DebugAgent", "session_id": "missing"},
    )
    assert resp.status_code == 404


def _arm(monkeypatch, mock_identity, session: dict) -> AsyncMock:
    monkeypatch.setattr(hb.resource_manager, "is_owner", _owns)
    monkeypatch.setattr(
        hb.identity_service, "get_identity", lambda _aid: mock_identity(agent_id="DebugAgent")
    )
    monkeypatch.setattr(hb.permission_service, "can_awaken", lambda _x: True)
    monkeypatch.setattr(
        SessionService, "read_persistence_snapshot",
        AsyncMock(return_value=SessionSnapshot(session, "revision")),
    )
    loop_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(hb, "_process_heartbeat_loop", loop_spy)
    return loop_spy


@pytest.mark.parametrize(
    "caller",
    [
        AgentIdentity.for_role("DebugAgent", "Debugger"),
        AgentIdentity.for_role("DebugAgent", "Owner", capabilities=["heartbeat_awakening"]),
    ],
    ids=["debugger", "soul_itself"],
)
def test_retrigger_runs_auto_only_for_admin_or_human_owner(
    auth_client, monkeypatch, mock_identity, caller
):
    """Debugger 的 is_owner 旁路與靈魂自己都不能讓對話以 Auto 跑（只在這條路徑收緊）。"""
    loop_spy = _arm(monkeypatch, mock_identity, dict(OWN_SESSION))
    _as(caller)
    resp = auth_client.post(
        "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-x"}
    )
    assert resp.status_code == 403, resp.text
    loop_spy.assert_not_awaited()


@pytest.mark.parametrize(
    "caller",
    [
        HUMAN_OWNER,
        # 兩個靈魂都擁有（存取得到那個對話）也不行：脈衝存檔會把對話改掛到 DebugAgent
        AgentIdentity.for_role(
            "retrigger-owner", "normal", is_human=True, owned_agents=["DebugAgent", "VictimSoul"]
        ),
    ],
    ids=["stranger_session", "own_other_soul_session"],
)
def test_retrigger_rejects_a_session_of_another_soul(
    auth_client, monkeypatch, mock_identity, caller
):
    """擁有 DebugAgent 的人知道別人的 session id：不能讓 DebugAgent 在那個對話跑。"""
    foreign = {**OWN_SESSION, "agent_id": "VictimSoul", "owner_user_id": "victim"}
    loop_spy = _arm(monkeypatch, mock_identity, foreign)
    _as(caller)
    resp = auth_client.post(
        "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-v"}
    )
    assert resp.status_code == 403, resp.text
    loop_spy.assert_not_awaited()


@pytest.mark.parametrize(
    "caller",
    [HUMAN_OWNER, AgentIdentity.for_role("retrigger-admin", "admin", is_human=True)],
    ids=["human_owner", "admin"],
)
def test_retrigger_on_own_session_still_runs_auto(auth_client, monkeypatch, mock_identity, caller):
    loop_spy = _arm(monkeypatch, mock_identity, dict(OWN_SESSION))
    _as(caller)
    resp = auth_client.post(
        "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-x"}
    )
    assert resp.status_code == 202, resp.text
    assert loop_spy.await_args.args[3].auth_level == "Auto"


def test_retrigger_is_refused_while_the_soul_is_reset_or_deleted(
    auth_client, monkeypatch, mock_identity
):
    """舊審查報告丙-4：重置、清除或刪除這個靈魂期間重新觸發回 409，「上次未完成」保留。"""
    loop_spy = _arm(monkeypatch, mock_identity, dict(OWN_SESSION))
    autonomy_service._interrupted_runs = [
        {"session_id": "sess-x", "agent": "DebugAgent", "total": 3}
    ]
    _as(HUMAN_OWNER)
    destructive_ops.hold(_who("DebugAgent"))
    try:
        resp = auth_client.post(
            "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-x"}
        )
    finally:
        destructive_ops.release(_who("DebugAgent"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_RESET_IN_PROGRESS"
    loop_spy.assert_not_awaited()
    assert [r["session_id"] for r in autonomy_service.get_interrupted_runs()] == ["sess-x"]
    autonomy_service._interrupted_runs = []


def test_retrigger_checks_after_reading_the_session(auth_client, monkeypatch, mock_identity):
    """讀對話時有 await；這段期間開始的重置也擋，「上次未完成」照留。"""
    loop_spy = _arm(monkeypatch, mock_identity, dict(OWN_SESSION))

    async def read_during_a_reset(*_args: object, **_kwargs: object) -> SessionSnapshot:
        destructive_ops.hold(_who("DebugAgent"))
        return SessionSnapshot(dict(OWN_SESSION), "revision")

    monkeypatch.setattr(SessionService, "read_persistence_snapshot", read_during_a_reset)
    autonomy_service._interrupted_runs = [
        {"session_id": "sess-x", "agent": "DebugAgent", "total": 3}
    ]
    _as(HUMAN_OWNER)
    try:
        resp = auth_client.post(
            "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-x"}
        )
    finally:
        destructive_ops.release(_who("DebugAgent"))
    assert resp.status_code == 409, resp.text
    loop_spy.assert_not_awaited()
    assert [r["session_id"] for r in autonomy_service.get_interrupted_runs()] == ["sess-x"]
    autonomy_service._interrupted_runs = []


def test_retrigger_hands_the_entry_generation_to_the_loop(auth_client, monkeypatch, mock_identity):
    loop_spy = _arm(monkeypatch, mock_identity, dict(OWN_SESSION))
    _as(HUMAN_OWNER)
    resp = auth_client.post(
        "/v1/heartbeat/retrigger", json={"agent_id": "DebugAgent", "session_id": "sess-x"}
    )
    assert resp.status_code == 202, resp.text
    since = loop_spy.await_args.kwargs["not_disturbed_since"]
    assert since is not None
    assert since == destructive_ops.generation(_who("DebugAgent"))

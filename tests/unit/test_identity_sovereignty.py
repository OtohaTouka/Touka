"""批⑤-B 身分主權（E24／E27／E69）：伺服器注入的身分永遠勝過呼叫者/模型輸入。

三條共通型態＝「防護正確但無測驅動真正的消費點」（家族③）：
- E24（ADR-0039；ADR-0245 收緊）：無認證請求一律 401、X-Agent-ID header 不變成身分——原 else 分支只有
  pure helper `_build_guest_context` 被單測，無端點測試證明它真的接上且不採信 header。
- E27（ADR-0046）：具 can_supervise_dispatch 者查 DispatchAgent 看板時，抓板身分必須是
  非特權固定 DispatchAgent（agent_role=base, caps=[]），而非呼叫者自身——僅源碼字串測。
- E69（ADR-0119）：tool_executor 的 call_kwargs 把 agent_id/identity 注入在 `**args`
  之後，永遠蓋過模型偽造值——無測餵 args 內 identity 鍵、調 merge 序全綠。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.routers.ui_sessions import _require_authenticated
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.utils.permission_grants import NameGrant


def _clear() -> None:
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# E24（ADR-0039；ADR-0245 收緊）：無認證聊天一律 401，忽略 X-Agent-ID
# --------------------------------------------------------------------------- #


def test_unauthenticated_request_rejected_ignoring_x_agent_id(monkeypatch):
    """無認證請求一律 401，X-Agent-ID header 不會變成身分（C3 防提權；ADR-0245 收緊）。

    原本無認證會降級成 Guest 照樣進 pipeline；現在連 pipeline 都進不去。
    把聊天端點的 Depends 換回 get_optional_identity 或讓 header 成為身分 → 非 401／spy 被呼叫 → 紅。
    """
    captured: dict[str, Any] = {}

    async def _spy(request, ctx, is_anthropic=True):
        captured["ctx"] = ctx
        return {"ok": True}

    monkeypatch.setattr("src.routers.proxy.process_proxy_request", _spy)
    monkeypatch.setattr(
        "src.services.identity_router.is_request_from_trusted_local", lambda _request: False
    )
    app.state.context = {}
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/messages",
            json={"model": "ToukaSoul", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Agent-ID": "Owner"},  # 攻擊者嘗試冒充特權 agent
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"]["code"] == "AUTH_INVALID_TOKEN"
        assert captured == {}  # 沒進 pipeline
    finally:
        _clear()


# --------------------------------------------------------------------------- #
# E27（ADR-0046）：DispatchAgent 看板以非特權固定身分抓取
# --------------------------------------------------------------------------- #


def test_dispatch_board_fetched_with_nonprivileged_identity(monkeypatch):
    """具 can_supervise_dispatch 者查 DispatchAgent 板 → 抓板身分為非特權固定 DispatchAgent。

    把 :788-790 改成 `target_identity = identity`（用呼叫者身分）→ 抓板身分帶
    can_supervise_dispatch 能力／非 DispatchAgent → 紅。
    """
    captured: dict[str, Any] = {}

    class _SpyManager:
        async def get_current_mission(self, target_identity):
            captured["identity"] = target_identity
            # 隱含回 None → 端點走 404，但抓板身分已被捕獲

    monkeypatch.setattr("src.routers.ui_sessions.MissionManager", lambda: _SpyManager())

    supervisor = AgentIdentity.for_role(
        agent_id="Supervisor", role="Base", capabilities=["can_supervise_dispatch"]
    )
    app.dependency_overrides[_require_authenticated] = lambda: supervisor
    try:
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/v1/ui/mission", headers={"X-Touka-Soul-ID": "DispatchAgent"})
        target = captured["identity"]
        assert target.agent_id == "DispatchAgent"  # 固定 DispatchAgent，非呼叫者
        assert target.is_privileged() is False  # 非特權
        assert target.capabilities == []  # 不帶呼叫者的 can_supervise_dispatch
    finally:
        _clear()


# --------------------------------------------------------------------------- #
# E69（ADR-0119）：tool_executor call_kwargs 伺服器鍵在 **args 之後 → 蓋過模型偽造
# --------------------------------------------------------------------------- #


async def test_executor_server_identity_overrides_model_forged_args(monkeypatch):
    """executor 注入的 agent_id/identity 在 `**args` 之後，永遠蓋過模型偽造值。

    模型在 tool args 裡塞 agent_id/identity（冒充）；把 :494 的 merge 序反轉
    （`**args` 移到伺服器鍵之後）→ 工具收到偽造身分 → 紅。
    """
    # 政策閘在身分主權上游、非本測標的——固定 ALLOW（同 test_vault_jail_executor_wiring 慣例）
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="ALLOW", reason="test-allow"),
    )

    captured: dict[str, Any] = {}

    async def _probe(**kwargs):
        captured.update(kwargs)
        return "ok"

    real = AgentIdentity.for_role(agent_id="RealAgent", role="Base", is_human=False)
    grants = permission_service.resolve_grants(real.agent_role)
    monkeypatch.setattr(
        permission_service,
        "resolve_grants",
        lambda _role: replace(grants, tools=NameGrant.from_lists(["probe_tool"])),
    )
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={"probe_tool": _probe},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    queue: asyncio.Queue = asyncio.Queue()
    await executor.execute_to_queue(
        "probe_tool",
        # 模型偽造 agent_id/identity；real_arg 是合法參數
        {"agent_id": "ForgedOwner", "identity": "FORGED", "real_arg": "keep-me"},
        "req-sov",
        {"identity": real, "auth_level": "Manual", "session_id": None},
        queue,
    )

    assert captured["agent_id"] == "RealAgent"  # 伺服器注入勝出，非 ForgedOwner
    assert captured["identity"] is real  # 真身分物件，非 "FORGED" 字串
    assert captured["real_arg"] == "keep-me"  # 合法 args 仍保留

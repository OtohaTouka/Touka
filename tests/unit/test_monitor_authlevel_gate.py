"""E01（早期守衛缺口 backlog）— auth-level 授權閘的位置（ADR-0246 U2c-4 起移到 session 寫入）。

缺口背景（docs/architecture/proposals/early-adr-guard-coverage-gap-backlog.md · E01）：
    ADR-0021 的 Auth-Level-Change Guard 原在 MonitorLayer 3.4 節，對非 admin、非 target
    擁有者的請求參數 SafeAuto/FullYOLO 降級成 Manual。但管線最後一層 ToolLayer 一律以
    session 的值（沒有 session 就是 Manual）覆寫 ``ctx["auth_level"]``，中間沒有任何層讀它
    （D_permissions §1.9-1）——這道閘實際失效，請求參數從來不是有效的模式來源。

U2c-4 把授權移到 session 寫入（``ui_sessions.save_session``），MonitorLayer 不再讀請求參數的
auth_level。本檔改釘這件事：任何身分（含 admin、擁有者）帶 Auto／YOLO 都不會被寫進 ctx，
所以沒有經請求參數提權的路徑；靈魂代打照常發生。真正的閘由
``test_auth_mode_authority_security.py`` 對一般使用者、別人的靈魂、admin、擁有者逐一驗證。

真呼叫 MonitorLayer.process()，只 monkeypatch agent_service 的兩個查詢方法
（identify_agent / resolve_role_and_caps）保持封閉。
"""

from __future__ import annotations

import pytest
from src.core.exceptions import ToukaException
from src.middleware.layers import monitor as monitor_module
from src.middleware.layers.monitor import MonitorLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity

_SOUL = "Sumi"


class _Req:
    """最小請求替身：只帶 model 與 auth_level（無 session_id 屬性）。"""

    def __init__(self, auth_level: str | None, model: str = "unknown-model-x") -> None:
        self.model = model
        self.auth_level = auth_level


@pytest.fixture
def hermetic_agent_service(monkeypatch):
    """封閉 agent_service：identify_agent 只認 Sumi、resolve 回 Owner。"""

    def _identify(agent_id: str):
        if agent_id == _SOUL:
            return {"is_toukasoul": True, "is_custom": False, "agent_id": _SOUL}
        return None

    monkeypatch.setattr(monitor_module.agent_service, "identify_agent", _identify)
    monkeypatch.setattr(
        monitor_module.agent_service,
        "resolve_role_and_caps",
        lambda agent_id: ("Owner", []),
    )


_IDENTITIES = {
    "normal": AgentIdentity.for_role("human-uuid", "normal", is_human=True, owned_agents=[]),
    "admin": AgentIdentity.for_role("human-admin", "admin", is_human=True),
    "owner": AgentIdentity.for_role("human-uuid", "normal", is_human=True, owned_agents=[_SOUL]),
    "anonymous": None,
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("hermetic_agent_service")
@pytest.mark.parametrize("who", list(_IDENTITIES))
@pytest.mark.parametrize("requested", ["YOLO", "Auto", "Manual", "FullYOLO"])
async def test_request_auth_level_never_becomes_the_mode(who: str, requested: str):
    """請求參數不是模式來源：任何身分、任何值，MonitorLayer 之後 ctx 都沒有模式。"""
    ctx = PipelineContext()
    if _IDENTITIES[who] is not None:
        ctx["identity"] = _IDENTITIES[who]
    ctx["headers"] = {"x-touka-soul-id": _SOUL}

    if who == "normal":
        # ADR-0245 修補：不擁有 Sumi 的一般使用者在 MonitorLayer 就被 403 擋下（更嚴）
        with pytest.raises(ToukaException) as exc_info:
            await MonitorLayer().process(_Req(auth_level=requested), ctx)
        assert exc_info.value.status_code == 403
    else:
        await MonitorLayer().process(_Req(auth_level=requested), ctx)

    assert ctx.auth_level is None, (
        f"{who} 帶 {requested} 的請求參數被寫進 ctx——請求參數成了可提權的模式來源（E01）"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("hermetic_agent_service")
async def test_owner_soul_rebind_still_happens_without_mode_gate():
    """拿掉閘之後，擁有者的靈魂代打照常（非空砲：代打路徑真的走到）。"""
    ctx = PipelineContext()
    ctx["identity"] = _IDENTITIES["owner"]
    ctx["headers"] = {"x-touka-soul-id": _SOUL}

    await MonitorLayer().process(_Req(auth_level="YOLO"), ctx)

    assert ctx["identity"].agent_id == _SOUL, "靈魂代打（re-bind）未發生——場景失效"
    assert ctx["identity"].is_human is False
    assert ctx.auth_level is None

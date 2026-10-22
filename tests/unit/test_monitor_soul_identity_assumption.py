"""④ 身分裂腦修復（Option A：前景對話以靈魂身分執行）。

根因：MonitorLayer 解析出 target 靈魂後，只把 ``ctx["agent_id"]`` 字串改成靈魂 id，
``ctx["identity"]`` 仍是人類請求者 → 下游全走人類身分：
- ``permission_service.get_allowed_tools`` 讀 ``identity.agent_role``（人類為 None）回受限集
  → **靈魂在前景對話「看不到工具」的真因**（Sumi 對話實錄的症狀）；
- ``resource_manager.get_vault_path`` 依 ``is_toukasoul(identity)`` 分流，人類 → 記憶/檔案
  落到 ``custom/{human_uuid}/drive``，與心跳的 ``personal_drive/Sumi`` 分家；
- payload logger 記人類 UUID（非靈魂）。

修法：target 為「人類擁有（或 admin）的靈魂」時，把 ``ctx["identity"]`` 換成該靈魂身分
（鏡像心跳 ``identity_service.get_identity`` / X-Agent-ID 代打的語意——agent_role=Owner）。
人類請求者的角色僅作 Gate-1 授權（擁有該靈魂或人類 admin）。session ``owner_user_id`` 不受
影響（僅 INSERT 寫入、前端以人類 token 建立時記錄；server-side 續寫恆為 UPDATE 不改歸屬）。
"""
from __future__ import annotations

import pytest
from src.middleware.layers import monitor as monitor_mod
from src.middleware.layers.monitor import MonitorLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity


@pytest.fixture(autouse=True)
def _stub_resolve(monkeypatch):
    """靈魂 id → (Owner, caps)，免依賴沙箱 soul 目錄（鏡像 resolve_role_and_caps SSOT）。"""
    monkeypatch.setattr(
        monitor_mod.agent_service,
        "resolve_role_and_caps",
        lambda _id: ("Owner", ["heartbeat_awakening"]),
    )


def _ctx(identity: AgentIdentity | None) -> PipelineContext:
    return PipelineContext(
        agent_id=(identity.agent_id if identity else None), identity=identity
    )


def _human(agent_id: str = "human-uuid", role: str = "normal", owned=("Sumi",)) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id, role, is_human=True, owned_agents=list(owned))


def _assume(ctx: PipelineContext, target: str = "Sumi", is_soul: bool = True) -> None:
    MonitorLayer()._maybe_assume_soul_identity(ctx, target, is_soul)


def test_normal_owner_assumes_owned_soul():
    """一般人類擁有者 → identity 換成靈魂（Owner、is_human=False），角色/能力同步。"""
    ctx = _ctx(_human(role="normal", owned=("Sumi",)))
    _assume(ctx)
    soul = ctx["identity"]
    assert soul.agent_id == "Sumi"
    assert soul.agent_role == "Owner"
    assert soul.is_human is False
    assert ctx["role"] == "Owner"           # display_role 同步（供 log/顯示）
    assert ctx["capabilities"] == ["heartbeat_awakening"]


def test_admin_can_assume_even_if_not_in_owned_list():
    """人類 admin 即使 owned_agents 未列該靈魂也可以其身分驅動（admin 權威）。"""
    ctx = _ctx(_human(role="admin", owned=()))
    _assume(ctx)
    assert ctx["identity"].agent_id == "Sumi"
    assert ctx["identity"].agent_role == "Owner"


def test_non_owner_non_admin_human_not_assumed():
    """非擁有者非 admin 的人類 → 不代打（維持人類身分，不越權變成靈魂）。"""
    human = _human(role="normal", owned=())
    ctx = _ctx(human)
    _assume(ctx)
    assert ctx["identity"] is human


def test_custom_or_non_soul_target_not_assumed():
    """target 非靈魂（is_toukasoul=False）→ 不代打。"""
    human = _human(role="admin", owned=("Sumi",))
    ctx = _ctx(human)
    _assume(ctx, target="some-custom-model", is_soul=False)
    assert ctx["identity"] is human


def test_non_human_caller_not_reassumed():
    """caller 已是靈魂/代理身分（心跳、X-Agent-ID 代打）→ 不重複代打。"""
    soul = AgentIdentity.for_role("Sumi", "Owner")
    ctx = _ctx(soul)
    _assume(ctx)
    assert ctx["identity"] is soul


def test_guest_none_identity_not_assumed():
    """無身分（未驗證）→ 不代打、不崩潰。"""
    ctx = _ctx(None)
    _assume(ctx)
    assert ctx["identity"] is None

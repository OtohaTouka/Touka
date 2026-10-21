"""G16（守衛涵蓋缺口待辦）— 模式授權必須看「人類請求者」，不能看代打後的靈魂。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G16）：
    舊的 MonitorLayer 降級閘必須在 `_maybe_assume_soul_identity`（re-bind）之前判定，否則
    會改讀代打後的靈魂身分（Owner、human_role 非 admin、owned_agents 通常空）而靜默降級。

ADR-0246 U2c-4 起，模式授權移到 session 寫入（``ui_sessions._authorized_changes``）：那裡的
身分是路由驗證過的人類請求者，沒有代打這一步，所以「閘在 re-bind 之前」在結構上成立。本檔
以兩條鎖住之：
  1. 源碼鎖：MonitorLayer.process 不再讀請求參數的 auth_level、也不寫 ctx 的模式（不留第二個
     看似有效、其實會被覆寫的閘），re-bind 呼叫仍在。
  2. 非空砲行為證明：同一個靈魂，人類擁有者可以設 YOLO，代打後的靈魂身分不行——證明授權若
     讀錯身分會得到相反結果。
"""

from __future__ import annotations

import inspect

from src.middleware.layers.monitor import MonitorLayer
from src.models.identity import AgentIdentity
from src.utils.auth_mode import authorize_auth_mode

_SOUL = "Sumi"


def test_monitor_no_longer_derives_mode_but_still_rebinds():
    src = inspect.getsource(MonitorLayer.process)
    assert 'getattr(request, "auth_level"' not in src
    assert 'ctx["auth_level"]' not in src
    assert "_maybe_assume_soul_identity" in src


def _authorize(identity: AgentIdentity) -> str:
    return authorize_auth_mode(
        "YOLO",
        is_admin=identity.is_admin_authority(),
        owns_agent=_SOUL in identity.owned_agents,
    )


def test_gate_attributes_differ_between_human_owner_and_assumed_soul():
    """非空砲：人類擁有者 → YOLO 保留；代打後靈魂（owned 空、非 admin）→ 降成 Manual。"""
    human_owner = AgentIdentity.for_role(
        "human-uuid", "normal", is_human=True, owned_agents=[_SOUL]
    )
    assumed_soul = AgentIdentity.for_role(_SOUL, "Owner")  # re-bind 後的身分

    assert _authorize(human_owner) == "YOLO"
    assert _authorize(assumed_soul) == "Manual"

"""P2 — AgentIdentity 角色權限面謂詞（ADR-0141）。

閘遷移的目標形式：`_require_*` 與 inline 閘不再比對扁平 `identity.role in {...}`，改呼叫
`identity.is_privileged()` 等謂詞（讀雙欄位）。本檔把每個謂詞對完整身分空間的布林值
釘死，確保它們**精確等於**拆分前對應的閘家族允許集合——這是「遷移後矩陣零 cell 變動」
的模型層保證。含 admin 代打形態（`assumed_admin`＝human_role="admin"／is_human=False，
P5 Option A 的合法代打）——含 admin 的謂詞放行之＝保管理權（P6-d 純度 validator 不影響它）。
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity

_SHAPES = {
    "human_admin": AgentIdentity.for_role(agent_id="H", role="admin", is_human=True),
    "human_normal": AgentIdentity.for_role(agent_id="H", role="normal", is_human=True),
    "agent_owner": AgentIdentity.for_role(agent_id="A", role="Owner", is_human=False),
    "agent_base": AgentIdentity.for_role(agent_id="A", role="Base", is_human=False),
    "agent_subagent": AgentIdentity.for_role(agent_id="A", role="Subagent", is_human=False),
    "agent_debugger": AgentIdentity.for_role(agent_id="A", role="Debugger", is_human=False),
    "assumed_admin": AgentIdentity.for_role(agent_id="A", role="admin", is_human=False),
    "agent_system": AgentIdentity.for_role(agent_id="A", role="System", is_human=False),
}

# 各謂詞的允許集合＝對應閘家族（與 test_role_plane_characterization.py 家族一致）。
_EXPECTED = {
    "is_admin_authority": {"human_admin", "assumed_admin"},
    "is_privileged": {"human_admin", "assumed_admin", "agent_owner", "agent_debugger"},
    "is_owner_or_admin": {"human_admin", "assumed_admin", "agent_owner"},
    "is_admin_or_debugger": {"human_admin", "assumed_admin", "agent_debugger"},
}

_PARAMS = [
    pytest.param(pred, shape, id=f"{pred}[{shape}]")
    for pred in _EXPECTED
    for shape in sorted(_SHAPES)
]


@pytest.mark.parametrize(("pred_name", "shape_key"), _PARAMS)
def test_predicate_matches_gate_family(pred_name: str, shape_key: str):
    """謂詞布林值精確等於其閘家族允許集合（過渡安全、含 assumed_admin 放行）。"""
    identity = _SHAPES[shape_key]
    result = getattr(identity, pred_name)()
    assert result is (shape_key in _EXPECTED[pred_name])


def test_assumed_admin_still_admitted_by_all_admin_families():
    """admin 代打混淆產物在 P6-d 前仍被所有含 admin 的謂詞放行（行為保持）。"""
    art = _SHAPES["assumed_admin"]
    assert art.is_admin_authority()
    assert art.is_privileged()
    assert art.is_owner_or_admin()
    assert art.is_admin_or_debugger()


def test_normal_human_rejected_by_all_privileged_predicates():
    """normal 人類被所有特權謂詞拒絕（現狀凍結）。"""
    normal = _SHAPES["human_normal"]
    assert not normal.is_admin_authority()
    assert not normal.is_privileged()
    assert not normal.is_owner_or_admin()
    assert not normal.is_admin_or_debugger()


def test_inconsistent_human_with_agent_role_routes_by_value():
    """既有 fixture 慣例（拆分前 ``AgentIdentity(role="Owner", is_human=True)``、現以 ``for_role`` 建構；人類帶 agent 角色）
    ——依值路由存進 agent_role，`.role` 仍讀 "Owner"，特權謂詞放行（扁平模型舊行為保持）。

    P0 矩陣只涵蓋「一致」身分形態、未含此組合；P2 由完整測試套件（scorecard/reset
    fixture）補抓後加此回歸鎖。"""
    ident = AgentIdentity.for_role(agent_id="OwnerFixture", role="Owner", is_human=True)
    assert ident.agent_role == "Owner"
    assert ident.human_role is None
    assert ident.display_role == "Owner"
    assert ident.is_privileged()

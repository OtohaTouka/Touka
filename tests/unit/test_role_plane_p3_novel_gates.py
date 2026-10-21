"""P3 — 兩個「非家族」inline 閘的行為保持等價鎖（ADR-0141）。

P3 遷移的 21 檔中，多數閘屬三大已特徵化家族（三元組／{Owner,admin}／admin-only），
由 test_role_plane_characterization.py 的矩陣直接鎖住 helper。但有兩個 inline 閘**不對應
任何謂詞家族**，需獨立證明「改讀雙欄位 ≡ 拆分前扁平 `.role` 檢查」：

1. ``middleware/layers/monitor.py``：auth_level 自動化模式閘的 admin 判定
   ``identity.role == "admin"`` → ``identity.is_admin_authority()``。
2. ``routers/chat_upload.py``：Guest 拒絕閘 ``identity.role == "Guest"``
   → ``identity.agent_role == "Guest"``。ADR-0245 已把這個閘改寫成「只放行人類、admin
   代打與 Owner／Debugger」，行為改由 ``test_chat_upload_endpoint`` 經真端點的完整形態
   矩陣鎖住，本檔不再保留它的等價鎖。

策略＝**等價鎖**：對完整 8 身分形態，斷言新表達式的布林值**逐一等於**拆分前扁平
``identity.role`` 檢查（``.role`` 為 P1 保留的相容計算屬性，即拆分前語意的權威參照）。
這正是絞殺者的行為保持性質。實際源碼路徑另由既有測試涵蓋（整合套件每請求皆過
MonitorLayer）。
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity

# 完整 8 形態（與 test_role_plane_characterization.SHAPES 對齊；含 admin 代打混淆產物）。
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


@pytest.mark.parametrize("shape_key", sorted(_SHAPES))
def test_monitor_admin_gate_equivalence(shape_key: str):
    """monitor auth_level 閘：``is_admin_authority()`` ≡ 拆分前 ``role == "admin"``。"""
    identity = _SHAPES[shape_key]
    assert identity.is_admin_authority() == (identity.display_role == "admin")


def test_monitor_admin_gate_admits_only_admin_authority():
    """顯式活證據：monitor 自動化閘只認 admin 權威（人類 admin＋admin 代打產物）。"""
    admitted = {k for k, v in _SHAPES.items() if v.is_admin_authority()}
    assert admitted == {"human_admin", "assumed_admin"}

"""P0 特徵化矩陣（路徑／主權面）— 角色權限面分離（ADR-0141）。

補 test_role_plane_characterization.py（扁平閘矩陣）之不足：把 `resource_manager` 的
**乾淨樣板**（正確按 is_human 分支者）對身分空間的行為拍照。

**核心對比（本檔要凍住、也是拆分的目標行為）**：混淆產物 `assumed_admin`
（role="admin"、is_human=False）**通過所有扁平閘**（見閘矩陣），但 `is_owner` 與
`get_vault_root` **拒絕它**——因為這兩者正確地 `is_human and role=="admin"` 配對。
換言之：拆分要做的，就是把全系統的閘都變成 resource_manager 這三個函式現在的樣子。

只選 **DB 無關的決定性列**（在碰 users.json 前就回值者），避免相依測試沙盒的
使用者資料；自主寬限期與 human-pilot 擁有權路徑由既有 IDOR/session 測試覆蓋。
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager

_CALLER = "CallerAgent"


def _shape(role: str, is_human: bool) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=_CALLER, role=role, is_human=is_human)


# ── is_owner 旁路：只取在碰使用者 DB 前就回值的決定性三列 ──────────────────────
# target 固定為「不是 caller」的另一 agent（避開 self-access 短路與 DB 查詢）。
_TARGET = AgentIdentity.for_role(agent_id="OtherTarget", role="Owner", is_human=False)

_IS_OWNER_DETERMINISTIC = [
    # (形態, 預期 is_owner) — 皆在 resource_manager.py:278-281 的旁路內決定，零 DB
    pytest.param(_shape("admin", is_human=True), True, id="human_admin→bypass"),
    pytest.param(_shape("Debugger", is_human=False), True, id="agent_debugger→bypass"),
    # 混淆產物：is_human=False 使 admin 旁路失效、Debugger 不符、Owner/System 不符
    # → 在 :343 return False（未觸 DB）。這正是「乾淨碼抵抗 admin 走私」的活證據。
    pytest.param(_shape("admin", is_human=False), False, id="assumed_admin→DENIED(clean)"),
]


@pytest.mark.parametrize(("caller", "expected"), _IS_OWNER_DETERMINISTIC)
def test_is_owner_bypass_matrix(caller: AgentIdentity, expected: bool):
    """拍照 is_owner 的旁路決定（拆分前）——含 §8-4b 待修的 Debugger 無 is_human 防護。"""
    assert resource_manager.is_owner(_TARGET, caller) is expected


# ── get_vault_root 存取決定：非 None（放行）vs None（拒絕），全 8 形態 ─────────
# 放行集合 = {human_admin, agent_owner, agent_debugger}（is_admin 或 privileged_agent）。
# 關鍵：assumed_admin 被拒（is_human=False ⇒ 非 is_admin；admin ∉ [Owner,Debugger]
# ⇒ 非 privileged_agent）——與扁平閘放行它，形成拆分要消除的落差。
_VAULT_ROOT_ALLOWED = {
    "human_admin": _shape("admin", is_human=True),
    "agent_owner": _shape("Owner", is_human=False),
    "agent_debugger": _shape("Debugger", is_human=False),
}
_VAULT_ROOT_DENIED = {
    "human_normal": _shape("normal", is_human=True),
    "agent_base": _shape("Base", is_human=False),
    "agent_subagent": _shape("Subagent", is_human=False),
    "assumed_admin": _shape("admin", is_human=False),  # 乾淨碼拒絕混淆產物
    "agent_system": _shape("System", is_human=False),
}


@pytest.mark.parametrize("key", sorted(_VAULT_ROOT_ALLOWED))
def test_get_vault_root_allows(key: str):
    """放行形態拿得到非 None 根路徑（在測試沙盒內建目錄）。"""
    assert resource_manager.get_vault_root("personal", _VAULT_ROOT_ALLOWED[key]) is not None


@pytest.mark.parametrize("key", sorted(_VAULT_ROOT_DENIED))
def test_get_vault_root_denies(key: str):
    """拒絕形態回 None（在 mkdir 前於 :235 早退）——含 assumed_admin 被乾淨碼擋下。"""
    assert resource_manager.get_vault_root("personal", _VAULT_ROOT_DENIED[key]) is None


def test_clean_template_resists_the_conflation_artifact():
    """總結性斷言：assumed_admin 被 is_owner 與 get_vault_root 雙雙拒絕。

    這是全提案的北極星——拆分後所有閘都應具備這個「不被 admin 字串走私」的性質。
    （對照閘矩陣中同一 assumed_admin **通過**所有扁平閘。）
    """
    artifact = _shape("admin", is_human=False)
    assert resource_manager.is_owner(_TARGET, artifact) is False
    assert resource_manager.get_vault_root("personal", artifact) is None

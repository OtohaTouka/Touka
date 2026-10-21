"""P0 特徵化矩陣 — 角色權限面分離提案（role-plane-separation，ADR-0141）。

**這是遷移安全網，不是新行為。** 它把「拆分前」每個角色閘對**完整 (role × is_human)
身分空間**的 allow/deny 拍照固定成表格驅動測試。之後所有遷移批（P2–P5）的驗收鐵律：
**這裡一個 cell 都不能變**；P6 的刻意行為修正必須以對本檔的**顯式 diff＋理由**呈現。

**涵蓋邊界（誠實標明，經獨立驗證覆核）**：本檔涵蓋**全部 21 個可匯入的 `_require_*`
角色閘 helper**（三種混合形狀——三元組／{Owner,admin}／admin-only）＋**工具面 `_gate`**
（tools/builtin/creation.py，{Owner,admin}，回拒絕字串而非 raise，守 create_skill/tool/mcp）
＋None 未認證＝可匯入角色閘的完整集合。`resource_manager` 乾淨樣板（is_owner／
get_vault_root）於 test_role_plane_path_characterization.py。**不在本 P0 純矩陣內**者
＝**端點 inline 閘**（terminal `_APPROVAL_ROLES`、debug.py:43/140/186/240、souls.py:95/145、
group_settings:80、mcp:445、ui_sessions `_can_*`、mission_manager:279/358、
resource_manager:500）——嵌在端點/方法內、需 HTTP 或重 fixture，於**各自遷移批次**
（見 role-plane-migration-baseline.md）以該批閘測試 RED→GREEN 涵蓋；其邏輯多與本檔某個
已特徵化 helper 家族**相同**（如 debug inline＝三元組、terminal＝三元組）。

**admin 代打的活證據**（本矩陣凍住）：`assumed_admin`（role="admin"、is_human=False，
即 admin 代打自有 agent 的產物）依值路由成 `human_role="admin"`／`agent_role=None`，
**通過所有含 admin 的閘**（謂詞 `is_admin_authority` 讀 human_role）＝admin 代打保權。
P5 Option A 確立它為**合法的代打形態**（非待消滅的混淆）；`human_normal` 則被所有特權閘拒絕。
P6-d 的跨面純度 validator 使 `agent_role` 不可能為人類角色，但**不影響**此形態
（`human_role="admin"` 合法、admin ∉ AGENT_ROLES）——故本矩陣對 assumed_admin 的預期不變。
"""

from __future__ import annotations

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity

# 匯入全部 21 個角色閘 helper（直呼式，raise ToukaException on deny / 無 raise on allow）
# ＋ADR-0239 安全網：4 個已認證閘 _require_authenticated 與 mcp_oauth.require_admin_human
from src.routers.admin import require_admin as g_admin_require_admin
from src.routers.ask_user import _require_authenticated as g_ask_user_authenticated
from src.routers.autonomy import _require_autonomy_role
from src.routers.bots import _require_bots_role
from src.routers.config import _require_config_role
from src.routers.connections import _require_connections_role
from src.routers.creation import _require_creation_role
from src.routers.custom_models import _require_models_role
from src.routers.debug import _require_debug_role, _require_reset_role
from src.routers.editor import _require_editor_role
from src.routers.feedback import _require_authenticated as g_feedback_authenticated
from src.routers.feedback import _require_owner_role as g_feedback_owner
from src.routers.identity import _require_admin_only, _require_identity_role
from src.routers.mcp import _require_mcp_role
from src.routers.mcp_oauth import require_admin_human
from src.routers.models import _require_models_mgmt
from src.routers.observability import _require_privileged
from src.routers.permissions import _require_admin as g_permissions_admin
from src.routers.skills import _require_owner_role as g_skills_owner
from src.routers.skills import _require_skills_role
from src.routers.system import _require_admin as g_system_admin
from src.routers.system import _require_authenticated as g_system_authenticated
from src.routers.ui_sessions import _require_admin_role
from src.routers.ui_sessions import _require_authenticated as g_ui_sessions_authenticated

# 工具面角色閘（獨立驗證覆核發現）：回拒絕字串而非 raise，故另行斷言
from src.tools.builtin.creation import _gate as g_creation_gate

# ── 9 個標準身分形態（涵蓋完整 role × is_human 可達空間）──────────────────────
_A = "AgentX"


def _shape(role: str, is_human: bool) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=_A, role=role, is_human=is_human)


SHAPES: dict[str, AgentIdentity] = {
    "human_admin": _shape("admin", is_human=True),
    "human_normal": _shape("normal", is_human=True),
    "agent_owner": _shape("Owner", is_human=False),
    "agent_base": _shape("Base", is_human=False),
    "agent_subagent": _shape("Subagent", is_human=False),
    "agent_debugger": _shape("Debugger", is_human=False),
    "assumed_admin": _shape("admin", is_human=False),  # 混淆產物（admin 代打）
    "agent_system": _shape("System", is_human=False),
}
ALL_SHAPE_KEYS = frozenset(SHAPES)

# ── 三種閘家族的「允許集合」（其餘＝拒絕）——由拆分前現行碼推導，run 對帳 ──────
# 三元組 {Owner, Debugger, admin}
_TRIAD_ALLOW = frozenset({"human_admin", "assumed_admin", "agent_owner", "agent_debugger"})
# {Owner, admin}（刻意排除 Debugger）
_OWNER_ADMIN_ALLOW = frozenset({"human_admin", "assumed_admin", "agent_owner"})
# admin-only（role == "admin"，無 is_human 檢查）
_ADMIN_ONLY_ALLOW = frozenset({"human_admin", "assumed_admin"})

# 閘 → 允許集合。key 用 "module:helper" 便於失敗定位。
GATES: dict[str, tuple[object, frozenset[str]]] = {
    # ── 三元組 {Owner, Debugger, admin}（10 個；排程 CRUD 的閘隨 routers/scheduler.py 刪除）──
    "editor:_require_editor_role": (_require_editor_role, _TRIAD_ALLOW),
    "connections:_require_connections_role": (_require_connections_role, _TRIAD_ALLOW),
    "autonomy:_require_autonomy_role": (_require_autonomy_role, _TRIAD_ALLOW),
    "custom_models:_require_models_role": (_require_models_role, _TRIAD_ALLOW),
    "models:_require_models_mgmt": (_require_models_mgmt, _TRIAD_ALLOW),
    "mcp:_require_mcp_role": (_require_mcp_role, _TRIAD_ALLOW),
    "observability:_require_privileged": (_require_privileged, _TRIAD_ALLOW),
    "identity:_require_identity_role": (_require_identity_role, _TRIAD_ALLOW),
    "skills:_require_skills_role": (_require_skills_role, _TRIAD_ALLOW),
    "debug:_require_debug_role": (_require_debug_role, _TRIAD_ALLOW),
    # ── {Owner, admin}（5 個，Debugger 刻意排除）──
    "creation:_require_creation_role": (_require_creation_role, _OWNER_ADMIN_ALLOW),
    "feedback:_require_owner_role": (g_feedback_owner, _OWNER_ADMIN_ALLOW),
    "skills:_require_owner_role": (g_skills_owner, _OWNER_ADMIN_ALLOW),
    "bots:_require_bots_role": (_require_bots_role, _OWNER_ADMIN_ALLOW),
    "debug:_require_reset_role": (_require_reset_role, _OWNER_ADMIN_ALLOW),
    # ── admin-only（6 個，role != "admin"，無 is_human 檢查）──
    "admin:require_admin": (g_admin_require_admin, _ADMIN_ONLY_ALLOW),
    "config:_require_config_role": (_require_config_role, _ADMIN_ONLY_ALLOW),
    "permissions:_require_admin": (g_permissions_admin, _ADMIN_ONLY_ALLOW),
    "system:_require_admin": (g_system_admin, _ADMIN_ONLY_ALLOW),
    "identity:_require_admin_only": (_require_admin_only, _ADMIN_ONLY_ALLOW),
    "ui_sessions:_require_admin_role": (_require_admin_role, _ADMIN_ONLY_ALLOW),
}

_MATRIX_PARAMS = [
    pytest.param(gate_key, shape_key, id=f"{gate_key}[{shape_key}]")
    for gate_key in GATES
    for shape_key in sorted(ALL_SHAPE_KEYS)
]

# 拒絕時的精確錯誤碼（ADR-0239 純重構安全網）：三元組與 {Owner,admin} 回
# AUTH_PERMISSION_DENIED；admin-only 回 AUTH_ADMIN_REQUIRED。未認證（None）同一代碼。
_PD = ErrorCode.AUTH_PERMISSION_DENIED
_ADM = ErrorCode.AUTH_ADMIN_REQUIRED
_DENY_CODE_BY_ALLOW: dict[frozenset[str], ErrorCode] = {
    _TRIAD_ALLOW: _PD,
    _OWNER_ADMIN_ALLOW: _PD,
    _ADMIN_ONLY_ALLOW: _ADM,
}
GATE_DENY_CODE: dict[str, ErrorCode] = {
    gate_key: _DENY_CODE_BY_ALLOW[allow_set] for gate_key, (_, allow_set) in GATES.items()
}


@pytest.mark.parametrize(("gate_key", "shape_key"), _MATRIX_PARAMS)
def test_gate_matrix(gate_key: str, shape_key: str):
    """拍照：每個閘 × 每個身分形態的 allow/deny（拆分前現行行為）＋拒絕錯誤碼。"""
    gate, allow_set = GATES[gate_key]
    identity = SHAPES[shape_key]
    if shape_key in allow_set:
        gate(identity)  # allow：不得 raise
    else:
        with pytest.raises(ToukaException) as exc_info:
            gate(identity)
        assert exc_info.value.code is GATE_DENY_CODE[gate_key]


@pytest.mark.parametrize("gate_key", list(GATES))
def test_gate_denies_none_identity(gate_key: str):
    """未認證（identity=None）：所有閘一律拒絕，錯誤碼與該閘家族一致。"""
    gate, _ = GATES[gate_key]
    with pytest.raises(ToukaException) as exc_info:
        gate(None)
    assert exc_info.value.code is GATE_DENY_CODE[gate_key]


def test_gate_deny_codes_are_pinned_per_family():
    """錯誤碼分佈拍照：15 個 PD（三元組 10＋{Owner,admin} 5）、6 個 ADM（admin-only）。"""
    codes = list(GATE_DENY_CODE.values())
    assert codes.count(_PD) == 15
    assert codes.count(_ADM) == 6


# ── 已認證閘 _require_authenticated（4 個）：任何非 None 身分放行並回傳原物件 ──────
AUTHENTICATED_GATES: dict[str, object] = {
    "ask_user:_require_authenticated": g_ask_user_authenticated,
    "feedback:_require_authenticated": g_feedback_authenticated,
    "system:_require_authenticated": g_system_authenticated,
    "ui_sessions:_require_authenticated": g_ui_sessions_authenticated,
}

_AUTHENTICATED_PARAMS = [
    pytest.param(gate_key, shape_key, id=f"{gate_key}[{shape_key}]")
    for gate_key in AUTHENTICATED_GATES
    for shape_key in sorted(ALL_SHAPE_KEYS)
]


@pytest.mark.parametrize(("gate_key", "shape_key"), _AUTHENTICATED_PARAMS)
def test_authenticated_gate_allows_every_shape(gate_key: str, shape_key: str):
    """已認證閘不看角色：所有身分形態放行，且回傳同一個 identity 物件。"""
    identity = SHAPES[shape_key]
    assert AUTHENTICATED_GATES[gate_key](identity) is identity


@pytest.mark.parametrize("gate_key", list(AUTHENTICATED_GATES))
def test_authenticated_gate_denies_none_with_permission_denied(gate_key: str):
    """未認證（None）：已認證閘回 AUTH_PERMISSION_DENIED。"""
    with pytest.raises(ToukaException) as exc_info:
        AUTHENTICATED_GATES[gate_key](None)
    assert exc_info.value.code is _PD


# ── mcp_oauth.require_admin_human：人類＋admin 權威，拒絕一律 AUTH_ADMIN_REQUIRED ────
# 刻意不斷言 None 行為（現行碼無 None 防護；ADR-0239 原樣搬移、不修正）。
_ADMIN_HUMAN_ALLOW = frozenset({"human_admin"})


@pytest.mark.parametrize("shape_key", sorted(ALL_SHAPE_KEYS))
def test_require_admin_human_matrix(shape_key: str):
    """拍照：只有人類 admin 放行（回傳原物件）；其餘含 admin 代打一律 ADM。"""
    identity = SHAPES[shape_key]
    if shape_key in _ADMIN_HUMAN_ALLOW:
        assert require_admin_human(identity) is identity
    else:
        with pytest.raises(ToukaException) as exc_info:
            require_admin_human(identity)
        assert exc_info.value.code is _ADM


def test_require_admin_human_rejects_assumed_admin():
    """admin 代打產物（is_human=False）雖有 admin 權威，仍被人類限定閘以 ADM 拒絕。"""
    with pytest.raises(ToukaException) as exc_info:
        require_admin_human(SHAPES["assumed_admin"])
    assert exc_info.value.code is _ADM


# ── 工具面 _gate：契約不同（回拒絕字串 None，非 raise），故獨立特徵化 ──────────
# 行為＝{Owner,admin} 家族（allow: human_admin/assumed_admin/agent_owner），純 role
# membership、無 is_human 檢查。守 create_skill/create_tool/create_mcp 內建工具。


@pytest.mark.parametrize("shape_key", sorted(ALL_SHAPE_KEYS))
def test_tool_layer_creation_gate(shape_key: str):
    """拍照工具面 _gate（allow → None；deny → 非空拒絕字串）。"""
    result = g_creation_gate(SHAPES[shape_key])
    if shape_key in _OWNER_ADMIN_ALLOW:
        assert result is None
    else:
        assert isinstance(result, str) and result


def test_tool_layer_creation_gate_denies_none_identity():
    """未認證（identity=None）：_gate 回拒絕字串。"""
    assert isinstance(g_creation_gate(None), str)


# ── 針對混淆熱點的顯式斷言（人類審核可直接讀懂的活證據）──────────────────────


def test_assumed_admin_conflation_is_captured():
    """admin 代打產物（role=admin/is_human=False）通過所有三元組與 admin-only 閘。

    這正是需求要根治的混淆：閘無視 is_human，把人類 admin 值當通行證。
    P5 的驗收＝admin 代打**仍**通過管理閘（保權）；P6-d 落地後此形態不可建構。
    """
    art = SHAPES["assumed_admin"]
    _require_editor_role(art)      # 三元組：通過
    _require_creation_role(art)    # {Owner,admin}：通過
    g_admin_require_admin(art)     # admin-only：通過（role=="admin"，不看 is_human）


def test_normal_human_is_silently_outside_every_privileged_gate():
    """normal 人類被所有特權閘拒絕（role="normal" 不在任何集合）——現狀凍結。

    P6 之後 normal 仍過不了特權閘（本提案不擴充 normal 權限）；此測試保證這點不被
    遷移意外改動。
    """
    normal = SHAPES["human_normal"]
    for gate_key, (gate, _allow) in GATES.items():
        with pytest.raises(ToukaException):
            gate(normal)
        assert "human_normal" not in GATES[gate_key][1]

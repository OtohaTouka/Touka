"""權限授權值物件單測（permission-representation-convergence P1）。

釘死四件錯了會靜默的事：

1. **``"*"`` 的兩種詞性被拆開**——`open_ended`／`deny_all` 是布林，`explicit`／`denied`
   只裝真實名稱。任何一邊殘留 ``"*"`` 字串都是解析失敗。
2. **拒絕恆優先**，``open_ended`` 也擋不住。
3. **繼承時拒絕面不傳播**（`extend`）——這是對現況 `merge_with` 的刻意語意修正，
   也是 `from_lists` 把 deny 面 ``"*"`` 解讀為「全關」的安全前提。
4. **全域地板只能收緊、不能放寬**（`with_floor`）——能放寬的地板等同第二套 RBAC。

本檔對應的生產模組 P1 階段**刻意不接線**；接線後的行為以
`test_permission_matrix_baseline.py` 的矩陣把關。
"""

from __future__ import annotations

import pytest
from src.utils.permission_grants import McpGrant, NameGrant

# --- 解析：`list[str]` → 兩個布林 ＋ 兩個純名稱集合 ---


def test_wildcard_becomes_open_ended_not_a_name():
    """allow 面的 ``"*"`` 升成布林，且**不得**殘留在 `explicit` 裡。"""
    g = NameGrant.from_lists(["*"])
    assert g.open_ended is True
    assert g.explicit == frozenset()


def test_wildcard_and_names_coexist():
    """``["*", "read"]``：開放式授權 ＋ 一個冗餘的明示名稱，兩者互不干擾。"""
    g = NameGrant.from_lists(["*", "read"])
    assert (g.open_ended, g.explicit) == (True, frozenset({"read"}))


def test_empty_list_grants_nothing():
    """``[]`` ＝什麼都沒授予（預設監獄），不是「未設定」。"""
    g = NameGrant.from_lists([])
    assert (g.open_ended, g.explicit) == (False, frozenset())
    assert g.allows("read") is False


def test_none_is_treated_as_empty():
    """`RoleConfig` 的欄位預設為 ``[]``，但呼叫端可能傳 ``None``——不得炸。"""
    assert NameGrant.from_lists(None, None) == NameGrant()


def test_deny_wildcard_becomes_deny_all():
    """⚠ **行為變更**：deny 面的 ``"*"`` 解讀為「全關」。

    現況 `permission_service` 把它當字面工具名（永不匹配），使 `Base` 的
    ``denied_tools: ["*"]`` 成為無聲 no-op。此變更只有在拒絕不繼承時才安全
    （見 `test_deny_all_does_not_propagate_to_child`）。
    """
    g = NameGrant.from_lists([], ["*"])
    assert g.deny_all is True
    assert g.denied == frozenset()


# --- 判別：四個分支 ---


def test_open_ended_allows_unknown_future_names():
    """開放式授權的**存在理由**：涵蓋尚未存在的東西（自我演化建的技能／動態 MCP 工具）。"""
    assert NameGrant.from_lists(["*"]).allows("a_tool_invented_tomorrow") is True


def test_explicit_is_an_allowlist():
    g = NameGrant.from_lists(["read", "write"])
    assert g.allows("read") is True
    assert g.allows("delete") is False


def test_denied_beats_open_ended():
    """拒絕恆優先——``open_ended`` 擋不住 `denied`。"""
    g = NameGrant.from_lists(["*"], ["edit_soul"])
    assert g.allows("edit_soul") is False
    assert g.allows("read") is True


def test_deny_all_beats_everything():
    g = NameGrant.from_lists(["*", "read"], ["*"])
    assert g.allows("read") is False
    assert g.allows("anything") is False


def test_no_prefix_glob_support():
    """刻意不支援前綴 glob——上游新增工具不得靜默落進既有樣式。"""
    assert NameGrant.from_lists(["create_*"]).allows("create_skill") is False


# --- 繼承：授予累加、拒絕不傳播 ---


def test_extend_accumulates_grants():
    child = NameGrant.from_lists(["write"])
    parent = NameGrant.from_lists(["read"])
    assert child.extend(parent).explicit == frozenset({"read", "write"})


def test_extend_propagates_open_ended_upward():
    """父角色的開放式授權會被子角色繼承（能力鏈語意）。"""
    assert NameGrant.from_lists([]).extend(NameGrant.from_lists(["*"])).open_ended is True


def test_denied_does_not_propagate_to_child():
    """**核心語意修正**：子角色不繼承父角色的拒絕。

    對應現實：`Owner` inherits `Guest`，而 Guest 拒絕 ``edit_soul``。現況
    `merge_with` 取聯集，使 Owner 永久帶著該拒絕——但 ``edit_soul`` 是 ADR-0119/0120
    刻意建立給 agent 用的原生工具。今天沒出事，只因為唯一會扣除它的閘門零呼叫端。
    """
    owner = NameGrant.from_lists(["*"])
    guest = NameGrant.from_lists(["read"], ["edit_soul"])
    assert owner.extend(guest).allows("edit_soul") is True


def test_deny_all_does_not_propagate_to_child():
    """`Base` 的 ``denied_tools: ["*"]`` 不得殃及 `Guest`／`Owner`。

    這條是 `from_lists` 把 deny 面 ``"*"`` 解讀為「全關」的**安全前提**：若拒絕仍沿
    繼承鏈聯集，那個解讀會把全系統關成空。兩項變更必須同批落地。
    """
    guest = NameGrant.from_lists(["read"])
    base = NameGrant.from_lists([], ["*"])
    assert guest.extend(base).allows("read") is True


def test_extend_keeps_own_denials():
    """不傳播的是**父角色的**拒絕；自己的拒絕當然還在。"""
    child = NameGrant.from_lists(["*"], ["edit_soul"])
    assert child.extend(NameGrant.from_lists(["read"])).allows("edit_soul") is False


# --- 全域地板：只能收緊 ---


def test_floor_denies_even_open_ended():
    """地板對所有角色生效，**含 Owner**（開放式授權也擋得住）。"""
    owner = NameGrant.from_lists(["*"])
    floor = NameGrant.from_lists([], ["execute_command"])
    assert owner.with_floor(floor).allows("execute_command") is False
    assert owner.with_floor(floor).allows("read") is True


def test_floor_can_never_widen():
    """**刻意的不對稱**：地板的授予面完全被忽略。

    一個能放寬的「地板」等同第二套 RBAC，而權限系統只該有一個授予來源。
    """
    narrow = NameGrant.from_lists(["read"])
    generous_floor = NameGrant.from_lists(["*", "execute_command"])
    result = narrow.with_floor(generous_floor)
    assert result.allows("execute_command") is False
    assert result.open_ended is False


def test_floor_deny_all_shuts_everything():
    assert NameGrant.from_lists(["*"]).with_floor(NameGrant.from_lists([], ["*"])).allows(
        "read"
    ) is False


# --- MCP 軸：委派 ADR-0195 的比對 SSOT ---


@pytest.mark.parametrize(
    ("patterns", "server", "tool", "expected"),
    [
        (["*"], "stripe", "create_refund", True),
        (["stripe"], "stripe", "create_refund", True),
        (["stripe/*"], "stripe", "create_refund", True),
        (["stripe/create_refund"], "stripe", "create_refund", True),
        (["stripe/create_refund"], "stripe", "list_customers", False),
        (["stripe"], "github", "list_issues", False),
    ],
)
def test_mcp_grant_matching(patterns, server, tool, expected):
    assert McpGrant.from_lists(patterns).allows(server, tool) is expected


def test_mcp_denied_ref_beats_open_ended():
    g = McpGrant.from_lists(["*"], ["stripe/create_refund"])
    assert g.allows("stripe", "create_refund") is False
    assert g.allows("stripe", "list_customers") is True


def test_mcp_server_mount_allowed_by_a_single_tool_grant():
    """只被授予個別工具時仍須能掛載，否則連線建不起來、工具面授權形同虛設（ADR-0195）。"""
    g = McpGrant.from_lists(["stripe/create_refund"])
    assert g.allows_server("stripe") is True
    assert g.allows_server("github") is False


def test_mcp_server_mount_ignores_denials():
    """**掛載面刻意不讀拒絕**——與現況 `is_mcp_server_allowed` 逐位元一致。

    拒個別工具卻連帶擋掉掛載，會讓同台的其他授權工具一起失效；真正的邊界在工具面。
    """
    g = McpGrant.from_lists(["stripe"], ["stripe/*"])
    assert g.allows_server("stripe") is True
    assert g.allows("stripe", "create_refund") is False


# --- 值物件性質 ---


def test_grants_are_frozen_and_comparable():
    """不可變＋值相等：解析結果可安全快取、可直接拿來做斷言。"""
    g = NameGrant.from_lists(["read"])
    assert g == NameGrant.from_lists(["read"])
    with pytest.raises(AttributeError):
        g.open_ended = True


@pytest.mark.parametrize("op", ["extend", "with_floor"])
def test_combinators_never_mutate_their_inputs(op):
    """組合子回傳新物件；就地改寫會讓「用錯變數」在別名下僥倖正確、日後無聲斷裂
    （`carry_over_policy` 踩過同一個坑，見 ADR-0195）。"""
    a = NameGrant.from_lists(["read"], ["x"])
    b = NameGrant.from_lists(["write"], ["y"])
    before_a, before_b = a, b
    result = getattr(a, op)(b)
    assert result is not a
    assert (a, b) == (before_a, before_b)


def test_extend_preserves_subclass_type():
    """組合子必須回傳**子類**——回基底會讓 `allows` 的 arity 對不上而在執行期爆。"""
    assert isinstance(McpGrant.from_lists(["a"]).extend(McpGrant.from_lists(["b"])), McpGrant)

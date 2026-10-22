"""P0 基線凍結 — 權限表示法收斂提案（permission-representation-convergence）。

**這是遷移安全網。** 它把每個角色對**完整權限空間**（40 個真實工具 × 5 個角色 ×
兩份設定 × 兩個閘門，＋技能軸與 MCP 兩軸）的實際答案拍照固定。P1–P6 的驗收鐵律：
刻意變更必須以對本檔的顯式 diff＋理由呈現。P2 已翻轉生產 Gate 2 四個 cell；P4 再把
fallback 的 Debugger 三軸與 Base MCP 改為 fail-closed。

**為什麼要凍結「兩份」設定**（提案 §2.4 的實測發現）：
P0 發現測試沙盒當時只播種 `auth_policies.json`，整個既有套件都誤跑
`DEFAULT_PERMISSIONS_CONFIG` fallback。P4 已讓正常 sandbox 播種生產 permissions；
本檔仍以顯式路徑同時釘住生產與 fallback，避免任一邊無聲漂移。

**本檔刻意不使用 `permission_service` 單例的預設路徑**——那會落在沙盒（＝fallback），
使「生產設定」那一半變成幻覺。兩份設定各自建服務，路徑顯式指定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.models.identity import AgentIdentity
from src.services.permission_service import DEFAULT_PERMISSIONS_MODEL, PermissionService
from src.tools.builtin import tool_registry
from src.utils.permission_grants import NameGrant

_REPO = Path(__file__).resolve().parents[2]
_PROD_PERMISSIONS = _REPO / "ToukaProxy" / "config" / "permissions.json"

# 真實工具名取自**執行期 registry 本體**，不在此重刻清單，也不走 Guard #32 的 AST 抽取
# ——後者是為了「零 import 副作用」而做的近似，測試沒有那個限制，直接讀本體更忠實。
ALL_TOOLS = frozenset(tool_registry)

# Subagent 的 13 項白名單（ADR-0245：逐項照抄改名前的 Guest）：**刻意硬編**——它是一份
# 人工維護的清單，任何增減都是權限變更，必須讓本檔紅。（相對地 `ALL_TOOLS` 由 registry
# 推導：新增工具自動落進 Owner 的開放式授權是既定語意，不該讓基線假紅。）
SUBAGENT_TOOLS = frozenset({
    "delete", "edit", "find", "grep", "ide_open_diff", "insert", "list",
    "project_tools", "read", "research", "undo", "vault_tools", "write",
})

ROLES = ["Owner", "Debugger", "Subagent", "Base", None]
PROD, FALLBACK = "prod", "fallback"
CONFIGS = [PROD, FALLBACK]

SKILL_PROBES = ["deep_research", "brand_new_skill"]
MCP_PROBES = [("stripe", "create_refund"), ("github", "list_issues")]


# --- 預期表（2026-07-26 對生產程式碼實測所得，非設計推導）---

# Gate 1＝廣告面：`is_tool_allowed(t, get_allowed_tools(identity))`
GATE1: dict[tuple[str, str | None], frozenset[str]] = {
    (PROD, "Owner"): ALL_TOOLS,
    (PROD, "Debugger"): ALL_TOOLS,
    (PROD, "Subagent"): SUBAGENT_TOOLS,
    (PROD, "Base"): frozenset(),
    (PROD, None): frozenset(),
    (FALLBACK, "Owner"): ALL_TOOLS,
    # P4：fallback 只保留 Owner 開放式授權，其餘角色一律封閉。
    (FALLBACK, "Debugger"): frozenset(),
    (FALLBACK, "Subagent"): frozenset(),
    (FALLBACK, "Base"): frozenset(),
    (FALLBACK, None): frozenset(),
}

# Gate 2＝`is_authorized`。P2 收斂後與 Gate 1 共用同一個 Grant，所以兩張表必須一致。
SOUL_TOOLS = frozenset({"edit_soul", "update_my_soul"})
GATE2: dict[tuple[str, str | None], frozenset[str]] = {
    # P2 刻意翻轉的四個 cell：Owner／Debugger 不再繼承 Guest 的 deny，與 Gate 1 對齊。
    (PROD, "Owner"): ALL_TOOLS,
    (PROD, "Debugger"): ALL_TOOLS,
    (PROD, "Subagent"): SUBAGENT_TOOLS,
    (PROD, "Base"): frozenset(),
    (PROD, None): frozenset(),
    # fallback 的 denied_tools 全空 → 兩閘門一致（也就是說 fallback 連這個矛盾都
    # 「碰巧」沒有，因此既有測試套件永遠看不到 M2）。
    (FALLBACK, "Owner"): ALL_TOOLS,
    (FALLBACK, "Debugger"): frozenset(),
    (FALLBACK, "Subagent"): frozenset(),
    (FALLBACK, "Base"): frozenset(),
    (FALLBACK, None): frozenset(),
}

# P2 合流後兩閘門不得再有任何分歧。
EXPECTED_DIVERGENCE: dict[tuple[str, str | None], frozenset[str]] = {
    (PROD, "Owner"): frozenset(),
    (PROD, "Debugger"): frozenset(),
    (PROD, "Subagent"): frozenset(),
    (PROD, "Base"): frozenset(),
    (PROD, None): frozenset(),
    (FALLBACK, "Owner"): frozenset(),
    (FALLBACK, "Debugger"): frozenset(),
    (FALLBACK, "Subagent"): frozenset(),
    (FALLBACK, "Base"): frozenset(),
    (FALLBACK, None): frozenset(),
}

SKILLS: dict[tuple[str, str | None], bool] = {
    (PROD, "Owner"): True, (PROD, "Debugger"): True, (PROD, "Subagent"): False,
    (PROD, "Base"): False, (PROD, None): False,
    (FALLBACK, "Owner"): True, (FALLBACK, "Debugger"): False, (FALLBACK, "Subagent"): False,
    (FALLBACK, "Base"): False, (FALLBACK, None): False,
}

# MCP 兩軸（掛不掛這台／這台的這個工具）。P4 後 fallback 只讓 Owner 開放。
MCP: dict[tuple[str, str | None], bool] = {
    (PROD, "Owner"): True, (PROD, "Debugger"): True, (PROD, "Subagent"): False,
    (PROD, "Base"): False, (PROD, None): False,
    (FALLBACK, "Owner"): True, (FALLBACK, "Debugger"): False, (FALLBACK, "Subagent"): False,
    (FALLBACK, "Base"): False, (FALLBACK, None): False,
}


@pytest.fixture
def service(tmp_path):
    """建服務工廠：`prod` 指向生產設定的副本、`fallback` 指向不存在的路徑。

    `fallback` 刻意用「檔案不存在」而非「餵壞 JSON」——那正是 `_load_config:206-208`
    真實落到 `DEFAULT_PERMISSIONS_CONFIG` 的其中一條路徑，忠實重現。
    """

    def _build(which: str) -> PermissionService:
        PermissionService.reset_singleton()
        if which == PROD:
            path = tmp_path / "permissions.json"
            path.write_text(_PROD_PERMISSIONS.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path = tmp_path / "does_not_exist" / "permissions.json"
        svc = PermissionService(config_path=str(path))
        svc.disable_auto_reload()
        return svc

    return _build


def _identity(role: str | None) -> AgentIdentity:
    return AgentIdentity.for_role("baseline_probe", role)


def _cases():
    return [(c, r) for c in CONFIGS for r in ROLES]


def _ids(case):
    return f"{case[0]}-{case[1] or 'None'}"


# --- 工具軸：兩個閘門各自的完整答案 ---


@pytest.mark.parametrize("case", _cases(), ids=_ids)
def test_gate1_advertising_matrix(service, case):
    """Gate 1（廣告面）對全部 40 個真實工具的答案。"""
    which, role = case
    svc, ident = service(which), _identity(role)
    advertised = svc.get_allowed_tools(ident)
    assert isinstance(advertised, NameGrant)
    actual = frozenset(t for t in ALL_TOOLS if svc.is_tool_allowed(t, advertised))
    assert actual == GATE1[case], f"Gate1 漂移：{sorted(actual ^ GATE1[case])}"


@pytest.mark.parametrize("case", _cases(), ids=_ids)
def test_gate2_authorization_matrix(service, case):
    """Gate 2（`is_authorized`）對全部 40 個真實工具的答案。"""
    which, role = case
    svc, ident = service(which), _identity(role)
    actual = frozenset(t for t in ALL_TOOLS if svc.is_authorized(ident, t, {}, {}))
    assert actual == GATE2[case], f"Gate2 漂移：{sorted(actual ^ GATE2[case])}"


@pytest.mark.parametrize("case", _cases(), ids=_ids)
def test_gate_divergence_is_exactly_where_we_think_it_is(service, case):
    """**M2 的專屬凍結**：兩閘門在哪些 cell 給出相反答案。

    期望值**獨立硬編**、刻意不寫成 `GATE1[case] ^ GATE2[case]`——後者可由上面兩張表
    推導，於是「兩張表被一致地改掉」時本條會跟著一起變綠，看不出發生過什麼。獨立硬編
    使 P2 合流（此集合必須變空）成為一個必須動手的顯式事件。
    """
    which, role = case
    svc, ident = service(which), _identity(role)
    advertised = svc.get_allowed_tools(ident)
    g1 = {t for t in ALL_TOOLS if svc.is_tool_allowed(t, advertised)}
    g2 = {t for t in ALL_TOOLS if svc.is_authorized(ident, t, {}, {})}
    assert frozenset(g1 ^ g2) == EXPECTED_DIVERGENCE[case]


# --- 技能軸與 MCP 兩軸 ---


@pytest.mark.parametrize("skill_id", SKILL_PROBES)
@pytest.mark.parametrize("case", _cases(), ids=_ids)
def test_skill_matrix(service, case, skill_id):
    """`can_use_skill`。兩個探針涵蓋「已存在技能」與「尚未存在的新技能」——後者是
    開放式授權不可被列舉取代的直接證據（自我演化建的技能必須立即可用）。"""
    which, role = case
    assert service(which).can_use_skill(_identity(role), skill_id) is SKILLS[case]


@pytest.mark.parametrize(("server", "tool"), MCP_PROBES)
@pytest.mark.parametrize("case", _cases(), ids=_ids)
def test_mcp_matrix(service, case, server, tool):
    """MCP 兩軸：掛不掛這台（`is_mcp_server_allowed`）／這台的這個工具（`is_mcp_tool_allowed`）。"""
    which, role = case
    svc, ident = service(which), _identity(role)
    assert svc.is_mcp_server_allowed(server, ident) is MCP[case]
    assert svc.is_mcp_tool_allowed(server, tool, ident) is MCP[case]


# --- 基線的前提事實（若這些變了，上面整張表的意義就變了）---


def test_registry_tool_count_is_frozen():
    """40 個工具。新增工具會讓這條紅——那是提醒你覆核新工具該落在哪些角色，不是誤報。"""
    assert len(ALL_TOOLS) == 40
    assert SOUL_TOOLS <= ALL_TOOLS
    assert SUBAGENT_TOOLS <= ALL_TOOLS


def test_production_config_equals_example():
    """`permissions.json` 與 `.example` 整份相同——本檔只凍結前者，靠這條涵蓋後者。

    live 權限設定檔已改成新版面（ADR-0245），不再容許任何差異。
    """
    live = json.loads(_PROD_PERMISSIONS.read_text(encoding="utf-8"))
    example = json.loads(
        _PROD_PERMISSIONS.with_suffix(".json.example").read_text(encoding="utf-8")
    )
    assert live == example


def test_fallback_contains_no_dead_tool_names():
    """P4：fallback 的工具引用必須全部對應 registry；死字串集合恆為空。"""
    fallback_named = {
        name
        for role in DEFAULT_PERMISSIONS_MODEL.roles.values()
        for name in role.allowed_tools + role.denied_tools
        if name != "*"
    }
    assert fallback_named - ALL_TOOLS == frozenset()


def test_fallback_mcp_is_closed_for_every_non_owner_role(service):
    """P4 回歸防線：fallback 不得讓任何非 Owner 角色掛載 MCP。

    刻意對**兩個真實服務**取答案而非讀上面的 `MCP` 表——讀表會是同義反覆，
    對 fallback 漂移零判別力。Debugger 的生產權限仍為開放，證明本斷言不是
    「所有設定都碰巧封閉」。
    """
    assert service(PROD).is_mcp_server_allowed("stripe", _identity("Debugger")) is True
    for role in ("Debugger", "Subagent", "Base", None):
        assert service(FALLBACK).is_mcp_server_allowed("stripe", _identity(role)) is False

"""G04（守衛涵蓋缺口待辦）— Guard #41 第三支：禁跨面混合角色字面量集合。

缺口背景（見 docs/architecture/proposals/guard-coverage-gap-backlog.md · G04）：
    ADR-0141 拍板點 6 終態守衛第二支：「禁跨面混合角色字面量集合出現在 routers/services」
    （如 `display_role in {"Owner","admin"}`）——此跨面混合閘正是釀成 soul IDOR 的原始
    bug 類。P1–P6 regex 棘輪版曾以 `_*ROLES` frozenset 近似看守，P7 升 AST 版時漏建此支。

    此不變式**先前零執法**：guard_role_plane P7 AST 版只實作 `.role` 存取與
    `AgentIdentity(role=)` 兩條，無角色字面量集合偵測。本檔驗新增的第三支 AST 規則。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from guard_role_plane import _RolePlaneVisitor, _scan  # noqa: E402


def _violations(snippet: str) -> list[str]:
    visitor = _RolePlaneVisitor("snippet.py")
    visitor.visit(ast.parse(snippet))
    return visitor.violations


def _mixed_only(snippet: str) -> list[str]:
    """只取「混合角色字面量集合」那一類違規（排除 .role／role= 兩支）。"""
    return [v for v in _violations(snippet) if "mixed cross-plane role literal set" in v]


def test_flags_mixed_set_literal():
    """`display_role in {"Owner","admin"}`＝Owner(agent)＋admin(human) 混合 → 違規。"""
    assert _mixed_only('x = display_role in {"Owner", "admin"}')


def test_flags_mixed_list_and_tuple():
    """list/tuple 形態同樣攔截（元素順序不拘）。"""
    assert _mixed_only('allowed = ["admin", "Debugger"]')
    assert _mixed_only('gate = role in ("Subagent", "normal")')


def test_pure_agent_set_not_flagged():
    """純 agent 面集合（無 human 成員）不誤報——這是合法的 agent-only 閘。"""
    assert not _mixed_only('x = role in {"Owner", "Base", "Subagent"}')


def test_pure_human_set_not_flagged():
    """純 human 面集合不誤報。"""
    assert not _mixed_only('x = human_role in {"admin", "normal"}')


def test_non_role_collection_not_flagged():
    """不含角色成員的一般集合不誤報。"""
    assert not _mixed_only('x = status in {"active", "suspended"}')


def test_current_src_has_no_mixed_role_literal():
    """現行 src（扣豁免的 identity.py）無任何跨面混合角色字面量集合。"""
    mixed = [v for v in _scan() if "mixed cross-plane role literal set" in v]
    assert mixed == [], f"src 出現跨面混合角色字面量集合：{mixed}"

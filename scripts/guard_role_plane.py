# ruff: noqa: T201
"""Role Plane Guard — 第 41 道（ADR-0141，角色權限面分離 P7 永久禁令版）。

拆分收官後的**永久 AST 禁令**：AgentIdentity 的扁平 ``role`` 相容層（``.role`` 計算屬性、
``role=`` 建構相容映射）已於 P7 移除。本守衛以 AST 靜態禁止其在 src 端復活——

FAIL 條件（掃 ToukaProxy/src ＋ ToukaMemory/src，扣 identity.py 本體）：
1. **identity-ish 接收者的 ``.role`` 屬性存取**（``identity.role``／``caller.role``／
   ``verified.role`` 等讀取或比較）——扁平角色讀法一律改 ``display_role`` 或謂詞。
2. **``AgentIdentity(role=…)`` 建構**（Name callee ＋ ``role`` 關鍵字）——改走
   ``AgentIdentity.for_role(agent_id, role)`` 依值路由，或顯式 ``human_role=``/``agent_role=``。
3. **跨面混合角色字面量集合**（G04／ADR-0141 拍板點 6 第二支）：同一 set/list/tuple
   字面量同時含 HUMAN_ROLES 與 AGENT_ROLES 成員（如 ``display_role in {"Owner","admin"}``）
   ——此跨面混合閘正是釀成 soul IDOR 的原始 bug 類；人類面與 agent 面須分開把關。
   （P1–P6 regex 棘輪版曾以 ``_*ROLES`` frozenset 近似看守，P7 升 AST 版時漏建此支。）

刻意不匹配 User 面（``user.role``／``current_user.role``）、chat 訊息（``msg.role``）、
identity_map 設定模型（``AgentOverride``/``IdentityRule`` 的 ``rule.role``／下標存取）；
檢查 3 只擋「字面量」集合（``HUMAN_ROLES | {"Owner"}`` 之類具名集合運算不在範圍）。
純 AST／零 import src／ASCII 輸出（Windows cp950 相容）。取代 P1–P6 的 regex 棘輪版。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SCAN_DIRS = [
    PROJECT_ROOT / "ToukaProxy" / "src",
    PROJECT_ROOT / "ToukaMemory" / "src",
]

# 永久豁免：SSOT 模型本體（定義雙欄位模型、for_role(role=) 方法、IdentityRule/AgentOverride）。
_EXEMPT = frozenset({"ToukaProxy/src/models/identity.py"})

# identity-ish 接收者名（子字串、小寫比對）：只擋 AgentIdentity 物件的 .role，放行 user/msg/rule。
_IDENTITY_HINTS = ("ident", "caller", "verified")

# 角色字面量（鏡像 ToukaProxy/src/models/identity.py 的 HUMAN_ROLES/AGENT_ROLES；本守衛刻意
# 零 import src，故在此複製。ADR-0141 已凍結角色面，漂移風險低）。檢查 3 用。
_HUMAN_ROLE_LITERALS = frozenset({"admin", "normal"})
_AGENT_ROLE_LITERALS = frozenset({"Owner", "Base", "Subagent", "Debugger", "System"})


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _base_name(node: ast.expr) -> str | None:
    """屬性存取的接收者名（Name.id／Attribute.attr）；其他（下標/呼叫）回 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_identity_ish(node: ast.expr) -> bool:
    name = _base_name(node)
    if name is None:
        return False
    low = name.lower()
    return any(hint in low for hint in _IDENTITY_HINTS)


class _RolePlaneVisitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.violations: list[str] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "role" and _is_identity_ish(node.value):
            self.violations.append(
                f"{self.rel}:{node.lineno} - flat `.role` access on an identity object "
                "(read display_role or gate via a predicate)"
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "AgentIdentity"
            and any(kw.arg == "role" for kw in node.keywords)
        ):
            self.violations.append(
                f"{self.rel}:{node.lineno} - legacy `AgentIdentity(role=...)` construction "
                "(use AgentIdentity.for_role(agent_id, role))"
            )
        self.generic_visit(node)

    def _check_mixed_role_literal(self, node: ast.Set | ast.List | ast.Tuple) -> None:
        """檢查 3：set/list/tuple 字面量若同時含 HUMAN 與 AGENT 角色成員即違規。"""
        vals = {
            e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        if vals & _HUMAN_ROLE_LITERALS and vals & _AGENT_ROLE_LITERALS:
            mixed = sorted(vals & (_HUMAN_ROLE_LITERALS | _AGENT_ROLE_LITERALS))
            self.violations.append(
                f"{self.rel}:{node.lineno} - mixed cross-plane role literal set {mixed} "
                "(human + agent roles in one collection = the soul-IDOR gate pattern; "
                "gate the human plane and the agent plane separately)"
            )

    def visit_Set(self, node: ast.Set) -> None:
        self._check_mixed_role_literal(node)
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._check_mixed_role_literal(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._check_mixed_role_literal(node)
        self.generic_visit(node)


def _scan() -> list[str]:
    violations: list[str] = []
    for base in _SCAN_DIRS:
        if not base.exists():
            continue
        for py in base.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            rel = _rel(py)
            if rel in _EXEMPT:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            visitor = _RolePlaneVisitor(rel)
            visitor.visit(tree)
            violations.extend(visitor.violations)
    return violations


def main() -> int:
    """AST 永久禁令：src 端扁平 AgentIdentity `.role` 存取或 `role=` 建構即 FAIL。"""
    violations = _scan()
    if violations:
        print("[Guard #41] FAIL: Role Plane violations in src:")
        for v in violations:
            print(f"   - {v}")
        print(
            "   -> ADR-0141: (1/2) the flat role compat layer was removed in P7 - read the "
            "effective role via `display_role`, gate via predicates (is_admin_authority/"
            "is_privileged/...), construct via `AgentIdentity.for_role(agent_id, role)`. "
            "(3) never mix human + agent role literals in one collection - gate the two "
            "planes separately (this cross-plane pattern caused the soul IDOR)."
        )
        return 1
    print(
        "[Guard #41] PASS: Role Plane - no flat `.role` access, `role=` construction, "
        "or mixed cross-plane role literal set in src."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

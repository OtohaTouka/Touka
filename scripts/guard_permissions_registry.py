# ruff: noqa: T201
"""Permissions ↔ Registry Guard — 第 32 道守衛（ADR-0088）.

防止出貨 `config/permissions.json.example` 及執行期安全 fallback 的角色工具權限漂移：
每個角色 `allowed_tools` / `denied_tools` 內的每個名稱，**必須**是
`ToukaProxy/src/tools/builtin/__init__.py` 之 `tool_registry` 真實工具名，或萬用 `"*"`。

亦驗證出貨 `config/auth_policies.json.example` 的 `tool_risk` 分類表
（hitl-unification H1；前身 ADR-0089 read_only_tools）雙向對齊：
1. **打錯字防護**：`tool_risk.tools` 每個鍵皆為真實工具名、每個風險類值皆合法
   （read_only/safe/dangerous）——防分類靜默失效。
2. **parity 棘輪**：`tool_registry` 每個工具都必須有分類——新工具未分類＝CI 紅
   （未列名工具執行期吃 default dangerous fail-safe，但原生工具必須顯式分類）。

背景：曾出現 `memory_manager`（真名 `memory_tools`）、`read_file`/`list_directory`/
`get_workspace_structure`（其實是 ResourceManager 方法/IDE 協定工具，非 model-facing 工具名）
等死字串——它們對不上任何工具，`is_tool_allowed()` 永不匹配，導致權限「看似有、實則無」。

另驗 `allowed_mcp_servers` / `denied_mcp_tools` / `global_denied_mcp_tools`
（ADR-0195 軸 B）的**字串形狀**：
合法形態僅 `"*"`、`"server"`、`"server/*"`、`"server/tool"`。
比對規則的 SSOT 在 `src/utils/mcp_tool_policy.validate_tool_ref`，本守衛只是它的執法點——
形狀驗證刻意不接進 Pydantic（`_load_config` 對驗證失敗會退回**更寬**的預設，一條打錯字的
deny 會讓整份權限檔降級；詳見該函式 docstring 的爆炸半徑論證）。

備註（誠實邊界）：**只驗形狀、驗不了存在性**——MCP 伺服器與其工具皆由 hub 動態提供，
無法靜態窮舉。`stripe/creat_refund`（拼錯）形狀合法故本守衛放行，但 `stripe//x`、`/tool`、
`" stripe"` 這類「形狀就不可能匹配」的死字串會被擋下。

純 AST 抽 registry keys + json 載入 config；fallback 從標準庫限定的零應用相依模組 import。
不載入 PermissionService 或應用單例，秒級完成。掃描函式為純函式、可單測。
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# 形狀比對 SSOT 與 runtime 同源——在守衛裡重刻一份規則，就是讓它與 `matches_tool_ref` 漂移。
# `src/utils/mcp_tool_policy` 只 import typing，零副作用（比照 guard_provider_parity 體例）。
for _p in (PROJECT_ROOT / "ToukaProxy",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from src.utils.mcp_tool_policy import validate_tool_ref  # noqa: E402
from src.utils.permission_defaults import DEFAULT_PERMISSIONS_CONFIG  # noqa: E402

_BUILTIN_INIT = PROJECT_ROOT / "ToukaProxy" / "src" / "tools" / "builtin" / "__init__.py"
_FALLBACK_PERMISSIONS_SOURCE = (
    PROJECT_ROOT / "ToukaProxy" / "src" / "utils" / "permission_defaults.py"
)
_PERMISSION_FILES = [
    PROJECT_ROOT / "ToukaProxy" / "config" / "permissions.json.example",
]
# 工具風險分類表（hitl-unification H1）：tool_risk 鍵須為真實工具名且全 registry 有分類。
_AUTH_POLICY_FILES = [
    PROJECT_ROOT / "ToukaProxy" / "config" / "auth_policies.json.example",
]
_WILDCARD = "*"
_TOOL_FIELDS = ("allowed_tools", "denied_tools")
_GLOBAL_TOOL_FIELDS = ("global_denied_tools",)
_RISK_CLASSES = ("read_only", "safe", "dangerous")
# P2：角色 deny self-only，全域 deny 走明示地板；兩者的裸 `"*"` 都是安全的 deny-all。
_MCP_REF_FIELDS = {"allowed_mcp_servers": True, "denied_mcp_tools": True}
_GLOBAL_MCP_REF_FIELDS = {"global_denied_mcp_tools": True}


@dataclass
class Violation:
    """一條權限漂移違規。"""

    file: Path
    role: str
    field: str
    name: str
    # 形狀違規才帶原因；工具名漂移（既有型態）維持空字串，訊息不變。
    reason: str = ""

    def __str__(self) -> str:
        try:
            rel = self.file.relative_to(PROJECT_ROOT)
        except ValueError:
            rel = self.file
        loc = f"角色 `{self.role}` 的 {self.field}" if self.role else self.field
        detail = f"（{self.reason}）" if self.reason else ""
        kind = "條目形狀非法" if self.reason else "含未知工具名"
        return f"  [{rel}] {loc} {kind} `{self.name}`{detail}"


def extract_registry_tool_names(src: str) -> set[str]:
    """從 builtin/__init__.py 原始碼 AST 抽出 `tool_registry` 的字串鍵（純函式）。"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "tool_registry" not in targets or not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    return names


def find_invalid_tool_refs(perms: dict, valid: set[str], file: Path) -> list[Violation]:
    """檢查 permissions dict 各角色的工具欄位，回傳所有非法引用（純函式）。"""
    out: list[Violation] = []
    roles = perms.get("roles", {})
    if not isinstance(roles, dict):
        return out
    allowed = valid | {_WILDCARD}
    for role, cfg in roles.items():
        if not isinstance(cfg, dict):
            continue
        for field in _TOOL_FIELDS:
            for name in cfg.get(field, []) or []:
                if name not in allowed:
                    out.append(Violation(file=file, role=role, field=field, name=name))
    for field in _GLOBAL_TOOL_FIELDS:
        for name in perms.get(field, []) or []:
            if name not in allowed:
                out.append(Violation(file=file, role="<global>", field=field, name=name))
    return out


def find_invalid_mcp_tool_refs(perms: dict, file: Path) -> list[Violation]:
    """檢查各角色的 MCP 條目**字串形狀**（純函式，ADR-0195 軸 B）。

    只擋「形狀就不可能匹配」的死字串（`/tool`、`a//b`、前後空白…）——
    存在性驗不了（見模組 docstring 的誠實邊界）。比對規則委派 `validate_tool_ref`，
    不在此重刻，否則與 runtime 的 `matches_tool_ref` 必然漂移。
    """
    out: list[Violation] = []
    roles = perms.get("roles", {})
    if not isinstance(roles, dict):
        return out
    for role, cfg in roles.items():
        if not isinstance(cfg, dict):
            continue
        for field, allow_star in _MCP_REF_FIELDS.items():
            for ref in cfg.get(field, []) or []:
                reason = validate_tool_ref(ref, allow_bare_wildcard=allow_star)
                if reason:
                    out.append(
                        Violation(file=file, role=role, field=field, name=str(ref), reason=reason)
                    )
    for field, allow_star in _GLOBAL_MCP_REF_FIELDS.items():
        for ref in perms.get(field, []) or []:
            reason = validate_tool_ref(ref, allow_bare_wildcard=allow_star)
            if reason:
                out.append(
                    Violation(
                        file=file,
                        role="<global>",
                        field=field,
                        name=str(ref),
                        reason=reason,
                    )
                )
    return out


def find_invalid_tool_risk_refs(policies: dict, valid: set[str], file: Path) -> list[Violation]:
    """檢查 auth_policies 的 tool_risk 分類表（純函式，hitl-unification H1）。

    1. 每個 ``tool_risk.tools`` 鍵須為真實工具名（打錯字防護）。
    2. 風險類值（純字串或聚合條目的 actions 值）須 ∈ {read_only, safe, dangerous}。
    3. parity 棘輪：registry 每個工具必須有分類（新工具未分類＝違規）。
    """
    out: list[Violation] = []
    tool_risk = policies.get("tool_risk")
    if not isinstance(tool_risk, dict):
        out.append(Violation(file=file, role="", field="tool_risk", name="<缺 tool_risk 區塊>"))
        return out
    tools = tool_risk.get("tools", {}) or {}

    for name, entry in tools.items():
        if name not in valid:
            out.append(Violation(file=file, role="", field="tool_risk.tools", name=name))
        if isinstance(entry, str):
            if entry not in _RISK_CLASSES:
                out.append(
                    Violation(file=file, role="", field="tool_risk.class", name=f"{name}={entry}")
                )
        elif isinstance(entry, dict):
            for verb, cls in (entry.get("actions", {}) or {}).items():
                if name == "memory_tools" and verb == "manage_dups" and cls != "dangerous":
                    out.append(
                        Violation(
                            file=file,
                            role="",
                            field="tool_risk.write_floor",
                            name="memory_tools.manage_dups must be dangerous",
                        )
                    )
                if cls not in _RISK_CLASSES:
                    out.append(
                        Violation(
                            file=file,
                            role="",
                            field="tool_risk.class",
                            name=f"{name}.{verb}={cls}",
                        )
                    )
        else:
            out.append(
                Violation(file=file, role="", field="tool_risk.tools", name=f"{name}=<非法型別>")
            )

    # parity 棘輪：registry 全員必須顯式分類
    for name in sorted(valid - set(tools)):
        out.append(Violation(file=file, role="", field="tool_risk.parity（未分類）", name=name))
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_all() -> list[Violation]:
    """核對必要出貨範本及 fallback；不讀取或探查私有現場設定。"""
    if not _BUILTIN_INIT.exists():
        return [Violation(_BUILTIN_INIT, "<n/a>", "<n/a>", "builtin/__init__.py 不存在")]
    valid = extract_registry_tool_names(_read(_BUILTIN_INIT))
    if not valid:
        return [Violation(_BUILTIN_INIT, "<n/a>", "<n/a>", "無法抽出 tool_registry 鍵")]

    out: list[Violation] = []
    out.extend(
        find_invalid_tool_refs(
            DEFAULT_PERMISSIONS_CONFIG,
            valid,
            _FALLBACK_PERMISSIONS_SOURCE,
        )
    )
    out.extend(
        find_invalid_mcp_tool_refs(
            DEFAULT_PERMISSIONS_CONFIG,
            _FALLBACK_PERMISSIONS_SOURCE,
        )
    )
    for pf in _PERMISSION_FILES:
        if not pf.exists():
            out.append(Violation(pf, "<n/a>", "<n/a>", "必要出貨範本不存在"))
            continue
        try:
            perms = json.loads(_read(pf))
        except json.JSONDecodeError:
            out.append(Violation(pf, "<n/a>", "<n/a>", "JSON 解析失敗"))
            continue
        out.extend(find_invalid_tool_refs(perms, valid, pf))
        out.extend(find_invalid_mcp_tool_refs(perms, pf))

    # auth_policies.json 的 tool_risk 分類表：打錯字防護＋parity 棘輪（H1）
    for af in _AUTH_POLICY_FILES:
        if not af.exists():
            out.append(Violation(af, "", "<n/a>", "必要出貨範本不存在"))
            continue
        try:
            policies = json.loads(_read(af))
        except json.JSONDecodeError:
            out.append(Violation(af, "", "<n/a>", "JSON 解析失敗"))
            continue
        out.extend(find_invalid_tool_risk_refs(policies, valid, af))
    return out


def main() -> int:
    """執行守衛並回傳退出碼。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Permissions <-> Registry Guard (Guard #32)")
    print("=" * 60)
    violations = run_all()
    if not violations:
        print(
            "[OK] 出貨 permissions.json.example 與 fallback 的角色工具權限皆對應真實 "
            "tool_registry；MCP 條目形狀合法。"
        )
        return 0
    print(f"[FAIL] 發現 {len(violations)} 條權限漂移：")
    for v in violations:
        print(str(v))
    print("\n修法：改用真實工具名（見 tool_registry），或移除死字串。")
    print("MCP 條目形狀請用 '*'、'server'、'server/*' 或 'server/tool'。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

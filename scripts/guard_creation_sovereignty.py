# ruff: noqa: T201
"""Creation Sovereignty Guard — 第 29 道守衛（ADR-0069 Phase D）.

強制三條創建主權不變量：
  1. ``routers/creation.py`` 每個端點都以 ``_require_creation_role`` 把關（Owner/admin）。
  2. ``services/creation/`` 以外的模組不得直呼 ``.update_skill(`` / ``.register_server(``
     （必須走創建引擎；allowlist：定義處 + 既有合法呼叫者 routers/skills.py）——防繞過引擎
     直寫、保 CREATE 不覆寫不變量。
  3. ``services/creation/api.py`` 門面不得 import fastapi（保持傳輸無關、雙介面共用）。

任一違反即非零退出。純 AST、零 import 副作用、秒級。掃描函式（``_scan_*``）為純函式、可單測。
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SRC = PROJECT_ROOT / "ToukaProxy" / "src"
_CREATION_ROUTE = _SRC / "routers" / "creation.py"
_CREATION_PKG = _SRC / "services" / "creation"
_API_FACADE = _CREATION_PKG / "api.py"

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_PRIVILEGED_CALLS = {"update_skill", "register_server"}
_REQUIRED_DEP = "_require_creation_role"

# 允許直呼特權寫入方法的檔案（定義處 + 既有合法呼叫者）。creation 套件內部另由 parents 判定放行。
_BYPASS_ALLOWLIST = {
    _SRC / "services" / "skill_service.py",
    _SRC / "services" / "mcp_hub_service.py",
    _SRC / "routers" / "skills.py",
}


@dataclass
class Violation:
    """一條主權違規。"""

    rule: str
    detail: str
    file: Path
    line: int

    def __str__(self) -> str:
        try:
            rel = self.file.relative_to(PROJECT_ROOT)
        except ValueError:
            rel = self.file
        return f"  [{self.rule}] {self.detail} ({rel}:{self.line})"


def _route_method(decorator: ast.expr) -> str | None:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    method = decorator.func.attr.lower()
    return method if method in _HTTP_METHODS else None


def _has_required_dep(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    defaults = list(func.args.defaults) + list(func.args.kw_defaults)
    for default in defaults:
        if default is not None and _REQUIRED_DEP in ast.unparse(default):
            return True
    for arg in func.args.args + func.args.kwonlyargs:
        if arg.annotation is not None and _REQUIRED_DEP in ast.unparse(arg.annotation):
            return True
    return False


def _scan_route_gating(src: str) -> list[tuple[str, int]]:
    """(func_name, line)：缺 _require_creation_role 的路由端點（純函式）。"""
    out: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_route_method(d) for d in node.decorator_list) and not _has_required_dep(node):
            out.append((node.name, node.lineno))
    return out


def _scan_engine_bypass(src: str) -> list[tuple[str, int]]:
    """(method, line)：直呼特權寫入方法（純函式）。"""
    out: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PRIVILEGED_CALLS
        ):
            out.append((node.func.attr, node.lineno))
    return out


def _scan_facade_fastapi(src: str) -> list[int]:
    """回傳 fastapi import 的行號（純函式）。"""
    out: list[int] = []
    for node in ast.walk(ast.parse(src)):
        is_fastapi_import = (
            isinstance(node, ast.Import)
            and any(n.name.split(".")[0] == "fastapi" for n in node.names)
        ) or (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "fastapi"
        )
        if is_fastapi_import:
            out.append(node.lineno)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_all() -> list[Violation]:
    """執行三條規則，回傳所有違規。"""
    out: list[Violation] = []

    # 規則 1：creation 路由端點都有 _require_creation_role
    if not _CREATION_ROUTE.exists():
        out.append(Violation("ROUTE_MISSING", "routers/creation.py 不存在", _CREATION_ROUTE, 0))
    else:
        for name, line in _scan_route_gating(_read(_CREATION_ROUTE)):
            out.append(
                Violation("ROUTE_UNGATED", f"端點 {name} 缺 Depends({_REQUIRED_DEP})",
                          _CREATION_ROUTE, line)
            )

    # 規則 3：門面 api.py 不得 import fastapi
    if _API_FACADE.exists():
        for line in _scan_facade_fastapi(_read(_API_FACADE)):
            out.append(Violation("FACADE_IMPURE", "api.py 不應 import fastapi", _API_FACADE, line))

    # 規則 2：creation 套件外不得繞過引擎直呼特權寫入
    allow = {p.resolve() for p in _BYPASS_ALLOWLIST}
    pkg = _CREATION_PKG.resolve()
    for py in sorted(_SRC.rglob("*.py")):
        rp = py.resolve()
        if rp in allow or pkg in rp.parents:
            continue
        try:
            calls = _scan_engine_bypass(_read(py))
        except SyntaxError:
            continue
        for method, line in calls:
            out.append(Violation("ENGINE_BYPASS", f"直呼 .{method}() 繞過創建引擎", py, line))

    return out


def main() -> int:
    """執行守衛並回傳退出碼。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Creation Sovereignty Guard (Guard #29)")
    print("=" * 60)
    violations = run_all()
    if not violations:
        print("[OK] 創建主權三條不變量皆通過。")
        return 0
    print(f"[FAIL] 發現 {len(violations)} 條違規：")
    for v in violations:
        print(str(v))
    return 1


if __name__ == "__main__":
    sys.exit(main())

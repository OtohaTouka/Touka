# ruff: noqa: T201, S603, S607
"""Endpoint Auth Dependency Guard — 第 24 道守衛 (Guard #24).

掃描 ToukaProxy 的所有 router 檔案，確保每個非公開端點都有
auth dependency（`Depends(get_verified_identity)` 或等效守衛）。

用途：防止新增端點時忘記加驗證，讓 N1/N2/N3/N10 類弱點在 CI 被攔截。

允許清單（刻意公開的端點）由 endpoint_auth_allowlist.py（SSOT）管理。

等級辨識（E05/E06/E07，家族④）：舊版只驗「有認證」不驗「等級」——
``require_admin → get_verified_identity`` 的降級全綠通過。現以
endpoint_auth_admin_required.py（SSOT）做雙向同步：表列端點必帶 admin 級
Depends（降級即 FAIL）、帶 admin 級 Depends 的端點必列表（漂移即 FAIL）；
並強制 GRANDFATHER 永久為空（ADR-0034 承諾的清單長度靜態斷言）。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from endpoint_auth_admin_required import ADMIN_REQUIRED  # noqa: TID252
from endpoint_auth_allowlist import ALLOWLIST  # noqa: TID252
from endpoint_auth_grandfather import GRANDFATHER  # noqa: TID252

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
ROUTERS_DIR = PROJECT_ROOT / "ToukaProxy" / "src" / "routers"

# HTTP 方法修飾器（涵蓋 FastAPI 支援的所有方法）
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}

# Depends 參數名稱的關鍵字：只要 Depends 的引數包含其中一個字串，視為 auth guard
_AUTH_KEYWORDS = {
    "get_verified_identity",
    "get_optional_identity",
    "get_current_user",
    "require_admin",
    "_require_",  # 捕捉所有 _require_*_role 模式
    "verify_token",
    "verify_schedule_token",
    "verify_heartbeat",  # heartbeat.py 的 verify_heartbeat_token dependency
    "verify_origin",  # CSRF 守衛（僅為補充，不算主要 auth）
}

# admin 級 Depends 的判準：子串涵蓋現行四種拼法
# （require_admin／_require_admin／_require_admin_only／_require_admin_role）
_ADMIN_KEYWORD = "require_admin"


@dataclass
class EndpointViolation:
    """表示一個缺少 auth dependency 的端點。"""

    file: Path
    function: str
    method: str
    path: str
    line: int

    def __str__(self) -> str:
        try:
            rel = self.file.relative_to(PROJECT_ROOT)
        except ValueError:
            rel = self.file
        return (
            f"  [{self.method.upper()}] {self.path}  "
            f"(func={self.function}, file={rel}, line={self.line})"
        )


def _iter_depends_sources(
    func_def: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Yield the unparsed source of every annotation/default that contains Depends."""
    sources: list[str] = []
    for arg in func_def.args.args + func_def.args.kwonlyargs:
        if arg.annotation is None:
            continue
        ann_src = ast.unparse(arg.annotation)
        if "Depends" in ann_src:
            sources.append(ann_src)
    # Also check default values (common FastAPI pattern: foo = Depends(guard))
    for default in list(func_def.args.defaults) + list(func_def.args.kw_defaults):
        if default is None:
            continue
        default_src = ast.unparse(default)
        if "Depends" in default_src:
            sources.append(default_src)
    return sources


def _has_auth_depends(func_def: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if any parameter uses an auth-related Depends()."""
    return any(
        keyword in src for src in _iter_depends_sources(func_def) for keyword in _AUTH_KEYWORDS
    )


def _has_admin_depends(func_def: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if any parameter uses an admin-level Depends()（require_admin 族）."""
    return any(_ADMIN_KEYWORD in src for src in _iter_depends_sources(func_def))


def _matches_list(entries: list[dict[str, str]], method: str, full_path: str) -> bool:
    """Return True if the endpoint matches any entry in the given list."""
    for entry in entries:
        if entry["method"] != method:
            continue
        prefix = entry["path_prefix"]
        if (
            full_path == prefix
            or full_path.startswith(prefix + "/")
            or full_path.startswith(prefix + "{")
        ):
            return True
    return False


def _is_allowlisted(method: str, full_path: str) -> bool:
    """Return True if the endpoint is intentionally public OR grandfathered."""
    return _matches_list(ALLOWLIST, method, full_path) or _matches_list(
        GRANDFATHER, method, full_path
    )


@dataclass
class EndpointRecord:
    """一個掃描到的端點（含認證/等級判定）。"""

    file: Path
    function: str
    method: str
    path: str
    line: int
    has_auth: bool
    has_admin: bool


def _literal_router_prefix(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg is None:
            message = "expanded router options are not supported"
            raise ValueError(message)
        if keyword.arg == "prefix":
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
            message = "dynamic router prefix is not supported"
            raise ValueError(message)
    return ""


def _local_router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Resolve finite local APIRouter assignments and include_router edges."""
    prefixes: dict[str, str] = {}
    parents: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id in prefixes for target in node.targets):
            message = "router reassignment is not supported"
            raise ValueError(message)
        if not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Name) or call.func.id != "APIRouter":
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            message = "ambiguous router assignment"
            raise ValueError(message)
        name = node.targets[0].id
        if name in prefixes:
            message = "duplicate router assignment"
            raise ValueError(message)
        prefixes[name] = _literal_router_prefix(call)
    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "include_router":
            continue
        if (
            not isinstance(call.func.value, ast.Name)
            or call.func.value.id not in prefixes
            or len(call.args) != 1
            or not isinstance(call.args[0], ast.Name)
            or call.args[0].id not in prefixes
        ):
            message = "unresolved local router include"
            raise ValueError(message)
        child = call.args[0].id
        if child in parents:
            message = "ambiguous router parent"
            raise ValueError(message)
        parents[child] = (call.func.value.id, _literal_router_prefix(call))
    resolved: dict[str, str] = {}
    for name, prefix in prefixes.items():
        visited = {name}
        current = name
        while current in parents:
            parent, include_prefix = parents[current]
            if parent in visited:
                message = "cyclic router include"
                raise ValueError(message)
            visited.add(parent)
            prefix = prefixes[parent] + include_prefix + prefix
            current = parent
        resolved[name] = prefix
    return resolved


def collect_endpoints(filepath: Path) -> list[EndpointRecord]:
    """Parse a router file and return every route endpoint with auth/admin info."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    records: list[EndpointRecord] = []

    router_prefixes = _local_router_prefixes(tree)

    # Scan all function definitions for router decorators
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            method, route_path = _extract_route_info(decorator)
            if method is None:
                continue
            receiver = decorator.func.value
            if not isinstance(receiver, ast.Name) or receiver.id not in router_prefixes:
                message = "unresolved router decorator receiver"
                raise ValueError(message)

            records.append(
                EndpointRecord(
                    file=filepath,
                    function=node.name,
                    method=method,
                    path=router_prefixes[receiver.id] + route_path,
                    line=node.lineno,
                    has_auth=_has_auth_depends(node),
                    has_admin=_has_admin_depends(node),
                )
            )

    return records


def scan_router_file(filepath: Path) -> list[EndpointViolation]:
    """Parse a router file and return violations (endpoints missing auth)."""
    return [
        EndpointViolation(
            file=rec.file,
            function=rec.function,
            method=rec.method,
            path=rec.path,
            line=rec.line,
        )
        for rec in collect_endpoints(filepath)
        if not _is_allowlisted(rec.method, rec.path) and not rec.has_auth
    ]


def check_admin_level(
    records: list[EndpointRecord],
    registry: list[dict[str, str]] | None = None,
) -> list[str]:
    """雙向同步檢查：ADMIN_REQUIRED ↔ 實際帶 admin 級 Depends 的端點。

    - 表列端點缺席（改名/刪除未更新表）→ 錯誤（表過期，fail-loud 強制同步）。
    - 表列端點存在但無 admin 級 Depends → 錯誤（**降級**，E06/E07 的核心缺口）。
    - 帶 admin 級 Depends 的端點未列表 → 錯誤（新增未登記，防清單靜默漂移）。
    """
    reg = ADMIN_REQUIRED if registry is None else registry
    errors: list[str] = []

    by_key: dict[tuple[str, str], EndpointRecord] = {(rec.method, rec.path): rec for rec in records}

    for entry in reg:
        key = (entry["method"], entry["path"])
        rec = by_key.get(key)
        if rec is None:
            errors.append(
                f"[REGISTRY STALE] {entry['method'].upper()} {entry['path']} 列於 "
                "ADMIN_REQUIRED 但已不存在——端點改名/移除須同步更新 "
                "endpoint_auth_admin_required.py"
            )
        elif not rec.has_admin:
            errors.append(
                f"[ADMIN DOWNGRADE] {entry['method'].upper()} {entry['path']} "
                f"(func={rec.function}, line={rec.line}) 必須 admin 級守衛"
                "（require_admin 族），現行 Depends 不含之——認證等級被降級"
            )

    registered = {(e["method"], e["path"]) for e in reg}
    for rec in records:
        if rec.has_admin and (rec.method, rec.path) not in registered:
            errors.append(
                f"[UNREGISTERED ADMIN] {rec.method.upper()} {rec.path} "
                f"(func={rec.function}) 帶 admin 級 Depends 但未列於 "
                "endpoint_auth_admin_required.py——新增 admin 端點須同步登記"
            )

    return errors


def _extract_route_info(decorator: ast.expr) -> tuple[str | None, str]:
    """Extract HTTP method and path from a @router.METHOD("/path") decorator."""
    if not isinstance(decorator, ast.Call):
        return None, ""

    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None, ""

    method = func.attr.lower()
    if method not in _HTTP_METHODS:
        return None, ""

    # Extract path from first positional argument
    if decorator.args:
        first_arg = decorator.args[0]
        if isinstance(first_arg, ast.Constant):
            return method, str(first_arg.value)

    return method, ""


def main() -> int:
    """Scan all routers and print violations. Return exit code."""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Endpoint Auth Dependency Guard (Guard #24)")
    print("=" * 60)

    if not ROUTERS_DIR.exists():
        print(f"[ERROR] Routers directory not found: {ROUTERS_DIR}")
        return 1

    all_violations: list[EndpointViolation] = []
    all_records: list[EndpointRecord] = []
    scanned = 0

    for router_file in sorted(ROUTERS_DIR.glob("*.py")):
        if router_file.name == "__init__.py":
            continue
        records = collect_endpoints(router_file)
        all_records.extend(records)
        all_violations.extend(
            EndpointViolation(
                file=rec.file,
                function=rec.function,
                method=rec.method,
                path=rec.path,
                line=rec.line,
            )
            for rec in records
            if not _is_allowlisted(rec.method, rec.path) and not rec.has_auth
        )
        scanned += 1

    # E05（ADR-0034）：GRANDFATHER 永久空清單靜態斷言——逃生口不得再被使用
    grandfather_errors: list[str] = []
    if GRANDFATHER:
        grandfather_errors.append(
            f"[GRANDFATHER NOT EMPTY] 技術債逃生口必須永久為空（ADR-0034），"
            f"現有 {len(GRANDFATHER)} 條——不得以 grandfather 豁免新端點；"
            "刻意公開請走 endpoint_auth_allowlist.py"
        )

    # E06/E07（家族④）：admin 等級雙向同步
    admin_errors = check_admin_level(all_records)

    total_problems = len(all_violations) + len(grandfather_errors) + len(admin_errors)
    print(
        f"Scanned {scanned} router files, {len(all_records)} endpoints, "
        f"{total_problems} problem(s) found.\n"
    )

    if not total_problems:
        print(
            "[OK] All non-public endpoints have auth dependencies; "
            f"admin-level registry in sync ({len(ADMIN_REQUIRED)} endpoints); "
            "grandfather list empty."
        )
        return 0

    if all_violations:
        print("[FAIL] Endpoints missing auth dependency:")
        for v in all_violations:
            print(str(v))
        print(
            "\nFix: add Depends(get_verified_identity) or an equivalent role guard.\n"
            "If the endpoint is intentionally public, add it to:\n"
            "  scripts/endpoint_auth_allowlist.py"
        )
    for err in grandfather_errors:
        print(f"[FAIL] {err}")
    if admin_errors:
        print("[FAIL] Admin-level registry violations:")
        for err in admin_errors:
            print(f"  {err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""重生 openapi 的腳本先開沙盒再載入產品程式（禁令 63，ADR-0250 B1）。

純 AST／文字掃描，不 import 任何腳本（``update_openapi`` 一匯入就會開沙盒並載入產品程式）。

- ``update_openapi.py``：模組頂層先呼叫 ``enable_smoke_sandbox``，之後才匯入 ``src``／``awaken``
  ／``memory_server``；輸出位置由 ``Path(__file__)`` 推出，不用 ``get_workspace_root``（沙盒會把
  ``WORKSPACE_ROOT`` 指到暫存資料夾）。
- ``openapi_merge.py``：純合併函式，不匯入產品程式。
- ``update_docs.py``：不直接匯入產品程式，第一個專案匯入就是 ``update_openapi``。
- 測試改從 ``openapi_merge`` 匯入，不再匯入會開沙盒的入口。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
_PRODUCT_PREFIXES = ("src", "awaken", "memory_server")
_PROJECT_PREFIXES = (*_PRODUCT_PREFIXES, "update_openapi", "openapi_merge")


def _tree(name: str) -> ast.Module:
    path = SCRIPTS / name
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return [alias.name for alias in node.names]


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(f"{p}.") for p in prefixes)


def _imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def _sandbox_call_lines(tree: ast.Module) -> list[int]:
    """模組頂層（不在函式、if 或 try 裡）的 ``enable_smoke_sandbox(...)`` 呼叫。"""
    lines = []
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, (ast.Expr, ast.Assign)) else None
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "enable_smoke_sandbox"
        ):
            lines.append(stmt.lineno)
    return lines


def _names(tree: ast.Module) -> set[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in _imports(tree):
        names |= {alias.name for alias in node.names}
    return names


def test_update_openapi_opens_sandbox_before_any_product_import():
    tree = _tree("update_openapi.py")
    product = [n.lineno for n in _imports(tree) if any(
        _matches(m, _PRODUCT_PREFIXES) for m in _module_names(n)
    )]
    sandbox = _sandbox_call_lines(tree)
    assert product, "update_openapi.py 應載入 src.main 與 awaken"
    assert sandbox, "update_openapi.py 沒有在模組頂層呼叫 enable_smoke_sandbox"
    assert min(sandbox) < min(product), "enable_smoke_sandbox 必須在任何產品匯入之前"


def test_update_openapi_product_imports_are_top_level_only():
    """產品匯入只在模組頂層（Import SSOT），不藏在函式裡繞過沙盒順序。"""
    tree = _tree("update_openapi.py")
    top_level = {id(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    nested = [n.lineno for n in _imports(tree) if id(n) not in top_level]
    assert nested == []


def test_update_openapi_writes_next_to_the_script_not_the_workspace_root():
    tree = _tree("update_openapi.py")
    names = _names(tree)
    assert "get_workspace_root" not in names
    assert "WORKSPACE_ROOT" not in names
    assert "PROJECT_ROOT" in names


def test_openapi_merge_is_pure_and_imports_no_product_code():
    assert (SCRIPTS / "openapi_merge.py").is_file(), "純合併函式應獨立成 openapi_merge.py"
    tree = _tree("openapi_merge.py")
    modules = [m for n in _imports(tree) for m in _module_names(n)]
    assert not [m for m in modules if _matches(m, _PROJECT_PREFIXES)], modules
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert {"merge_schemas", "OpenAPIMergeConflict"} <= defined


def test_update_openapi_reuses_the_pure_merge_instead_of_redefining_it():
    tree = _tree("update_openapi.py")
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not {"merge_schemas", "OpenAPIMergeConflict"} & defined
    sources = {n.module for n in _imports(tree) if isinstance(n, ast.ImportFrom)}
    assert "openapi_merge" in sources


def test_update_docs_first_project_import_is_the_sandboxed_entry():
    tree = _tree("update_docs.py")
    modules = [m for n in _imports(tree) for m in _module_names(n)]
    project = [m for m in modules if _matches(m, _PROJECT_PREFIXES)]
    assert project, "update_docs.py 應經 update_openapi 重生 openapi"
    assert project[0] == "update_openapi", project
    assert not [m for m in project if _matches(m, _PRODUCT_PREFIXES)], project
    assert "WORKSPACE_ROOT" not in _names(tree)


def test_tests_import_the_pure_merge_not_the_sandboxed_entry():
    """pytest 裡匯入入口腳本會在測試行程內再開一次沙盒，測試一律改用純合併函式。"""
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == Path(__file__).name or "update_openapi" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        if any(_matches(m, ("update_openapi",)) for n in _imports(tree) for m in _module_names(n)):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []

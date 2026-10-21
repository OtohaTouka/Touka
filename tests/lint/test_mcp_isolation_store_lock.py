"""隔離設定檔的 ``threading.Lock`` 前提（ADR-0275 §1）。

``mcp_server_isolation.json`` 用程序內的 ``threading.Lock``，不用 ``AsyncFileLock``，前提是：

1. 只有 proxy 一個行程會寫：檔名只出現在隔離設定檔模組與 ``bootstrap.ESSENTIAL_CONFIGS``。
   以後有人在別的行程（Dispatch、Heartbeat、Memory、scripts）加寫入點會在這裡紅，屆時先換成
   跨行程的鎖。
2. 鎖只包同步程式碼：模組裡只有 ``amutate`` 是 async，而且本體只把 ``mutate`` 交給
   ``asyncio.to_thread``；任何 ``with _LOCK:`` 區塊裡沒有 await／async with／async for，
   也沒有呼叫模組自己的公開方法（會重入、卡死）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "ToukaProxy" / "src" / "services" / "mcp_isolation_store.py"
BOOTSTRAP = ROOT / "ToukaProxy" / "src" / "utils" / "bootstrap.py"
SCAN_ROOTS = (
    ROOT / "ToukaProxy" / "src",
    ROOT / "ToukaMemory",
    ROOT / "ToukaDispatch",
    ROOT / "ToukaHeartbeat",
    ROOT / "scripts",
)
FILENAME_STEM = "mcp_server_isolation"
ALLOWED = {STORE, BOOTSTRAP}
PUBLIC_METHODS = frozenset({"load", "record_for", "mutate", "amutate"})


def _python_files() -> list[Path]:
    skip = {"__pycache__", ".venv", "node_modules", ".pytest_cache"}
    return sorted(
        path
        for root in SCAN_ROOTS
        for path in root.rglob("*.py")
        if not skip.intersection(path.parts)
    )


def test_filename_only_appears_in_the_store_and_bootstrap() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _python_files()
        if path not in ALLOWED and FILENAME_STEM in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    for path in ALLOWED:
        assert FILENAME_STEM in path.read_text(encoding="utf-8"), path


def _tree() -> ast.Module:
    return ast.parse(STORE.read_text(encoding="utf-8"))


def test_only_amutate_is_async_and_it_only_hands_mutate_to_a_thread() -> None:
    async_defs = [n for n in ast.walk(_tree()) if isinstance(n, ast.AsyncFunctionDef)]
    assert [n.name for n in async_defs] == ["amutate"]

    body = [
        stmt
        for stmt in async_defs[0].body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    assert len(body) == 1
    assert ast.unparse(body[0]) == "return await asyncio.to_thread(self.mutate, fn)"


def _lock_blocks(tree: ast.Module) -> list[ast.With]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(ast.unparse(item.context_expr) == "_LOCK" for item in node.items)
    ]


def test_lock_blocks_never_await_or_reenter_the_store() -> None:
    tree = _tree()
    blocks = _lock_blocks(tree)
    assert blocks, "找不到 with _LOCK: 區塊"
    module_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    for block in blocks:
        for node in ast.walk(block):
            assert not isinstance(node, ast.Await | ast.AsyncWith | ast.AsyncFor), ast.unparse(node)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in PUBLIC_METHODS, ast.unparse(node)
                if isinstance(func, ast.Name):
                    assert func.id not in module_functions, ast.unparse(node)


def test_the_lock_is_a_threading_lock_with_its_premise_next_to_it() -> None:
    source = STORE.read_text(encoding="utf-8")
    assert "_LOCK = threading.Lock()" in source
    for reference in (
        "ADR-0207",
        "tests/lint/test_mcp_isolation_store_lock.py",
        "tests/lint/test_single_proxy_process.py",
        "tests/unit/test_mcp_isolation_store_security.py",
    ):
        assert reference in source, reference

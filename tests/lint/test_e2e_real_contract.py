"""實際驗證環境的不變條件（ADR-0295）：不用 secrets、不關網路防護、不被自動測試撿走.

這些條件壞掉時測試照樣會綠（只是開始連外或花額度），所以在這裡靜態釘住。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/e2e-real.yml"
BACKEND = ROOT / "tests/e2e/serve_real_backend.py"
PINNED_ACTION = re.compile(r"[^@]+@[0-9a-f]{40}")
CREDENTIAL_USE = re.compile(r"\bsecrets\b|github\.token|GITHUB_TOKEN")
READ_ONLY = {"contents": "read"}
PRODUCT_PACKAGES = {"src", "memory_server", "toukaheartbeat", "toukadispatch"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def _imported_packages(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {(node.module or "").split(".")[0]}
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        callee = node.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
        if name in {"import_module", "__import__"}:
            return {str(node.args[0].value).split(".")[0]}
    return set()


def _runs_the_sandbox(value: ast.expr) -> bool:
    """只認直接呼叫，或外面只包呼叫（如 Path(...)）；lambda、條件式裡的不一定會執行."""
    if not isinstance(value, ast.Call):
        return False
    if isinstance(value.func, ast.Name) and value.func.id == "enable_smoke_sandbox":
        return True
    return any(_runs_the_sandbox(argument) for argument in value.args)


def _sandbox_runs_before_product_imports(source: str) -> bool:
    """沙盒呼叫要是最上層敘述（不在函式或條件裡），而且排在所有產品匯入之前."""
    tree = ast.parse(source)
    sandbox_lines = [
        statement.lineno
        for statement in tree.body
        if isinstance(statement, ast.Expr | ast.Assign) and _runs_the_sandbox(statement.value)
    ]
    product_lines = [
        node.lineno for node in ast.walk(tree) if _imported_packages(node) & PRODUCT_PACKAGES
    ]
    return len(sandbox_lines) == 1 and bool(product_lines) and sandbox_lines[0] < min(product_lines)


def test_workflow_has_no_secrets_and_read_only_permissions() -> None:
    assert not CREDENTIAL_USE.search(WORKFLOW.read_text(encoding="utf-8"))
    workflow = _workflow()
    assert workflow["permissions"] == READ_ONLY
    for name, job in workflow["jobs"].items():
        assert job.get("permissions", READ_ONLY) == READ_ONLY, name


def test_workflow_triggers_only_on_prs_into_dev_and_main_or_by_hand() -> None:
    triggers = _workflow()[True]  # PyYAML 把 on 讀成 True
    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert set(triggers["pull_request"]["branches"]) == {"dev", "main"}


def test_workflow_pins_actions_and_does_not_keep_the_checkout_credential() -> None:
    uses = [step for step in _steps(_workflow()) if "uses" in step]
    assert uses
    for step in uses:
        assert PINNED_ACTION.fullmatch(step["uses"]), step["uses"]
        if step["uses"].startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_backend_enables_the_sandbox_before_any_product_import() -> None:
    assert _sandbox_runs_before_product_imports(BACKEND.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import src.main\nenable_smoke_sandbox()\n",
            id="import-statement-first",
        ),
        pytest.param(
            "from memory_server import x\nroot = enable_smoke_sandbox()\n",
            id="from-import-first",
        ),
        pytest.param(
            "import toukaheartbeat\nenable_smoke_sandbox()\n",
            id="heartbeat-first",
        ),
        pytest.param(
            "import toukadispatch\nenable_smoke_sandbox()\n",
            id="dispatch-first",
        ),
        pytest.param(
            "if False:\n    enable_smoke_sandbox()\nimport src.main\n",
            id="sandbox-under-condition",
        ),
        pytest.param(
            "def setup():\n    enable_smoke_sandbox()\nimport src.main\n",
            id="sandbox-in-uncalled-function",
        ),
        pytest.param(
            "enable_smoke_sandbox()\n",
            id="no-product-import-means-the-check-saw-nothing",
        ),
        pytest.param(
            "try:\n    import src.main\nexcept ImportError:\n    pass\nenable_smoke_sandbox()\n",
            id="import-nested-first",
        ),
        pytest.param(
            "run = lambda: enable_smoke_sandbox()\nimport src.main\n",
            id="sandbox-in-lambda",
        ),
        pytest.param(
            "done = False and enable_smoke_sandbox()\nimport src.main\n",
            id="sandbox-short-circuited",
        ),
        pytest.param(
            "done = enable_smoke_sandbox() if False else None\nimport src.main\n",
            id="sandbox-in-conditional-expression",
        ),
        pytest.param(
            "importlib.import_module('src.main')\nenable_smoke_sandbox()\nimport uvicorn\n",
            id="import-module-first",
        ),
        pytest.param(
            "__import__('memory_server')\nenable_smoke_sandbox()\nimport uvicorn\n",
            id="dunder-import-first",
        ),
    ],
)
def test_sandbox_order_check_rejects(source: str) -> None:
    assert not _sandbox_runs_before_product_imports(source)


def test_sandbox_order_check_accepts_the_wrapped_call() -> None:
    source = "root = Path(enable_smoke_sandbox('x'))\nimport uvicorn\nfrom src.main import app\n"
    assert _sandbox_runs_before_product_imports(source)


def test_backend_keeps_the_network_guard_on() -> None:
    assert "allow_external_network" not in BACKEND.read_text(encoding="utf-8")


def test_backend_is_not_picked_up_by_automated_runners() -> None:
    # pytest 只收 test_*.py；run_all_smoke_tests 只收 smoke_*.py
    assert not BACKEND.name.startswith(("test_", "smoke_"))

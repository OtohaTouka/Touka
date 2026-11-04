"""實際驗證環境的不變條件（ADR-0295）：不用 secrets、不關網路防護、不被自動測試撿走.

這些條件壞掉時測試照樣會綠（只是開始連外或花額度），所以在這裡靜態釘住。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/e2e-real.yml"
BACKEND = ROOT / "tests/e2e/serve_real_backend.py"
PINNED_ACTION = re.compile(r"[^@]+@[0-9a-f]{40}")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_has_no_secrets_and_read_only_permissions() -> None:
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
    assert _workflow()["permissions"] == {"contents": "read"}


def test_workflow_runs_on_prs_into_dev_and_main_and_is_pinned() -> None:
    workflow = _workflow()
    # PyYAML 把 on 讀成 True
    assert set(workflow[True]["pull_request"]["branches"]) == {"dev", "main"}
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert PINNED_ACTION.fullmatch(step["uses"]), step["uses"]


def test_backend_enables_the_sandbox_before_any_product_import() -> None:
    body = ast.parse(BACKEND.read_text(encoding="utf-8")).body
    sandbox_lines = [
        node.lineno
        for node in body
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "enable_smoke_sandbox"
    ]
    product_lines = [
        node.lineno
        for node in body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] in {"src", "memory_server"}
    ]
    assert len(sandbox_lines) == 1
    assert product_lines
    assert sandbox_lines[0] < min(product_lines)


def test_backend_keeps_the_network_guard_on() -> None:
    assert "allow_external_network" not in BACKEND.read_text(encoding="utf-8")


def test_backend_is_not_picked_up_by_automated_runners() -> None:
    # pytest 只收 test_*.py；run_all_smoke_tests 只收 smoke_*.py
    assert not BACKEND.name.startswith(("test_", "smoke_"))

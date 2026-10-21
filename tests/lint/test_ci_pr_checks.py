"""PR checks: what CI enforces and what CONTRIBUTING tells contributors to run.

Pins the CI steps so a later edit cannot quietly drop them, and keeps the
CONTRIBUTING "PR 前必做" list honest about which checks CI runs. The product source
roots match the subprocess ratchet in ``tests/lint/test_security_kernel_ratchet.py``.
Ruff runs with ``--no-cache`` so the check reads the tree and writes nothing.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/native-tool-contracts.yml"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
CI_LABEL = "（CI 會跑）"
LOCAL_LABEL = "（只在本機）"
RUFF_CONFIG = "ruff.toml"
PRODUCT_SOURCE_ROOTS = (
    "ToukaProxy/src",
    "ToukaMemory/src",
    "ToukaHeartbeat",
    "ToukaDispatch",
)
IDE_CI_COMMANDS = (
    "npm run install:controlled",
    "npm test",
    "npm run typecheck",
    "npm run compile",
)


def test_product_sources_have_no_pyflakes_errors() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "F",
            "--config",
            RUFF_CONFIG,
            "--output-format",
            "concise",
            *PRODUCT_SOURCE_ROOTS,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _run_steps(job: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (working-directory, command) for each run step of a job."""
    default_dir = job.get("defaults", {}).get("run", {}).get("working-directory", ".")
    return [
        (step.get("working-directory", default_dir), " ".join(step["run"].split()))
        for step in job["steps"]
        if "run" in step
    ]


def _assert_blocking(name: str, job: dict[str, Any]) -> None:
    """A check only counts when it cannot be skipped or ignored."""
    assert "if" not in job and "continue-on-error" not in job, name
    for step in job["steps"]:
        assert "if" not in step and "continue-on-error" not in step, (name, step)


def test_ci_runs_locked_pyflakes_over_every_product_source_root() -> None:
    found = []
    for name, job in _jobs().items():
        runs = [run for _, run in _run_steps(job)]
        for index, run in enumerate(runs):
            argv = run.split()
            if "ruff" in argv and "check" in argv and "F" in argv:
                found.append((name, job, runs, index, argv))
    assert len(found) == 1, "CI must run exactly one `ruff check --select F` step"
    name, job, runs, index, argv = found[0]
    _assert_blocking(name, job)
    assert argv[:3] == ["uv", "run", "--no-sync"], "use the ruff locked in uv.lock"
    assert any(run.startswith("uv sync --locked") for run in runs[:index])
    assert argv[argv.index("--select") + 1] == "F"
    assert argv[argv.index("--config") + 1] == RUFF_CONFIG
    assert set(PRODUCT_SOURCE_ROOTS) <= set(argv)
    loosening = {"--exit-zero", "--fix", "--exclude", "--extend-exclude", "--ignore", "||"}
    assert not loosening & set(argv)


def test_ci_runs_toukaide_checks_serially_on_the_pinned_node() -> None:
    engines = json.loads((ROOT / "ToukaIDE/package.json").read_text(encoding="utf-8"))
    runtime = engines["devEngines"]["runtime"]["version"]
    package_manager = engines["devEngines"]["packageManager"]["version"]
    found = []
    for name, job in _jobs().items():
        ide_runs = [
            run.replace("npm run test", "npm test")
            for directory, run in _run_steps(job)
            if directory == "ToukaIDE"
        ]
        if all(command in ide_runs for command in IDE_CI_COMMANDS):
            found.append((name, job, ide_runs))
    assert len(found) == 1, f"CI must run {IDE_CI_COMMANDS} in ToukaIDE in one job"
    name, job, ide_runs = found[0]
    _assert_blocking(name, job)
    positions = [ide_runs.index(command) for command in IDE_CI_COMMANDS]
    assert positions == sorted(positions), "install, test, typecheck, compile run in order"
    node_versions = [
        step["with"]["node-version"]
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/setup-node@")
    ]
    assert node_versions == [runtime]
    assert any(
        f"npm@{package_manager}" in run and "--ignore-scripts" in run
        for _, run in _run_steps(job)
    )


def _pr_section() -> str:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    return text.split("### PR 前必做", 1)[1].split("\n### ", 1)[0]


def _pr_checklist() -> list[tuple[str, str, list[str]]]:
    """Return (label, header, commands) for each numbered item of CONTRIBUTING's PR checks."""
    block = _pr_section().split("```bash", 1)[1].split("```", 1)[0]
    items: list[tuple[str, str, list[str]]] = []
    for line in block.splitlines():
        stripped = line.strip()
        if re.match(r"# \d+\. ", stripped):
            labels = [label for label in (CI_LABEL, LOCAL_LABEL) if label in stripped]
            assert len(labels) == 1, f"label each item with {CI_LABEL} or {LOCAL_LABEL}: {line}"
            items.append((labels[0], stripped, []))
        elif stripped and not stripped.startswith("#"):
            assert items, f"command before the first numbered item: {line}"
            items[-1][2].append(" ".join(stripped.split("#", 1)[0].split()))
    return items


def _item_running(items: list[tuple[str, str, list[str]]], *needles: str) -> str:
    """Return the label of the single item with a command containing every needle."""
    matches = [
        label
        for label, _, commands in items
        if any(all(needle in command for needle in needles) for command in commands)
    ]
    assert len(matches) == 1, f"expected one PR check running {needles}, found {len(matches)}"
    return matches[0]


def test_contributing_labels_every_pr_check_as_ci_or_local() -> None:
    items = _pr_checklist()
    assert len(items) >= 7
    for _, header, commands in items:
        assert commands, f"item has no command: {header}"
    for stale in ("npx vitest", "npx eslint"):
        assert stale not in _pr_section(), f"{stale} does not match how the project runs it"


def test_contributing_keeps_full_ruff_and_eslint_in_pr_checks() -> None:
    items = _pr_checklist()
    full_ruff = [
        label
        for label, _, commands in items
        for command in commands
        if command.startswith("ruff check") and "--select" not in command
    ]
    assert full_ruff and set(full_ruff) == {LOCAL_LABEL}, "full ruff stays, run locally"
    assert _item_running(items, "scripts/guard_eslint_ratchet.py") == LOCAL_LABEL


def test_contributing_ci_labels_match_what_ci_runs() -> None:
    items = _pr_checklist()
    assert _item_running(items, "ruff check --select F", *PRODUCT_SOURCE_ROOTS) == CI_LABEL
    assert _item_running(items, "-m pytest tests/") == CI_LABEL
    assert _item_running(items, "cd ToukaChat", "npm run test") == CI_LABEL
    assert _item_running(items, "cd ToukaIDE", *IDE_CI_COMMANDS) == CI_LABEL
    assert _item_running(items, "cd ToukaIDE", "npm run package") == LOCAL_LABEL
    assert _item_running(items, "scripts/guard_all.py") == LOCAL_LABEL
    frontend = _run_steps(_jobs()["frontend"])
    assert ("ToukaChat", "npm run test") in frontend

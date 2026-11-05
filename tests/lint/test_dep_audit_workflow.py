"""每日依賴檢查的 workflow（ADR-0299）：權限分開、回報只從 main 跑、action 釘 SHA。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/dep-audit.yml"
PINNED_ACTION = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_runs_on_a_schedule_by_hand_and_on_its_own_changes() -> None:
    triggers = _workflow()[True]  # YAML 把 on 讀成 True
    assert triggers["schedule"]
    assert "simulate" in triggers["workflow_dispatch"]["inputs"]
    paths = triggers["pull_request"]["paths"]
    assert ".github/workflows/dep-audit.yml" in paths
    assert "scripts/dep_audit_report.py" in paths


def test_the_default_token_can_only_read() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}


def test_the_scan_job_only_reads_and_runs_both_guards() -> None:
    scan = _workflow()["jobs"]["scan"]
    assert scan["permissions"] == {"contents": "read"}
    text = _steps_text(scan)
    assert "scripts/guard_python_cve.py" in text
    assert "scripts/guard_node_cve.py" in text
    assert "collect-exceptions" in text
    assert "['main','dev']" in str(scan["strategy"]["matrix"]["branch"]).replace('"', "'")


def test_the_scan_job_keeps_its_results_even_when_a_step_fails() -> None:
    uploads = [
        step
        for step in _workflow()["jobs"]["scan"]["steps"]
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "always()"


def test_only_the_report_job_can_write_issues_and_it_runs_no_branch_code() -> None:
    report = _workflow()["jobs"]["report"]
    assert report["permissions"] == {"contents": "read", "issues": "write"}
    text = _steps_text(report)
    assert "python3 -I scripts/dep_audit_report.py report" in text
    for banned in ("uv ", "npm ", "guard_"):
        assert banned not in text
    assert "--now" in text
    checkouts = [step for step in report["steps"] if "checkout" in str(step.get("uses", ""))]
    assert len(checkouts) == 1
    assert "ref" not in checkouts[0].get("with", {})
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_the_report_job_runs_only_from_main_and_also_after_a_failed_scan() -> None:
    condition = _workflow()["jobs"]["report"]["if"]
    assert "always()" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    assert "github.event_name != 'pull_request'" in condition


def test_every_action_is_pinned_to_a_commit() -> None:
    for name, job in _workflow()["jobs"].items():
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is not None:
                assert PINNED_ACTION.fullmatch(uses), f"{name}: {uses}"


def test_checkouts_do_not_keep_credentials() -> None:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if "actions/checkout" in str(step.get("uses", "")):
                assert step["with"]["persist-credentials"] is False


def test_workflow_inputs_never_reach_a_shell_directly() -> None:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            run = str(step.get("run", ""))
            assert "${{" not in run, run

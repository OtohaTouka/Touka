"""暫停期間每月巡查的通知流程：只能手動觸發、參數沒有自由文字、只有開 issue 的權限。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/patrol-notify.yml"
CLEAR_TITLE = "每月巡查完成"
ATTENTION_TITLE = "每月巡查：有事要看"
FAKE_GH = """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
if [ "$1 $2" = "issue list" ]; then
  if [ -n "${GH_EXISTING_TITLE:-}" ]; then printf '%s\n' "$GH_EXISTING_TITLE"; fi
  printf '%s\n' "另一張不相關的 issue"
elif [ "$1 $2" = "issue create" ]; then
  echo "https://github.com/example/repo/issues/7"
fi
"""


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _workflow()["jobs"]["notify"]


def _script() -> str:
    steps = [step for step in _job()["steps"] if "run" in step]
    assert len(steps) == 1
    return str(steps[0]["run"])


def test_only_runs_by_hand_so_github_cannot_disable_it() -> None:
    triggers = _workflow()[True]  # YAML 把 on 讀成 True
    assert set(triggers) == {"workflow_dispatch"}


def test_the_only_input_is_a_fixed_choice_so_no_details_reach_a_public_issue() -> None:
    inputs = _workflow()[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"result"}
    assert inputs["result"]["type"] == "choice"
    assert inputs["result"]["required"] is True
    assert inputs["result"]["options"] == ["clear", "needs-attention"]


def test_can_only_write_issues_and_runs_no_repository_code() -> None:
    assert _workflow()["permissions"] == {}
    job = _job()
    assert job["permissions"] == {"issues": "write"}
    assert all("uses" not in step for step in job["steps"])


def test_only_runs_from_main() -> None:
    assert _job()["if"] == "github.ref == 'refs/heads/main'"


def test_the_input_reaches_the_script_only_through_the_environment() -> None:
    assert "${{" not in _script()
    assert _job()["env"]["PATROL_RESULT"] == "${{ inputs.result }}"


def _run(tmp_path: Path, result: str, existing: str = "") -> tuple[int, list[str]]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("這條測試要 bash")
    fake = tmp_path / "bin"
    fake.mkdir()
    gh = fake / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    script = tmp_path / "notify.sh"
    script.write_text("set -eu\n" + _script(), encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_EXISTING_TITLE": existing,
        "OWNER": "example-owner",
        "PATROL_RESULT": result,
    }
    done = subprocess.run(  # noqa: S603 - resolved bash, fixed argv, no shell
        [bash, str(script)], env=env, capture_output=True, check=False
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, calls


def test_a_clear_month_opens_a_notice_for_the_owner_and_closes_it(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, "clear")
    assert code == 0
    assert len(calls) == 2
    assert calls[0].startswith("issue create")
    assert CLEAR_TITLE in calls[0]
    assert "--assignee example-owner" in calls[0]
    assert calls[1] == "issue close https://github.com/example/repo/issues/7"


def test_needs_attention_opens_one_issue_without_any_details(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, "needs-attention")
    assert code == 0
    created = [call for call in calls if call.startswith("issue create")]
    assert len(created) == 1
    assert ATTENTION_TITLE in created[0]
    assert "--assignee example-owner" in created[0]
    assert not [call for call in calls if call.startswith("issue close")]


def test_needs_attention_does_not_open_a_second_issue(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, "needs-attention", existing=ATTENTION_TITLE)
    assert code == 0
    assert not [call for call in calls if call.startswith("issue create")]


def test_an_unknown_result_fails_and_opens_nothing(tmp_path: Path) -> None:
    code, calls = _run(tmp_path, "anything-else")
    assert code != 0
    assert calls == []

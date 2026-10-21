# ruff: noqa: S603
"""End-to-end tests for explicit pytest basetemp policy sources."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from src.services.evolution_security import EvolutionPathGuard

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_mini_pytest(
    project: Path,
    *,
    args: list[str] | None = None,
    pytest_addopts: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = EvolutionPathGuard.get_clean_env()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT / "scripts"), env.get("PYTHONPATH", "")) if part
    )
    if pytest_addopts is None:
        env.pop("PYTEST_ADDOPTS", None)
    else:
        env["PYTEST_ADDOPTS"] = pytest_addopts
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_basetemp_plugin",
        "-q",
        "test_sample.py",
        *(args or []),
    ]
    return subprocess.run(
        command,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture
def mini_pytest_project(tmp_path: Path) -> Path:
    project = tmp_path / "mini-project"
    project.mkdir()
    (project / "test_sample.py").write_text(
        "def test_sample():\n    assert True\n",
        encoding="utf-8",
    )
    return project


def test_cli_basetemp_inside_project_is_rejected(mini_pytest_project: Path) -> None:
    result = _run_mini_pytest(
        mini_pytest_project,
        args=[f"--basetemp={mini_pytest_project / '.pytest_cache' / 'cli'}"],
    )

    assert result.returncode != 0
    assert "PYTEST_BASETEMP_REPO_LOCAL" in result.stderr


def test_pytest_addopts_basetemp_inside_project_is_rejected(
    mini_pytest_project: Path,
) -> None:
    result = _run_mini_pytest(
        mini_pytest_project,
        pytest_addopts=(f"--basetemp={(mini_pytest_project / '.pytest_cache' / 'env').as_posix()}"),
    )

    assert result.returncode != 0
    assert "PYTEST_BASETEMP_REPO_LOCAL" in result.stderr


def test_pytest_config_basetemp_inside_project_is_rejected(
    mini_pytest_project: Path,
) -> None:
    (mini_pytest_project / "pytest.ini").write_text(
        "[pytest]\n"
        "addopts = --basetemp="
        f"{(mini_pytest_project / '.pytest_cache' / 'ini').as_posix()}\n",
        encoding="utf-8",
    )

    result = _run_mini_pytest(mini_pytest_project)

    assert result.returncode != 0
    assert "PYTEST_BASETEMP_REPO_LOCAL" in result.stderr


def test_external_basetemp_is_accepted(
    mini_pytest_project: Path,
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-basetemp"

    result = _run_mini_pytest(
        mini_pytest_project,
        args=[f"--basetemp={external}"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout

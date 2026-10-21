"""Every normally ending pytest process removes its own session sandbox.

Each case runs a child pytest whose temp folder is this test's tmp_path, then
checks that no touka_test_session_* folder is left behind.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.services.evolution_security import EvolutionPathGuard

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = "tests/integration/sandbox_child_probe.py"
CHILD_TIMEOUT_SECONDS = 300


def _run_child_pytest(
    tmp_path: Path, *args: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    temp_root = tmp_path / "child-temp"
    temp_root.mkdir()
    env = EvolutionPathGuard.get_clean_env()
    env.pop("PYTEST_ADDOPTS", None)
    for name in ("TMP", "TEMP", "TMPDIR"):
        env[name] = str(temp_root)
    command = [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:randomly"]
    result = subprocess.run(  # noqa: S603 — this interpreter's pytest, fixed argv, clean env, timeout.
        [*command, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    return result, temp_root


def _leftover_sandboxes(temp_root: Path) -> list[str]:
    return sorted(path.name for path in temp_root.glob("touka_test_session_*"))


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + result.stderr)[-3000:]


def test_collect_only_leaves_no_session_sandbox(tmp_path):
    result, temp_root = _run_child_pytest(tmp_path, "--collect-only", "-q", PROBE)

    assert result.returncode == 0, _tail(result)
    assert _leftover_sandboxes(temp_root) == [], _tail(result)


def test_rejected_repo_basetemp_leaves_no_session_sandbox(tmp_path):
    """--basetemp is rejected in pytest_configure, before any session hook runs."""
    rejected = REPO_ROOT / ".pytest_cache" / "sandbox-residue-e2e"
    result, temp_root = _run_child_pytest(tmp_path, f"--basetemp={rejected}", PROBE)

    assert result.returncode != 0, _tail(result)
    assert "PYTEST_BASETEMP_REPO_LOCAL" in result.stderr, _tail(result)
    assert _leftover_sandboxes(temp_root) == [], _tail(result)


def test_open_qdrant_client_leaves_no_session_sandbox(tmp_path):
    """Qdrant local mode locks vault/_qdrant_db/.lock on Windows until the client closes."""
    result, temp_root = _run_child_pytest(tmp_path, "-q", PROBE)

    assert result.returncode == 0, _tail(result)
    assert "1 passed" in result.stdout, _tail(result)
    assert _leftover_sandboxes(temp_root) == [], _tail(result)

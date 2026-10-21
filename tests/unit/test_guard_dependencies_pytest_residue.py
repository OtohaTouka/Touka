"""TDD: Guard #16 must fail before dependency sandbox work on unsafe pytest residue."""

from __future__ import annotations

from typing import TYPE_CHECKING

import guard_dependencies as guard
import pytest
from pytest_residue_safety import PytestResidueViolation

if TYPE_CHECKING:
    from pathlib import Path


def test_guard_fails_before_cache_creation_or_dependency_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_dir = tmp_path / "dependency-cache"
    dependency_called = False
    resolved_roots: list[Path] = []
    violation = PytestResidueViolation(
        relative_path=".pytest_cache/denied",
        code="directory_unreadable",
    )

    def dependency_probe(project: dict, **_: object) -> bool:
        nonlocal dependency_called
        dependency_called = True
        return True

    # No override: the location is resolved (git) only after the residue preflight (ADR-0240).
    monkeypatch.setattr(guard, "CACHE_DIR", None)
    monkeypatch.setattr(
        guard, "resolve_cache_root", lambda root: resolved_roots.append(root) or cache_dir
    )
    monkeypatch.setattr(guard, "scan_pytest_residue", lambda root: (violation,))
    monkeypatch.setattr(guard, "check_project_dependencies", dependency_probe)

    with pytest.raises(SystemExit) as exc_info:
        guard.main()

    assert exc_info.value.code == 1
    assert not dependency_called
    assert resolved_roots == []
    assert not cache_dir.exists()
    output = capsys.readouterr().out
    assert ".pytest_cache/denied" in output
    assert str(tmp_path.resolve()) not in output


def test_guard_continues_after_clean_residue_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = tmp_path / "dependency-cache"
    calls: list[str] = []
    target = {"name": "example"}

    monkeypatch.setattr(guard, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(guard, "TARGET_PROJECTS", [target])
    monkeypatch.setattr(guard, "scan_pytest_residue", lambda root: ())
    monkeypatch.setattr(
        guard,
        "check_project_dependencies",
        lambda project, **_: calls.append(project["name"]) is None or True,
    )

    with pytest.raises(SystemExit) as exc_info:
        guard.main()

    assert exc_info.value.code == 0
    assert calls == ["example"]
    assert cache_dir.is_dir()

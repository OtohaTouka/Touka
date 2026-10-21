# ruff: noqa: S603, S607
"""TDD: repository-local pytest residue and explicit basetemp safety."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import pytest_residue_safety as safety
from pytest_residue_safety import (
    BasetempPolicyError,
    scan_pytest_residue,
    validate_explicit_basetemp,
)

_NO_CONTENT_READ_ERROR = "pytest residue scanner must not read file contents"
_HARD_LINK_OPEN_ERROR = "hard link must be rejected before open"
_PROTECTED_ENUMERATION_ERROR = "protected data tree must never be enumerated"
_SIMULATED_DENIAL = "simulated denial"
_SIMULATED_RESOLUTION_FAILURE = "simulated resolution failure"


def _codes(violations: tuple[safety.PytestResidueViolation, ...]) -> set[str]:
    return {violation.code for violation in violations}


def _paths(violations: tuple[safety.PytestResidueViolation, ...]) -> set[str]:
    return {violation.relative_path for violation in violations}


def test_basetemp_rejects_repository_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_REPO_LOCAL"):
        validate_explicit_basetemp(repo, repo_root=repo, cwd=repo)


def test_basetemp_rejects_repository_descendant_absolute(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_REPO_LOCAL"):
        validate_explicit_basetemp(repo / ".pytest_cache" / "run", repo_root=repo, cwd=repo)


def test_basetemp_rejects_repository_descendant_relative(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_REPO_LOCAL"):
        validate_explicit_basetemp(".pytest_cache/run", repo_root=repo, cwd=repo)


def test_basetemp_rejects_dotdot_path_resolving_into_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested_cwd = repo / "nested"
    nested_cwd.mkdir(parents=True)

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_REPO_LOCAL"):
        validate_explicit_basetemp(
            "../.pytest_cache/run",
            repo_root=repo,
            cwd=nested_cwd,
        )


def test_basetemp_accepts_external_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    external = tmp_path / "external" / "pytest"
    repo.mkdir()

    result = validate_explicit_basetemp(external, repo_root=repo, cwd=repo)

    assert result == external.resolve(strict=False)


def test_basetemp_accepts_sibling_prefix_directory(tmp_path: Path) -> None:
    repo = tmp_path / "Touka"
    sibling = tmp_path / "Touka-other" / "pytest"
    repo.mkdir()

    result = validate_explicit_basetemp(sibling, repo_root=repo, cwd=repo)

    assert result == sibling.resolve(strict=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_basetemp_rejects_mixed_case_repository_path(tmp_path: Path) -> None:
    repo = tmp_path / "MixedCaseRepo"
    repo.mkdir()
    mixed_case = Path(str(repo).swapcase()) / "pytest"

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_REPO_LOCAL"):
        validate_explicit_basetemp(mixed_case, repo_root=repo, cwd=repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_basetemp_rejects_drive_relative_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    drive_relative = f"{repo.drive}pytest-relative"

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_UNCERTAIN"):
        validate_explicit_basetemp(drive_relative, repo_root=repo, cwd=repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_basetemp_rejects_unc_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_UNCERTAIN"):
        validate_explicit_basetemp(
            r"\\untrusted-server\share\pytest",
            repo_root=repo,
            cwd=repo,
        )


def test_basetemp_fails_closed_when_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_resolution(path: Path) -> Path:
        raise OSError(_SIMULATED_RESOLUTION_FAILURE)

    monkeypatch.setattr(safety, "_resolve_without_redirects", fail_resolution)

    with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_UNCERTAIN"):
        validate_explicit_basetemp(tmp_path / "external", repo_root=repo, cwd=repo)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_basetemp_fails_closed_for_external_junction_into_repository(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    external = tmp_path / "external"
    junction = external / "repo-redirect"
    repo.mkdir()
    external.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    try:
        with pytest.raises(BasetempPolicyError, match="PYTEST_BASETEMP_UNCERTAIN"):
            validate_explicit_basetemp(
                junction / ".pytest_cache" / "run",
                repo_root=repo,
                cwd=external,
            )
    finally:
        junction.rmdir()


def test_missing_pytest_cache_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert scan_pytest_residue(repo) == ()


def test_readable_nested_pytest_cache_passes_without_reading_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    cache_file = repo / ".pytest_cache" / "nested" / "result.txt"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("must-not-be-read", encoding="utf-8")
    original_open = Path.open

    class NoReadHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._handle.__exit__(exc_type, exc, traceback)

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, *args, **kwargs):
            raise AssertionError(_NO_CONTENT_READ_ERROR)

    def guarded_open(path: Path, *args, **kwargs):
        return NoReadHandle(original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", guarded_open)

    assert scan_pytest_residue(repo) == ()


def test_directory_permission_error_is_relative_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    denied = repo / ".pytest_cache" / "denied"
    denied.mkdir(parents=True)
    original_scandir = safety.os.scandir

    def guarded_scandir(path: Path):
        if Path(path) == denied:
            raise PermissionError(_SIMULATED_DENIAL)
        return original_scandir(path)

    monkeypatch.setattr(safety.os, "scandir", guarded_scandir)

    violations = scan_pytest_residue(repo)

    assert _codes(violations) == {"directory_unreadable"}
    assert _paths(violations) == {".pytest_cache/denied"}


def test_file_permission_error_is_relative_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    denied = repo / ".pytest_cache" / "denied.txt"
    denied.parent.mkdir(parents=True)
    denied.write_text("secret", encoding="utf-8")
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == denied:
            raise PermissionError(_SIMULATED_DENIAL)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    violations = scan_pytest_residue(repo)

    assert _codes(violations) == {"file_unreadable"}
    assert _paths(violations) == {".pytest_cache/denied.txt"}


def test_hard_link_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    cache = repo / ".pytest_cache"
    outside = tmp_path / "outside.txt"
    linked = cache / "linked.txt"
    cache.mkdir(parents=True)
    outside.write_text("sensitive", encoding="utf-8")
    os.link(outside, linked)
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == linked:
            raise AssertionError(_HARD_LINK_OPEN_ERROR)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    violations = scan_pytest_residue(repo)

    assert _codes(violations) == {"hard_link"}
    assert _paths(violations) == {".pytest_cache/linked.txt"}


def test_file_identity_change_during_probe_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    cache_file = repo / ".pytest_cache" / "race.txt"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("stable", encoding="utf-8")
    original_fstat = safety.os.fstat

    def changed_fstat(fd: int):
        result = original_fstat(fd)
        values = list(result)
        values[1] = result.st_ino + 1
        return os.stat_result(values)

    monkeypatch.setattr(safety.os, "fstat", changed_fstat)

    violations = scan_pytest_residue(repo)

    assert _codes(violations) == {"entry_changed"}
    assert _paths(violations) == {".pytest_cache/race.txt"}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
@pytest.mark.parametrize("protected_relative", ["data", "ToukaProxy/data"])
def test_windows_junction_to_protected_tree_is_not_traversed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_relative: str,
) -> None:
    repo = tmp_path / "repo"
    cache = repo / ".pytest_cache"
    protected = repo / protected_relative
    junction = cache / "redirect"
    protected.mkdir(parents=True)
    cache.mkdir(parents=True)
    (protected / "sentinel.txt").write_text("owner-data", encoding="utf-8")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(protected)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    original_scandir = safety.os.scandir
    scanned: list[Path] = []

    def guarded_scandir(path: Path):
        candidate = Path(path)
        scanned.append(candidate)
        if candidate == protected:
            raise AssertionError(_PROTECTED_ENUMERATION_ERROR)
        return original_scandir(path)

    monkeypatch.setattr(safety.os, "scandir", guarded_scandir)
    try:
        violations = scan_pytest_residue(repo)
    finally:
        junction.rmdir()

    assert _codes(violations) == {"redirect_entry"}
    assert _paths(violations) == {".pytest_cache/redirect"}
    assert protected not in scanned

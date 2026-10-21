"""Fail-closed safety helpers for pytest residue and ``--basetemp`` paths."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_REDIRECT_COMPONENT_ERROR = "path contains a redirect component"


class BasetempPolicyError(ValueError):
    """Raised when an explicit pytest basetemp cannot be proven external."""


@dataclass(frozen=True, slots=True)
class PytestResidueViolation:
    """A relative, content-free diagnostic emitted by the residue scanner."""

    relative_path: str
    code: str


def _is_redirect(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0) or 0
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _reject_existing_redirects(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if _is_redirect(metadata):
            raise OSError(_REDIRECT_COMPONENT_ERROR)


def _resolve_without_redirects(path: Path) -> Path:
    absolute = path.absolute()
    _reject_existing_redirects(absolute)
    resolved = absolute.resolve(strict=False)
    _reject_existing_redirects(resolved)
    return resolved


def _has_unc_or_drive_relative_syntax(raw_value: str) -> bool:
    if os.name != "nt":
        return False
    if raw_value.startswith(("\\\\", "//")):
        return True
    parsed = PureWindowsPath(raw_value)
    return bool(parsed.drive) and not bool(parsed.root)


def validate_explicit_basetemp(
    value: str | os.PathLike[str],
    *,
    repo_root: Path,
    cwd: Path,
) -> Path:
    """Return a proven external basetemp or reject uncertain/repository paths."""
    raw_value = os.fspath(value)
    if not raw_value.strip() or _has_unc_or_drive_relative_syntax(raw_value):
        raise BasetempPolicyError("PYTEST_BASETEMP_UNCERTAIN")

    raw_path = Path(raw_value)
    candidate = raw_path if raw_path.is_absolute() else cwd / raw_path
    try:
        resolved_repo = _resolve_without_redirects(repo_root)
        resolved_candidate = _resolve_without_redirects(candidate)
    except OSError as exc:
        raise BasetempPolicyError("PYTEST_BASETEMP_UNCERTAIN") from exc

    if resolved_candidate == resolved_repo or resolved_repo in resolved_candidate.parents:
        raise BasetempPolicyError("PYTEST_BASETEMP_REPO_LOCAL")
    return resolved_candidate


def _relative_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return ".pytest_cache/<unavailable>"


def _probe_regular_file(
    path: Path,
    metadata: os.stat_result,
    repo_root: Path,
) -> PytestResidueViolation | None:
    relative_path = _relative_path(path, repo_root)
    if metadata.st_nlink != 1:
        return PytestResidueViolation(relative_path, "hard_link")

    try:
        with path.open("rb", buffering=0) as handle:
            opened_metadata = os.fstat(handle.fileno())
    except OSError:
        return PytestResidueViolation(relative_path, "file_unreadable")

    if _is_redirect(opened_metadata) or not _same_identity(metadata, opened_metadata):
        return PytestResidueViolation(relative_path, "entry_changed")
    if opened_metadata.st_nlink != 1:
        return PytestResidueViolation(relative_path, "hard_link")
    return None


def _scan_directory(
    directory: Path,
    expected_metadata: os.stat_result,
    repo_root: Path,
) -> list[PytestResidueViolation]:
    relative_path = _relative_path(directory, repo_root)
    try:
        before_metadata = directory.lstat()
        if _is_redirect(before_metadata):
            return [PytestResidueViolation(relative_path, "redirect_entry")]
        if not _same_identity(expected_metadata, before_metadata):
            return [PytestResidueViolation(relative_path, "entry_changed")]
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        after_metadata = directory.lstat()
    except OSError:
        return [PytestResidueViolation(relative_path, "directory_unreadable")]

    if _is_redirect(after_metadata) or not _same_identity(before_metadata, after_metadata):
        return [PytestResidueViolation(relative_path, "entry_changed")]

    violations: list[PytestResidueViolation] = []
    for entry in entries:
        path = directory / entry.name
        entry_relative_path = _relative_path(path, repo_root)
        try:
            metadata = path.lstat()
        except OSError:
            violations.append(PytestResidueViolation(entry_relative_path, "entry_unreadable"))
            continue

        if _is_redirect(metadata):
            violations.append(PytestResidueViolation(entry_relative_path, "redirect_entry"))
        elif stat.S_ISDIR(metadata.st_mode):
            violations.extend(_scan_directory(path, metadata, repo_root))
        elif stat.S_ISREG(metadata.st_mode):
            violation = _probe_regular_file(path, metadata, repo_root)
            if violation is not None:
                violations.append(violation)
        else:
            violations.append(PytestResidueViolation(entry_relative_path, "unsupported_entry"))
    return violations


def scan_pytest_residue(repo_root: Path) -> tuple[PytestResidueViolation, ...]:
    """Probe only ``.pytest_cache`` metadata/readability without reading contents."""
    canonical_root = repo_root.absolute()
    cache_root = canonical_root / ".pytest_cache"
    try:
        metadata = cache_root.lstat()
    except FileNotFoundError:
        return ()
    except OSError:
        return (PytestResidueViolation(".pytest_cache", "directory_unreadable"),)

    if _is_redirect(metadata):
        return (PytestResidueViolation(".pytest_cache", "redirect_entry"),)
    if not stat.S_ISDIR(metadata.st_mode):
        return (PytestResidueViolation(".pytest_cache", "unsupported_entry"),)
    return tuple(_scan_directory(cache_root, metadata, canonical_root))

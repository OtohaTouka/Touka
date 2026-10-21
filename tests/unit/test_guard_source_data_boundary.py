"""Source guards must prune protected data before inspecting filesystem entries."""

from __future__ import annotations

import importlib.util
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from src.services import execution_broker

if TYPE_CHECKING:
    from collections.abc import Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
SCANNERS = ("lint_ssot", "guard_noqa_registry", "guard_adr_integrity")
GIT = shutil.which("git")


def _scanner(name: str) -> Callable[[Path], Any]:
    spec = importlib.util.spec_from_file_location(f"boundary_{name}", SCRIPTS_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if name == "lint_ssot":
        return lambda path: module.run_guard(str(path))
    if name == "guard_noqa_registry":
        return module.scan_codebase
    return module.scan_references


def _source(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "example.py"
    source.write_text(
        'import time\ntime.time()  # noqa: SYNTHETIC_RULE\nnote = "' + "ADR-" + '9999"\n',
        encoding="utf-8",
    )
    return source


def _deny_data_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    for operation in ("stat", "lstat", "iterdir", "open", "read_text"):
        original = getattr(Path, operation)

        def guarded(
            path: Path,
            *args: Any,
            original: Callable[..., Any] = original,
            operation: str = operation,
            **kwargs: Any,
        ) -> Any:
            if any(part.casefold() == "data" for part in path.parts):
                pytest.fail(f"Forbidden {operation} on synthetic protected entry: {path}")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, operation, guarded)


@pytest.mark.parametrize("scanner_name", SCANNERS)
@pytest.mark.parametrize("protected_name", ["data", "DaTa"])
def test_scanners_prune_data_before_stat_or_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scanner_name: str,
    protected_name: str,
) -> None:
    scan = _scanner(scanner_name)
    safe = tmp_path / "src"
    _source(safe)
    expected = scan(safe / "example.py") if scanner_name == "lint_ssot" else scan(safe)
    assert expected, "Positive control must prove regular source is still inspected"
    _source(tmp_path / protected_name)
    _source(safe / "nested" / protected_name)
    _deny_data_operations(monkeypatch)
    actual = scan(tmp_path)
    if scanner_name == "guard_adr_integrity":
        assert set(actual) == set(expected)
        assert actual["9999"] == {str(Path("src") / "example.py")}
    else:
        assert actual == expected


@pytest.mark.parametrize("scanner_name", SCANNERS)
@pytest.mark.parametrize("child_target", [False, True])
def test_direct_data_target_has_no_filesystem_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scanner_name: str,
    child_target: bool,
) -> None:
    scan = _scanner(scanner_name)
    protected = tmp_path / "DaTa"
    source = _source(protected)
    _deny_data_operations(monkeypatch)
    assert not scan(source if child_target else protected)


@pytest.mark.parametrize("scanner_name", SCANNERS)
@pytest.mark.parametrize("link_kind", ["symlink", "junction", "file_reparse"])
def test_scanners_do_not_follow_alias_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scanner_name: str,
    link_kind: str,
) -> None:
    scan = _scanner(scanner_name)
    _source(tmp_path / "src")
    alias = tmp_path / ("alias.py" if link_kind == "file_reparse" else "alias")
    if link_kind == "file_reparse":
        alias.write_text("unreadable synthetic alias", encoding="utf-8")
    else:
        _source(alias)
    expected = scan(tmp_path / "src")
    original_stat = Path.stat
    metadata_checks: list[Path] = []

    def guarded_stat(path: Path, *, follow_symlinks: bool = True) -> Any:
        if path == alias:
            assert not follow_symlinks, "Alias target metadata must never be followed"
            metadata_checks.append(path)
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if link_kind == "symlink" else stat.S_IFDIR,
                st_file_attributes=0
                if link_kind == "symlink"
                else stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        if alias in path.parents:
            pytest.fail(f"Alias descendants must not be inspected: {path}")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    for operation in ("iterdir", "open", "read_text"):
        original = getattr(Path, operation)

        def guarded_io(
            path: Path, *args: Any, original: Callable[..., Any] = original, **kwargs: Any
        ) -> Any:
            if path == alias or alias in path.parents:
                pytest.fail(f"Alias must not be opened: {path}")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, operation, guarded_io)
    actual = scan(tmp_path)
    if scanner_name == "guard_adr_integrity":
        assert set(actual) == set(expected)
    else:
        assert actual == expected
    assert metadata_checks
    assert not scan(alias)
    if scanner_name == "lint_ssot" and link_kind != "file_reparse":
        assert not scan(alias / "example.py")


# --- ToukaDev local workspace (.toukadev/: lessons, worktrees, tmp) ---------------------
# The three repo-wide scanners prune it by directory *name* only. Path-segment pruning
# (how `data` is handled) would make a scan rooted in `.toukadev/worktrees/<unit>` inspect
# zero files and pass silently, so the root-inside-workspace cases below must keep reporting.


def _reported(scanner_name: str, result: Any, root: Path) -> tuple[int, set[str]]:
    """Normalize a scan to (synthetic findings, reported files relative to root).

    The noqa guard only returns rule counts, so its file set stays empty and the
    count alone shows how many synthetic files were inspected.
    """
    if scanner_name == "lint_ssot":
        return len(result), {path.relative_to(root).as_posix() for path, _ in result}
    if scanner_name == "guard_noqa_registry":
        return result.get("SYNTHETIC_RULE", 0), set()
    files = {Path(name).as_posix() for name in result.get("9999", set())}
    return len(files), files


def _expected(scanner_name: str, *files: str) -> tuple[int, set[str]]:
    return len(files), set() if scanner_name == "guard_noqa_registry" else set(files)


def _deny_operations_under(monkeypatch: pytest.MonkeyPatch, *pruned: Path) -> None:
    """Fail on any filesystem operation on or below the given synthetic roots.

    Matching is anchored to these roots, not to any `.toukadev` path segment, because
    pytest's own basetemp may legitimately live under a real `.toukadev/tmp`.
    """
    for operation in ("stat", "lstat", "iterdir", "open", "read_text"):
        original = getattr(Path, operation)

        def guarded(
            path: Path,
            *args: Any,
            original: Callable[..., Any] = original,
            operation: str = operation,
            **kwargs: Any,
        ) -> Any:
            if any(path == root or root in path.parents for root in pruned):
                pytest.fail(f"Forbidden {operation} inside pruned ToukaDev workspace: {path}")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, operation, guarded)


@pytest.mark.parametrize("scanner_name", SCANNERS)
@pytest.mark.parametrize("workspace_name", [".toukadev", ".ToukaDev"])
def test_scanners_prune_toukadev_workspace_before_stat_or_descent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scanner_name: str,
    workspace_name: str,
) -> None:
    scan = _scanner(scanner_name)
    _source(tmp_path / "src")
    only_source = _expected(scanner_name, "src/example.py")
    assert _reported(scanner_name, scan(tmp_path), tmp_path) == only_source, (
        "Positive control must prove regular source is still inspected"
    )
    workspaces = (tmp_path / workspace_name, tmp_path / "src" / "nested" / workspace_name)
    for workspace in workspaces:
        _source(workspace / "worktrees" / "x")
    _deny_operations_under(monkeypatch, *workspaces)
    assert _reported(scanner_name, scan(tmp_path), tmp_path) == only_source


@pytest.mark.parametrize("scanner_name", SCANNERS)
def test_scan_rooted_inside_toukadev_worktree_still_reports(
    tmp_path: Path, scanner_name: str
) -> None:
    root = tmp_path / ".toukadev" / "worktrees" / "x"
    _source(root / "src")
    reported = _reported(scanner_name, _scanner(scanner_name)(root), root)
    assert reported == _expected(scanner_name, "src/example.py")


@pytest.mark.parametrize("scanner_name", SCANNERS)
@pytest.mark.parametrize("near_name", ["toukadev", ".toukadev-notes"])
def test_scanners_only_prune_the_exact_toukadev_name(
    tmp_path: Path, scanner_name: str, near_name: str
) -> None:
    _source(tmp_path / near_name / "worktrees" / "x")
    reported = _reported(scanner_name, _scanner(scanner_name)(tmp_path), tmp_path)
    assert reported == _expected(scanner_name, f"{near_name}/worktrees/x/example.py")


def test_gitignore_keeps_toukadev_workspace_untracked() -> None:
    """Name pruning is only safe while `.toukadev/` can never enter version control."""
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert ".toukadev/" in entries
    reopened = [entry for entry in entries if entry.startswith("!")]
    assert not [entry for entry in reopened if ".toukadev" in entry.casefold()]


def test_no_tracked_path_is_hidden_by_toukadev_pruning() -> None:
    if not GIT:
        pytest.skip("Git unavailable")
    result = execution_broker.run_sync(
        [GIT, "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    hidden = [
        tracked
        for tracked in result.stdout.decode("utf-8").split("\0")
        if any(part.casefold() == ".toukadev" for part in tracked.split("/"))
    ]
    assert hidden == []

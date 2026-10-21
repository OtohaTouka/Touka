"""Build-output declaration `rebuildable-outputs.txt` read by the development cleanup check.

The development cleanup check deletes a worktree without asking when every ignored item
in it is a rebuildable cache or sits under a path listed here. One bad line makes it
drop the whole declaration, so this test applies the same rules first: a plain UTF-8
file, one repo-relative `/`-separated path per line, every path ignored by git and with
no tracked file underneath.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from src.services import execution_broker

if TYPE_CHECKING:
    import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
DECLARATION = REPO_ROOT / "rebuildable-outputs.txt"
GIT = shutil.which("git")

# Already deletable by the cleanup check; a declaration that lists one is void.
REBUILDABLE_CACHES = frozenset(
    {
        ".venv",
        ".uv-cache",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        "__pycache__",
        "node_modules",
    }
)
FORBIDDEN_SEGMENTS = frozenset({".git", ".toukadev"})
_LIST_MARKER = re.compile(r"(?:[-*+]|\d+[.)])\s")


def _declared_paths() -> list[str]:
    """Lines as the cleanup check splits them; a Windows checkout may turn LF into CRLF."""
    lines = DECLARATION.read_bytes().decode("utf-8").split("\n")[:-1]
    return [line.removesuffix("\r") for line in lines]


def _both_forms(path: str) -> tuple[str, str]:
    bare = path.removesuffix("/")
    return bare, bare + "/"


def _format_problem(path: str) -> str | None:
    parts = path.removesuffix("/").split("/")
    if not path or path != path.strip() or not path.isprintable():
        return "empty, padded or holds invisible characters"
    if _LIST_MARKER.match(path) or "`" in path or "#" in path:
        return "list marker, backtick or comment"
    if path.startswith("/") or ":" in path or "\\" in path:
        return "not a repo-relative path separated by /"
    if any(mark in path for mark in "*?["):
        return "wildcard"
    if any(part in ("", ".", "..") for part in parts):
        return "empty, . or .. segment"
    if any(part.casefold() in FORBIDDEN_SEGMENTS for part in parts):
        return ".git or .toukadev segment"
    if any(part.casefold() in REBUILDABLE_CACHES for part in parts):
        return "rebuildable cache name"
    return None


def _git(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    if not GIT:
        pytest.skip("Git unavailable")
    return execution_broker.run_sync(
        [GIT, "-C", str(REPO_ROOT), *args],
        input=stdin,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_declaration_is_a_plain_utf8_file() -> None:
    assert DECLARATION.is_file(), "rebuildable-outputs.txt is missing at the repo root"
    assert not DECLARATION.is_symlink()
    raw = DECLARATION.read_bytes()
    raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf"), "no BOM"
    assert raw.endswith(b"\n"), "ends with a newline"
    assert all(_declared_paths()), "no blank lines"


def test_every_line_follows_the_cleanup_check_format() -> None:
    paths = _declared_paths()
    problems = [f"{path!r}: {reason}" for path in paths if (reason := _format_problem(path))]
    # The cleanup check compares without the trailing / and, on Windows, without case.
    keys = [path.removesuffix("/").casefold() for path in paths]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})

    assert paths, "the declaration lists no path"
    assert problems == []
    assert duplicates == []


def test_every_declared_path_is_ignored_by_git() -> None:
    """Either form counts (a `dist/` rule only matches the trailing-slash form).

    Only a real rule counts. In a CRLF checkout a blank `.gitignore` line becomes an empty
    pattern that `git check-ignore` reports for any untracked path written with a
    trailing `/`; a negated (`!`) rule means the path is not ignored.
    """
    paths = _declared_paths()
    forms = [form for path in paths for form in _both_forms(path)]
    stdin = "\0".join(forms).encode("utf-8") + b"\0"
    result = _git("check-ignore", "--stdin", "-z", "--verbose", stdin=stdin)
    assert result.returncode in (0, 1), result.stderr
    fields = result.stdout.decode("utf-8").split("\0")[:-1]
    # --verbose -z: source, line number, pattern, path for every matched path.
    records = [fields[index : index + 4] for index in range(0, len(fields), 4)]
    ignored = {path for _, _, rule, path in records if rule and not rule.startswith("!")}

    not_ignored = [path for path in paths if not set(_both_forms(path)) & ignored]
    assert not_ignored == []


def test_no_declared_path_holds_tracked_files() -> None:
    paths = [path.removesuffix("/") for path in _declared_paths()]
    result = _git("ls-files", "-z", "--", *paths)
    assert result.returncode == 0, result.stderr

    tracked = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    assert tracked == []

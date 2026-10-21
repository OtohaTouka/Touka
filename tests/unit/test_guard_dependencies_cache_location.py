"""ADR-0240: Guard #16 keeps its sandbox cache in the git common dir, locked per member."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import guard_dependencies as guard
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="real git is required to resolve worktrees")
# The guard's own runner, captured before any test replaces it with a fake.
NATIVE_RUN = guard.run_command

# Holds or probes the member lock from another process; the guard locks byte 0.
LOCK_CHILD = """
import os, sys, time
path, mode = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_RDWR | (os.O_CREAT if mode == "hold" else 0))
try:
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("busy")
    sys.exit(0)
print("free")
if mode == "hold":
    held, release = sys.argv[3], sys.argv[4]
    open(held, "w").close()
    deadline = time.monotonic() + 60
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)
"""


def _git(cwd: Path, *args: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    result = NATIVE_RUN([GIT, *identity, "-c", "commit.gpgsign=false", *args], cwd)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    empty_config = tmp_path / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path


def _write_sources(root: Path) -> None:
    (root / "Example").mkdir(parents=True)
    (root / "uv.lock").write_text("lock fixture", encoding="utf-8")
    (root / "Example" / "pyproject.toml").write_text("[project]\nname='example'", encoding="utf-8")


@pytest.fixture
def repo(isolated_git: Path) -> Path:
    main = isolated_git / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _write_sources(main)
    _git(main, "add", "uv.lock", "Example/pyproject.toml")
    _git(main, "commit", "-q", "-m", "fixture")
    return main


def _add_worktree(main: Path, name: str) -> Path:
    path = main.parent / name
    _git(main, "worktree", "add", "-q", "--detach", str(path))
    return path


def _use_checkout(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict:
    member = {
        "name": "Example",
        "package": "example",
        "path": root / "Example",
        "import_test": "print('OK')",
    }
    monkeypatch.setattr(guard, "PROJECT_ROOT", root)
    monkeypatch.setattr(guard, "UV_LOCK", root / "uv.lock")
    monkeypatch.setattr(guard, "CACHE_DIR", None)
    monkeypatch.setattr(guard, "LEGACY_CACHE_DIR", root / "scripts" / ".cache" / "guards")
    monkeypatch.setattr(guard, "TARGET_PROJECTS", [member])
    monkeypatch.setattr(guard, "scan_pytest_residue", lambda _root: ())
    return member


class FakeRunner:
    """Real git; recorded ``uv sync`` / member import, optionally probing the member lock."""

    def __init__(self, *, probe: Path | None = None, sync_code: int = 0) -> None:
        self.calls: list[tuple[list[str], Path, dict | None]] = []
        self.probes: list[str] = []
        self._probe, self._sync_code = probe, sync_code

    def __call__(self, cmd: list[str], cwd: Path, env: dict | None = None):
        if cmd[0] == "git":
            return NATIVE_RUN(cmd, cwd, env)
        self.calls.append((list(cmd), Path(cwd), env))
        if self._probe is not None:
            self.probes.append(_probe_lock(self._probe))
        if cmd[:2] == ["uv", "sync"]:
            Path(env["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, self._sync_code, "", "sync fixture")
        return subprocess.CompletedProcess(cmd, 0, "OK", "")


def _run_main(monkeypatch: pytest.MonkeyPatch, *, cache_only: bool = False) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(guard, "run_command", runner)
    with pytest.raises(SystemExit) as result:
        guard.main(cache_only=cache_only)
    assert result.value.code == 0
    return runner


def _tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def _shared_root(main: Path) -> Path:
    return (main / ".git" / "touka-guard-cache" / sys.platform).resolve()


@requires_git
def test_worktree_and_main_resolve_same_common_cache_root(repo: Path) -> None:
    worktree = _add_worktree(repo, "wt1")
    from_main = guard.resolve_cache_root(repo).resolve()
    from_worktree = guard.resolve_cache_root(worktree).resolve()
    assert from_worktree == from_main == _shared_root(repo)
    assert not from_worktree.is_relative_to(worktree.resolve())


@requires_git
def test_subfolder_of_another_repository_falls_back_to_legacy(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES")
    nested = repo / "vendored" / "checkout"
    nested.mkdir(parents=True)
    legacy = nested / "scripts" / ".cache" / "guards"
    monkeypatch.setattr(guard, "LEGACY_CACHE_DIR", legacy)
    assert guard.resolve_cache_root(nested) == legacy
    assert "not the git toplevel" in capsys.readouterr().out


@requires_git
def test_guard_run_in_worktree_leaves_no_new_paths_inside_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = _add_worktree(repo, "wt1")
    _use_checkout(monkeypatch, worktree)
    before = _tree(worktree)
    runner = _run_main(monkeypatch)
    assert any(cmd[:2] == ["uv", "sync"] for cmd, _, _ in runner.calls)
    added = {path for path in _tree(worktree) - before if "__pycache__" not in path}
    assert added == set()
    assert (_shared_root(repo) / "Example" / "lock.hash").is_file()


@requires_git
def test_second_worktree_hits_shared_cache_without_sync(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _add_worktree(repo, "wt1"), _add_worktree(repo, "wt2")
    _use_checkout(monkeypatch, first)
    assert any(cmd[:2] == ["uv", "sync"] for cmd, _, _ in _run_main(monkeypatch).calls)

    member = _use_checkout(monkeypatch, second)
    runner = _run_main(monkeypatch)
    assert len(runner.calls) == 1, "a warm shared cache must skip uv sync"
    command, cwd, env = runner.calls[0]
    shared_venv = _shared_root(repo) / "Example" / "venv"
    assert Path(command[0]).resolve().is_relative_to(shared_venv)
    # The shared venv holds no project code: imports still run against this worktree.
    assert cwd == member["path"]
    assert env["PYTHONPATH"] == str(member["path"])


@pytest.fixture
def locked_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path]:
    root = tmp_path / "repository"
    _write_sources(root)
    member = _use_checkout(monkeypatch, root)
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(guard, "CACHE_DIR", cache_root)
    venv = cache_root / "Example" / "venv"
    venv.mkdir(parents=True)
    (venv / "sentinel.txt").write_text("in use", encoding="utf-8")
    (cache_root / "Example" / "lock.hash").write_text("stale", encoding="utf-8")
    return member, cache_root


@contextmanager
def _lock_held_elsewhere(lock_path: Path, flags: Path) -> Iterator[None]:
    """Another process holds the lock until the release flag appears (≤60 s)."""
    held, release = flags / "held", flags / "release"
    command = [sys.executable, "-c", LOCK_CHILD, str(lock_path), "hold", str(held), str(release)]
    holder = threading.Thread(target=NATIVE_RUN, args=(command, flags))
    holder.start()
    try:
        deadline = time.monotonic() + 30
        while not held.exists():
            assert holder.is_alive(), "lock holder exited without the lock"
            assert time.monotonic() < deadline, "lock holder did not start"
            time.sleep(0.02)
        yield
    finally:
        release.touch()
        holder.join(timeout=60)


def _probe_lock(lock_path: Path) -> str:
    command = [sys.executable, "-c", LOCK_CHILD, str(lock_path), "probe"]
    return NATIVE_RUN(command, lock_path.parent).stdout.strip()


def test_busy_member_lock_fails_closed_without_touching_cache(
    tmp_path: Path,
    locked_member: tuple[dict, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    member, cache_root = locked_member
    monkeypatch.setattr(guard, "CACHE_LOCK_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(guard, "CACHE_LOCK_POLL_SECONDS", 0.05)
    runner = FakeRunner()
    monkeypatch.setattr(guard, "run_command", runner)

    with _lock_held_elsewhere(cache_root / "Example.lock", tmp_path):
        assert guard.check_project_dependencies(member) is False

    assert runner.calls == []
    assert (cache_root / "Example" / "lock.hash").read_text(encoding="utf-8") == "stale"
    assert (cache_root / "Example" / "venv" / "sentinel.txt").is_file()
    assert "holds the cache lock" in capsys.readouterr().out


def test_lock_held_through_rebuild_and_import_then_released(
    locked_member: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    member, cache_root = locked_member
    lock_path = cache_root / "Example.lock"
    runner = FakeRunner(probe=lock_path)
    monkeypatch.setattr(guard, "run_command", runner)

    assert guard.check_project_dependencies(member) is True

    assert [cmd[:2] for cmd, _, _ in runner.calls][0] == ["uv", "sync"]
    assert runner.probes == ["busy", "busy"], "sync and import must run under the lock"
    assert not (cache_root / "Example" / "venv" / "sentinel.txt").exists()
    assert lock_path.is_file()
    assert _probe_lock(lock_path) == "free"


def test_cache_only_waits_on_held_lock_and_fails_closed(
    tmp_path: Path,
    locked_member: tuple[dict, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    member, cache_root = locked_member
    monkeypatch.setattr(guard, "CACHE_LOCK_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(guard, "CACHE_LOCK_POLL_SECONDS", 0.05)
    runner = FakeRunner()
    monkeypatch.setattr(guard, "run_command", runner)

    with _lock_held_elsewhere(cache_root / "Example.lock", tmp_path):
        assert guard.check_project_dependencies(member, cache_only=True) is False

    assert runner.calls == []
    assert "holds the cache lock" in capsys.readouterr().out


def test_failed_sync_leaves_no_cache_key(
    locked_member: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    member, cache_root = locked_member
    runner = FakeRunner(sync_code=1)
    monkeypatch.setattr(guard, "run_command", runner)

    assert guard.check_project_dependencies(member) is False
    assert [cmd[:2] for cmd, _, _ in runner.calls] == [["uv", "sync"]]
    assert not (cache_root / "Example" / "lock.hash").exists()


@pytest.mark.parametrize(
    "change", ["package", "schema", "uv_lock", "member_pyproject", "python_version"]
)
def test_changed_key_input_resyncs(
    locked_member: tuple[dict, Path], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Every key input invalidates the shared cache (independent of the key helper)."""
    member = locked_member[0]
    pinned = guard.PROJECT_ROOT / ".python-version"
    pinned.write_text("3.12\n", encoding="utf-8")
    monkeypatch.setattr(guard, "run_command", FakeRunner())
    assert guard.check_project_dependencies(member) is True
    warm = FakeRunner()
    monkeypatch.setattr(guard, "run_command", warm)
    assert guard.check_project_dependencies(member) is True
    assert len(warm.calls) == 1, "unchanged inputs must hit the cache"

    key_files = {
        "uv_lock": guard.UV_LOCK,
        "member_pyproject": member["path"] / "pyproject.toml",
        "python_version": pinned,
    }
    if change == "package":
        member["package"] = "example-other"
    elif change == "schema":
        monkeypatch.setattr(guard, "CACHE_KEY_SCHEMA", guard.CACHE_KEY_SCHEMA + 1)
    else:
        with key_files[change].open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")
    rebuilt = FakeRunner()
    monkeypatch.setattr(guard, "run_command", rebuilt)
    assert guard.check_project_dependencies(member) is True
    assert rebuilt.calls[0][0][:2] == ["uv", "sync"]


@requires_git
def test_non_git_root_falls_back_to_legacy_cache(
    isolated_git: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = isolated_git / "plain"
    plain.mkdir()
    legacy = plain / "scripts" / ".cache" / "guards"
    monkeypatch.setattr(guard, "LEGACY_CACHE_DIR", legacy)
    assert guard.resolve_cache_root(plain) == legacy
    assert "legacy cache" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["oserror", "exit", "empty", "not_a_dir"])
def test_unusable_git_answer_falls_back_to_legacy_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    legacy = tmp_path / "scripts" / ".cache" / "guards"
    monkeypatch.setattr(guard, "LEGACY_CACHE_DIR", legacy)

    def git(cmd: list[str], cwd: Path, env: dict | None = None):
        if answer == "oserror":
            raise FileNotFoundError(cmd[0])
        code, out = {
            "exit": (128, ""),
            "empty": (0, "\n"),
            "not_a_dir": (0, f"{tmp_path}\n{tmp_path / 'missing'}"),
        }[answer]
        return subprocess.CompletedProcess(cmd, code, out, "fatal: dubious ownership")

    monkeypatch.setattr(guard, "run_command", git)
    assert guard.resolve_cache_root(tmp_path) == legacy
    assert "legacy cache" in capsys.readouterr().out
    assert not legacy.exists()


@pytest.mark.parametrize("root_exists", [False, True])
def test_cache_only_creates_no_paths_at_resolved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_exists: bool
) -> None:
    root = tmp_path / "repository"
    _write_sources(root)
    _use_checkout(monkeypatch, root)
    resolved = tmp_path / "common" / "touka-guard-cache"
    if root_exists:
        resolved.mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_cache_root", lambda _root: resolved)
    runner = FakeRunner()
    monkeypatch.setattr(guard, "run_command", runner)
    before = _tree(tmp_path)

    with pytest.raises(SystemExit) as result:
        guard.main(cache_only=True)

    assert result.value.code == 1
    assert runner.calls == []
    assert _tree(tmp_path) == before


def test_legacy_cache_is_reported_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repository"
    _write_sources(root)
    _use_checkout(monkeypatch, root)
    monkeypatch.setattr(guard, "TARGET_PROJECTS", [])
    sentinel = guard.LEGACY_CACHE_DIR / "Example" / "venv" / "sentinel.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(guard, "resolve_cache_root", lambda _root: tmp_path / "common")

    _run_main(monkeypatch, cache_only=True)
    assert "no longer used" in capsys.readouterr().out
    assert sentinel.read_text(encoding="utf-8") == "keep"

    # Falling back to the legacy location itself is not "unused": no notice.
    monkeypatch.setattr(guard, "resolve_cache_root", lambda _root: guard.LEGACY_CACHE_DIR)
    _run_main(monkeypatch, cache_only=True)
    assert "no longer used" not in capsys.readouterr().out

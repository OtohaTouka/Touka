"""Native Guard #16 cache-only and product-import sandbox contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import guard_dependencies as guard
import pytest


@pytest.fixture
def member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root = tmp_path / "repository"
    project_path = root / "Example"
    project_path.mkdir(parents=True)
    (root / "uv.lock").write_text("lock fixture", encoding="utf-8")
    (project_path / "pyproject.toml").write_text("[project]\nname='example'", encoding="utf-8")
    monkeypatch.setattr(guard, "PROJECT_ROOT", root)
    monkeypatch.setattr(guard, "UV_LOCK", root / "uv.lock")
    monkeypatch.setattr(guard, "CACHE_DIR", root / "cache")
    return {
        "name": "Example",
        "package": "example",
        "path": project_path,
        "import_test": "print('OK')",
    }


def _seed_cache(member: dict) -> Path:
    cache = guard.CACHE_DIR / member["name"]
    (cache / "venv").mkdir(parents=True)
    digest = guard._cache_key(member, guard._sync_argv(member))  # ADR-0240 key
    (cache / "lock.hash").write_text(digest, encoding="utf-8")
    return cache


@pytest.mark.parametrize("stale", [False, True])
def test_cache_only_refuses_missing_or_stale_cache_without_writes(
    member: dict, monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    cache = guard.CACHE_DIR / member["name"]
    if stale:
        cache = _seed_cache(member)
        (cache / "lock.hash").write_text("old", encoding="utf-8")
        (cache / "preserve.txt").write_text("untouched", encoding="utf-8")
    calls = []
    monkeypatch.setattr(guard, "run_command", lambda *args, **kwargs: calls.append(args))
    assert guard.check_project_dependencies(member, cache_only=True) is False
    assert calls == []
    if stale:
        assert (cache / "lock.hash").read_text(encoding="utf-8") == "old"
        assert (cache / "preserve.txt").read_text(encoding="utf-8") == "untouched"
    else:
        assert not guard.CACHE_DIR.exists()


def test_cache_only_main_never_creates_cache_root(
    member: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "scan_pytest_residue", lambda _: ())
    monkeypatch.setattr(guard, "TARGET_PROJECTS", [member])
    with pytest.raises(SystemExit) as result:
        guard.main(cache_only=True)
    assert result.value.code == 1
    assert not guard.CACHE_DIR.exists()


def test_cached_member_failure_cannot_fall_back_to_root_runtime(
    member: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _seed_cache(member)
    calls = []

    def failed_import(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1, "", "ModuleNotFoundError: missing_member_dep"
        )

    monkeypatch.setattr(guard, "run_command", failed_import)
    assert guard.check_project_dependencies(member, cache_only=True) is False
    assert len(calls) == 1
    assert Path(calls[0][0]).is_relative_to(cache / "venv")


def test_cached_import_uses_existing_smoke_sandbox_before_product_code(
    member: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _seed_cache(member)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-never-send")
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    monkeypatch.setenv("DISPATCH_AUTOSPAWN", "true")
    monkeypatch.setenv("TOUKA_ENV_FILE", "fixture-host.env")
    monkeypatch.setenv("TOUKA_CONFIG_DIR", "fixture-host-config")
    observations = []
    member["import_test"] = (
        "import json, os; from pathlib import Path; "
        "print(json.dumps({'key': os.environ.get('OPENAI_API_KEY'), "
        "'bootstrap': os.environ.get('TOUKA_BOOTSTRAP_ENABLED'), "
        "'dispatch': os.environ.get('DISPATCH_AUTOSPAWN'), "
        "'env': os.environ.get('TOUKA_ENV_FILE'), "
        "'config': os.environ.get('TOUKA_CONFIG_DIR'), "
        "'data': os.environ.get('MEMORY_APP_DATA_DIR'), "
        "'config_exists': Path(os.environ['TOUKA_CONFIG_DIR']).is_dir(), "
        "'env_exists': Path(os.environ['TOUKA_ENV_FILE']).is_file()}))"
    )

    native_runner = guard.run_command

    def observe_child(command, *, cwd, env):
        assert Path(command[0]).is_relative_to(cache / "venv")
        # Only this synthetic member uses the current test interpreter; no product is imported.
        result = native_runner(
            [sys.executable, *command[1:]],
            cwd=cwd,
            env=env,
        )
        if result.returncode == 0:
            observations.append(json.loads(result.stdout.strip().splitlines()[-1]))
        return result

    monkeypatch.setattr(guard, "run_command", observe_child)
    assert guard.check_project_dependencies(member) is True
    assert len(observations) == 1
    observed = observations[0]
    assert observed["key"] is None
    assert observed["bootstrap"] == "false"
    assert observed["dispatch"] == "false"
    assert observed["config_exists"] is True
    assert observed["env_exists"] is True
    assert Path(observed["env"]).parent == Path(observed["data"])
    assert Path(observed["config"]).parent == Path(observed["data"])
    assert not Path(observed["data"]).exists(), "child-owned smoke sandbox must be cleaned up"

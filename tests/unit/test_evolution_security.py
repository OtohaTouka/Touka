"""Mutation-killing unit tests for EvolutionPathGuard (the self-modification sandbox).

Source: src/services/evolution_security.py

These tests pin EXACT behaviour of the "default jail" sandbox: path validation
(traversal / absolute-escape / garbage), env sanitisation (one var per blacklist
category + benign survivor + forced PYTHONPATH), file roundtrips + sandbox-escape
PermissionErrors, run_sandbox_command cwd/env wiring, the POSIX rlimit preexec
(via a fake `resource` module), and get_tool_root resolution.

Deterministic setup: MANAGED_BASE_DIR is monkeypatched to tmp_path so the sandbox
lives entirely under pytest's tmp dir (no real data/ touched). settings.get_full_env
is patched to a known dict for env tests. subprocess.run is patched to capture kwargs.
"""

from __future__ import annotations

import types

import pytest
from src.services.evolution_security import EvolutionPathGuard

from tests.support.isolation import passthrough_launch

_RESOURCE_ATTR = "src.services.evolution_security.resource"
_SETTINGS_ATTR = "src.services.evolution_security.settings"
_RUN_ATTR = "src.services.evolution_security.subprocess.run"


def _patch_full_env(monkeypatch, env: dict) -> None:
    """Patch settings.get_full_env -> known dict.

    `settings` is a frozen pydantic model that rejects attribute assignment, so we
    swap the whole module-level `settings` reference for a lightweight stand-in that
    only needs to expose get_full_env (the single method get_clean_env calls).
    """
    monkeypatch.setattr(
        _SETTINGS_ATTR,
        types.SimpleNamespace(get_full_env=lambda: dict(env)),
    )

_MB = 1024 * 1024
_SKILL = "skill_alpha"


@pytest.fixture()
def sandbox_base(monkeypatch, tmp_path):
    """Relocate the managed base dir into tmp_path and create the skill root."""
    monkeypatch.setattr(EvolutionPathGuard, "MANAGED_BASE_DIR", tmp_path)
    EvolutionPathGuard.mkdir_tool(_SKILL)
    return tmp_path


def _fake_resource(calls: list) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        RLIMIT_FSIZE="FSIZE",
        RLIMIT_CPU="CPU",
        RLIMIT_AS="AS",
        setrlimit=lambda res, limits: calls.append((res, limits)),
    )


# ---------------------------------------------------------------------------
# get_tool_root
# ---------------------------------------------------------------------------

def test_get_tool_root_is_base_dir_joined_with_skill_resolved(monkeypatch, tmp_path):
    monkeypatch.setattr(EvolutionPathGuard, "MANAGED_BASE_DIR", tmp_path)
    root = EvolutionPathGuard.get_tool_root(_SKILL)
    assert root == (tmp_path / _SKILL).resolve()


# ---------------------------------------------------------------------------
# validate_path
# ---------------------------------------------------------------------------

def test_validate_path_inside_root_is_true(sandbox_base):
    root = EvolutionPathGuard.get_tool_root(_SKILL)
    inside = str(root / "sub" / "file.py")
    assert EvolutionPathGuard.validate_path(_SKILL, inside) is True


def test_validate_path_traversal_escapes_root_is_false(sandbox_base):
    root = EvolutionPathGuard.get_tool_root(_SKILL)
    # ../../evil climbs above the skill root → must be rejected
    escaping = str(root / ".." / ".." / "evil.txt")
    assert EvolutionPathGuard.validate_path(_SKILL, escaping) is False


def test_validate_path_absolute_outside_sandbox_is_false(sandbox_base, tmp_path):
    # An absolute path that is a sibling of MANAGED_BASE_DIR, not under the skill root.
    outside = str(tmp_path.parent / "totally_other_dir" / "x.txt")
    assert EvolutionPathGuard.validate_path(_SKILL, outside) is False


def test_validate_path_garbage_does_not_raise_and_is_false(sandbox_base):
    # A NUL-embedded target_path makes Path(target_path).resolve() raise ValueError;
    # validate_path's except-branch must catch it and return False (no propagation).
    garbage = "bad\x00name"
    result = EvolutionPathGuard.validate_path(_SKILL, garbage)
    assert result is False


# ---------------------------------------------------------------------------
# get_clean_env
# ---------------------------------------------------------------------------

def test_get_clean_env_strips_every_blacklist_category_keeps_benign(monkeypatch):
    parent_env = {
        "MY_SECRET": "s",
        "API_KEY": "k",
        "AUTH_TOKEN": "t",          # AUTH + TOKEN
        "DB_PASSWORD": "p",
        "A_CREDENTIAL": "c",
        "PYTHONHOME": "/evil",
        "LD_PRELOAD": "/evil.so",
        "DYLD_LIBRARY_PATH": "/evil.dylib",
        "NODE_OPTIONS": "--inspect",
        "GEM_PATH": "/gems",
        "JAVA_OPTS": "-Xmx",
        "PERL5LIB": "/perl",
        "PATH": "/usr/bin",         # benign survivor
    }
    _patch_full_env(monkeypatch, parent_env)
    clean = EvolutionPathGuard.get_clean_env()

    for blacklisted in (
        "MY_SECRET", "API_KEY", "AUTH_TOKEN", "DB_PASSWORD", "A_CREDENTIAL",
        "PYTHONHOME", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "NODE_OPTIONS",
        "GEM_PATH", "JAVA_OPTS", "PERL5LIB",
    ):
        assert blacklisted not in clean, f"{blacklisted} must be stripped"

    assert clean["PATH"] == "/usr/bin", "benign PATH must survive"


def test_get_clean_env_forces_pythonpath_to_dot(monkeypatch):
    _patch_full_env(monkeypatch, {"PYTHONPATH": "/attacker/site-packages"})
    clean = EvolutionPathGuard.get_clean_env()
    assert clean["PYTHONPATH"] == "."


# ---------------------------------------------------------------------------
# write / read / delete / exists  — happy path + escape
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip_returns_exact_content(sandbox_base):
    EvolutionPathGuard.write_tool_file(_SKILL, "server.py", "print('hi')\n")
    assert EvolutionPathGuard.read_tool_file(_SKILL, "server.py") == "print('hi')\n"


def test_read_missing_file_returns_none(sandbox_base):
    assert EvolutionPathGuard.read_tool_file(_SKILL, "nope.py") is None


def test_write_read_delete_escape_raise_permission_error(sandbox_base):
    evil = "../../evil.txt"
    with pytest.raises(PermissionError):
        EvolutionPathGuard.write_tool_file(_SKILL, evil, "x")
    with pytest.raises(PermissionError):
        EvolutionPathGuard.read_tool_file(_SKILL, evil)
    with pytest.raises(PermissionError):
        EvolutionPathGuard.delete_tool_file(_SKILL, evil)


def test_delete_existing_returns_true_then_gone_missing_returns_false(sandbox_base):
    EvolutionPathGuard.write_tool_file(_SKILL, "tmp.py", "data")
    assert EvolutionPathGuard.delete_tool_file(_SKILL, "tmp.py") is True
    # second delete: file already gone → False
    assert EvolutionPathGuard.delete_tool_file(_SKILL, "tmp.py") is False


def test_tool_file_exists_true_for_present_false_for_absent(sandbox_base):
    assert EvolutionPathGuard.tool_file_exists(_SKILL, "ghost.py") is False
    EvolutionPathGuard.write_tool_file(_SKILL, "ghost.py", "boo")
    assert EvolutionPathGuard.tool_file_exists(_SKILL, "ghost.py") is True


# ---------------------------------------------------------------------------
# run_sandbox_command
# ---------------------------------------------------------------------------

def test_run_sandbox_command_defaults_cwd_to_root_and_cleans_env(
    sandbox_base, monkeypatch
):
    _patch_full_env(monkeypatch, {"MY_SECRET": "leak", "PATH": "/usr/bin"})
    # No resource module → no preexec_fn injected (keeps assertion on env/cwd clean).
    monkeypatch.setattr(_RESOURCE_ATTR, None)

    captured: dict = {}

    def _fake_run(cmd_list, **kwargs):
        captured["cmd"] = cmd_list
        captured.update(kwargs)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(_RUN_ATTR, _fake_run)

    EvolutionPathGuard.run_sandbox_command(
        _SKILL, ["python", "x.py"], launch=passthrough_launch(_SKILL)
    )

    root = EvolutionPathGuard.get_tool_root(_SKILL)
    # (a) cwd defaults to str(root)
    assert captured["cwd"] == str(root)
    # (b) env is the cleaned env: PYTHONPATH forced to '.', secret stripped
    env = captured["env"]
    assert env["PYTHONPATH"] == "."
    assert "MY_SECRET" not in env
    assert env["PATH"] == "/usr/bin"


def test_run_sandbox_command_cwd_outside_sandbox_raises_before_run(
    sandbox_base, monkeypatch, tmp_path
):
    ran = {"called": False}

    def _fake_run(cmd_list, **kwargs):  # noqa: ARG001
        ran["called"] = True
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(_RUN_ATTR, _fake_run)

    outside = str(tmp_path.parent / "escape_dir")
    with pytest.raises(PermissionError):
        EvolutionPathGuard.run_sandbox_command(
            _SKILL, ["python", "x.py"], cwd=outside, launch=passthrough_launch(_SKILL)
        )
    assert ran["called"] is False, "subprocess.run must NOT execute when cwd escapes"


# ---------------------------------------------------------------------------
# _build_rlimit_preexec  (the one POSIX path worth testing — via fake resource)
# ---------------------------------------------------------------------------

def test_build_rlimit_preexec_returns_none_without_resource(monkeypatch):
    monkeypatch.setattr(_RESOURCE_ATTR, None)
    assert EvolutionPathGuard._build_rlimit_preexec(256, 50, 30) is None


def test_build_rlimit_preexec_sets_expected_rlimit_tuples(monkeypatch):
    calls: list = []
    monkeypatch.setattr(_RESOURCE_ATTR, _fake_resource(calls))

    preexec = EvolutionPathGuard._build_rlimit_preexec(
        memory_mb=256, storage_mb=50, cpu_sec=30
    )
    assert callable(preexec)
    preexec()  # invoke the child-side fn directly; records onto the fake

    by_res = dict(calls)
    assert by_res["FSIZE"] == (50 * _MB, 50 * _MB)     # storage → RLIMIT_FSIZE (limit, limit)
    assert by_res["AS"] == (256 * _MB, 256 * _MB)      # memory → RLIMIT_AS (limit, limit)
    assert by_res["CPU"] == (30, 31)                   # cpu → RLIMIT_CPU (cpu, cpu+1)

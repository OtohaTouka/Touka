"""Mutation-killing unit tests for EvolutionPathGuard (the self-modification sandbox).

Source: src/services/evolution_security.py

These tests pin EXACT behaviour of the "default jail" sandbox: path validation
(traversal / absolute-escape / garbage), env sanitisation (one var per blacklist
category + benign survivor + forced PYTHONPATH), file roundtrips + sandbox-escape
PermissionErrors, run_sandbox_command cwd/env wiring, the POSIX rlimit preexec
(via a fake `resource` module), and get_tool_root resolution.

Deterministic setup: the data root is moved to ``zone_data_root`` (tmp_path) so the
sandbox lives entirely under pytest's tmp dir (no real data/ touched). settings.get_full_env
is patched to a known dict for env tests. subprocess.run is patched to capture kwargs.
"""

from __future__ import annotations

import inspect
import types

import pytest
from memory_server.utils.zone_paths import HandoffKind, is_handed_off
from src.services.evolution_security import EvolutionPathGuard, EvolutionRefused

from tests.support.isolation import passthrough_launch
from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

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
def sandbox_base(zone_data_root):
    """技能資料夾放在這個測試自己的 data 根（``zone_data_root``）底下並建好；回 managed 根."""
    EvolutionPathGuard.mkdir_tool(_SKILL)
    return zone_data_root / "mcp_servers" / "managed"


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

def test_get_tool_root_is_the_skill_folder_zone_text(zone_data_root):
    root = EvolutionPathGuard.get_tool_root(_SKILL)
    assert root == str(zone_data_root / "mcp_servers" / "managed" / _SKILL)


# ---------------------------------------------------------------------------
# file names that cannot be one segment (was validate_path; ADR-0293 PR 2)
# ---------------------------------------------------------------------------

_EVIL_NAMES = ["../../evil.txt", "..", "sub/file.py", "sub\\file.py", "bad\x00name", ""]


@pytest.mark.parametrize("evil", _EVIL_NAMES)
def test_file_names_that_escape_the_skill_folder_are_refused(sandbox_base, evil):
    calls = [
        lambda: EvolutionPathGuard.write_tool_file(_SKILL, evil, "x"),
        lambda: EvolutionPathGuard.read_tool_file(_SKILL, evil),
        lambda: EvolutionPathGuard.delete_tool_file(_SKILL, evil),
        lambda: EvolutionPathGuard.tool_file_exists(_SKILL, evil),
    ]
    for call in calls:
        with (
            expect_zone_refusal("ZoneSegmentError"),
            pytest.raises(EvolutionRefused, match="skill_changed"),
        ):
            call()
    assert [p.name for p in sandbox_base.iterdir()] == [_SKILL]
    assert list((sandbox_base / _SKILL).iterdir()) == []


def test_absolute_file_name_outside_the_sandbox_is_refused(sandbox_base, tmp_path):
    outside = tmp_path.parent / "totally_other_dir" / "x.txt"
    with (
        expect_zone_refusal("ZoneSegmentError"),
        pytest.raises(EvolutionRefused, match="skill_changed"),
    ):
        EvolutionPathGuard.write_tool_file(_SKILL, str(outside), "x")
    assert not outside.exists()


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

def test_run_sandbox_command_hands_off_the_skill_folder_and_cleans_env(
    sandbox_base, monkeypatch
):
    _patch_full_env(monkeypatch, {"MY_SECRET": "leak", "PATH": "/usr/bin"})
    # No resource module → no preexec_fn injected (keeps assertion on env/cwd clean).
    monkeypatch.setattr(_RESOURCE_ATTR, None)

    captured: dict = {}

    def _fake_run(cmd_list, **kwargs):
        captured["cmd"] = cmd_list
        captured.update(kwargs)
        # spawn 的當下 cwd 以「子行程的工作資料夾」交接中（ADR-0293 P6）
        captured["handed"] = is_handed_off(kwargs["cwd"], HandoffKind.SUBPROCESS_CWD)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(_RUN_ATTR, _fake_run)

    EvolutionPathGuard.run_sandbox_command(
        _SKILL, ["python", "x.py"], launch=passthrough_launch(_SKILL)
    )

    # (a) cwd 是技能資料夾的區字串，spawn 當下交接中、結束後收回
    assert captured["cwd"] == EvolutionPathGuard.get_tool_root(_SKILL)
    assert captured["handed"] is True
    assert not is_handed_off(captured["cwd"], HandoffKind.SUBPROCESS_CWD)
    # (b) env is the cleaned env: PYTHONPATH forced to '.', secret stripped
    env = captured["env"]
    assert env["PYTHONPATH"] == "."
    assert "MY_SECRET" not in env
    assert env["PATH"] == "/usr/bin"


def test_run_sandbox_command_has_no_cwd_parameter():
    """cwd 只能是技能資料夾（ADR-0293 P6）：沒有讓呼叫端另給 cwd 的參數."""
    assert "cwd" not in inspect.signature(EvolutionPathGuard.run_sandbox_command).parameters


def test_run_sandbox_command_refuses_a_linked_skill_before_run(
    zone_data_root, monkeypatch, tmp_path
):
    managed = zone_data_root / "mcp_servers" / "managed"
    managed.mkdir(parents=True)
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    make_dir_link(managed / _SKILL, fake)
    ran = {"called": False}

    def _fake_run(cmd_list, **kwargs):  # noqa: ARG001
        ran["called"] = True
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(_RUN_ATTR, _fake_run)

    with (
        expect_zone_refusal("ZoneLinkRefusedError"),
        pytest.raises(EvolutionRefused, match="skill_changed"),
    ):
        EvolutionPathGuard.run_sandbox_command(
            _SKILL, ["python", "x.py"], launch=passthrough_launch(_SKILL)
        )
    assert ran["called"] is False, LINK_FOLLOWED


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

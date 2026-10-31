"""DA28 / ADR-0076: OS-level resource limits for the evolution sandbox subprocess.

The sandbox previously had only a wall-clock timeout + a bypassable in-process
builtins.open monkeypatch (JailedWriter) for storage, and `max_memory_mb` was inert.
`EvolutionPathGuard.run_sandbox_command` now attaches a POSIX `preexec_fn` (built by
`_build_rlimit_preexec`) that sets RLIMIT_FSIZE (storage), RLIMIT_CPU and RLIMIT_AS
(memory). On Windows there is no `resource` module, so it degrades to None (the
existing timeout + JailedWriter remain). These tests inject a fake `resource` module
so the POSIX path is exercised even on the Windows dev box.
"""

from __future__ import annotations

import types

from src.services.evolution_security import EvolutionPathGuard

from tests.support.isolation import passthrough_launch

_RESOURCE_ATTR = "src.services.evolution_security.resource"

_MB = 1024 * 1024


def _fake_resource(calls: list) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        RLIMIT_FSIZE="FSIZE",
        RLIMIT_CPU="CPU",
        RLIMIT_AS="AS",
        setrlimit=lambda res, limits: calls.append((res, limits)),
    )


class TestBuildRlimitPreexec:
    def test_returns_none_when_resource_module_unavailable(self, monkeypatch):
        # resource is None on Windows (the top-level import failed) ≈ no OS rlimits
        monkeypatch.setattr(_RESOURCE_ATTR, None)
        assert EvolutionPathGuard._build_rlimit_preexec(256, 50, 30) is None

    def test_sets_fsize_cpu_and_as_on_posix(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_RESOURCE_ATTR, _fake_resource(calls))

        preexec = EvolutionPathGuard._build_rlimit_preexec(
            memory_mb=256, storage_mb=50, cpu_sec=30
        )
        assert preexec is not None
        preexec()  # runs in the child after fork — here we invoke it directly

        by_res = dict(calls)
        assert by_res["FSIZE"] == (50 * _MB, 50 * _MB)  # storage quota → RLIMIT_FSIZE
        assert by_res["AS"] == (256 * _MB, 256 * _MB)  # memory → RLIMIT_AS
        assert by_res["CPU"][0] == 30  # CPU seconds (soft)

    def test_applies_nothing_when_all_limits_none(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(_RESOURCE_ATTR, _fake_resource(calls))
        preexec = EvolutionPathGuard._build_rlimit_preexec(None, None, None)
        assert preexec is not None
        preexec()
        assert calls == []  # nothing to enforce → no setrlimit calls


class TestRunSandboxCommandWiring:
    def test_run_sandbox_command_passes_preexec_on_posix(self, monkeypatch, zone_data_root):
        """run_sandbox_command must attach the rlimit preexec_fn when on a POSIX host."""
        monkeypatch.setattr(_RESOURCE_ATTR, _fake_resource([]))
        # the skill folder lives under zone_data_root; neutralise the actual subprocess
        EvolutionPathGuard.mkdir_tool("skill")
        monkeypatch.setattr(EvolutionPathGuard, "get_clean_env", lambda: {})

        captured: dict = {}

        def _fake_run(cmd_list, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        monkeypatch.setattr("src.services.evolution_security.subprocess.run", _fake_run)

        EvolutionPathGuard.run_sandbox_command(
            "skill", ["python", "x.py"],
            memory_mb=256, storage_mb=50, timeout=30, launch=passthrough_launch("skill"),
        )
        assert callable(captured.get("preexec_fn"))

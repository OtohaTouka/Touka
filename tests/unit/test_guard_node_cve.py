"""Unit tests for the fail-closed Node dependency audit guard."""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / "scripts" / "guard_node_cve.py"
sys.path.insert(0, str(ROOT / "scripts"))


def _load_guard() -> ModuleType:
    assert GUARD_PATH.is_file(), "scripts/guard_node_cve.py must be added"
    spec = importlib.util.spec_from_file_location("guard_node_cve_under_test", GUARD_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit_payload(*, high: int = 0, critical: int = 0, moderate: int = 0) -> str:
    counts = {"moderate": moderate, "high": high, "critical": critical}
    vulnerabilities = {
        f"{severity}-package-{index}": {"severity": severity}
        for severity, count in counts.items()
        for index in range(count)
    }
    return json.dumps(
        {
            "auditReportVersion": 2,
            "vulnerabilities": vulnerabilities,
            "metadata": {
                "vulnerabilities": {
                    "info": 0,
                    "low": 0,
                    "moderate": moderate,
                    "high": high,
                    "critical": critical,
                    "total": high + critical + moderate,
                }
            },
        }
    )


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    return runtime / "node.exe", runtime / "node_modules" / "npm" / "bin" / "npm-cli.js"


def test_guard_file_exists() -> None:
    assert GUARD_PATH.is_file()


def test_projects_and_scopes_are_exact() -> None:
    guard = _load_guard()

    assert guard.PROJECT_NAMES == ("ToukaChat", "ToukaIDE")
    assert guard.AUDIT_SCOPES == (("runtime", True), ("full", False))


def test_current_projects_have_public_registry_locks() -> None:
    guard = _load_guard()

    assert guard._load_tracked_npm_policy() == guard.EXPECTED_NPM_POLICY
    for project_name in guard.PROJECT_NAMES:
        guard._validate_lockfile(ROOT / project_name)


def test_scan_api_requires_direct_node_and_npm_cli_paths() -> None:
    guard = _load_guard()
    parameters = inspect.signature(guard.scan_project).parameters

    assert "node_executable" in parameters
    assert "npm_cli" in parameters
    assert "npm_executable" not in parameters


def test_parse_rejects_malformed_or_error_shaped_json() -> None:
    guard = _load_guard()

    for output in ("", "not json", "[]", '{"error": {"code": "EAUDITNOLOCK"}}'):
        with pytest.raises(guard.AuditExecutionError):
            guard._parse_npm_audit_output(output)


def test_parse_returns_blocking_and_warning_counts() -> None:
    guard = _load_guard()

    blocking, warnings = guard._parse_npm_audit_output(
        _audit_payload(high=2, critical=1, moderate=3)
    )

    assert blocking == 3
    assert warnings == 3


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("high,exit_code", [(0, 0), (1, 1)])
def test_consume_result_is_pure_and_keeps_warning_policy(
    monkeypatch: pytest.MonkeyPatch, as_bytes: bool, high: int, exit_code: int
) -> None:
    guard = _load_guard()
    raw = _audit_payload(high=high, moderate=2)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pure audit consumption must not execute a command")

    monkeypatch.setattr(guard.subprocess, "run", forbidden)
    assert guard.consume_npm_audit_result(
        raw.encode() if as_bytes else raw, exit_code=exit_code
    ) == (high, 2)


@pytest.mark.parametrize(
    "raw,exit_code",
    [
        (b"\xff", 0),
        (b"{}", 2),
        (_audit_payload(high=1).encode(), 0),
        (_audit_payload(moderate=1).encode(), 1),
        (b'{"error": {"code": "EAUDITNOLOCK"}}', 0),
    ],
)
def test_consume_result_rejects_decode_exit_or_error_payload(raw: bytes, exit_code: int) -> None:
    guard = _load_guard()
    with pytest.raises(guard.AuditExecutionError):
        guard.consume_npm_audit_result(raw, exit_code=exit_code)


def test_scan_uses_shared_pure_result_consumer_and_public_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}', encoding="utf-8")
    observed = []
    commands = []
    original_command = guard.audit_command

    def audit_command(*args: object, **kwargs: object) -> list[str]:
        command = original_command(*args, **kwargs)
        commands.append(command)
        return command

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert commands == [command]
        return SimpleNamespace(returncode=0, stdout=b"original bytes", stderr=b"")

    def consume(stdout: bytes | str, **kwargs: object) -> tuple[int, int]:
        observed.append((stdout, kwargs))
        return 0, 2

    monkeypatch.setattr(guard, "consume_npm_audit_result", consume)
    monkeypatch.setattr(guard, "audit_command", audit_command)
    monkeypatch.setattr(guard.subprocess, "run", run)
    node_executable, npm_cli = _runtime_paths(tmp_path)
    assert guard.scan_project(
        project,
        scope_name="runtime",
        omit_dev=True,
        node_executable=node_executable,
        npm_cli=npm_cli,
        audit_root=tmp_path / "audit",
    ) == (0, 2)
    assert observed == [(b"original bytes", {"exit_code": 0})]


def test_parse_rejects_metadata_vulnerability_mismatch() -> None:
    guard = _load_guard()
    payload = json.loads(_audit_payload())
    payload["vulnerabilities"]["example-pkg"] = {"severity": "high"}

    with pytest.raises(guard.AuditExecutionError):
        guard._parse_npm_audit_output(json.dumps(payload))


def test_tracked_policy_rejects_custom_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    policy_root = tmp_path / "repo" / "ToukaChat"
    policy_root.mkdir(parents=True)
    (policy_root / ".npmrc").write_text(
        "audit=false\n"
        "engine-strict=true\n"
        "fund=false\n"
        "ignore-scripts=true\n"
        "registry=https://packages.example.invalid/\n"
        "update-notifier=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "PROJECT_ROOT", tmp_path / "repo")

    with pytest.raises(guard.AuditExecutionError):
        guard._load_tracked_npm_policy()


def test_scan_uses_argument_array_without_fix_or_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=_audit_payload(), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    assert guard.scan_project(
        project,
        scope_name="runtime",
        omit_dev=True,
        node_executable=node_executable,
        npm_cli=npm_cli,
        audit_root=tmp_path / "outside-audit",
    ) == (0, 0)
    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert command[0].endswith("node.exe")
    assert command[1].endswith("npm-cli.js")
    assert command[2:5] == ["audit", "--json", "--audit-level=high"]
    assert "--omit=dev" in command
    assert "fix" not in command
    assert "--force" not in command
    assert "--proxy=null" in command
    assert "--https-proxy=null" in command
    assert "--//registry.npmjs.org/:_authToken=" in command
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert "PATH" not in environment
    assert all(
        marker not in key.upper()
        for key in environment
        for marker in ("TOKEN", "AUTH", "CREDENTIAL", "PASSWORD")
    )


def test_non_public_lock_destination_stops_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "node_modules/example": {
                        "resolved": "https://packages.example.invalid/example.tgz"
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("npm must not run for a non-public lock destination")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="runtime",
            omit_dev=True,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_invalid_package_manifest_stops_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("not json\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("npm must not run with an invalid package manifest")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="runtime",
            omit_dev=True,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_nested_non_public_lock_destination_stops_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaIDE"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {},
                "dependencies": {
                    "example": {"resolved": "https://packages.example.invalid/example.tgz"}
                },
            }
        ),
        encoding="utf-8",
    )

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("npm must not run with a nested non-public lock destination")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="full",
            omit_dev=False,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_full_scope_does_not_omit_development_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaIDE"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=_audit_payload(), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    guard.scan_project(
        project,
        scope_name="full",
        omit_dev=False,
        node_executable=node_executable,
        npm_cli=npm_cli,
        audit_root=tmp_path / "outside-audit",
    )

    assert "--omit=dev" not in commands[0]


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (2, ""),
        (1, ""),
        (1, "not json"),
        (0, "not json"),
        (1, _audit_payload()),
    ],
)
def test_scan_rejects_transport_parser_and_exit_code_inconsistency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="audit failed",
        ),
    )
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="runtime",
            omit_dev=True,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_missing_lock_stops_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaIDE"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("npm must not run without a committed lockfile")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="full",
            omit_dev=False,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_timeout_is_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")

    def timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=["npm"], timeout=1)

    monkeypatch.setattr(guard.subprocess, "run", timeout)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError):
        guard.scan_project(
            project,
            scope_name="runtime",
            omit_dev=True,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_main_runs_four_project_scope_pairs_outside_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    calls: list[tuple[str, str, bool, Path]] = []

    monkeypatch.setattr(guard, "PROJECT_ROOT", tmp_path / "repo")
    monkeypatch.setattr(guard, "_load_tracked_npm_policy", lambda: {})
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        guard,
        "_resolve_npm",
        lambda: (node_executable, npm_cli),
    )

    def fake_scan(
        project: Path,
        *,
        scope_name: str,
        omit_dev: bool,
        node_executable: Path,
        npm_cli: Path,
        audit_root: Path,
    ) -> tuple[int, int]:
        del node_executable, npm_cli
        calls.append((project.name, scope_name, omit_dev, audit_root))
        return 0, 0

    monkeypatch.setattr(guard, "scan_project", fake_scan)
    monkeypatch.setattr(
        guard.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TemporaryDirectory(tmp_path / "os-temp"),
    )

    assert guard.main() == 0
    assert [(project, scope, omit) for project, scope, omit, _root in calls] == [
        ("ToukaChat", "runtime", True),
        ("ToukaChat", "full", False),
        ("ToukaIDE", "runtime", True),
        ("ToukaIDE", "full", False),
    ]
    assert all(not root.is_relative_to(guard.PROJECT_ROOT) for *_, root in calls)


class _TemporaryDirectory:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        self._path.mkdir(parents=True, exist_ok=True)
        return str(self._path)

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize("failure", ["success", "decode", "timeout", "oserror"])
def test_scan_saves_raw_output_inputs_and_bounded_retry_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_bytes(b"{}\r\n")
    (project / "package-lock.json").write_bytes(b'{"packages": {}}\r\n')
    audit_root = tmp_path / "evidence"
    scope_root = audit_root / "ToukaChat-runtime"
    stdout = b"bad\xff" if failure == "decode" else _audit_payload().encode()

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert "--fetch-retries=0" in command
        assert kwargs["text"] is False
        assert kwargs["timeout"] == 300
        assert (scope_root / "inputs" / "package-lock.json").read_bytes() == (
            project / "package-lock.json"
        ).read_bytes()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 300, output=stdout, stderr=b"partial")
        if failure == "oserror":
            message = "synthetic launch failure"
            raise OSError(message)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"diagnostic\xfe")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)
    arguments = {
        "scope_name": "runtime",
        "omit_dev": True,
        "node_executable": node_executable,
        "npm_cli": npm_cli,
        "audit_root": audit_root,
    }
    if failure == "success":
        assert guard.scan_project(project, **arguments) == (0, 0)
    else:
        with pytest.raises(guard.AuditExecutionError):
            guard.scan_project(project, **arguments)
    process_root = scope_root / "npm-audit"
    assert (process_root / "stdout.bin").read_bytes() == (b"" if failure == "oserror" else stdout)
    receipt = json.loads((process_root / "receipt.json").read_text())
    expected_status = {"timeout": "timeout", "oserror": "error"}.get(failure, "completed")
    assert receipt["status"] == expected_status


def test_evidence_cli_rejects_repo_output_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    monkeypatch.setattr(guard, "PROJECT_ROOT", tmp_path)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid evidence path must stop before running a child")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    assert guard.main(["--evidence-dir", str(tmp_path / "evidence")]) == 1
    assert not (tmp_path / "evidence").exists()


def test_evidence_cli_snapshots_all_inputs_before_first_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    root = tmp_path / "retained"
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(guard, "_resolve_npm", lambda: (node_executable, npm_cli))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        for project_name in ("ToukaChat", "ToukaIDE"):
            for filename in ("package.json", "package-lock.json"):
                assert (root / "inputs" / project_name / filename).is_file()
        return SimpleNamespace(returncode=0, stdout=_audit_payload().encode(), stderr=b"")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.main(["--evidence-dir", str(root)]) == 0
    assert len(commands) == 4
    for project_name in ("ToukaChat", "ToukaIDE"):
        for scope_name in ("runtime", "full"):
            assert (root / f"{project_name}-{scope_name}" / "npm-audit" / "receipt.json").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native runtime contract")
def test_isolated_environment_bootstraps_windows_native_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    monkeypatch.setenv("AUDIT_TEST_CREDENTIAL", "synthetic-must-not-propagate")
    environment = guard._isolated_environment(
        tmp_path,
        cache=tmp_path / "cache",
        logs=tmp_path / "logs",
        user_config=tmp_path / "user.npmrc",
        global_config=tmp_path / "global.npmrc",
    )
    result = subprocess.run(  # noqa: S603 -- fixed interpreter, standard-library-only probe
        [sys.executable, "-B", "-c", "import _overlapped; import asyncio"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=False,
        stdin=subprocess.DEVNULL,
        shell=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "SystemRoot" in environment
    assert all(key not in environment for key in ("WINDIR", "PATH", "AUDIT_TEST_CREDENTIAL"))

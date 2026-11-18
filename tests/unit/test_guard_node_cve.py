"""Unit tests for the fail-closed Node dependency audit guard."""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
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


def _load_report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dep_audit_report_under_test", ROOT / "scripts" / "dep_audit_report.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 要從 sys.modules 找到模組
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


def _audit_payload_with(vulnerabilities: dict[str, dict[str, object]]) -> str:
    counts = dict.fromkeys(("info", "low", "moderate", "high", "critical"), 0)
    for finding in vulnerabilities.values():
        counts[str(finding["severity"])] += 1
    return json.dumps(
        {
            "auditReportVersion": 2,
            "vulnerabilities": vulnerabilities,
            "metadata": {"vulnerabilities": {**counts, "total": sum(counts.values())}},
        }
    )


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    return runtime / "node.exe", runtime / "node_modules" / "npm" / "bin" / "npm-cli.js"


def _fake_repo(tmp_path: Path) -> Path:
    """Create the five tracked npm sources the guard snapshots at start."""
    repo = tmp_path / "repo"
    for project_name in ("ToukaChat", "ToukaIDE"):
        project = repo / project_name
        project.mkdir(parents=True)
        (project / "package.json").write_text("{}\n", encoding="utf-8")
        (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    (repo / "ToukaChat" / ".npmrc").write_text("registry=https://registry.npmjs.org/\n")
    (repo / "ToukaIDE" / ".npmrc").write_text("min-release-age=3\n")
    return repo


def _scan_with_stdout(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    *,
    returncode: int = 1,
) -> tuple[int, int]:
    project = tmp_path / "ToukaIDE"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout=stdout.encode(), stderr=b""
        ),
    )
    node_executable, npm_cli = _runtime_paths(tmp_path)
    return guard.scan_project(
        project,
        scope_name="full",
        omit_dev=False,
        node_executable=node_executable,
        npm_cli=npm_cli,
        audit_root=tmp_path / "outside-audit",
    )


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
    times: set[datetime] = set()

    monkeypatch.setattr(guard, "PROJECT_ROOT", _fake_repo(tmp_path))
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
        now: datetime,
    ) -> tuple[int, int]:
        del node_executable, npm_cli
        calls.append((project.name, scope_name, omit_dev, audit_root))
        times.add(now)
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
    assert len(times) == 1  # 一次執行只取一次時間，四個範圍看到的例外一致


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


def test_lock_without_sha512_integrity_stops_before_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    project = tmp_path / "ToukaIDE"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "touka-ide"},
                    "node_modules/example": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("npm must not run for a lock entry without sha512 integrity")

    monkeypatch.setattr(guard.subprocess, "run", unexpected_run)
    node_executable, npm_cli = _runtime_paths(tmp_path)

    with pytest.raises(guard.AuditExecutionError, match="node_modules/example"):
        guard.scan_project(
            project,
            scope_name="full",
            omit_dev=False,
            node_executable=node_executable,
            npm_cli=npm_cli,
            audit_root=tmp_path / "outside-audit",
        )


def test_scan_prints_blocking_package_names_and_advisory_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    payload = _audit_payload_with(
        {
            "body-parser": {
                "severity": "high",
                "via": [
                    {
                        "name": "body-parser",
                        "severity": "high",
                        "title": "\x1b[2Jterminal control in a title",
                        "url": "https://github.com/advisories/GHSA-27r3-jxmh-p828",
                    },
                    {
                        "severity": "moderate",
                        "url": "https://github.com/advisories/GHSA-5555-6666-7777",
                    },
                ],
            },
            "@vscode/vsce": {"severity": "high", "via": ["@azure/msal-node"]},
            "evil\x1b[2Jname": {"severity": "critical", "via": ["also\x1bbad"]},
            "left-pad": {
                "severity": "moderate",
                "via": [
                    {
                        "severity": "moderate",
                        "url": "https://github.com/advisories/GHSA-2222-3333-4444",
                    }
                ],
            },
        }
    )

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, payload) == (3, 1)
    output = capsys.readouterr().out
    assert "high body-parser GHSA-27r3-jxmh-p828" in output
    assert "high @vscode/vsce via @azure/msal-node" in output
    assert "critical <unprintable>" in output
    assert "left-pad" not in output
    assert "GHSA-2222-3333-4444" not in output
    assert "GHSA-5555-6666-7777" not in output
    assert "\x1b" not in output
    assert "terminal control" not in output


@pytest.mark.parametrize(
    "finding",
    [
        {"severity": "high"},
        {"severity": "high", "via": ["only-a-name"]},
        {"severity": "high", "via": 5},
        {"severity": "high", "via": [5, None, {"severity": 3, "url": 7}]},
        {"severity": "high", "via": [{"severity": "high"}]},
    ],
)
def test_blocking_count_survives_any_via_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, finding: dict[str, object]
) -> None:
    guard = _load_guard()
    payload = _audit_payload_with({"pkg": finding})

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, payload) == (1, 0)


def test_main_scans_every_scope_and_counts_a_full_scope_high(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(guard, "_resolve_npm", lambda: (node_executable, npm_cli))
    commands: list[list[str]] = []
    full_high = _audit_payload_with(
        {
            "body-parser": {
                "severity": "high",
                "via": [
                    {
                        "severity": "high",
                        "url": "https://github.com/advisories/GHSA-27r3-jxmh-p828",
                    }
                ],
            }
        }
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if "--omit=dev" in command:
            return SimpleNamespace(returncode=0, stdout=_audit_payload().encode(), stderr=b"")
        return SimpleNamespace(returncode=1, stdout=full_high.encode(), stderr=b"")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        guard.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TemporaryDirectory(tmp_path / "os-temp"),
    )

    assert guard.main() == 1
    assert len(commands) == 4
    output = capsys.readouterr().out
    assert "[FAIL] 2 HIGH/CRITICAL finding(s) across audited scopes." in output
    assert "evidence invalid" not in output


def _run_main_with_fake_scan(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: Path,
    *,
    policy_loader: Any = None,
    on_scan: Any = None,
) -> int:
    monkeypatch.setattr(guard, "PROJECT_ROOT", repo)
    monkeypatch.setattr(guard, "_load_tracked_npm_policy", policy_loader or (lambda: {}))
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(guard, "_resolve_npm", lambda: (node_executable, npm_cli))

    def fake_scan(project: Path, **_kwargs: object) -> tuple[int, int]:
        if on_scan is not None:
            on_scan(project)
        return 0, 0

    monkeypatch.setattr(guard, "scan_project", fake_scan)
    monkeypatch.setattr(
        guard.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TemporaryDirectory(tmp_path / "os-temp"),
    )
    return guard.main()


@pytest.mark.parametrize(
    "relative",
    [
        "ToukaChat/.npmrc",
        "ToukaIDE/.npmrc",
        "ToukaChat/package.json",
        "ToukaChat/package-lock.json",
        "ToukaIDE/package.json",
        "ToukaIDE/package-lock.json",
    ],
)
def test_main_aborts_when_a_tracked_source_changes_during_the_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    relative: str,
) -> None:
    guard = _load_guard()
    repo = _fake_repo(tmp_path)

    def change_source(project: Path) -> None:
        if project.name == "ToukaIDE":
            with (repo / relative).open("a", encoding="utf-8") as handle:
                handle.write("\n")

    assert _run_main_with_fake_scan(guard, monkeypatch, tmp_path, repo, on_scan=change_source) == 1
    assert "changed during execution" in capsys.readouterr().out


def test_source_snapshot_is_taken_before_the_policy_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    repo = _fake_repo(tmp_path)

    def policy_loader_that_races() -> dict[str, str]:
        (repo / "ToukaChat" / ".npmrc").write_text("registry=https://example.invalid/\n")
        return {}

    assert (
        _run_main_with_fake_scan(
            guard, monkeypatch, tmp_path, repo, policy_loader=policy_loader_that_races
        )
        == 1
    )
    assert "changed during execution" in capsys.readouterr().out


def test_main_has_no_profile_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guard = _load_guard()
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(guard, "_resolve_npm", lambda: (node_executable, npm_cli))

    def no_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a rejected option must stop before any npm audit runs")

    monkeypatch.setattr(guard.subprocess, "run", no_network)

    with pytest.raises(SystemExit):
        guard.main(["--profile", "node-lock-and-braces-backport-v1"])


def test_missing_tracked_source_fails_with_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    repo = _fake_repo(tmp_path)
    (repo / "ToukaChat" / ".npmrc").unlink()

    assert _run_main_with_fake_scan(guard, monkeypatch, tmp_path, repo) == 1
    output = capsys.readouterr().out
    assert "tracked npm source is unavailable" in output
    assert str(tmp_path) not in output


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


# --------------------------------------------------------------------------- #
# 維護者核准、會到期的公告例外（ADR-0321）
# --------------------------------------------------------------------------- #

EXCUSED = "GHSA-fc5q-39xc-vjpf"
OTHER = "GHSA-27r3-jxmh-p828"
LATER = "GHSA-2222-3333-4444"
APPROVED_AT = datetime(2026, 11, 18, tzinfo=UTC)
BEFORE_EXPIRY = APPROVED_AT + timedelta(days=2)


def _advisory(advisory: str, severity: str = "high") -> dict[str, str]:
    return {"severity": severity, "url": f"https://github.com/advisories/{advisory}"}


def _chain(**extra: dict[str, object]) -> str:
    """picomatch 本身有公告，micromatch、fast-glob 經它變成 high（和真的 npm audit 一樣）。"""
    return _audit_payload_with(
        {
            "picomatch": {"severity": "high", "via": [_advisory(EXCUSED)]},
            "micromatch": {"severity": "high", "via": ["picomatch"]},
            "fast-glob": {"severity": "high", "via": ["micromatch"]},
            **extra,
        }
    )


def _with_exception(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime = BEFORE_EXPIRY,
    advisory: str = EXCUSED,
    severity: str = "high",
    approved_at: datetime = APPROVED_AT,
    expires_at: datetime = APPROVED_AT + timedelta(days=7),
) -> None:
    exception = guard.AdvisoryException(
        advisory, severity, approved_at, expires_at, "maintainer test"
    )
    monkeypatch.setattr(guard, "ADVISORY_EXCEPTIONS", (exception,))
    monkeypatch.setattr(guard, "_current_time", lambda: now)


def _scan_scope(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    scope_name: str,
) -> tuple[int, int]:
    project = tmp_path / "ToukaChat"
    project.mkdir()
    (project / "package.json").write_text("{}\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"packages": {}}\n', encoding="utf-8")
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=stdout.encode(), stderr=b""),
    )
    node_executable, npm_cli = _runtime_paths(tmp_path)
    return guard.scan_project(
        project,
        scope_name=scope_name,
        omit_dev=scope_name == "runtime",
        node_executable=node_executable,
        npm_cli=npm_cli,
        audit_root=tmp_path / "outside-audit",
    )


def test_an_approved_exception_excuses_its_advisory_in_the_full_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain()) == (0, 0)
    output = capsys.readouterr().out
    assert f"  excused high picomatch {EXCUSED}" in output
    assert f"  excused high fast-glob {EXCUSED}" in output
    assert "  high " not in output  # 每日檢查數的是這種開頭；放行的不算進去


def test_the_runtime_scope_never_uses_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)

    assert _scan_scope(guard, monkeypatch, tmp_path, _chain(), "runtime") == (3, 0)
    assert "excused" not in capsys.readouterr().out


def test_a_package_with_another_high_reason_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)
    payload = _chain(
        vite={"severity": "high", "via": ["fast-glob", _advisory(OTHER)]},
        vitest={"severity": "high", "via": ["vite"]},
    )

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, payload) == (2, 0)
    output = capsys.readouterr().out
    assert f"  high vite {OTHER} via fast-glob" in output
    assert "  high vitest via vite" in output
    assert f"  excused high micromatch {EXCUSED}" in output


def test_a_lower_severity_advisory_on_the_way_does_not_stop_the_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)
    payload = _chain(knip={"severity": "high", "via": ["fast-glob", _advisory(OTHER, "moderate")]})

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, payload) == (0, 0)


@pytest.mark.parametrize(
    "expires_at",
    [BEFORE_EXPIRY, BEFORE_EXPIRY - timedelta(seconds=1)],
    ids=["expires-now", "already-expired"],
)
def test_an_expired_exception_blocks_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, expires_at: datetime
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch, expires_at=expires_at)

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain()) == (3, 0)


@pytest.mark.parametrize(
    "finding",
    [
        {"severity": "high", "via": ["not-in-the-report"]},
        {"severity": "high", "via": ["picomatch", "not-in-the-report"]},
        {"severity": "high", "via": [{"severity": "high"}]},
        {"severity": "high", "via": [{"severity": "high", "url": f"{EXCUSED} {EXCUSED}"}]},
        {
            "severity": "high",
            "via": [{"severity": "high", "url": f"https://example.com/{EXCUSED}"}],
        },
        {"severity": "high", "via": ["picomatch", {**_advisory(OTHER), "severity": "unknown"}]},
        {"severity": "high", "via": [_advisory(OTHER, "moderate")]},
        {"severity": "high", "via": ["picomatch", 5]},
        {"severity": "high", "via": []},
        {"severity": "high"},
    ],
)
def test_reasons_that_cannot_be_read_are_never_excused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, finding: dict[str, object]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain(odd=finding)) == (1, 0)


def test_an_exception_approved_later_is_not_active_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """年份打錯成未來：不是延長，而是不生效、照擋。"""
    guard = _load_guard()
    later = BEFORE_EXPIRY + timedelta(days=365)
    _with_exception(guard, monkeypatch, approved_at=later, expires_at=later + timedelta(days=7))

    assert guard.active_exceptions(BEFORE_EXPIRY) == {}
    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain()) == (3, 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"odd": {"severity": "critical", "via": [_advisory(EXCUSED, "critical")]}},
        {"odd": {"severity": "critical", "via": ["picomatch"]}},
    ],
    ids=["advisory-raised-to-critical", "package-more-severe-than-its-reasons"],
)
def test_anything_more_severe_than_approved_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: dict[str, dict[str, object]]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain(**extra)) == (1, 0)


def test_a_repeated_advisory_keeps_the_stricter_severity(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _load_guard()
    window = (APPROVED_AT, APPROVED_AT + timedelta(days=7), "test")
    monkeypatch.setattr(
        guard,
        "ADVISORY_EXCEPTIONS",
        (
            guard.AdvisoryException(EXCUSED, "critical", *window),
            guard.AdvisoryException(EXCUSED, "high", *window),
        ),
    )

    assert guard.active_exceptions(BEFORE_EXPIRY) == {EXCUSED: guard.SEVERITY_RANK["high"]}


def test_a_via_cycle_is_followed_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)
    payload = _chain(
        a={"severity": "high", "via": ["b", "picomatch"]},
        b={"severity": "high", "via": ["a"]},
    )

    assert _scan_with_stdout(guard, monkeypatch, tmp_path, payload) == (0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"advisory": "GHSA-fc5q-39xc-vjp"},
        {"advisory": "picomatch"},
        {"severity": "moderate"},
        {"severity": "High"},
        {"approved_at": datetime(2026, 11, 18)},  # 沒有時區就不算數
        {"expires_at": datetime(2026, 11, 25)},
        {"expires_at": APPROVED_AT + timedelta(days=15)},
        {"expires_at": APPROVED_AT},
    ],
)
def test_a_malformed_exception_is_never_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any]
) -> None:
    guard = _load_guard()
    now = APPROVED_AT + timedelta(days=1)  # 格式對的話這時應該有效
    _with_exception(guard, monkeypatch, now=now, **change)

    assert not guard._is_well_formed(guard.ADVISORY_EXCEPTIONS[0])
    assert guard.active_exceptions(now) == {}
    assert _scan_with_stdout(guard, monkeypatch, tmp_path, _chain()) == (3, 0)


def test_the_approved_exceptions_are_well_formed() -> None:
    """不看今天的日期：到期後由守衛變紅提醒，測試不跟著日期變紅（同 ADR-0299）。"""
    guard = _load_guard()
    advisories = [exception.advisory for exception in guard.ADVISORY_EXCEPTIONS]
    assert len(advisories) == len(set(advisories))
    for exception in guard.ADVISORY_EXCEPTIONS:
        assert guard._is_well_formed(exception), exception.advisory
    assert guard.MAX_EXCEPTION_DAYS == 14
    assert guard.EXCEPTION_SCOPE == "full"


def test_main_says_which_exceptions_are_active_or_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    expired = guard.AdvisoryException(
        OTHER, "high", APPROVED_AT - timedelta(days=9), APPROVED_AT - timedelta(days=1), "test"
    )
    active = guard.AdvisoryException(
        EXCUSED, "high", APPROVED_AT, APPROVED_AT + timedelta(days=7), "maintainer test"
    )
    later = guard.AdvisoryException(
        LATER, "high", BEFORE_EXPIRY + timedelta(days=1), BEFORE_EXPIRY + timedelta(days=8), "test"
    )
    monkeypatch.setattr(guard, "ADVISORY_EXCEPTIONS", (active, expired, later))
    monkeypatch.setattr(guard, "_current_time", lambda: BEFORE_EXPIRY)

    assert _run_main_with_fake_scan(guard, monkeypatch, tmp_path, _fake_repo(tmp_path)) == 0
    output = capsys.readouterr().out
    assert f"[EXCEPTION] {EXCUSED} " in output
    assert "2026-11-25" in output
    assert f"[EXPIRED] {OTHER} " in output
    assert f"[WARN] {LATER} exception starts " in output
    assert "outside the exceptions above" in output  # 有例外時不說「完全沒有」


def test_the_daily_report_counts_only_the_findings_that_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_guard()
    _with_exception(guard, monkeypatch)
    node_executable, npm_cli = _runtime_paths(tmp_path)
    monkeypatch.setattr(guard, "_resolve_npm", lambda: (node_executable, npm_cli))
    full = _chain(vite={"severity": "high", "via": ["fast-glob", _advisory(OTHER)]})

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "--omit=dev" in command:
            return SimpleNamespace(returncode=0, stdout=_audit_payload().encode(), stderr=b"")
        return SimpleNamespace(returncode=1, stdout=full.encode(), stderr=b"")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        guard.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: _TemporaryDirectory(tmp_path / "os-temp"),
    )

    assert guard.main() == 1
    output = capsys.readouterr().out
    assert "[FAIL] 2 HIGH/CRITICAL finding(s) across audited scopes." in output
    findings, complete = _load_report()._parse_node(output)
    assert complete
    assert {finding.package for finding in findings} == {"vite"}

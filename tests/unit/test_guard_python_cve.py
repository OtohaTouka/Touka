"""Unit tests for the fail-closed Python dependency audit guard."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import guard_python_cve  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_coverage_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess doubles export fixture packages, never the real product graph."""
    projects = {".": {"project": {"name": "unit-project", "version": "0.1.0"}}}
    rows = [
        {"name": "unit-project", "version": "0.1.0", "source": {"virtual": "."}},
        {
            "name": "example-pkg",
            "version": "1.0.0",
            "source": {"registry": "https://pypi.org/simple"},
        },
        {
            "name": "uncovered-pkg",
            "version": "1.0.0",
            "source": {"registry": "https://pypi.org/simple"},
        },
    ]
    monkeypatch.setattr(
        guard_python_cve, "_load_coverage_context", lambda: ({"package": rows}, projects, {})
    )


@pytest.mark.parametrize("member", ["../data", "data", ".gitignore", "../outside", "Member"])
def test_workspace_boundary_rejects_untrusted_member_before_content_read(
    member: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    root_manifest = repository / "pyproject.toml"
    root_manifest.write_text(
        "[tool.uv.workspace]\nmembers = [" + json.dumps(member) + "]\n", encoding="utf-8"
    )
    monkeypatch.setattr(guard_python_cve, "PROJECT_ROOT", repository)
    original_read = Path.read_text
    reads: list[Path] = []

    def read_only_root(path: Path, *args: object, **kwargs: object) -> str:
        reads.append(path)
        assert path == root_manifest, "untrusted member was read before rejection"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_only_root)
    if member == "Member":
        original_lstat = Path.lstat

        def reject_reparse(path: Path) -> object:
            if path == repository / "Member":
                return SimpleNamespace(st_mode=0, st_file_attributes=0x400)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", reject_reparse)
    with pytest.raises(guard_python_cve.AuditExecutionError):
        guard_python_cve._workspace_inputs()
    assert reads == [root_manifest]


def _pip_audit_payload(*, severity: str | None = None) -> str:
    vulnerability: dict[str, object] = {
        "id": "GHSA-xxxx-yyyy-zzzz",
        "fix_versions": ["1.0.1"],
        "description": "test advisory",
        "aliases": ["CVE-2026-1234"],
    }
    if severity is not None:
        vulnerability["severity"] = severity
    return json.dumps(
        {
            "dependencies": [
                {
                    "name": "example-pkg",
                    "version": "1.0.0",
                    "vulns": [vulnerability],
                }
            ]
        }
    )


def test_parse_clean_output_returns_empty() -> None:
    result = guard_python_cve._parse_pip_audit_output('{"dependencies": []}')

    assert result.vulnerabilities == []
    assert result.skipped == []


@pytest.mark.parametrize("as_bytes", [False, True])
def test_consume_result_is_pure_and_uses_complete_normalized_coverage(
    monkeypatch: pytest.MonkeyPatch, as_bytes: bool
) -> None:
    raw = json.dumps({"dependencies": [{"name": "Example_Pkg", "version": "1.0.0", "vulns": []}]})

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("pure audit consumption must not execute a command")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", forbidden)
    result = guard_python_cve.consume_pip_audit_result(
        raw.encode() if as_bytes else raw,
        exit_code=0,
        expected=(("example-pkg", "1.0.0"),),
    )
    assert result == guard_python_cve.AuditResult(blocking=[], warnings=[], skipped=[])


@pytest.mark.parametrize("severity,blocking", [("LOW", False), ("HIGH", True), (None, True)])
def test_consume_result_keeps_original_severity_policy(
    severity: str | None, blocking: bool
) -> None:
    result = guard_python_cve.consume_pip_audit_result(
        _pip_audit_payload(severity=severity).encode(),
        exit_code=1,
        expected=(("example-pkg", "1.0.0"),),
    )
    assert len(result.blocking) == int(blocking)
    assert len(result.warnings) == int(not blocking)
    assert result.skipped == []


@pytest.mark.parametrize(
    "raw,exit_code,expected",
    [
        (b"\xff", 0, ()),
        (b"{}", 2, ()),
        (b'{"dependencies": []}', 1, ()),
        (_pip_audit_payload(severity="LOW").encode(), 0, (("example-pkg", "1.0.0"),)),
        (b'{"dependencies": []}', 0, (("missing-pkg", "1.0.0"),)),
    ],
)
def test_consume_result_rejects_decode_exit_and_coverage_mismatch(
    raw: bytes, exit_code: int, expected: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(guard_python_cve.AuditExecutionError):
        guard_python_cve.consume_pip_audit_result(raw, exit_code=exit_code, expected=expected)


def test_consume_result_preserves_skip_for_original_main_fail_closed_policy() -> None:
    raw = b'{"dependencies": [{"name": "uncovered-pkg", "skip_reason": "not found"}]}'
    result = guard_python_cve.consume_pip_audit_result(
        raw, exit_code=0, expected=(("uncovered-pkg", "1.0.0"),)
    )
    assert result.skipped == [{"package": "uncovered-pkg", "reason": "not found"}]
    assert result.blocking == result.warnings == []


def test_scan_uses_shared_pure_result_consumer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "query.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    observed = []
    expected_result = guard_python_cve.AuditResult(blocking=[], warnings=[], skipped=[])

    def consume(stdout: bytes | str, **kwargs: object) -> guard_python_cve.AuditResult:
        observed.append((stdout, kwargs))
        return expected_result

    monkeypatch.setattr(guard_python_cve, "consume_pip_audit_result", consume)
    monkeypatch.setattr(
        guard_python_cve,
        "_run_audit_child",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=b"original bytes", stderr=b""
        ),
    )
    assert guard_python_cve.scan_requirements(requirements) is expected_result
    assert observed == [
        (b"original bytes", {"exit_code": 0, "expected": (("example-pkg", "1.0.0"),)})
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"name": "other-pkg", "version": "1.0.0", "vulns": []}],
        [{"name": "example-pkg", "version": "2.0.0", "vulns": []}],
        [
            {"name": "example-pkg", "version": "1.0.0", "vulns": []},
            {"name": "extra-pkg", "version": "1.0.0", "vulns": []},
        ],
        [
            {"name": "example-pkg", "version": "1.0.0", "vulns": []},
            {"name": "example_pkg", "version": "1.0.0", "vulns": []},
        ],
    ],
)
def test_scan_rejects_incomplete_or_mismatched_dependency_coverage(
    rows: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(
        guard_python_cve.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({"dependencies": rows}), stderr=""
        ),
    )

    with pytest.raises(guard_python_cve.AuditExecutionError, match="coverage"):
        guard_python_cve.scan_requirements(requirements)


@pytest.mark.parametrize("content", ["", " \n\t", "# exported requirements\n# comment\n"])
def test_scan_rejects_empty_query_before_audit(
    content: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(content, encoding="utf-8")
    commands: list[object] = []

    def empty_audit(*args: object, **_kwargs: object) -> SimpleNamespace:
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout='{"dependencies": []}', stderr="")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", empty_audit)
    with pytest.raises(guard_python_cve.AuditExecutionError, match="empty"):
        guard_python_cve.scan_requirements(requirements)
    assert commands == []


def test_main_invalid_requirement_does_not_disclose_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_marker = "synthetic-audit-password"
    retained = tmp_path / "retained"

    def export_invalid_requirement(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert "--requirement" not in command
        if "export" in command:
            Path(command[command.index("-o") + 1]).write_text(
                f"--index-url https://user:{credential_marker}@example.invalid/simple\n",
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0, stdout=b"tool output", stderr=b"")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", export_invalid_requirement)
    assert guard_python_cve.main(["--evidence-dir", str(retained)]) == 1
    captured = capsys.readouterr()
    failure = (retained / "failure.json").read_text(encoding="utf-8")
    for output in (captured.out, captured.err, failure):
        assert credential_marker not in output
        assert "example.invalid" not in output


def test_parse_preserves_authoritative_severity() -> None:
    result = guard_python_cve._parse_pip_audit_output(_pip_audit_payload(severity="HIGH"))

    assert result.vulnerabilities == [
        {
            "package": "example-pkg",
            "version": "1.0.0",
            "id": "GHSA-xxxx-yyyy-zzzz",
            "fix_versions": ["1.0.1"],
            "description": "test advisory",
            "aliases": ["CVE-2026-1234"],
            "severity": "HIGH",
        }
    ]
    assert result.skipped == []


@pytest.mark.parametrize("output", ["", "not json", "[]", '{"dependencies": {}}'])
def test_parse_rejects_missing_or_malformed_json(output: str) -> None:
    error_type = getattr(guard_python_cve, "AuditExecutionError", RuntimeError)

    with pytest.raises(error_type):
        guard_python_cve._parse_pip_audit_output(output)


@pytest.mark.parametrize(
    "dependency",
    [
        {"name": "skipped-pkg"},
        {"name": "skipped-pkg", "skip_reason": ""},
        {"name": "skipped-pkg", "skip_reason": " \t"},
        {"name": "skipped-pkg", "skip_reason": None},
        {"name": "skipped-pkg", "skip_reason": 1},
        {"name": "skipped-pkg", "skip_reason": []},
        {"skip_reason": "not found"},
        {"name": "", "skip_reason": "not found"},
        {"name": " \t", "skip_reason": "not found"},
        {"name": 1, "skip_reason": "not found"},
        {"name": "skipped-pkg", "skip_reason": "not found", "vulns": []},
        {"name": "skipped-pkg", "skip_reason": "not found", "vulns": None},
        {"name": "skipped-pkg", "skip_reason": "not found", "version": "1.0.0"},
        {
            "name": "skipped-pkg",
            "skip_reason": "not found",
            "version": "1.0.0",
            "vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}],
        },
    ],
)
def test_parse_rejects_invalid_or_contradictory_skip_rows(dependency: dict[str, object]) -> None:
    with pytest.raises(guard_python_cve.AuditExecutionError):
        guard_python_cve._parse_pip_audit_output(json.dumps({"dependencies": [dependency]}))


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH", "UNKNOWN", ""])
def test_blocking_policy_fails_closed_for_high_or_unrated(severity: str) -> None:
    assert guard_python_cve._severity_blocks(severity) is True


@pytest.mark.parametrize("severity", ["MEDIUM", "MODERATE", "LOW"])
def test_blocking_policy_warns_for_known_lower_severity(severity: str) -> None:
    assert guard_python_cve._severity_blocks(severity) is False


def test_scan_blocks_vulnerability_without_authoritative_severity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert environment["PIP_NO_INPUT"] == "1"
        assert environment["PIP_NO_INDEX"] == "1"
        assert all(
            marker not in key.upper()
            for key in environment
            for marker in ("TOKEN", "AUTH", "CREDENTIAL", "PASSWORD")
        )
        return SimpleNamespace(
            returncode=1,
            stdout=_pip_audit_payload(),
            stderr="",
        )

    monkeypatch.setattr(
        guard_python_cve.subprocess,
        "run",
        fake_run,
    )

    result = guard_python_cve.scan_requirements(requirements)

    assert len(result.blocking) == 1
    assert result.warnings == []
    assert result.skipped == []
    assert "--disable-pip" in commands[0]
    assert commands[0][commands[0].index("--vulnerability-service") + 1] == "pypi"


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (2, "", "resolver failed"),
        (1, "", "network failed"),
        (1, "not json", ""),
        (0, "not json", ""),
        (1, '{"dependencies": []}', "unexpected empty finding set"),
        (0, _pip_audit_payload(severity="LOW"), "contradictory clean exit"),
    ],
)
def test_scan_rejects_execution_errors_instead_of_reporting_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(
        guard_python_cve.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )
    error_type = getattr(guard_python_cve, "AuditExecutionError", RuntimeError)

    with pytest.raises(error_type):
        guard_python_cve.scan_requirements(requirements)


@pytest.mark.parametrize("returncode", [0, 1])
def test_skips_do_not_exempt_exit_and_finding_contradictions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int
) -> None:
    payload = json.loads(_pip_audit_payload(severity="LOW"))
    findings = payload["dependencies"] if returncode == 0 else []
    payload["dependencies"] = [
        {"name": "uncovered-pkg", "skip_reason": "not found on PyPI"},
        *findings,
    ]
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("uncovered-pkg==1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(
        guard_python_cve.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout=json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(guard_python_cve.AuditExecutionError, match="vulnerability evidence"):
        guard_python_cve.scan_requirements(requirements)


_CLEAN_AUDIT = b'{"dependencies": [{"name": "example-pkg", "version": "1.0.0", "vulns": []}]}'


def _scripted_audit(
    monkeypatch: pytest.MonkeyPatch, outputs: list[tuple[int, bytes]]
) -> list[list[str]]:
    """Each audit child returns the next scripted (exit, stdout); a third call fails the test."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if "export" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"example-pkg==1.0.0\n")
            return SimpleNamespace(returncode=0, stdout=b"tool output", stderr=b"")
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout=b"pip-audit 2.10.1", stderr=b"")
        returncode, stdout = outputs.pop(0)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"network down")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)
    return commands


@pytest.mark.parametrize("returncode", [0, 1, 2])
@pytest.mark.parametrize("empty", [b"", b" \r\n"])
def test_empty_audit_output_is_retried_once_with_its_own_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, empty: bytes
) -> None:
    requirements = tmp_path / "runtime.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    commands = _scripted_audit(monkeypatch, [(returncode, empty), (0, _CLEAN_AUDIT)])

    result = guard_python_cve.scan_requirements(requirements)

    assert result == guard_python_cve.AuditResult(blocking=[], warnings=[], skipped=[])
    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert (tmp_path / "audit-runtime" / "stdout.bin").read_bytes() == empty
    assert (tmp_path / "audit-runtime-retry" / "stdout.bin").read_bytes() == _CLEAN_AUDIT


def test_audit_that_stays_empty_is_reported_as_not_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = tmp_path / "runtime.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    commands = _scripted_audit(monkeypatch, [(1, b""), (1, b"\n")])

    with pytest.raises(guard_python_cve.AuditNotRunError, match="did not run"):
        guard_python_cve.scan_requirements(requirements)
    assert len(commands) == 2
    assert issubclass(guard_python_cve.AuditNotRunError, guard_python_cve.AuditExecutionError)


@pytest.mark.parametrize(
    "error", [OSError("spawn failed"), subprocess.TimeoutExpired(cmd=["pip_audit"], timeout=1)]
)
def test_audit_that_cannot_start_or_finish_is_not_run_and_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: BaseException
) -> None:
    requirements = tmp_path / "runtime.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        raise error

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)

    with pytest.raises(guard_python_cve.AuditNotRunError, match="did not run"):
        guard_python_cve.scan_requirements(requirements)
    assert len(commands) == 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(0, b"not json"), (1, b'{"dependencies": []}'), (2, b"partial")],
)
def test_audit_output_that_is_not_empty_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stdout: bytes
) -> None:
    requirements = tmp_path / "runtime.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")
    commands = _scripted_audit(monkeypatch, [(returncode, stdout)])

    with pytest.raises(guard_python_cve.AuditExecutionError) as caught:
        guard_python_cve.scan_requirements(requirements)
    assert not isinstance(caught.value, guard_python_cve.AuditNotRunError)
    assert len(commands) == 1


def test_main_reports_a_scan_that_did_not_run_apart_from_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "retained"
    commands = _scripted_audit(monkeypatch, [(1, b""), (1, b"")])

    assert guard_python_cve.main(["--evidence-dir", str(root)]) == 1
    output = capsys.readouterr().out
    assert "掃描沒跑成" in output
    assert "not a vulnerability finding" in output
    assert f"kept under {root}" in output
    assert "blocking or unrated" not in output
    assert "evidence invalid" not in output
    assert "[OK]" not in output
    assert len(commands) == 4  # Probe, runtime export, two audits; build-test never starts.
    assert not (root / "build-test.txt").exists()
    assert json.loads((root / "failure.json").read_text(encoding="utf-8"))["type"] == (
        "AuditNotRunError"
    )


def test_missing_requirements_is_an_execution_error(tmp_path: Path) -> None:
    error_type = getattr(guard_python_cve, "AuditExecutionError", RuntimeError)

    with pytest.raises(error_type):
        guard_python_cve.scan_requirements(tmp_path / "missing.txt")


def test_pip_audit_probe_never_installs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)

    assert guard_python_cve._ensure_pip_audit(tmp_path) is False
    assert len(calls) == 1
    command = calls[0][0][0]
    assert isinstance(command, list)
    assert "install" not in command
    assert command[-1] == "--version"


def test_export_api_requires_caller_owned_path_and_explicit_scope() -> None:
    parameters = inspect.signature(guard_python_cve._export_lock).parameters

    assert list(parameters) == ["output_path", "include_dev"]
    assert "_EXPORT_PATH" not in vars(guard_python_cve)


@pytest.mark.parametrize(
    ("include_dev", "expected_flag"),
    [(False, "--no-dev"), (True, "--all-groups")],
)
def test_export_uses_argument_array_and_explicit_workspace_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_dev: bool,
    expected_flag: str,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)
    output_path = tmp_path / "scope.txt"

    assert guard_python_cve._export_lock(output_path, include_dev=include_dev) == output_path
    command, kwargs = calls[0]
    assert Path(command[0]).is_absolute()
    assert command[1:4] == ["export", "--frozen", "--all-packages"]
    assert expected_flag in command
    assert ("--no-dev" in command) is (not include_dev)
    assert kwargs["shell"] is False
    assert kwargs["text"] is False
    assert "encoding" not in kwargs
    assert "errors" not in kwargs
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_NO_CONFIG"] == "1"
    assert environment["UV_CACHE_DIR"].startswith(str(tmp_path))


def test_subprocess_timeout_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")

    def timeout(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=["pip_audit"], timeout=1)

    monkeypatch.setattr(guard_python_cve.subprocess, "run", timeout)
    error_type = getattr(guard_python_cve, "AuditExecutionError", RuntimeError)

    with pytest.raises(error_type):
        guard_python_cve.scan_requirements(requirements)


@pytest.mark.parametrize(
    "stdout",
    [
        b'{"dependencies": [{"name": "example-pkg", "version": "1.0.0", "vulns": []}]}',
        b"invalid\xff",
    ],
)
def test_scan_retains_raw_bytes_before_strict_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: bytes
) -> None:
    requirements = tmp_path / "runtime.txt"
    requirements.write_text("example-pkg==1.0.0\n", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        assert kwargs["text"] is False
        assert command[command.index("--timeout") + 1] == "15"
        cache = Path(command[command.index("--cache-dir") + 1])
        assert cache.is_relative_to(tmp_path)
        assert kwargs["timeout"] == 300
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"diagnostic\xfe")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)
    if stdout.startswith(b"invalid"):
        with pytest.raises(guard_python_cve.AuditExecutionError):
            guard_python_cve.scan_requirements(requirements)
    else:
        result = guard_python_cve.scan_requirements(requirements)
        assert result.blocking == []
        assert result.warnings == []
        assert result.skipped == []
    assert (tmp_path / "audit-runtime" / "stdout.bin").read_bytes() == stdout
    assert (tmp_path / "audit-runtime" / "stderr.bin").read_bytes() == b"diagnostic\xfe"
    assert (tmp_path / "audit-runtime" / "inputs" / "runtime.txt").read_bytes() == (
        requirements.read_bytes()
    )


def test_evidence_cli_retains_probe_failure_and_rejects_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(
        guard_python_cve.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=b"missing"),
    )
    assert guard_python_cve.main(["--evidence-dir", str(evidence_root)]) == 1
    assert (evidence_root / "pip-audit-version" / "stderr.bin").read_bytes() == b"missing"
    assert (evidence_root / "inputs" / "uv.lock").is_file()
    assert guard_python_cve.main(["--evidence-dir", str(evidence_root)]) == 1
    assert (evidence_root / "pip-audit-version" / "stderr.bin").read_bytes() == b"missing"


def test_evidence_cli_captures_probe_exports_and_both_audits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "retained"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        assert (root / "inputs" / "ToukaProxy" / "pyproject.toml").is_file()
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        if "export" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"example-pkg==1.0.0\r\n")
        stdout = (
            b'{"dependencies": [{"name": "example-pkg", "version": "1.0.0", "vulns": []}]}'
            if "--requirement" in command
            else b"tool output"
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"diagnostic")

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)
    assert guard_python_cve.main(["--evidence-dir", str(root)]) == 0
    assert len(commands) == 5  # One version probe, two exports, and two audit processes.
    for name in (
        "pip-audit-version",
        "export-runtime",
        "audit-runtime-query",
        "export-build-test",
        "audit-build-test-query",
    ):
        assert (root / name / "receipt.json").is_file()
    assert (root / "runtime.txt").read_bytes() == b"example-pkg==1.0.0\r\n"
    assert (root / "build-test.txt").read_bytes() == b"example-pkg==1.0.0\r\n"


@pytest.mark.parametrize("severity", [None, "LOW"])
def test_main_reports_skips_and_findings_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    severity: str | None,
) -> None:
    payload = json.loads(_pip_audit_payload(severity=severity))
    skipped = [
        {"name": "-e -/local-example", "skip_reason": "cannot determine editable version"},
        {"name": "torch", "skip_reason": "cannot audit torch (2.12.1+cpu)"},
    ]
    payload["dependencies"] = [*skipped, *payload["dependencies"]]
    stdout = json.dumps(payload).encode("utf-8")
    root = tmp_path / "retained"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if "export" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"example-pkg==1.0.0\r\n")
        auditing = "--requirement" in command
        return SimpleNamespace(
            returncode=1 if auditing else 0,
            stdout=stdout if auditing else b"tool output",
            stderr=b"diagnostic",
        )

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)

    assert guard_python_cve.main(["--evidence-dir", str(root)]) == 1
    output = capsys.readouterr().out
    assert "example-pkg==1.0.0" in output
    assert "GHSA-xxxx-yyyy-zzzz" in output
    assert f"[{severity or 'UNRATED'}]" in output
    assert "[WARN]" in output if severity == "LOW" else "blocking or unrated" in output
    for item in skipped:
        assert item["name"] in output
        assert item["skip_reason"] in output
    assert "[INCOMPLETE]" in output
    assert "[OK]" not in output
    assert "evidence invalid" not in output
    assert len(commands) == 3  # Stop before exporting or auditing build-test.
    assert not (root / "build-test.txt").exists()
    assert (root / "audit-runtime-query" / "stdout.bin").read_bytes() == stdout
    assert (root / "audit-runtime-query" / "stderr.bin").read_bytes() == b"diagnostic"


def test_main_reports_skipped_coverage_even_with_clean_child_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "retained"
    stdout = json.dumps(
        {"dependencies": [{"name": "uncovered-pkg", "skip_reason": "not found on PyPI"}]}
    ).encode("utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if "export" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"uncovered-pkg==1.0.0\n")
        return SimpleNamespace(
            returncode=0,
            stdout=stdout if "--requirement" in command else b"tool output",
            stderr=b"",
        )

    monkeypatch.setattr(guard_python_cve.subprocess, "run", fake_run)

    assert guard_python_cve.main(["--evidence-dir", str(root)]) == 1
    output = capsys.readouterr().out
    assert "[INCOMPLETE]" in output
    assert "uncovered-pkg" in output
    assert "not found on PyPI" in output
    assert "[OK]" not in output
    assert "evidence invalid" not in output
    assert len(commands) == 3
    assert not (root / "build-test.txt").exists()
    assert (root / "audit-runtime-query" / "stdout.bin").read_bytes() == stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native runtime contract")
def test_isolated_environment_bootstraps_windows_native_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIT_TEST_CREDENTIAL", "synthetic-must-not-propagate")
    environment = guard_python_cve._isolated_environment(tmp_path / "audit")
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

@pytest.mark.parametrize("ambient_value", [None, "0", "1"])
def test_audit_child_disables_discovered_uv_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ambient_value: str | None
) -> None:
    """Private or ancestor settings must not influence the frozen audit export."""
    if ambient_value is None:
        monkeypatch.delenv("UV_NO_CONFIG", raising=False)
    else:
        monkeypatch.setenv("UV_NO_CONFIG", ambient_value)
    monkeypatch.setenv("UV_CONFIG_FILE", str(tmp_path / "unread-foreign.toml"))

    environment = guard_python_cve._isolated_environment(tmp_path / "audit")

    assert environment["UV_NO_CONFIG"] == "1"
    assert "UV_CONFIG_FILE" not in environment


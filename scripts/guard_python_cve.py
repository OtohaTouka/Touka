# ruff: noqa: T201, S603, S607
"""Fail-closed Python dependency audit gate (Guard #22).

The gate exports the single uv workspace lock into two OS-temporary inputs:
runtime dependencies and the complete build/test dependency graph.  It never
installs tooling, mutates the lock, or writes an audit input inside the repo.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypedDict

from dependency_audit_coverage import (
    CoverageError,
    prepare_projection,
    query_identities,
    verify_installed_torch,
)
from dependency_audit_evidence import (
    ProcessEvidence,
    create_evidence_root,
    snapshot_inputs,
    write_json,
)
from packaging.utils import canonicalize_name
from packaging.version import Version

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
AUDIT_TIMEOUT_SECONDS = 300
AUDIT_SOCKET_TIMEOUT_SECONDS = 15
# pip-audit sometimes exits with nothing on stdout (a transient network fault): one
# retry, each attempt keeping its own evidence. Non-empty output, a failed start and a
# timeout are never retried.
AUDIT_ATTEMPT_SUFFIXES: tuple[str, ...] = ("", "-retry")
AUDIT_SCOPES: tuple[tuple[str, bool], ...] = (
    ("runtime", False),
    ("build-test", True),
)
_NON_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"INFO", "LOW", "MEDIUM", "MODERATE"})


class AuditExecutionError(RuntimeError):
    """Raised when audit execution or machine-readable evidence is invalid."""


class AuditNotRunError(AuditExecutionError):
    """pip-audit printed nothing on both attempts, so no scan result exists."""


def _abort(message: str, *, cause: BaseException | None = None) -> NoReturn:
    """Raise the single fail-closed error type with an optional causal chain."""
    if cause is None:
        raise AuditExecutionError(message)
    raise AuditExecutionError(message) from cause


class Vulnerability(TypedDict):
    """Normalized pip-audit vulnerability."""

    package: str
    version: str
    id: str
    fix_versions: list[str]
    description: str
    aliases: list[str]
    severity: str


class SkippedDependency(TypedDict):
    """An upstream dependency whose vulnerability coverage is incomplete."""

    package: str
    reason: str


@dataclass(frozen=True)
class ParsedAudit:
    """Validated findings and coverage gaps from one pip-audit response."""

    vulnerabilities: list[Vulnerability]
    skipped: list[SkippedDependency]
    audited: list[tuple[str, str]]


@dataclass(frozen=True)
class AuditResult:
    """Classified findings without discarding incomplete dependency coverage."""

    blocking: list[Vulnerability]
    warnings: list[Vulnerability]
    skipped: list[SkippedDependency]


def _display_path(path: Path) -> str:
    """Return a stable path label without requiring the path to be in the repo."""
    resolved = path.resolve()
    if resolved.is_relative_to(PROJECT_ROOT):
        return str(resolved.relative_to(PROJECT_ROOT))
    return str(resolved)


def _external_audit_root(path: Path) -> Path:
    """Reject audit scratch space inside the repository."""
    resolved = path.resolve()
    if resolved.is_relative_to(PROJECT_ROOT):
        _abort("audit temporary directory resolved inside repository")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _isolated_environment(audit_root: Path) -> dict[str, str]:
    """Build a minimal credential-free child environment below audit scratch."""
    root = _external_audit_root(audit_root)
    directories = {
        "HOME": root / "home",
        "USERPROFILE": root / "profile",
        "APPDATA": root / "appdata",
        "LOCALAPPDATA": root / "localappdata",
        "TEMP": root / "temp",
        "TMP": root / "temp",
    }
    for directory in set(directories.values()):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {name: str(path) for name, path in directories.items()}
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_OFFLINE": "1",
        }
    )
    if sys.platform == "win32":
        system_root = os.environ.get("SYSTEMROOT")  # noqa: ENV_SSOT_VIOLATION
        if not system_root:
            _abort("Windows SystemRoot is unavailable for the audit runtime")
        environment["SystemRoot"] = system_root
    return environment


def _run_audit_child(
    command: list[str], audit_root: Path, name: str, *, inputs: dict[str, Path] | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run one fixed guard command and preserve raw execution evidence first."""
    environment = _isolated_environment(audit_root)
    evidence = ProcessEvidence(
        audit_root, name, command, PROJECT_ROOT, environment, AUDIT_TIMEOUT_SECONDS
    )
    if inputs is not None:
        snapshot_inputs(evidence.root, inputs)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=False,
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=AUDIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        evidence.finish(error=exc)
        raise
    evidence.finish(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
    return result


def _decode_stdout(output: bytes | str) -> str:
    """Decode captured bytes strictly; retain text mock compatibility."""
    try:
        return output.decode("utf-8") if isinstance(output, bytes) else output
    except UnicodeDecodeError as exc:
        _abort("pip-audit stdout is not valid UTF-8", cause=exc)


def _resolve_uv() -> Path:
    """Resolve uv to an absolute executable before launching without a shell."""
    executable = shutil.which("uv")
    if executable is None:
        _abort("uv executable is unavailable")
    resolved = Path(executable).resolve()
    if not resolved.is_file() or not resolved.is_absolute():
        _abort("uv executable did not resolve to an absolute file")
    return resolved


def _export_lock(output_path: Path, *, include_dev: bool) -> Path:
    """Export one explicit dependency scope from the frozen workspace lock."""
    resolved_output = output_path.resolve()
    _external_audit_root(resolved_output.parent)

    command = [str(_resolve_uv()), "export", "--frozen", "--all-packages"]
    command.append("--all-groups" if include_dev else "--no-dev")
    command.extend(
        [
            "--no-hashes",
            "--format",
            "requirements-txt",
            "-o",
            str(resolved_output),
        ]
    )
    try:
        result = _run_audit_child(command, resolved_output.parent, f"export-{resolved_output.stem}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _abort("uv export could not complete", cause=exc)
    if result.returncode != 0:
        _abort(f"uv export exited {result.returncode}")
    return resolved_output


def _severity_blocks(severity: str) -> bool:
    """Block high/critical and any missing or unrecognized severity."""
    return severity.strip().upper() not in _NON_BLOCKING_SEVERITIES


def _string_list(value: object, *, field: str) -> list[str]:
    """Validate a JSON string-list field."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _abort(f"pip-audit field {field!r} is not a string list")
    return value


def _parse_pip_audit_output(output: str) -> ParsedAudit:
    """Parse and validate pip-audit JSON without converting parse errors to clean."""
    if not output.strip():
        _abort("pip-audit returned empty stdout")
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        _abort("pip-audit stdout is not valid JSON", cause=exc)
    if not isinstance(data, dict):
        _abort("pip-audit JSON root is not an object")

    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        _abort("pip-audit JSON has no dependencies list")

    results: list[Vulnerability] = []
    skipped: list[SkippedDependency] = []
    audited: list[tuple[str, str]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            _abort("pip-audit dependency entry is not an object")
        package = dependency.get("name")
        if "skip_reason" in dependency:
            reason = dependency["skip_reason"]
            if (
                not isinstance(package, str)
                or not package.strip()
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                _abort("pip-audit skipped dependency identity or reason is invalid")
            if "version" in dependency or "vulns" in dependency:
                _abort("pip-audit skipped dependency contradicts audited identity or findings")
            skipped.append({"package": package, "reason": reason})
            continue
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            _abort("pip-audit dependency has no vulns list")
        version = dependency.get("version")
        if not isinstance(package, str) or not package.strip() or not isinstance(version, str):
            _abort("pip-audit dependency identity is invalid")
        identity = (canonicalize_name(package), str(Version(version)))
        if identity in audited:
            _abort("pip-audit coverage contains duplicate dependency identities")
        audited.append(identity)

        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                _abort("pip-audit vulnerability is not an object")
            advisory_id = vulnerability.get("id")
            if not isinstance(advisory_id, str) or not advisory_id:
                _abort("pip-audit vulnerability has no advisory id")
            description = vulnerability.get("description", "")
            if not isinstance(description, str):
                _abort("pip-audit vulnerability description is invalid")
            severity_value = vulnerability.get("severity", "")
            severity = severity_value.strip().upper() if isinstance(severity_value, str) else ""
            results.append(
                {
                    "package": package,
                    "version": version,
                    "id": advisory_id,
                    "fix_versions": _string_list(
                        vulnerability.get("fix_versions", []),
                        field="fix_versions",
                    ),
                    "description": description,
                    "aliases": _string_list(
                        vulnerability.get("aliases", []),
                        field="aliases",
                    ),
                    "severity": severity,
                }
            )
    return ParsedAudit(vulnerabilities=results, skipped=skipped, audited=audited)


def consume_pip_audit_result(
    stdout: bytes | str,
    *,
    exit_code: int,
    expected: tuple[tuple[str, str], ...],
) -> AuditResult:
    """Consume retained output against the original normalized query coverage."""
    if exit_code not in {0, 1}:
        _abort(f"pip-audit exited {exit_code}")
    parsed = _parse_pip_audit_output(_decode_stdout(stdout))
    if exit_code == 0 and parsed.vulnerabilities:
        _abort("pip-audit exit 0 contradicts vulnerability evidence")
    if exit_code == 1 and not parsed.vulnerabilities:
        _abort("pip-audit exited 1 without vulnerability evidence")
    if not parsed.skipped and tuple(sorted(parsed.audited)) != expected:
        _abort("pip-audit coverage differs from the complete requested dependency set")

    blocking = [item for item in parsed.vulnerabilities if _severity_blocks(item["severity"])]
    warnings = [item for item in parsed.vulnerabilities if not _severity_blocks(item["severity"])]
    return AuditResult(blocking=blocking, warnings=warnings, skipped=parsed.skipped)


def _ensure_pip_audit(audit_root: Path) -> bool:
    """Probe the locked tool only; never install or repair it."""
    try:
        result = _run_audit_child(
            [sys.executable, "-m", "pip_audit", "--version"],
            audit_root,
            "pip-audit-version",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def scan_requirements(req_file: Path) -> AuditResult:
    """Audit one export and retain classified findings and coverage gaps."""
    resolved_file = req_file.resolve()
    if not resolved_file.is_file():
        _abort(f"audit input is missing: {_display_path(resolved_file)}")
    try:
        expected = query_identities(resolved_file.read_text(encoding="utf-8"))
    except CoverageError as exc:
        _abort(str(exc), cause=exc)
    command = [
        sys.executable,
        "-B",
        "-m",
        "pip_audit",
        "--requirement",
        str(resolved_file),
        "--format",
        "json",
        "--vulnerability-service",
        "pypi",
        "--no-deps",
        "--disable-pip",
        "--cache-dir",
        str(resolved_file.parent / "pip-audit-cache" / resolved_file.stem),
        "--timeout",
        str(AUDIT_SOCKET_TIMEOUT_SECONDS),
    ]
    exit_codes: list[int] = []
    for suffix in AUDIT_ATTEMPT_SUFFIXES:
        try:
            result = _run_audit_child(
                command,
                resolved_file.parent,
                f"audit-{resolved_file.stem}{suffix}",
                inputs={resolved_file.name: resolved_file},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            reason = type(exc).__name__
            message = f"pip-audit could not start or finish ({reason}); the scan did not run"
            raise AuditNotRunError(message) from exc
        if result.stdout.strip():
            return consume_pip_audit_result(
                result.stdout, exit_code=result.returncode, expected=expected
            )
        exit_codes.append(result.returncode)
    message = (
        f"pip-audit printed nothing in {len(exit_codes)} attempts (exit codes {exit_codes}); "
        "the scan did not run"
    )
    raise AuditNotRunError(message)


def _workspace_inputs() -> dict[str, Path]:
    """Enumerate only the root and explicit single-directory workspace members."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    members = metadata.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    if not isinstance(members, list):
        _abort("uv workspace members must be an explicit list")
    inputs = {
        "uv.lock": PROJECT_ROOT / "uv.lock",
        "pyproject.toml": pyproject,
        "docs/security/torch-cpu-advisory-identity.json": PROJECT_ROOT
        / "docs/security/torch-cpu-advisory-identity.json",
    }
    for member in members:
        if (
            not isinstance(member, str)
            or not member
            or member in {".", ".."}
            or Path(member).name != member
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", member)
            or member.casefold() in {"data", ".gitignore"}
        ):
            _abort("uv workspace member is not an explicit project directory")
        path = PROJECT_ROOT / member / "pyproject.toml"
        for candidate in (path.parent, path):
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                _abort("uv workspace member must not traverse a filesystem link")
        resolved = path.resolve()
        if not resolved.is_relative_to(PROJECT_ROOT) or any(
            part.casefold() == "data" for part in resolved.parts
        ):
            _abort("uv workspace member resolved outside the permitted project source")
        inputs[f"{member}/pyproject.toml"] = path
    return inputs


def _load_coverage_context() -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    """Load only explicit workspace manifests, the frozen lock and reviewed policy."""
    inputs = _workspace_inputs()
    projects = {
        "." if name == "pyproject.toml" else Path(name).parent.as_posix(): tomllib.loads(
            path.read_text(encoding="utf-8")
        )
        for name, path in inputs.items()
        if name.endswith("pyproject.toml")
    }
    return (
        tomllib.loads(inputs["uv.lock"].read_text(encoding="utf-8")),
        projects,
        json.loads(
            inputs["docs/security/torch-cpu-advisory-identity.json"].read_text(encoding="utf-8")
        ),
    )


def _installed_torch_evidence(policy: dict[str, Any]) -> dict[str, str]:
    """Read package metadata and version literals without importing Torch code."""
    distribution = importlib.metadata.distribution("torch")
    metadata_files = [
        item
        for item in distribution.files or []
        if item.name == "METADATA" and item.parent.name.startswith("torch-")
    ]
    if len(metadata_files) != 1:
        _abort("coverage installed Torch metadata is ambiguous")
    metadata_path = Path(distribution.locate_file(metadata_files[0])).resolve()
    version_path = Path(distribution.locate_file("torch/version.py")).resolve()
    for path in (metadata_path, version_path):
        if not path.is_relative_to(Path(sys.prefix).resolve()) or any(
            part.casefold() == "data" for part in path.parts
        ):
            _abort("coverage installed Torch evidence is outside the active environment")
    literals = {
        target.id: node.value.value
        for node in ast.parse(version_path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    version = literals.get("__version__")
    commit = literals.get("git_version")
    digest = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
    if (
        not isinstance(version, str)
        or not isinstance(commit, str)
        or distribution.version != version
    ):
        _abort("coverage installed Torch version literals contradict metadata")
    verify_installed_torch(policy, version, commit, digest)
    return {"actual_version": version, "upstream_commit": commit, "metadata_sha256": digest}


def _prepare_audit_scope(original: Path) -> Path:
    """Retain original input and save a separately named, fully accounted query."""
    lock, projects, policy = _load_coverage_context()
    projection = prepare_projection(original.read_text(encoding="utf-8"), lock, projects, policy)
    manifest = dict(projection.manifest)
    if any(name == "torch" for name, _version in projection.expected):
        manifest["installed_torch"] = _installed_torch_evidence(policy)
        manifest["torch_advisory_policy"] = policy
    query = original.with_name(f"{original.stem}-query.txt")
    with query.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(projection.query_text)
    manifest["original_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    manifest["query_sha256"] = hashlib.sha256(query.read_bytes()).hexdigest()
    write_json(original.with_name(f"{original.stem}-coverage.json"), manifest)
    return query


def _print_finding(scope: str, vulnerability: Vulnerability) -> None:
    fixes = ", ".join(vulnerability["fix_versions"]) or "no fix yet"
    severity = vulnerability["severity"] or "UNRATED"
    print(
        f"  [{scope}] [{severity}] [{vulnerability['id']}] "
        f"{vulnerability['package']}=={vulnerability['version']} fix: {fixes}"
    )


def main(argv: list[str] | None = None) -> int:
    """Audit both scopes, stopping on errors or incomplete dependency coverage."""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Python Dependency CVE Guard (Guard #22) — pip-audit")
    print("=" * 60)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    arguments = parser.parse_args(argv or [])
    retained_root: Path | None = None
    blocking_by_scope: list[tuple[str, Vulnerability]] = []
    warnings_by_scope: list[tuple[str, Vulnerability]] = []
    skipped_by_scope: list[tuple[str, SkippedDependency]] = []
    try:
        if arguments.evidence_dir is not None:
            retained_root = create_evidence_root(arguments.evidence_dir, PROJECT_ROOT)
            snapshot_inputs(retained_root, _workspace_inputs())
        context = (
            nullcontext(str(retained_root))
            if retained_root is not None
            else (tempfile.TemporaryDirectory(prefix="touka-pip-audit-"))
        )
        with context as temporary:
            audit_root = _external_audit_root(Path(temporary))
            if not _ensure_pip_audit(audit_root):
                print("[FAIL] Locked pip-audit is unavailable; no installation was attempted.")
                return 1
            for scope_name, include_dev in AUDIT_SCOPES:
                print(f"[Scan] Python {scope_name} dependency scope")
                requirements = _export_lock(
                    audit_root / f"{scope_name}.txt",
                    include_dev=include_dev,
                )
                result = scan_requirements(_prepare_audit_scope(requirements))
                blocking_by_scope.extend((scope_name, item) for item in result.blocking)
                warnings_by_scope.extend((scope_name, item) for item in result.warnings)
                skipped_by_scope.extend((scope_name, item) for item in result.skipped)
                if result.skipped:
                    break
    except (AuditExecutionError, OSError, ValueError) as exc:
        if retained_root is not None:
            write_json(
                retained_root / "failure.json", {"type": type(exc).__name__, "error": str(exc)}
            )
        if isinstance(exc, AuditNotRunError):
            print(f"[FAIL] Python dependency scan did not run (掃描沒跑成): {exc}")
            print("[FAIL] No audit result exists; this is not a vulnerability finding.")
            if retained_root is not None:
                print(f"[FAIL] pip-audit stdout and stderr are kept under {retained_root}.")
            else:
                print(
                    "[FAIL] To keep pip-audit stderr, rerun with --evidence-dir "
                    "(guard_all.py --audit-evidence-root)."
                )
        else:
            print(f"[FAIL] Dependency audit evidence invalid: {exc}")
        return 1

    if warnings_by_scope:
        print(f"\n[WARN] {len(warnings_by_scope)} lower-severity finding(s):")
        for scope_name, vulnerability in warnings_by_scope:
            _print_finding(scope_name, vulnerability)

    if blocking_by_scope:
        print(f"\n[FAIL] {len(blocking_by_scope)} blocking or unrated finding(s):")
        for scope_name, vulnerability in blocking_by_scope:
            _print_finding(scope_name, vulnerability)

    if skipped_by_scope:
        print(f"\n[INCOMPLETE] {len(skipped_by_scope)} dependency coverage gap(s):")
        for scope_name, skipped in skipped_by_scope:
            print(f"  [{scope_name}] [SKIPPED] {skipped['package']}: {skipped['reason']}")
        print("[INCOMPLETE] Audit stopped; no later dependency scope was started.")

    if blocking_by_scope or skipped_by_scope:
        return 1

    print("\n[OK] No blocking Python dependency vulnerabilities found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# ruff: noqa: T201, S603
"""Fail-closed Node dependency audit gate (Guard #23).

Audit both runtime and complete dependency graphs for ToukaChat and ToukaIDE.
The command is an argv array, uses repository lockfiles, writes cache/config
state below an OS-temporary directory, and never invokes npm fix or install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from braces_backport_evidence import (
    BRACES_HELPER_PATH,
    BRACES_NODE_PROFILE,
    BRACES_READER_PATH,
    BRACES_SOURCE_PATHS,
    BRACES_VERIFY_TIMEOUT,
    LEGACY_NODE_PROFILE,
    assess_braces_audit,
    consume_braces_installed,
)
from dependency_audit_evidence import (
    ProcessEvidence,
    create_evidence_root,
    snapshot_inputs,
    write_json,
)
from hermetic_gate.npm_offline_gate import (
    EXPECTED_POLICY as EXPECTED_NPM_POLICY,
)
from hermetic_gate.npm_offline_gate import (
    parse_npm_policy,
)

if TYPE_CHECKING:
    from pathlib import PurePath

    from braces_backport_evidence import BracesInstalledEvidence

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
PROJECT_NAMES: tuple[str, ...] = ("ToukaChat", "ToukaIDE")
AUDIT_SCOPES: tuple[tuple[str, bool], ...] = (
    ("runtime", True),
    ("full", False),
)
AUDIT_TIMEOUT_SECONDS = 300
PUBLIC_NPM_REGISTRY = EXPECTED_NPM_POLICY["registry"]
_BRACES_READSET = (
    "scripts/guard_node_cve.py",
    "scripts/dependency_audit_evidence.py",
    "scripts/hermetic_gate/npm_offline_gate.py",
    BRACES_READER_PATH,
    *BRACES_SOURCE_PATHS,
    "ToukaChat/.npmrc",
    *(
        f"{project}/{name}"
        for project in PROJECT_NAMES
        for name in ("package.json", "package-lock.json")
    ),
)


class AuditExecutionError(RuntimeError):
    """Raised when npm execution or its machine-readable evidence is invalid."""


def _abort(message: str, *, cause: BaseException | None = None) -> NoReturn:
    """Raise the single fail-closed error type with an optional causal chain."""
    if cause is None:
        raise AuditExecutionError(message)
    raise AuditExecutionError(message) from cause


def _external_audit_root(path: Path) -> Path:
    """Resolve and create scratch space only when it is outside the repo."""
    resolved = path.resolve()
    if resolved.is_relative_to(PROJECT_ROOT):
        _abort("audit temporary directory resolved inside repository")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _load_tracked_npm_policy() -> dict[str, str]:
    """Require the committed public-registry policy before any network audit."""
    policy_path = PROJECT_ROOT / "ToukaChat" / ".npmrc"
    try:
        policy_text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        _abort("tracked npm policy is unavailable", cause=exc)

    try:
        return parse_npm_policy(policy_text)
    except ValueError as exc:
        _abort("tracked npm policy is invalid", cause=exc)


def _validate_resolved_urls(value: object, *, project_name: str) -> None:
    """Reject every explicit lockfile resolution outside the public registry."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "resolved" and (
                not isinstance(nested, str) or not nested.startswith(PUBLIC_NPM_REGISTRY)
            ):
                _abort(f"{project_name} lock contains a non-public resolved dependency")
            _validate_resolved_urls(nested, project_name=project_name)
    elif isinstance(value, list):
        for nested in value:
            _validate_resolved_urls(nested, project_name=project_name)


def _validate_lockfile(project: Path) -> None:
    """Require parseable manifests and public destinations for all lock entries."""
    package_path = project / "package.json"
    lock_path = project / "package-lock.json"
    if not package_path.is_file() or not lock_path.is_file():
        _abort(f"{project.name} requires package.json and package-lock.json")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _abort(f"{project.name} package manifest or lock is invalid", cause=exc)
    if not isinstance(package, dict):
        _abort(f"{project.name} package.json root is not an object")
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), dict):
        _abort(f"{project.name} package-lock.json has no packages object")

    for metadata in lock["packages"].values():
        if not isinstance(metadata, dict):
            _abort(f"{project.name} lock package entry is invalid")
    _validate_resolved_urls(lock, project_name=project.name)


def _parse_npm_audit_output(output: str) -> tuple[int, int]:
    """Return blocking/warning totals from validated npm audit v2 JSON."""
    if not output.strip():
        _abort("npm audit returned empty stdout")
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        _abort("npm audit stdout is not valid JSON", cause=exc)
    if not isinstance(data, dict) or "error" in data:
        _abort("npm audit returned an error-shaped payload")
    if data.get("auditReportVersion") != 2:
        _abort("npm audit report version is missing or unsupported")
    vulnerabilities = data.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        _abort("npm audit vulnerabilities object is missing")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        _abort("npm audit metadata object is missing")
    counts = metadata.get("vulnerabilities")
    if not isinstance(counts, dict):
        _abort("npm audit severity counts are missing")

    required = ("info", "low", "moderate", "high", "critical", "total")
    normalized: dict[str, int] = {}
    for severity in required:
        value = counts.get(severity)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _abort("npm audit severity counts are invalid")
        normalized[severity] = value
    calculated_total = sum(normalized[item] for item in required[:-1])
    if normalized["total"] != calculated_total:
        _abort("npm audit severity total is inconsistent")

    observed: dict[str, int] = dict.fromkeys(required[:-1], 0)
    for package, finding in vulnerabilities.items():
        if not isinstance(package, str) or not package or not isinstance(finding, dict):
            _abort("npm audit vulnerability entry is invalid")
        severity_value = finding.get("severity")
        if not isinstance(severity_value, str):
            _abort("npm audit vulnerability severity is invalid")
        severity = severity_value.strip().lower()
        if severity not in observed:
            _abort("npm audit vulnerability severity is unsupported")
        observed[severity] += 1
    if any(observed[severity] != normalized[severity] for severity in observed):
        _abort("npm audit metadata contradicts vulnerability entries")

    blocking = normalized["high"] + normalized["critical"]
    warnings = normalized["info"] + normalized["low"] + normalized["moderate"]
    return blocking, warnings


def consume_npm_audit_result(stdout: bytes | str, *, exit_code: int) -> tuple[int, int]:
    """Consume retained npm output using the original exit and severity policy."""
    if exit_code not in {0, 1}:
        _abort(f"npm audit exited {exit_code}")
    try:
        output = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
    except UnicodeDecodeError as exc:
        _abort("npm audit stdout is not valid UTF-8", cause=exc)
    blocking, warnings = _parse_npm_audit_output(output)
    if exit_code == 0 and blocking:
        _abort("npm audit exit 0 contradicts blocking JSON findings")
    if exit_code == 1 and not blocking:
        _abort("npm audit exit 1 has no high/critical JSON findings")
    return blocking, warnings


def _resolve_npm() -> tuple[Path, Path]:
    """Resolve Node and npm-cli.js so no batch or PowerShell parser is involved."""
    node_command = shutil.which("node")
    npm_command = shutil.which("npm")
    if node_command is None or npm_command is None:
        _abort("Node or npm executable is unavailable")
    node_executable = Path(node_command).resolve()
    npm_launcher = Path(npm_command).resolve()
    npm_cli = (
        npm_launcher
        if npm_launcher.name == "npm-cli.js"
        else npm_launcher.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    ).resolve()
    if not node_executable.is_file() or not node_executable.is_absolute():
        _abort("Node executable did not resolve to an absolute file")
    if not npm_cli.is_file() or not npm_cli.is_absolute():
        _abort("npm CLI did not resolve to an absolute file")
    return node_executable, npm_cli


def _write_controlled_configs(scope_root: Path) -> tuple[Path, Path, Path]:
    """Create credential-free npm config/cache locations below audit scratch."""
    cache = scope_root / "cache"
    logs = scope_root / "logs"
    user_config = scope_root / "user.npmrc"
    global_config = scope_root / "global.npmrc"
    cache.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    controlled = (
        f"registry={PUBLIC_NPM_REGISTRY}\nalways-auth=false\nignore-scripts=true\nstrict-ssl=true\n"
    )
    user_config.write_text(controlled, encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    return cache, logs, user_config


def _isolated_environment(
    scope_root: Path,
    *,
    cache: Path,
    logs: Path,
    user_config: Path,
    global_config: Path,
) -> dict[str, str]:
    """Build a minimal child environment with no inherited secrets or controls."""
    paths = {
        "HOME": scope_root / "home",
        "USERPROFILE": scope_root / "profile",
        "APPDATA": scope_root / "appdata",
        "LOCALAPPDATA": scope_root / "localappdata",
        "TEMP": scope_root / "temp",
        "TMP": scope_root / "temp",
    }
    for directory in set(paths.values()):
        directory.mkdir(parents=True, exist_ok=True)
    environment = {name: str(path) for name, path in paths.items()}
    environment.update(
        {
            "NO_UPDATE_NOTIFIER": "1",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_CACHE": str(cache),
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_GLOBALCONFIG": str(global_config),
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_LOGS_DIR": str(logs),
            "NPM_CONFIG_REGISTRY": PUBLIC_NPM_REGISTRY,
            "NPM_CONFIG_STRICT_SSL": "true",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_USERCONFIG": str(user_config),
        }
    )
    if sys.platform == "win32":
        system_root = os.environ.get("SYSTEMROOT")  # noqa: ENV_SSOT_VIOLATION
        if not system_root:
            _abort("Windows SystemRoot is unavailable for the audit runtime")
        environment["SystemRoot"] = system_root
    return environment


def audit_command(
    node_executable: PurePath,
    npm_cli: PurePath,
    *,
    omit_dev: bool,
    cache: PurePath,
    logs: PurePath,
    user_config: PurePath,
    global_config: PurePath,
) -> list[str]:
    """Build the fixed npm audit argv; command-line config outranks ambient config."""
    command = [
        str(node_executable),
        str(npm_cli),
        "audit",
        "--json",
        "--audit-level=high",
        "--package-lock-only",
        f"--registry={PUBLIC_NPM_REGISTRY}",
        f"--cache={cache}",
        f"--logs-dir={logs}",
        f"--userconfig={user_config}",
        f"--globalconfig={global_config}",
        "--ignore-scripts=true",
        "--fund=false",
        "--update-notifier=false",
        "--strict-ssl=true",
        "--proxy=null",
        "--https-proxy=null",
        "--always-auth=false",
        "--fetch-retries=0",
        "--//registry.npmjs.org/:_authToken=",
    ]
    if omit_dev:
        command.append("--omit=dev")
    return command


def scan_project(
    project: Path,
    *,
    scope_name: str,
    omit_dev: bool,
    node_executable: Path,
    npm_cli: Path,
    audit_root: Path,
    installed: BracesInstalledEvidence | None = None,
) -> tuple[int, int]:
    """Audit one exact project/scope pair and return blocking/warning totals."""
    expected_scopes = dict(AUDIT_SCOPES)
    if expected_scopes.get(scope_name) is not omit_dev:
        _abort("unknown or inconsistent npm audit scope")
    _validate_lockfile(project)
    external_root = _external_audit_root(audit_root)
    scope_root = external_root / f"{project.name}-{scope_name}"
    scope_root.mkdir(parents=True, exist_ok=False)
    cache, logs, user_config = _write_controlled_configs(scope_root)
    global_config = scope_root / "global.npmrc"
    command = audit_command(
        node_executable,
        npm_cli,
        omit_dev=omit_dev,
        cache=cache,
        logs=logs,
        user_config=user_config,
        global_config=global_config,
    )
    environment = _isolated_environment(
        scope_root,
        cache=cache,
        logs=logs,
        user_config=user_config,
        global_config=global_config,
    )
    snapshot_inputs(
        scope_root,
        {
            "package.json": project / "package.json",
            "package-lock.json": project / "package-lock.json",
            "user.npmrc": user_config,
            "global.npmrc": global_config,
        },
    )
    evidence = ProcessEvidence(
        scope_root, "npm-audit", command, project.resolve(), environment, AUDIT_TIMEOUT_SECONDS
    )

    try:
        result = subprocess.run(
            command,
            cwd=str(project.resolve()),
            capture_output=True,
            text=False,
            env=environment,
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=AUDIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        evidence.finish(error=exc)
        _abort("npm audit could not complete", cause=exc)
    evidence.finish(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
    blocking, warnings = consume_npm_audit_result(result.stdout, exit_code=result.returncode)
    if installed is not None:
        assessment = assess_braces_audit(
            result.stdout, scope=scope_name, raw_blocking=blocking, installed=installed
        )
        print(
            f"  raw blocking={blocking}; "
            f"verified backport coverage={assessment.covered_blocking}; "
            f"unresolved blocking={assessment.unresolved_blocking}"
        )
        return assessment.unresolved_blocking, warnings
    return blocking, warnings


def _verify_braces(
    project: Path, node: Path, audit_root: Path, sources: dict[str, bytes]
) -> BracesInstalledEvidence:
    """Capture a readonly installed-byte proof before the project audit pair."""
    scope_root = audit_root / f"{project.name}-braces-environment"
    scope_root.mkdir()
    cache, logs, user_config = _write_controlled_configs(scope_root)
    environment = _isolated_environment(
        scope_root,
        cache=cache,
        logs=logs,
        user_config=user_config,
        global_config=scope_root / "global.npmrc",
    )
    command = [
        str(node),
        str(PROJECT_ROOT / BRACES_HELPER_PATH),
        "verify",
        "--project-root",
        str(project),
        "--scope",
        "full",
    ]
    evidence = ProcessEvidence(
        audit_root,
        f"{project.name}-braces-verify",
        command,
        project,
        environment,
        BRACES_VERIFY_TIMEOUT,
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(project),
            capture_output=True,
            text=False,
            env=environment,
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=BRACES_VERIFY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        evidence.finish(error=exc)
        _abort("braces readonly verification could not complete", cause=exc)
    evidence.finish(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
    if result.returncode != 0:
        _abort("braces readonly verification failed")
    return consume_braces_installed(result.stdout, project=project.name, source_files=sources)


def main(argv: list[str] | None = None) -> int:
    """Run both scopes for both Node projects; fail on errors or blockers."""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Node Dependency CVE Guard (Guard #23) — npm audit")
    print("=" * 60)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--profile", choices=(LEGACY_NODE_PROFILE, BRACES_NODE_PROFILE), default=LEGACY_NODE_PROFILE
    )
    arguments = parser.parse_args(argv or [])
    retained_root: Path | None = None
    try:
        if arguments.evidence_dir is not None:
            retained_root = create_evidence_root(arguments.evidence_dir, PROJECT_ROOT)
        _load_tracked_npm_policy()
        sources = (
            {name: (PROJECT_ROOT / name).read_bytes() for name in _BRACES_READSET}
            if arguments.profile == BRACES_NODE_PROFILE
            else {}
        )
        if retained_root is not None:
            inputs = {"tracked.npmrc": PROJECT_ROOT / "ToukaChat" / ".npmrc"}
            for project_name in PROJECT_NAMES:
                project = PROJECT_ROOT / project_name
                _validate_lockfile(project)
                for filename in ("package.json", "package-lock.json"):
                    inputs[f"{project_name}/{filename}"] = project / filename
            snapshot_inputs(retained_root, inputs)
        node_executable, npm_cli = _resolve_npm()
        blocking_total = 0
        warning_total = 0
        context = (
            nullcontext(str(retained_root))
            if retained_root is not None
            else (tempfile.TemporaryDirectory(prefix="touka-npm-audit-"))
        )
        with context as temporary:
            audit_root = _external_audit_root(Path(temporary))
            if sources:
                write_json(
                    audit_root / "batch.json",
                    {
                        "profile": BRACES_NODE_PROFILE,
                        "input_manifest": [
                            {
                                "path": name,
                                "bytes": len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest(),
                            }
                            for name, raw in sorted(sources.items())
                        ],
                    },
                )
            for project_name in PROJECT_NAMES:
                project = PROJECT_ROOT / project_name
                extra = (
                    {"installed": _verify_braces(project, node_executable, audit_root, sources)}
                    if sources
                    else {}
                )
                for scope_name, omit_dev in AUDIT_SCOPES:
                    print(f"[Scan] {project_name} {scope_name} dependency scope")
                    blocking, warnings = scan_project(
                        project,
                        scope_name=scope_name,
                        omit_dev=omit_dev,
                        node_executable=node_executable,
                        npm_cli=npm_cli,
                        audit_root=audit_root,
                        **extra,
                    )
                    blocking_total += blocking
                    warning_total += warnings
                    print(
                        f"  blocking={blocking} lower-severity={warnings} "
                        f"registry={PUBLIC_NPM_REGISTRY}"
                    )
            if any((PROJECT_ROOT / name).read_bytes() != raw for name, raw in sources.items()):
                _abort("braces audit source changed during execution")
    except (AuditExecutionError, OSError, ValueError) as exc:
        if retained_root is not None:
            write_json(
                retained_root / "failure.json", {"type": type(exc).__name__, "error": str(exc)}
            )
        print(f"[FAIL] Node dependency audit evidence invalid: {exc}")
        return 1

    if warning_total:
        print(f"[WARN] {warning_total} lower-severity finding(s) across audited scopes.")
    if blocking_total:
        print(f"[FAIL] {blocking_total} HIGH/CRITICAL finding(s) across audited scopes.")
        return 1
    print(
        "[OK] No unresolved HIGH/CRITICAL findings; original audit highs retained."
        if arguments.profile == BRACES_NODE_PROFILE
        else "[OK] No HIGH/CRITICAL Node dependency vulnerabilities found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

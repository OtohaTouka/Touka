"""Fail-closed npm offline install/build gate for the P8-B sealed runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EXPECTED_POLICY = {
    "audit": "false",
    "engine-strict": "true",
    "fund": "false",
    "ignore-scripts": "true",
    "registry": "https://registry.npmjs.org/",
    "update-notifier": "false",
}
_FORBIDDEN_ENVIRONMENT = frozenset({"http_proxy", "https_proxy", "no_proxy"})
_ENVIRONMENT_ALLOWLIST = frozenset(
    {"COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
)


class NpmPolicyError(ValueError):
    """Reject npm policy or lock metadata without changing the ValueError contract."""


class NpmExecutionError(RuntimeError):
    """Reject npm execution without changing the RuntimeError contract."""


def parse_npm_policy(text: str) -> dict[str, str]:
    """Parse the exact public-registry policy shared by offline and audit gates."""
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise NpmPolicyError("NPM_POLICY_INVALID_LINE")
        normalized_key = key.strip()
        if normalized_key in entries:
            raise NpmPolicyError("NPM_POLICY_DUPLICATE_KEY")
        entries[normalized_key] = value.strip()
    if entries != EXPECTED_POLICY:
        raise NpmPolicyError("NPM_POLICY_UNEXPECTED_VALUES")
    return entries


def load_npm_policy(project_root: Path) -> dict[str, str]:
    """Load and strictly validate the committed ``.npmrc`` policy."""
    return parse_npm_policy((project_root / ".npmrc").read_text(encoding="utf-8"))


def has_integrity_bound_bundle_parent(
    package_path: str,
    metadata: Mapping[str, Any],
    lock_packages: Mapping[str, Any],
) -> bool:
    """Allow omitted child fields only when its declaring tarball is bound."""
    if metadata.get("inBundle") is not True:
        return False
    parent_path, separator, bundled_name = package_path.rpartition("/node_modules/")
    if not separator or not bundled_name:
        return False
    parent = lock_packages.get(parent_path)
    if not isinstance(parent, dict):
        return False
    bundle_dependencies = parent.get("bundleDependencies")
    resolved = parent.get("resolved")
    integrity = parent.get("integrity")
    return (
        isinstance(bundle_dependencies, list)
        and bundled_name in bundle_dependencies
        and isinstance(resolved, str)
        and resolved.startswith(EXPECTED_POLICY["registry"])
        and isinstance(integrity, str)
        and integrity.startswith("sha512-")
    )


def validate_lockfile(project_root: Path) -> None:
    """Reject dependencies not integrity-bound to the official registry."""
    lock_path = project_root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), dict):
        raise NpmPolicyError("NPM_LOCK_PACKAGES_OBJECT_REQUIRED")
    lock_packages = lock["packages"]
    violations: list[str] = []
    for package_path, metadata in lock_packages.items():
        if not isinstance(metadata, dict):
            raise NpmPolicyError("NPM_LOCK_PACKAGE_OBJECT_REQUIRED")
        if not package_path:
            continue
        bundled = has_integrity_bound_bundle_parent(package_path, metadata, lock_packages)
        resolved = metadata.get("resolved")
        integrity = metadata.get("integrity")
        if ("resolved" in metadata or not bundled) and (
            not isinstance(resolved, str) or not resolved.startswith(EXPECTED_POLICY["registry"])
        ):
            violations.append(f"{package_path}: invalid resolved URL")
        if ("integrity" in metadata or not bundled) and (
            not isinstance(integrity, str) or not integrity.startswith("sha512-")
        ):
            violations.append(f"{package_path}: missing sha512 integrity")
    if violations:
        raise ValueError("lockfile policy violations:\n" + "\n".join(violations))


def forbidden_environment_keys(environment: Mapping[str, str]) -> list[str]:
    """Return inherited npm/proxy override keys using case-insensitive matching."""
    forbidden: list[str] = []
    for key in environment:
        normalized = key.casefold()
        if normalized.startswith("npm_config_") or normalized in _FORBIDDEN_ENVIRONMENT:
            forbidden.append(key)
    return sorted(forbidden, key=str.casefold)


def build_clean_environment(
    inherited: Mapping[str, str],
    *,
    cache_dir: Path,
    global_config: Path,
    home_dir: Path,
    user_config: Path,
) -> dict[str, str]:
    """Create the credential-free environment consumed by npm."""
    forbidden = forbidden_environment_keys(inherited)
    if forbidden:
        message = f"forbidden inherited environment keys: {forbidden!r}"
        raise NpmPolicyError(message)
    clean = {key: inherited[key] for key in _ENVIRONMENT_ALLOWLIST if key in inherited}
    clean.update(
        {
            "HOME": str(home_dir),
            "NEXT_TELEMETRY_DISABLED": "1",
            "npm_config_cache": str(cache_dir),
            "npm_config_globalconfig": str(global_config),
            "npm_config_offline": "true",
            "npm_config_userconfig": str(user_config),
        }
    )
    return clean


def npm_command_sequence(
    npm_executable: Path,
    *,
    runtime: bool,
    node_executable: Path | None = None,
    cache_dir: Path | None = None,
) -> tuple[list[str], ...]:
    """Return the fixed lifecycle, using logical names when host paths are absent.

    Admission consumes this canonical description; execution supplies resolved
    paths and pairs each actual command with the corresponding canonical row.
    """
    npm = str(npm_executable)
    node = str(node_executable) if node_executable is not None else "node"
    if runtime:
        return ([npm, "ci", "--omit=dev", "--offline", "--ignore-scripts"],)
    cache = str(cache_dir) if cache_dir is not None else "cache"
    return (
        [npm, "ci", "--offline", "--ignore-scripts"],
        [
            node,
            "scripts/ensure-critical-native.mjs",
            "--npm",
            npm,
            "--cache",
            cache,
            "--offline",
        ],
        [npm, "run", "gen:artifact-runtime", "--ignore-scripts"],
        [npm, "run", "build", "--ignore-scripts"],
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - absolute npm path and fixed argv
        list(command),
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (
            f"offline npm command failed with exit {result.returncode}: {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        raise NpmExecutionError(message)
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def execute_gate(args: argparse.Namespace) -> dict[str, Any]:
    """Validate policy and optionally execute the offline npm lifecycle."""
    project_root = args.project.resolve(strict=True)
    load_npm_policy(project_root)
    validate_lockfile(project_root)
    if args.validate_only:
        return {"mode": "validate-only", "policy": "ok", "lockfile": "ok"}

    npm_executable = args.npm.resolve(strict=True)
    if not npm_executable.is_file() or not npm_executable.is_absolute():
        message = "--npm must resolve to an absolute file"
        raise NpmPolicyError(message)
    global_config = args.global_config.resolve(strict=True)
    if global_config.read_text(encoding="utf-8"):
        message = "--global-config must be an empty file"
        raise NpmPolicyError(message)
    cache_dir = args.cache.resolve(strict=True)
    home_dir = args.home.resolve(strict=True)
    inherited = dict(os.environ)  # noqa: ENV_SSOT_VIOLATION - hermetic CLI boundary
    environment = build_clean_environment(
        inherited,
        cache_dir=cache_dir,
        global_config=global_config,
        home_dir=home_dir,
        user_config=project_root / ".npmrc",
    )

    config_output = _run(
        [str(npm_executable), "config", "ls", "-l"],
        cwd=project_root,
        environment=environment,
    )
    registry_output = _run(
        [str(npm_executable), "config", "get", "registry"],
        cwd=project_root,
        environment=environment,
    )
    registry = registry_output.stdout.strip()
    if registry != EXPECTED_POLICY["registry"]:
        message = f"npm registry drifted to {registry!r}"
        raise NpmExecutionError(message)

    command_hashes: list[dict[str, Any]] = []
    node_path = shutil.which("node", path=environment.get("PATH", ""))
    if node_path is None:
        raise NpmPolicyError("NATIVE_NODE_EXECUTABLE_REQUIRED")
    node_executable = Path(node_path).resolve(strict=True)
    actual_commands = npm_command_sequence(
        npm_executable,
        runtime=args.runtime,
        node_executable=node_executable,
        cache_dir=cache_dir,
    )
    canonical_commands = npm_command_sequence(Path("npm"), runtime=args.runtime)
    for command, canonical in zip(actual_commands, canonical_commands, strict=True):
        result = _run(command, cwd=project_root, environment=environment)
        command_hashes.append(
            {
                "argv": canonical[1:],
                "stdout_sha256": _sha256_text(result.stdout),
                "stderr_sha256": _sha256_text(result.stderr),
            }
        )
    return {
        "commands": command_hashes,
        "config_stderr_sha256": _sha256_text(config_output.stderr),
        "config_stdout_sha256": _sha256_text(config_output.stdout),
        "mode": "runtime" if args.runtime else "build",
        "registry": registry,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the fixed validation or offline execution arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--npm", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--global-config", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.validate_only and any(
        value is None for value in (args.npm, args.cache, args.global_config, args.home)
    ):
        parser.error("execution requires --npm, --cache, --global-config, and --home")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Write the gate result as one JSON line."""
    sys.stdout.write(json.dumps(execute_gate(parse_args(argv)), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

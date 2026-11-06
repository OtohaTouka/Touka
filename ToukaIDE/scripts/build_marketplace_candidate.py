"""Build one offline, deterministic ToukaIDE Marketplace candidate.

The builder invokes only the repository-local, lockfile-pinned ``vsce`` package.
It never logs in, publishes, downloads a fallback tool, or writes a candidate
inside the repository. The final output directory is create-only and contains
exactly one universal VSIX plus its canonical evidence manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol

import deterministic_vsix as vsix

if TYPE_CHECKING:
    from collections.abc import Sequence


EXPECTED_NODE_VERSION = "24.22.1"
EXPECTED_NPM_VERSION = "11.19.1"
EXPECTED_VSCE_VERSION = "3.9.2"
EXPECTED_RELEASE_TAG = f"v{vsix.EXPECTED_VERSION}"
CANDIDATE_SCHEMA = "touka.ide.marketplace-candidate.v1"
CANDIDATE_FILENAME = f"toukaide-{vsix.EXPECTED_VERSION}.vsix"
MANIFEST_FILENAME = f"toukaide-{vsix.EXPECTED_VERSION}.manifest.json"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_COPY_BYTES = 16 * 1024 * 1024
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_CREDENTIAL_ENV_KEYS = (
    "AZURE_DEVOPS_EXT_PAT",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NODE_AUTH_TOKEN",
    "NPM_TOKEN",
    "VSCE_PAT",
)


class CandidatePolicyError(RuntimeError):
    """The Marketplace candidate violates the approved release boundary."""


class CommandRunner(Protocol):
    """Injectable command boundary used by the offline builder."""

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Run one fixed-argument command in the explicit working directory."""
        ...


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    """Canonical identity linking a VSIX candidate to its source revision."""

    archive_sha256: str
    extension_id: str
    license: str
    members: tuple[vsix.VsixMember, ...]
    publisher: str
    release_tag: str
    source_commit: str
    version: str

    def canonical_manifest_bytes(self) -> bytes:
        """Serialize stable evidence without timestamps or host-specific paths."""
        payload = {
            "archive_sha256": self.archive_sha256,
            "extension_id": self.extension_id,
            "license": self.license,
            "members": [asdict(member) for member in self.members],
            "publisher": self.publisher,
            "release_tag": self.release_tag,
            "schema": CANDIDATE_SCHEMA,
            "source_commit": self.source_commit,
            "version": self.version,
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )


def _fail(reason: str, *, cause: BaseException | None = None) -> NoReturn:
    if cause is None:
        raise CandidatePolicyError(reason)
    raise CandidatePolicyError(reason) from cause


def _is_redirected(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _read_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    if _is_redirected(path) or not path.is_file():
        _fail(f"{label} must be a regular file")
    try:
        size = path.stat().st_size
        if size < 0 or size > limit:
            _fail(f"{label} exceeds the bounded size")
        return path.read_bytes()
    except OSError as exc:
        _fail(f"{label} is unreadable", cause=exc)


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    content = _read_regular_file(path, label=label, limit=MAX_JSON_BYTES)
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} is not valid UTF-8 JSON", cause=exc)
    if not isinstance(parsed, dict):
        _fail(f"{label} must contain a JSON object")
    return parsed


def _offline_child_environment(command: Sequence[str]) -> dict[str, str]:
    """Remove publishing credentials and force npm into offline mode."""
    environment = dict(os.environ)  # noqa: ENV_SSOT_VIOLATION - release CLI boundary
    for key in _CREDENTIAL_ENV_KEYS:
        environment.pop(key, None)
    environment.update(
        {
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_offline": "true",
            "npm_config_update_notifier": "false",
        },
    )
    executable = Path(command[0])
    if executable.is_absolute() and executable.name.casefold() in {"node", "node.exe"}:
        existing_path = environment.get("PATH", "")
        environment["PATH"] = (
            f"{executable.parent}{os.pathsep}{existing_path}"
            if existing_path
            else str(executable.parent)
        )
    return environment


def _run_subprocess(
    command: Sequence[str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - resolved tools; fixed argv; no shell
            tuple(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=False,
            shell=False,
            env=_offline_child_environment(command),
        )
    except OSError as exc:
        _fail(f"command could not start: {Path(command[0]).name}", cause=exc)
    try:
        if not isinstance(result.stdout, bytes) or not isinstance(result.stderr, bytes):
            _fail("command did not return byte output")
        stdout = result.stdout.decode("utf-8", errors="strict")
        stderr = result.stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        _fail(
            f"command output is not valid UTF-8: {Path(command[0]).name}",
            cause=exc,
        )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _checked_command(
    runner: CommandRunner,
    command: Sequence[str],
    cwd: Path,
    *,
    label: str,
) -> str:
    result = runner(tuple(str(part) for part in command), cwd)
    if not isinstance(result, subprocess.CompletedProcess):
        _fail(f"{label} did not return a completed process")
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        _fail("command did not return text output")
    if result.returncode != 0:
        _fail(f"{label} failed with exit code {result.returncode}")
    return result.stdout.strip()


def _resolve_executable(executable: str | Path, *, label: str) -> Path:
    requested = str(executable)
    candidate_text = requested if Path(requested).is_absolute() else shutil.which(requested)
    if not candidate_text:
        _fail(f"{label} executable is unavailable")
    try:
        candidate = Path(candidate_text).resolve(strict=True)
    except OSError as exc:
        _fail(f"{label} executable cannot be resolved", cause=exc)
    if _is_redirected(Path(candidate_text)) or not candidate.is_file():
        _fail(f"{label} executable must be a real file")
    return candidate


def _resolve_npm_cli(node: Path, npm_cli: Path | None) -> Path:
    requested = (
        npm_cli
        if npm_cli is not None
        else node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    )
    try:
        candidate = requested.resolve(strict=True)
    except OSError as exc:
        _fail("npm CLI is unavailable", cause=exc)
    if _is_redirected(requested) or not candidate.is_file():
        _fail("npm CLI must be a real file")
    package_root = candidate.parent.parent
    if candidate.name != "npm-cli.js" or not candidate.is_relative_to(package_root):
        _fail("npm CLI path is not a package-local npm-cli.js")
    package = _read_json_object(
        package_root / "package.json",
        label="npm CLI package.json",
    )
    if package.get("name") != "npm" or package.get("version") != EXPECTED_NPM_VERSION:
        _fail("npm CLI package identity does not match the approved version")
    return candidate


def _require_real_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _fail(f"{label} is unavailable", cause=exc)
    if _is_redirected(path) or _is_redirected(resolved) or not resolved.is_dir():
        _fail(f"{label} must be a real directory")
    return resolved


def _validate_roots(repo_root: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    repo = _require_real_directory(repo_root, label="repository root")
    ide = _require_real_directory(repo / "ToukaIDE", label="ToukaIDE root")
    unresolved_output = output_dir.absolute()
    if output_dir.exists() or _is_redirected(output_dir):
        _fail("candidate output directory already exists")
    parent = _require_real_directory(
        unresolved_output.parent,
        label="candidate output parent",
    )
    output = parent / unresolved_output.name
    if output == repo or output.is_relative_to(repo):
        _fail("candidate output directory must be outside the repository")
    return repo, ide, output


def _require_commit(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if _COMMIT_PATTERN.fullmatch(normalized) is None:
        _fail(f"{label} is not a full commit SHA")
    return normalized


def _verify_source_identity(
    repo: Path,
    runner: CommandRunner,
) -> str:
    status = _checked_command(
        runner,
        (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            "LICENSE",
            "ToukaIDE",
        ),
        repo,
        label="release-input cleanliness check",
    )
    if status:
        _fail("release inputs are dirty")
    head = _require_commit(
        _checked_command(
            runner,
            ("git", "rev-parse", "--verify", "HEAD"),
            repo,
            label="source commit lookup",
        ),
        label="source commit",
    )
    tagged = _require_commit(
        _checked_command(
            runner,
            (
                "git",
                "rev-parse",
                "--verify",
                f"refs/tags/{EXPECTED_RELEASE_TAG}^{{commit}}",
            ),
            repo,
            label="release tag lookup",
        ),
        label="tagged commit",
    )
    if tagged != head:
        _fail("release tag does not anchor HEAD")
    return head


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _validate_package_and_tools(ide: Path) -> Path:
    package = _read_json_object(ide / "package.json", label="ToukaIDE package.json")
    lock = _read_json_object(ide / "package-lock.json", label="ToukaIDE lockfile")
    dev_dependencies = _require_mapping(
        package.get("devDependencies"),
        label="ToukaIDE devDependencies",
    )
    if dev_dependencies.get("@vscode/vsce") != EXPECTED_VSCE_VERSION:
        _fail("@vscode/vsce must be an exact direct package pin")
    if "@vscode/test-electron" in dev_dependencies:
        _fail("@vscode/test-electron is outside the approved minimal release scope")

    packages = _require_mapping(lock.get("packages"), label="ToukaIDE lock packages")
    lock_root = _require_mapping(packages.get(""), label="ToukaIDE lock root")
    lock_dev_dependencies = _require_mapping(
        lock_root.get("devDependencies"),
        label="ToukaIDE lock root devDependencies",
    )
    locked_vsce = _require_mapping(
        packages.get("node_modules/@vscode/vsce"),
        label="locked @vscode/vsce package",
    )
    if (
        lock_dev_dependencies.get("@vscode/vsce") != EXPECTED_VSCE_VERSION
        or locked_vsce.get("version") != EXPECTED_VSCE_VERSION
    ):
        _fail("@vscode/vsce lockfile pin does not match the approved version")

    installed_root = ide / "node_modules" / "@vscode" / "vsce"
    installed = _read_json_object(
        installed_root / "package.json",
        label="installed @vscode/vsce package",
    )
    if installed.get("version") != EXPECTED_VSCE_VERSION:
        _fail("installed @vscode/vsce does not match the lockfile pin")
    bin_field = installed.get("bin")
    if isinstance(bin_field, str):
        entry_name = bin_field
    elif isinstance(bin_field, dict) and isinstance(bin_field.get("vsce"), str):
        entry_name = bin_field["vsce"]
    else:
        _fail("installed @vscode/vsce does not expose a local vsce entry")
    relative_entry = Path(entry_name)
    if relative_entry.is_absolute() or ".." in relative_entry.parts:
        _fail("installed @vscode/vsce entry is unsafe")
    try:
        resolved_root = installed_root.resolve(strict=True)
        entry = (installed_root / relative_entry).resolve(strict=True)
    except OSError as exc:
        _fail("installed @vscode/vsce entry is unavailable", cause=exc)
    if (
        not entry.is_relative_to(resolved_root)
        or _is_redirected(installed_root / relative_entry)
        or not entry.is_file()
    ):
        _fail("installed @vscode/vsce entry must be a real local file")
    return entry


def _verify_toolchain(
    ide: Path,
    runner: CommandRunner,
    node: Path,
    npm_cli: Path,
) -> None:
    node_version = _checked_command(
        runner,
        (str(node), "--version"),
        ide,
        label="Node version check",
    )
    npm_version = _checked_command(
        runner,
        (str(node), str(npm_cli), "--version"),
        ide,
        label="npm version check",
    )
    if node_version != f"v{EXPECTED_NODE_VERSION}":
        _fail(f"Node must be exactly {EXPECTED_NODE_VERSION}")
    if npm_version != EXPECTED_NPM_VERSION:
        _fail(f"npm must be exactly {EXPECTED_NPM_VERSION}")


def _write_create_only(path: Path, content: bytes, *, label: str) -> None:
    created = False
    try:
        with path.open("xb") as handle:
            created = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o644)
    except OSError as exc:
        if created:
            path.unlink(missing_ok=True)
        _fail(f"{label} could not be created", cause=exc)


def _copy_create_only(source: Path, destination: Path, *, label: str) -> None:
    content = _read_regular_file(source, label=label, limit=MAX_COPY_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_create_only(destination, content, label=label)


def _stage_extension(repo: Path, ide: Path, staging: Path) -> None:
    staging.mkdir()
    package = _read_json_object(ide / "package.json", label="ToukaIDE package.json")
    scripts = _require_mapping(package.get("scripts"), label="ToukaIDE scripts")
    staged_scripts = dict(scripts)
    if staged_scripts.pop("vscode:prepublish", None) is None:
        _fail("ToukaIDE vscode:prepublish hook is unavailable")
    staged_package = dict(package)
    staged_package["scripts"] = staged_scripts
    _write_create_only(
        staging / "package.json",
        (json.dumps(staged_package, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8",
        ),
        label="staged package.json",
    )
    _copy_create_only(
        ide / "README.md",
        staging / "README.md",
        label="ToukaIDE README",
    )
    for relative_name in vsix.PACKAGE_FILES:
        if relative_name in {"README.md", "LICENSE"}:
            continue
        relative = Path(relative_name)
        _copy_create_only(
            ide / relative,
            staging / relative,
            label=f"ToukaIDE package file {relative_name}",
        )
    try:
        vsix.inject_tracked_license(repo, staging)
    except vsix.VsixPolicyError as exc:
        _fail("root LICENSE could not be staged", cause=exc)


def _remove_owned_file(path: Path, owner: Path) -> None:
    if path.parent != owner or _is_redirected(path) or not path.is_file():
        _fail("owned temporary file boundary changed")
    try:
        path.unlink()
    except OSError as exc:
        _fail("owned temporary file cleanup failed", cause=exc)


def _remove_owned_tree(path: Path, owner: Path) -> None:
    if path.parent != owner or _is_redirected(path) or not path.is_dir():
        _fail("owned temporary directory boundary changed")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        _fail("owned temporary directory cleanup failed", cause=exc)


def _cleanup_failed_workspace(workspace: Path, parent: Path) -> None:
    if not workspace.exists():
        return
    if workspace.parent != parent or _is_redirected(workspace) or not workspace.is_dir():
        _fail("failed candidate workspace boundary changed")
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        _fail("failed candidate workspace cleanup failed", cause=exc)


def _assemble_candidate(
    repo: Path,
    ide: Path,
    output: Path,
    source_commit: str,
    runner: CommandRunner,
    node: Path,
    vsce_entry: Path,
) -> CandidateManifest:
    parent = output.parent
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=parent),
    ).resolve(strict=True)
    completed = False
    try:
        staging = workspace / "staging"
        raw_vsix = workspace / "raw.vsix"
        final_vsix = workspace / CANDIDATE_FILENAME
        manifest_path = workspace / MANIFEST_FILENAME
        _stage_extension(repo, ide, staging)
        _checked_command(
            runner,
            (
                str(node),
                str(vsce_entry),
                "package",
                "--no-dependencies",
                "--out",
                str(raw_vsix),
            ),
            staging,
            label="local vsce package",
        )
        root_license = _read_regular_file(
            repo / "LICENSE",
            label="repository root LICENSE",
            limit=256 * 1024,
        )
        try:
            vsix_report = vsix.normalize_vsix(raw_vsix, final_vsix, root_license)
        except vsix.VsixPolicyError as exc:
            _fail("VSIX normalization or verification failed", cause=exc)
        report = CandidateManifest(
            archive_sha256=vsix_report.archive_sha256,
            extension_id=vsix_report.extension_id,
            license=vsix.EXPECTED_LICENSE,
            members=vsix_report.members,
            publisher=vsix.EXPECTED_PUBLISHER,
            release_tag=EXPECTED_RELEASE_TAG,
            source_commit=source_commit,
            version=vsix_report.version,
        )
        _write_create_only(
            manifest_path,
            report.canonical_manifest_bytes(),
            label="candidate manifest",
        )
        _remove_owned_tree(staging, workspace)
        _remove_owned_file(raw_vsix, workspace)
        if {path.name for path in workspace.iterdir()} != {
            CANDIDATE_FILENAME,
            MANIFEST_FILENAME,
        }:
            _fail("candidate workspace contains unexpected outputs")
        try:
            workspace.rename(output)
        except OSError as exc:
            _fail("candidate output directory could not be created", cause=exc)
        completed = True
        return report
    finally:
        if not completed:
            _cleanup_failed_workspace(workspace, parent)


def build_marketplace_candidate(
    repo_root: Path,
    output_dir: Path,
    *,
    runner: CommandRunner = _run_subprocess,
    node_executable: str | Path = "node",
    npm_cli: Path | None = None,
) -> CandidateManifest:
    """Build one source-anchored candidate without login or publication."""
    repo, ide, output = _validate_roots(repo_root, output_dir)
    source_commit = _verify_source_identity(repo, runner)
    vsce_entry = _validate_package_and_tools(ide)
    node = _resolve_executable(node_executable, label="Node")
    resolved_npm_cli = _resolve_npm_cli(node, npm_cli)
    _verify_toolchain(ide, runner, node, resolved_npm_cli)
    _checked_command(
        runner,
        (str(node), str(resolved_npm_cli), "run", "package"),
        ide,
        label="ToukaIDE production bundle",
    )
    if _verify_source_identity(repo, runner) != source_commit:
        _fail("source commit changed during candidate construction")
    return _assemble_candidate(
        repo,
        ide,
        output,
        source_commit,
        runner,
        node,
        vsce_entry,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--npm-cli", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build one candidate and print the same canonical manifest to stdout."""
    args = _build_parser().parse_args(argv)
    report = build_marketplace_candidate(
        args.repo_root,
        args.output_dir,
        node_executable=args.node,
        npm_cli=args.npm_cli,
    )
    sys.stdout.buffer.write(report.canonical_manifest_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

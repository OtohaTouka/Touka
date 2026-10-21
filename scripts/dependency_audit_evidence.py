"""Create-only local evidence files for the two dependency audit guards.

These receipts describe captured inputs and child execution, not network egress
attestation, reviewer approval, or release qualification.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path


def create_evidence_root(path: Path, repository: Path) -> Path:
    """Create an explicitly named, new directory outside the repository."""
    if not path.is_absolute() or any(part.casefold() == "data" for part in path.parts):
        message = "evidence directory must be absolute and outside protected data paths"
        raise ValueError(message)
    if path.is_relative_to(repository):
        message = "evidence directory must be outside the repository"
        raise ValueError(message)
    for ancestor in (*reversed(path.parents), path):
        try:
            metadata = ancestor.stat(follow_symlinks=False)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            message = "evidence directory must not traverse a filesystem link"
            raise ValueError(message)
    resolved = path.resolve()
    if resolved.is_relative_to(repository.resolve()):
        message = "evidence directory resolved inside the repository"
        raise ValueError(message)
    resolved.mkdir(exist_ok=False)
    return resolved


def _write_bytes(path: Path, content: bytes) -> str:
    with path.open("xb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, value: object) -> str:
    """Save one new JSON artifact and return its byte digest."""
    content = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    return _write_bytes(path, content)


def snapshot_inputs(root: Path, inputs: dict[str, Path]) -> None:
    """Preserve only the caller's explicit, repository-owned audit inputs."""
    destination = root / "inputs"
    destination.mkdir(exist_ok=False)
    hashes: dict[str, str] = {}
    for name, source in inputs.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            message = "invalid audit input evidence name"
            raise ValueError(message)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        hashes[name] = _write_bytes(target, source.read_bytes())
    write_json(destination / "receipt.json", hashes)


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else value


class ProcessEvidence:
    """Record one existing guard subprocess without changing how it executes."""

    def __init__(
        self,
        root: Path,
        name: str,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        timeout: int,
    ) -> None:
        self.root = root / name
        self.root.mkdir(exist_ok=False)
        self.invocation_hash = write_json(
            self.root / "invocation.json",
            {
                "argv": argv,
                "cwd": str(cwd),
                "environment_keys": sorted(environment),
                "timeout_seconds": timeout,
                "shell": False,
                "output_mode": "bytes",
            },
        )

    def finish(
        self,
        *,
        stdout: bytes | str | None = None,
        stderr: bytes | str | None = None,
        exit_code: int | None = None,
        error: OSError | subprocess.TimeoutExpired | None = None,
    ) -> None:
        """Save output before any decoding, including partial timeout output."""
        if isinstance(error, subprocess.TimeoutExpired):
            stdout, stderr = error.stdout, error.stderr
        hashes = {
            "invocation.json": self.invocation_hash,
            "stdout.bin": _write_bytes(self.root / "stdout.bin", _output_bytes(stdout)),
            "stderr.bin": _write_bytes(self.root / "stderr.bin", _output_bytes(stderr)),
        }
        write_json(
            self.root / "receipt.json",
            {
                "status": "timeout"
                if isinstance(error, subprocess.TimeoutExpired)
                else "error"
                if error is not None
                else "completed",
                "exit_code": exit_code,
                "error_type": type(error).__name__ if error is not None else None,
                "error": str(error) if error is not None else None,
                "stdout_capture": "text-test-double" if isinstance(stdout, str) else "bytes",
                "stderr_capture": "text-test-double" if isinstance(stderr, str) else "bytes",
                "sha256": hashes,
            },
        )

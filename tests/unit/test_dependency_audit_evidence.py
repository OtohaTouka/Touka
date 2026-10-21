"""Synthetic evidence retention contracts; no registry or product data access."""

from __future__ import annotations

import hashlib
import importlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def test_evidence_root_is_absolute_external_and_create_only(tmp_path: Path) -> None:
    evidence = importlib.import_module("dependency_audit_evidence")
    repository = tmp_path / "repo"
    repository.mkdir()
    root = tmp_path / "retained"
    assert evidence.create_evidence_root(root, repository) == root
    marker = root / "previous-failure.bin"
    marker.write_bytes(b"keep")
    for rejected in (root, repository / "audit", Path("relative-audit")):
        with pytest.raises((OSError, ValueError)):
            evidence.create_evidence_root(rejected, repository)
    assert marker.read_bytes() == b"keep"


def test_parent_link_is_rejected_before_child_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = importlib.import_module("dependency_audit_evidence")
    parent_link = tmp_path / "synthetic-link"
    child = parent_link / "retained"
    original_stat = Path.stat
    visited: list[Path] = []

    def fake_stat(path: Path, *, follow_symlinks: bool = True) -> object:
        if path == child:
            pytest.fail("child metadata must not be touched before rejecting its linked parent")
        if path == parent_link:
            assert follow_symlinks is False
            visited.append(path)
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ValueError, match="filesystem link"):
        evidence.create_evidence_root(child, tmp_path / "repo")
    assert visited == [parent_link]


def test_process_receipt_retains_timeout_bytes_and_config_hash(tmp_path: Path) -> None:
    evidence = importlib.import_module("dependency_audit_evidence")
    capture = evidence.ProcessEvidence(
        tmp_path, "audit", ["runtime", "audit"], tmp_path, {"HOME": "controlled"}, 300
    )
    error = subprocess.TimeoutExpired("audit", 300, output=b"partial\xff", stderr=b"error\xfe")
    capture.finish(error=error)
    process_root = tmp_path / "audit"
    assert (process_root / "stdout.bin").read_bytes() == b"partial\xff"
    assert (process_root / "stderr.bin").read_bytes() == b"error\xfe"
    receipt = json.loads((process_root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "timeout"
    assert receipt["exit_code"] is None
    assert receipt["error_type"] == "TimeoutExpired"
    assert receipt["sha256"]["stdout.bin"] == hashlib.sha256(b"partial\xff").hexdigest()
    invocation = (process_root / "invocation.json").read_bytes()
    assert receipt["sha256"]["invocation.json"] == hashlib.sha256(invocation).hexdigest()
    config = json.loads(invocation)
    assert config["argv"] == ["runtime", "audit"]
    assert config["environment_keys"] == ["HOME"]
    assert "controlled" not in invocation.decode()
    with pytest.raises(FileExistsError):
        capture.finish(error=error)


def test_input_snapshots_preserve_bytes_before_execution(tmp_path: Path) -> None:
    evidence = importlib.import_module("dependency_audit_evidence")
    source = tmp_path / "source.lock"
    source.write_bytes(b"exact\r\nbytes")
    destination = tmp_path / "retained"
    destination.mkdir()
    evidence.snapshot_inputs(destination, {"source.lock": source})
    assert (destination / "inputs" / "source.lock").read_bytes() == source.read_bytes()
    receipt = json.loads((destination / "inputs" / "receipt.json").read_text())
    assert receipt["source.lock"] == hashlib.sha256(source.read_bytes()).hexdigest()

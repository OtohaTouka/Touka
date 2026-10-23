"""Opt-in offline parity test for the pinned real VSCE raw inventory."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
IDE_ROOT = REPO_ROOT / "ToukaIDE"
BUILDER_PATH = IDE_ROOT / "scripts" / "build_marketplace_candidate.py"
EXPECTED_NODE_VERSION = "v24.22.1"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "toukaide_marketplace_vsce_parity_builder",
        BUILDER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    scripts_path = str(BUILDER_PATH.parent)
    sys.path.insert(0, scripts_path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_path)
    return module


def _opt_in_node(gate: tuple[str, str | None]) -> Path:
    variable_name, configured = gate
    if configured is None:
        pytest.skip(
            f"set {variable_name} only under the separately approved exact-host gate",
        )
    node = Path(configured)
    assert node.is_absolute(), f"{variable_name} must be absolute"
    assert node.is_file(), f"{variable_name} must identify a regular file"
    assert not node.is_symlink() and not node.is_junction()
    return node.resolve(strict=True)


def _write_synthetic_staging(staging: Path) -> None:
    package = json.loads((IDE_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = dict(package["scripts"])
    scripts.pop("vscode:prepublish")
    package["scripts"] = scripts
    (staging / "package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for relative_name in package["files"]:
        destination = staging / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative_name == "README.md":
            content = b"# Touka IDE parity probe\n"
        elif relative_name == "LICENSE":
            content = b"MIT License\n"
        else:
            content = f"synthetic:{relative_name}\n".encode()
        destination.write_bytes(content)


def test_real_pinned_vsce_raw_inventory_matches_deterministic_vsix_members(
    tmp_path: Path,
    touka_node_24_gate: tuple[str, str | None],
) -> None:
    """Package only synthetic tmp content; never run the product bundle or builder."""
    node = _opt_in_node(touka_node_24_gate)
    builder = _load_builder()
    vsce_entry = builder._validate_package_and_tools(IDE_ROOT)
    version = builder._run_subprocess((str(node), "--version"), tmp_path)
    assert version.returncode == 0
    assert version.stdout.strip() == EXPECTED_NODE_VERSION

    staging = tmp_path / "staging"
    staging.mkdir()
    _write_synthetic_staging(staging)
    raw_vsix = tmp_path / "raw.vsix"
    result = builder._run_subprocess(
        (
            str(node),
            str(vsce_entry),
            "package",
            "--no-dependencies",
            "--out",
            str(raw_vsix),
        ),
        staging,
    )
    assert result.returncode == 0, f"repo-local vsce failed with exit code {result.returncode}"

    with zipfile.ZipFile(raw_vsix) as archive:
        members = tuple(info.filename for info in archive.infolist())

    expected = builder.vsix.EXPECTED_MEMBERS
    assert len(members) == len(expected) == 10
    assert set(members) == set(expected)
    assert "extension/readme.md" in members
    assert "extension/LICENSE.txt" in members

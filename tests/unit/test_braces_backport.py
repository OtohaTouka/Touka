"""Finite PR72 API and installed-byte contracts in explicitly bound fresh sandboxes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from braces_backport_evidence import consume_braces_installed

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "ToukaChat/container-security/braces-backport"
API_FIXTURE = ROOT / "tests/fixtures/braces-backport/api-compatibility.cjs"
PUBLIC_ARCHIVES = (
    ("braces", "3.0.3", 13972, "1cd18e862c8640b4568b1425a7df4ee030ff201d45b2da8f9f222d2987494ffc"),
    (
        "fill-range",
        "7.1.1",
        5723,
        "1a6c3a3446cb1d693cf6b62785e2f6d8f9596305aff474dd2b31b2d8d427593c",
    ),
    (
        "to-regex-range",
        "5.0.1",
        7479,
        "a63a255ec20a935c3a3334a6645f9245db36e2319ddf1eb6bc0eab2bfe5b42d7",
    ),
    (
        "is-number",
        "7.0.0",
        3730,
        "7b75c1057198cf97696909a9bee176c9c5e9bcb5b03bf3ecef2f484defadd51e",
    ),
)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.fixture(scope="module")
def public_inputs() -> dict[str, Any]:
    node_command = shutil.which("node")
    assert node_command is not None, "Node is required for the finite backport tests"
    inputs: dict[str, Any] = {
        "node_executable": str(Path(node_command).resolve()),
        "archives": {},
        "api_variant": "patched",
    }
    for name, version, size, sha256 in PUBLIC_ARCHIVES:
        path = API_FIXTURE.parent / f"{name}-{version}.tgz"
        assert path.is_file() and not path.is_symlink(), "Tracked public archive required"
        raw = path.read_bytes()
        assert len(raw) == size and _sha(raw) == "sha256:" + sha256
        inputs["archives"][name] = {
            "path": str(path),
            "bytes": size,
            "sha256": "sha256:" + sha256,
            "url": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(),
        }
    # Explicit operator input exists only to replay the saved original-package RED.
    # Normal Git-only runs need neither a cache directory nor an environment grant.
    selected = os.environ.get("TOUKA_BRACES_TEST_BINDINGS")  # noqa: ENV_SSOT_VIOLATION - explicit historical replay input; no product config
    if selected is not None:
        operator = json.loads(Path(selected).read_bytes())
        assert set(operator) == {"node_executable", "archives", "api_variant"}
        assert operator["api_variant"] == "original"
        inputs["api_variant"] = "original"
    node = Path(inputs["node_executable"])
    assert node.is_file() and not node.is_symlink()
    for row in inputs["archives"].values():
        path = Path(row["path"])
        assert path.is_file() and not path.is_symlink()
        raw = path.read_bytes()
        assert len(raw) == row["bytes"] and _sha(raw) == row["sha256"]
    return inputs


def _unpack(row: dict[str, Any], destination: Path) -> None:
    """Copy only bounded regular members from an already hash-bound public tarball."""
    destination.mkdir(parents=True)
    with tarfile.open(row["path"], "r:gz") as archive:
        members = archive.getmembers()
        assert len(members) <= 128
        total = 0
        names: set[str] = set()
        for member in members:
            assert member.name.startswith("package/")
            relative = member.name.removeprefix("package/")
            if member.isdir():
                continue
            assert member.isfile() and not member.issym() and not member.islnk()
            assert relative and "\\" not in relative and ":" not in relative
            assert not any(part in {"", ".", ".."} for part in relative.split("/"))
            assert relative not in names and member.size <= 1024**2
            names.add(relative)
            total += member.size
            assert total <= 2 * 1024**2
            stream = archive.extractfile(member)
            assert stream is not None
            raw = stream.read(member.size + 1)
            assert len(raw) == member.size
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)


@pytest.fixture
def project(tmp_path: Path, public_inputs: dict[str, Any]) -> Path:
    project = tmp_path / "owned-install"
    project.mkdir()
    package = {
        "name": "frontend",
        "version": "0.0.0",
        "private": True,
        "devDependencies": {"micromatch": "4.0.8"},
    }
    packages: dict[str, Any] = {"": package}
    for name, row in public_inputs["archives"].items():
        target = project / "node_modules" / name
        _unpack(row, target)
        document = json.loads((target / "package.json").read_bytes())
        packages[f"node_modules/{name}"] = {
            "version": document["version"],
            "resolved": row["url"],
            "integrity": row["integrity"],
            "dev": True,
            "dependencies": document.get("dependencies", {}),
        }
    parent = project / "node_modules/micromatch"
    parent.mkdir()
    parent_document = {
        "name": "micromatch",
        "version": "4.0.8",
        "dependencies": {"braces": "^3.0.3"},
    }
    (parent / "package.json").write_bytes(_json(parent_document))
    packages["node_modules/micromatch"] = parent_document | {"dev": True}
    (project / "package.json").write_bytes(_json(package))
    (project / "package-lock.json").write_bytes(
        _json({"name": "frontend", "version": "0.0.0", "lockfileVersion": 3, "packages": packages})
    )
    return project


def _node(
    public_inputs: dict[str, Any], project: Path, *args: str
) -> subprocess.CompletedProcess[bytes]:
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}  # noqa: ENV_SSOT_VIOLATION - only Windows bootstrap keys for the owned Node sandbox
    environment.update(TEMP=str(project), TMP=str(project))
    return subprocess.run(  # noqa: S603 - explicit bound Node, fixed source, owned sandbox
        [public_inputs["node_executable"], *args],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _helper(
    public_inputs: dict[str, Any], project: Path, action: str, scope: str = "full"
) -> subprocess.CompletedProcess[bytes]:
    args = [str(SHARED / "backport.mjs"), action, "--project-root", str(project)]
    if action == "verify":
        args.extend(("--scope", scope))
    return _node(public_inputs, project, *args)


def _tree(path: Path) -> dict[str, bytes]:
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file()
    }


@pytest.mark.parametrize("api", ("parse", "compile", "expand", "stringify"))
def test_small_api_max_depth_option(project: Path, public_inputs: dict[str, Any], api: str) -> None:
    if public_inputs["api_variant"] == "patched":
        applied = _helper(public_inputs, project, "apply")
        assert applied.returncode == 0, applied.stderr.decode()
    result = _node(
        public_inputs,
        project,
        str(API_FIXTURE),
        str(project / "node_modules/braces"),
        "option",
        api,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["api"] == api


def test_ordinary_compatibility_matches_official_package(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    args = (str(API_FIXTURE), str(project / "node_modules/braces"), "normal")
    original = _node(public_inputs, project, *args)
    assert original.returncode == 0, original.stderr.decode()
    applied = _helper(public_inputs, project, "apply")
    assert applied.returncode == 0, applied.stderr.decode()
    patched = _node(public_inputs, project, *args)
    assert patched.returncode == 0, patched.stderr.decode()
    assert patched.stdout == original.stdout


def test_apply_changes_only_reviewed_files_and_verify_retains_exact_bytes(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    package = project / "node_modules/braces"
    before = _tree(package)
    lock = (project / "package-lock.json").read_bytes()
    applied = _helper(public_inputs, project, "apply")
    assert applied.returncode == 0, applied.stderr.decode()
    after = _tree(package)
    manifest = json.loads((SHARED / "manifest.json").read_bytes())
    assert set(before) == set(after) == {row["path"] for row in manifest["files"]}
    assert (
        sorted(name for name in before if before[name] != after[name]) == manifest["patch"]["files"]
    )
    assert before["LICENSE"] == after["LICENSE"] and before["package.json"] == after["package.json"]
    assert (project / "package-lock.json").read_bytes() == lock
    for action in ("verify", "apply", "verify"):
        result = _helper(public_inputs, project, action)
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout == applied.stdout
        assert _tree(package) == after
    proof = json.loads(applied.stdout)
    assert proof["project"] == "ToukaChat" and proof["scope"] == "full"
    assert proof["nodes"][0]["parents"] == ["node_modules/micromatch"]
    for row in proof["nodes"][0]["files"]:
        raw = base64.b64decode(row["body_base64"], validate=True)
        assert raw == after[row["path"]] and row["bytes"] == len(raw) and row["sha256"] == _sha(raw)


def test_verify_rejects_original_bytes_without_writing(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    before = _tree(project)
    result = _helper(public_inputs, project, "verify")
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before


@pytest.mark.parametrize(
    "fault", ("changed", "missing", "extra", "version", "unknown-copy", "alias")
)
def test_apply_rejects_unrecognized_install_before_any_write(
    project: Path, public_inputs: dict[str, Any], fault: str
) -> None:
    package = project / "node_modules/braces"
    if fault == "changed":
        (package / "index.js").write_bytes(b"// different public source\n")
    elif fault == "missing":
        (package / "LICENSE").unlink()
    elif fault == "extra":
        (package / "unexpected.txt").write_bytes(b"fixture")
    elif fault == "version":
        lock = json.loads((project / "package-lock.json").read_bytes())
        lock["packages"]["node_modules/braces"]["version"] = "0.0.0"
        (project / "package-lock.json").write_bytes(_json(lock))
    else:
        target = project / (
            "node_modules/micromatch/node_modules/braces"
            if fault == "unknown-copy"
            else "node_modules/alias"
        )
        shutil.copytree(package, target)
    before = _tree(project)
    result = _helper(public_inputs, project, "apply")
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before


def test_partial_patched_install_is_not_resumed(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    original = (project / "node_modules/braces/lib/parse.js").read_bytes()
    assert _helper(public_inputs, project, "apply").returncode == 0
    (project / "node_modules/braces/lib/parse.js").write_bytes(original)
    before = _tree(project)
    for action in ("apply", "verify"):
        result = _helper(public_inputs, project, action)
        assert result.returncode != 0 and not result.stdout
        assert _tree(project) == before


def test_hardlinked_package_file_is_rejected(project: Path, public_inputs: dict[str, Any]) -> None:
    os.link(project / "node_modules/braces/index.js", project / "linked-fixture.js")
    before = _tree(project)
    result = _helper(public_inputs, project, "apply")
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before


def test_runtime_verifies_actual_absence_and_keeps_lock_binding(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    assert _helper(public_inputs, project, "verify", "runtime").returncode != 0
    package = project / "node_modules/braces"
    shutil.rmtree(package)
    before = _tree(project)
    result = _helper(public_inputs, project, "verify", "runtime")
    assert result.returncode == 0, result.stderr.decode()
    proof = json.loads(result.stdout)
    assert proof["scope"] == "runtime" and proof["nodes"][0]["files"] == []
    assert _tree(project) == before
    lock = json.loads((project / "package-lock.json").read_bytes())
    lock["packages"]["node_modules/braces"]["dev"] = False
    (project / "package-lock.json").write_bytes(_json(lock))
    assert _helper(public_inputs, project, "verify", "runtime").returncode != 0


def test_unlisted_cli_options_are_rejected(project: Path, public_inputs: dict[str, Any]) -> None:
    result = _node(
        public_inputs,
        project,
        str(SHARED / "backport.mjs"),
        "verify",
        "--project-root",
        str(project),
        "--scope",
        "full",
        "--policy",
        "other.json",
    )
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize(
    "project_name, expected", (("frontend", "ToukaChat"), ("toukaide", "ToukaIDE"))
)
def test_relocated_projects_feed_real_helper_bytes_to_reader(
    project: Path, public_inputs: dict[str, Any], project_name: str, expected: str
) -> None:
    package = json.loads((project / "package.json").read_bytes())
    lock = json.loads((project / "package-lock.json").read_bytes())
    package["name"] = lock["name"] = lock["packages"][""]["name"] = project_name
    (project / "package.json").write_bytes(_json(package))
    (project / "package-lock.json").write_bytes(_json(lock))
    applied = _helper(public_inputs, project, "apply")
    verified = _helper(public_inputs, project, "verify")
    assert applied.returncode == verified.returncode == 0, applied.stderr + verified.stderr
    assert applied.stdout == verified.stdout
    sources = {
        f"{expected}/package.json": (project / "package.json").read_bytes(),
        f"{expected}/package-lock.json": (project / "package-lock.json").read_bytes(),
        **{
            f"ToukaChat/container-security/braces-backport/{name}": (SHARED / name).read_bytes()
            for name in ("backport.mjs", "manifest.json", "braces-3.0.3-depth-limit.patch")
        },
    }
    assert (
        Path(consume_braces_installed.__code__.co_filename).resolve()
        == ROOT / "scripts/braces_backport_evidence.py"
    )
    evidence = consume_braces_installed(verified.stdout, project=expected, source_files=sources)
    assert evidence.project == expected and evidence.node_paths == ("node_modules/braces",)
    assert evidence.proof_sha256 == _sha(verified.stdout)
    assert evidence.policy_sha256 == _sha((SHARED / "manifest.json").read_bytes())
    assert evidence.patch_sha256 == _sha((SHARED / "braces-3.0.3-depth-limit.patch").read_bytes())


def test_two_installations_have_distinct_resolved_parents(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    nested = project / "node_modules/micromatch/node_modules/braces"
    shutil.copytree(project / "node_modules/braces", nested)
    package = json.loads((project / "package.json").read_bytes())
    lock = json.loads((project / "package-lock.json").read_bytes())
    package["dependencies"] = {"braces": "3.0.3"}
    lock["packages"][""]["dependencies"] = package["dependencies"]
    lock["packages"]["node_modules/micromatch/node_modules/braces"] = dict(
        lock["packages"]["node_modules/braces"]
    )
    (project / "package.json").write_bytes(_json(package))
    (project / "package-lock.json").write_bytes(_json(lock))
    result = _helper(public_inputs, project, "apply")
    assert result.returncode == 0, result.stderr.decode()
    nodes = json.loads(result.stdout)["nodes"]
    assert [(row["path"], row["parents"]) for row in nodes] == [
        ("node_modules/braces", [""]),
        ("node_modules/micromatch/node_modules/braces", ["node_modules/micromatch"]),
    ]


@pytest.mark.parametrize(
    "fault", ("duplicate-key", "unlisted-sri", "unused-node", "dependency-alias")
)
def test_lock_identity_drift_is_rejected_without_writes(
    project: Path, public_inputs: dict[str, Any], fault: str
) -> None:
    filename = project / "package-lock.json"
    lock = json.loads(filename.read_bytes())
    if fault == "duplicate-key":
        filename.write_bytes(
            filename.read_bytes().replace(
                b'"lockfileVersion":3', b'"lockfileVersion":3,"lockfileVersion":3'
            )
        )
    else:
        if fault == "unlisted-sri":
            lock["packages"]["node_modules/braces"]["integrity"] = "different-public-fixture"
        elif fault == "unused-node":
            lock["packages"]["node_modules/micromatch"]["dependencies"] = {}
        else:
            lock["packages"]["node_modules/micromatch"]["dependencies"] = {
                "alias": "npm:braces@3.0.3"
            }
        filename.write_bytes(_json(lock))
    before = _tree(project)
    result = _helper(public_inputs, project, "apply")
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before


@pytest.mark.parametrize("scope", ("full", "runtime"))
def test_dangling_modules_directory_is_not_absence(
    project: Path, public_inputs: dict[str, Any], scope: str
) -> None:
    if scope == "runtime":
        shutil.rmtree(project / "node_modules")
        link = "node_modules"
    else:
        link = "node_modules/micromatch/node_modules"
    # Node creates a Windows junction without requiring symlink developer mode;
    # on POSIX the same explicit owned path is a directory symlink.
    created = _node(
        public_inputs,
        project,
        "--eval",
        "const fs=require('node:fs');const path=require('node:path');"
        "fs.symlinkSync(path.resolve('missing-owned-fixture'),process.argv[1],"
        "process.platform==='win32'?'junction':'dir');",
        link,
    )
    assert created.returncode == 0, created.stderr.decode()
    before = _tree(project)
    target = os.readlink(project / link)
    result = _helper(public_inputs, project, "apply" if scope == "full" else "verify", scope)
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before and os.readlink(project / link) == target


def test_normal_vite_cache_survives_apply_and_verification(
    project: Path, public_inputs: dict[str, Any]
) -> None:
    cache = project / "node_modules/.vite"
    (cache / "vitest").mkdir(parents=True)
    (cache / "vitest/results.json").write_bytes(b'{"files":[]}\n')
    (cache / "deps").mkdir()
    (cache / "deps/package.json").write_bytes(b'{"type":"module"}\n')
    (cache / "deps/chunk.js").write_bytes(b"export const fixture = true;\n")
    temporary = project / "node_modules/.vite-temp"
    temporary.mkdir()
    (temporary / "config.mjs").write_bytes(b"export default {};\n")
    before = _tree(cache) | {"temporary": (temporary / "config.mjs").read_bytes()}
    for action in ("apply", "verify"):
        result = _helper(public_inputs, project, action)
        assert result.returncode == 0, result.stderr.decode()
        assert _tree(cache) | {"temporary": (temporary / "config.mjs").read_bytes()} == before


@pytest.mark.parametrize("fault", ("nested-modules", "package-alias", "braces-directory"))
def test_vite_cache_cannot_hide_installed_packages(
    project: Path, public_inputs: dict[str, Any], fault: str
) -> None:
    cache = project / "node_modules/.vite"
    cache.mkdir()
    if fault == "nested-modules":
        (cache / "node_modules").mkdir()
    elif fault == "package-alias":
        (cache / "package.json").write_bytes(_json({"name": "braces", "version": "3.0.3"}))
    else:
        (cache / "braces").mkdir()
    before = _tree(project)
    result = _helper(public_inputs, project, "apply")
    assert result.returncode != 0 and not result.stdout
    assert _tree(project) == before

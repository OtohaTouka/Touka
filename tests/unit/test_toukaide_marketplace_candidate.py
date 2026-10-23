"""Offline tests for the ToukaIDE Marketplace candidate builder."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType
    from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
IDE_ROOT = REPO_ROOT / "ToukaIDE"
SCRIPT_PATH = IDE_ROOT / "scripts" / "build_marketplace_candidate.py"
PACKAGE_PATH = IDE_ROOT / "package.json"
LOCK_PATH = IDE_ROOT / "package-lock.json"
VSCE_VERSION = "3.9.2"
SOURCE_COMMIT = "a" * 40
ROOT_LICENSE = (REPO_ROOT / "LICENSE").read_bytes()


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "toukaide_marketplace_candidate",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    scripts_path = str(SCRIPT_PATH.parent)
    sys.path.insert(0, scripts_path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_path)
    return module


def _package_manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": "toukaide",
        "displayName": "Touka IDE",
        "description": "Touka AI Assistant for VS Code",
        "version": "0.3.0",
        "publisher": "touka-ai",
        "license": "MIT",
        "repository": {
            "type": "git",
            "url": "https://github.com/OtohaTouka/Touka.git",
            "directory": "ToukaIDE",
        },
        "bugs": {"url": "https://github.com/OtohaTouka/Touka/issues"},
        "engines": {"node": ">=20", "vscode": "^1.90.0"},
        "packageManager": "npm@11.19.1",
        "devEngines": {
            "runtime": {
                "name": "node",
                "version": "24.22.1",
                "onFail": "error",
            },
            "packageManager": {
                "name": "npm",
                "version": "11.19.1",
                "onFail": "error",
            },
        },
        "files": [
            "dist/extension.js",
            "dist/webview/index.html",
            "dist/webview/main.js",
            "dist/webview/style.css",
            "README.md",
            "LICENSE",
            "THIRD_PARTY_NOTICES.txt",
        ],
        "main": "./dist/extension.js",
        "scripts": {
            "vscode:prepublish": "npm run package",
            "package": "node scripts/copy-webview.js && esbuild --minify",
        },
        "devDependencies": {"@vscode/vsce": VSCE_VERSION},
    }
    manifest.update(overrides)
    return manifest


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_raw_vsix(path: Path, staging_root: Path) -> None:
    package = json.loads((staging_root / "package.json").read_text(encoding="utf-8"))
    manifest_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" '
        'xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
        "<Metadata>"
        f'<Identity Language="en-US" Id="{package["name"]}" '
        f'Version="{package["version"]}" Publisher="{package["publisher"]}" />'
        "</Metadata>"
        "</PackageManifest>"
    ).encode()
    payloads = {
        "[Content_Types].xml": b"<Types />",
        "extension.vsixmanifest": manifest_xml,
        "extension/package.json": json.dumps(
            package,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
        "extension/readme.md": (staging_root / "README.md").read_bytes(),
        "extension/LICENSE.txt": (staging_root / "LICENSE").read_bytes(),
        "extension/THIRD_PARTY_NOTICES.txt": (
            staging_root / "THIRD_PARTY_NOTICES.txt"
        ).read_bytes(),
        "extension/dist/extension.js": (staging_root / "dist" / "extension.js").read_bytes(),
        "extension/dist/webview/index.html": (
            staging_root / "dist" / "webview" / "index.html"
        ).read_bytes(),
        "extension/dist/webview/main.js": (
            staging_root / "dist" / "webview" / "main.js"
        ).read_bytes(),
        "extension/dist/webview/style.css": (
            staging_root / "dist" / "webview" / "style.css"
        ).read_bytes(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (name, content) in enumerate(reversed(tuple(payloads.items()))):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 27, 0, 0, index * 2))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def _candidate_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    ide = repo / "ToukaIDE"
    scripts = ide / "scripts"
    dist_webview = ide / "dist" / "webview"
    vsce_root = ide / "node_modules" / "@vscode" / "vsce"
    tools = tmp_path / "tools"
    scripts.mkdir(parents=True)
    dist_webview.mkdir(parents=True)
    vsce_root.mkdir(parents=True)
    tools.mkdir()
    (repo / "LICENSE").write_bytes(ROOT_LICENSE)
    (ide / "README.md").write_text("# Touka IDE\n", encoding="utf-8")
    (ide / "THIRD_PARTY_NOTICES.txt").write_text("ToukaIDE third-party notices\n", encoding="utf-8")
    (ide / "dist" / "extension.js").write_bytes(b"exports.activate = () => {};\n")
    (dist_webview / "index.html").write_bytes(b"<!doctype html>\n")
    (dist_webview / "main.js").write_bytes(b"// webview\n")
    (dist_webview / "style.css").write_bytes(b"body {}\n")
    _write_json(ide / "package.json", _package_manifest())
    _write_json(
        ide / "package-lock.json",
        {
            "name": "toukaide",
            "version": "0.3.0",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "toukaide",
                    "version": "0.3.0",
                    "devDependencies": {"@vscode/vsce": VSCE_VERSION},
                },
                "node_modules/@vscode/vsce": {"version": VSCE_VERSION},
            },
        },
    )
    _write_json(
        vsce_root / "package.json",
        {
            "name": "@vscode/vsce",
            "version": VSCE_VERSION,
            "bin": {"vsce": "vsce"},
        },
    )
    vsce_entry = vsce_root / "vsce"
    vsce_entry.write_text("// local vsce entry\n", encoding="utf-8")
    node = tools / "node.exe"
    npm_root = tools / "npm"
    npm_bin = npm_root / "bin"
    npm_bin.mkdir(parents=True)
    npm_cli = npm_bin / "npm-cli.js"
    node.write_bytes(b"test node")
    npm_cli.write_bytes(b"// test npm CLI\n")
    _write_json(npm_root / "package.json", {"name": "npm", "version": "11.19.1"})
    return repo, node, npm_cli


class FakeRunner:
    """Return sealed command results and emulate only the local VSIX packager."""

    def __init__(self, *, dirty: bool = False, tagged_commit: str = SOURCE_COMMIT) -> None:
        self.commands: list[tuple[tuple[str, ...], Path]] = []
        self.dirty = dirty
        self.tagged_commit = tagged_commit

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        normalized = tuple(str(part) for part in command)
        self.commands.append((normalized, cwd))
        if normalized[0] == "git" and "status" in normalized:
            stdout = " M ToukaIDE/package.json\n" if self.dirty else ""
        elif normalized[:4] == ("git", "rev-parse", "--verify", "HEAD"):
            stdout = f"{SOURCE_COMMIT}\n"
        elif normalized[:3] == ("git", "rev-parse", "--verify"):
            stdout = f"{self.tagged_commit}\n"
        elif len(normalized) == 2 and normalized[-1:] == ("--version",):
            stdout = "v24.22.1\n"
        elif (
            len(normalized) == 3
            and normalized[-1:] == ("--version",)
            and Path(normalized[1]).name == "npm-cli.js"
        ):
            stdout = "11.19.1\n"
        elif len(normalized) == 7 and normalized[2] == "verify":
            assert (
                Path(normalized[1])
                == cwd.parent / "ToukaChat/container-security/braces-backport/backport.mjs"
            )
            assert normalized[3:] == ("--project-root", str(cwd), "--scope", "full")
            stdout = "synthetic installed-byte proof\n"
        elif len(normalized) >= 4 and normalized[2:4] == ("run", "package"):
            stdout = "bundle complete\n"
        elif "package" in normalized and "--out" in normalized:
            staged_package = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
            assert "vscode:prepublish" not in staged_package.get("scripts", {})
            output = Path(normalized[normalized.index("--out") + 1])
            _write_raw_vsix(output, cwd)
            stdout = "packaged\n"
        else:
            message = f"unexpected command: {normalized!r}"
            raise AssertionError(message)
        return subprocess.CompletedProcess(normalized, 0, stdout=stdout, stderr="")


def test_marketplace_candidate_builder_exists() -> None:
    assert SCRIPT_PATH.is_file()


def test_vsce_is_an_exact_direct_lockfile_pin() -> None:
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert package.get("devDependencies", {}).get("@vscode/vsce") == VSCE_VERSION
    assert lock["packages"][""]["devDependencies"].get("@vscode/vsce") == VSCE_VERSION
    assert lock["packages"].get("node_modules/@vscode/vsce", {}).get("version") == (VSCE_VERSION)


def test_builder_release_identity_and_tools_match_committed_package() -> None:
    builder = _load_module()
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert builder.vsix.EXPECTED_VERSION == package["version"] == "0.3.0"
    assert f"v{package['version']}" == builder.EXPECTED_RELEASE_TAG
    assert lock["version"] == lock["packages"][""]["version"] == package["version"]
    assert package["devEngines"]["runtime"]["version"] == builder.EXPECTED_NODE_VERSION
    assert package["devEngines"]["packageManager"]["version"] == builder.EXPECTED_NPM_VERSION
    assert package["packageManager"] == f"npm@{builder.EXPECTED_NPM_VERSION}"


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_builds_a_create_only_external_candidate_with_canonical_manifest(
    tmp_path: Path,
) -> None:
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    output = tmp_path / "candidate"
    runner = FakeRunner()

    report = builder.build_marketplace_candidate(
        repo,
        output,
        runner=runner,
        node_executable=node,
        npm_cli=npm_cli,
    )

    assert output.is_dir()
    assert tuple(sorted(path.name for path in output.iterdir())) == (
        "toukaide-0.3.0.manifest.json",
        "toukaide-0.3.0.vsix",
    )
    manifest_path = output / "toukaide-0.3.0.manifest.json"
    assert manifest_path.read_bytes() == report.canonical_manifest_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "touka.ide.marketplace-candidate.v1"
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["release_tag"] == "v0.3.0"
    assert manifest["extension_id"] == "touka-ai.toukaide"
    assert manifest["publisher"] == "touka-ai"
    assert manifest["license"] == "MIT"
    assert manifest["version"] == "0.3.0"
    assert manifest["archive_sha256"].startswith("sha256:")
    assert tuple(member["path"] for member in manifest["members"]) == (
        "[Content_Types].xml",
        "extension.vsixmanifest",
        "extension/package.json",
        "extension/readme.md",
        "extension/LICENSE.txt",
        "extension/THIRD_PARTY_NOTICES.txt",
        "extension/dist/extension.js",
        "extension/dist/webview/index.html",
        "extension/dist/webview/main.js",
        "extension/dist/webview/style.css",
    )
    assert not (repo / "ToukaIDE" / "LICENSE").exists()
    flattened = tuple(part for command, _cwd in runner.commands for part in command)
    assert "npx" not in flattened
    assert "publish" not in flattened
    assert sum(command[2:4] == ("run", "package") for command, _ in runner.commands) == 1
    vsce_commands = [
        command for command, _cwd in runner.commands if "package" in command and "--out" in command
    ]
    assert len(vsce_commands) == 1
    assert "node_modules" in vsce_commands[0][1]
    assert "@vscode" in vsce_commands[0][1]
    assert "vsce" in vsce_commands[0][1]


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_rejects_dirty_release_inputs_before_bundling(tmp_path: Path) -> None:
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    runner = FakeRunner(dirty=True)

    with pytest.raises(builder.CandidatePolicyError, match="release inputs are dirty"):
        builder.build_marketplace_candidate(
            repo,
            tmp_path / "candidate",
            runner=runner,
            node_executable=node,
            npm_cli=npm_cli,
        )

    assert all(command[2:4] != ("run", "package") for command, _ in runner.commands)


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_rejects_a_release_tag_that_does_not_anchor_head(tmp_path: Path) -> None:
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    runner = FakeRunner(tagged_commit="b" * 40)

    with pytest.raises(builder.CandidatePolicyError, match="release tag does not anchor HEAD"):
        builder.build_marketplace_candidate(
            repo,
            tmp_path / "candidate",
            runner=runner,
            node_executable=node,
            npm_cli=npm_cli,
        )


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_never_overwrites_an_existing_candidate_directory(tmp_path: Path) -> None:
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    output = tmp_path / "candidate"
    output.mkdir()
    sentinel = output / "owner.txt"
    sentinel.write_text("owner content", encoding="utf-8")

    with pytest.raises(builder.CandidatePolicyError, match="already exists"):
        builder.build_marketplace_candidate(
            repo,
            output,
            runner=FakeRunner(),
            node_executable=node,
            npm_cli=npm_cli,
        )

    assert sentinel.read_text(encoding="utf-8") == "owner content"


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_rejects_symlinked_candidate_parent(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("unprivileged Windows symlink creation is not portable")
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    real_parent = tmp_path / "real-output"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-output"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(builder.CandidatePolicyError, match="real directory"):
        builder.build_marketplace_candidate(
            repo,
            linked_parent / "candidate",
            runner=FakeRunner(),
            node_executable=node,
            npm_cli=npm_cli,
        )


@pytest.mark.skipif(not SCRIPT_PATH.is_file(), reason="builder not implemented yet")
def test_rejects_npm_cli_without_exact_package_identity(tmp_path: Path) -> None:
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    _write_json(
        npm_cli.parent.parent / "package.json",
        {"name": "npm", "version": "11.13.0"},
    )

    with pytest.raises(builder.CandidatePolicyError, match="npm CLI package"):
        builder.build_marketplace_candidate(
            repo,
            tmp_path / "candidate",
            runner=FakeRunner(),
            node_executable=node,
            npm_cli=npm_cli,
        )


def test_fake_vsix_members_are_regular_files(tmp_path: Path) -> None:
    """Keep the test packager from accidentally modeling symlink members."""
    repo, _node, _npm_cli = _candidate_repo(tmp_path)
    staging = repo / "ToukaIDE"
    (staging / "LICENSE").write_bytes(ROOT_LICENSE)
    archive_path = tmp_path / "raw.vsix"
    _write_raw_vsix(archive_path, staging)

    with zipfile.ZipFile(archive_path) as archive:
        assert all(
            stat.S_IFMT(info.external_attr >> 16) != stat.S_IFLNK for info in archive.infolist()
        )


def test_subprocess_boundary_decodes_utf8_bytes_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builder = _load_module()
    observed: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="✓ packaged\n".encode(),
            stderr="無警告\n".encode(),
        )

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = builder._run_subprocess(("node.exe", "--version"), tmp_path)

    assert observed["text"] is False
    assert result.stdout == "✓ packaged\n"
    assert result.stderr == "無警告\n"


@pytest.mark.parametrize("invalid_stream", ["stdout", "stderr"])
def test_subprocess_boundary_rejects_non_utf8_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_stream: str,
) -> None:
    builder = _load_module()

    def fake_run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        outputs = {"stdout": b"", "stderr": b""}
        outputs[invalid_stream] = b"\xff"
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=outputs["stdout"],
            stderr=outputs["stderr"],
        )

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    with pytest.raises(
        builder.CandidatePolicyError,
        match="command output is not valid UTF-8",
    ):
        builder._run_subprocess(("node.exe", "--version"), tmp_path)


def test_checked_command_rejects_missing_text_output(tmp_path: Path) -> None:
    builder = _load_module()

    def malformed_runner(
        command: Sequence[str],
        _cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

    with pytest.raises(
        builder.CandidatePolicyError,
        match="command did not return text output",
    ):
        builder._checked_command(
            malformed_runner,
            ("node.exe", "--version"),
            tmp_path,
            label="Node version",
        )


@pytest.mark.parametrize("failure_at", [1, 2])
def test_braces_verification_refusal_stops_package_or_vsce(tmp_path, failure_at):
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)

    class RefusingRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.verifies = 0

        def __call__(self, command, cwd):
            if len(command) > 2 and command[2] == "verify":
                self.verifies += 1
                if self.verifies == failure_at:
                    self.commands.append((tuple(command), cwd))
                    return subprocess.CompletedProcess(command, 2, "", "synthetic refusal")
            return super().__call__(command, cwd)

    runner = RefusingRunner()
    output = tmp_path / "candidate"
    with pytest.raises(builder.CandidatePolicyError, match="braces"):
        builder.build_marketplace_candidate(
            repo, output, runner=runner, node_executable=node, npm_cli=npm_cli
        )
    assert not output.exists()
    assert not any("--out" in command for command, _ in runner.commands)
    assert (
        sum(command[2:4] == ("run", "package") for command, _ in runner.commands) == failure_at - 1
    )


def test_braces_verification_uses_source_install_not_staging(tmp_path):
    builder = _load_module()
    repo, node, npm_cli = _candidate_repo(tmp_path)
    runner = FakeRunner()
    builder.build_marketplace_candidate(
        repo, tmp_path / "candidate", runner=runner, node_executable=node, npm_cli=npm_cli
    )
    calls = runner.commands
    verifies = [
        i for i, (command, _) in enumerate(calls) if len(command) > 2 and command[2] == "verify"
    ]
    assert len(verifies) == 2
    package = next(i for i, (command, _) in enumerate(calls) if command[2:4] == ("run", "package"))
    vsce = next(i for i, (command, _) in enumerate(calls) if "--out" in command)
    assert verifies[0] < package < verifies[1] < vsce
    assert all(calls[i][1] == repo / "ToukaIDE" for i in verifies)
    assert all(
        "ToukaChat/container-security/braces-backport" in command
        for command, _ in calls
        if "status" in command
    )

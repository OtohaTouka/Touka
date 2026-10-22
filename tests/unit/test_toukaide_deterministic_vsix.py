"""Offline tests for the deterministic ToukaIDE VSIX boundary."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import warnings
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType
    from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "ToukaIDE" / "scripts" / "deterministic_vsix.py"
PACKAGE_PATH = REPO_ROOT / "ToukaIDE" / "package.json"
LOCK_PATH = REPO_ROOT / "ToukaIDE" / "package-lock.json"
ROOT_LICENSE = b"MIT License\n\nCopyright (c) 2026 Otoha\n"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("toukaide_deterministic_vsix", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vsix = _load_module()
TEST_LICENSE_SHA256 = vsix.sha256_digest(ROOT_LICENSE)


def test_vsix_members_are_the_closed_package_files_projection() -> None:
    """VSIX members are vsce metadata plus package.json files (vsce renames README/LICENSE)."""
    renamed = {"README.md": "extension/readme.md", "LICENSE": vsix.EXPECTED_LICENSE_MEMBER}
    projected = {renamed.get(path, f"extension/{path}") for path in vsix.PACKAGE_FILES}
    metadata = {"[Content_Types].xml", "extension.vsixmanifest", "extension/package.json"}
    assert set(vsix.EXPECTED_MEMBERS) == metadata | projected
    assert len(vsix.EXPECTED_MEMBERS) == len(set(vsix.EXPECTED_MEMBERS)) == 10


def test_root_license_checkout_policy_forces_lf() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "/LICENSE text eol=lf" in attributes


def test_root_license_seal_uses_committed_lf_bytes() -> None:
    content = (REPO_ROOT / "LICENSE").read_bytes()
    expected = "sha256:1059380d76a5925c5d2bd7dcad7efa3118994a1455c977cf989a3012c8d3d69e"

    assert b"\r\n" not in content
    assert vsix.sha256_digest(content) == expected
    assert expected == vsix.EXPECTED_ROOT_LICENSE_SHA256


def _verify_vsix(path: Path, *, require_deterministic: bool = False) -> Any:
    return vsix.verify_vsix(
        path,
        ROOT_LICENSE,
        expected_license_sha256=TEST_LICENSE_SHA256,
        require_deterministic=require_deterministic,
    )


def _normalize_vsix(source: Path, output: Path) -> Any:
    return vsix.normalize_vsix(
        source,
        output,
        ROOT_LICENSE,
        expected_license_sha256=TEST_LICENSE_SHA256,
    )


def _license_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "LICENSE").write_bytes(ROOT_LICENSE)
    (repo / "ToukaIDE").mkdir()
    return repo


def _package_manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": "toukaide",
        "publisher": "touka-ai",
        "version": "0.3.0",
        "license": "MIT",
        "displayName": "Touka IDE",
        "description": "Touka AI Assistant for VS Code",
        "engines": {"node": ">=20", "vscode": "^1.90.0"},
        "main": "./dist/extension.js",
        "repository": {
            "type": "git",
            "url": "https://github.com/OtohaTouka/Touka.git",
            "directory": "ToukaIDE",
        },
        "bugs": {"url": "https://github.com/OtohaTouka/Touka/issues"},
        "files": list(vsix.PACKAGE_FILES),
    }
    manifest.update(overrides)
    return manifest


def _manifest_xml(*, version: str = "0.3.0", publisher: str = "touka-ai") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PackageManifest Version="2.0.0" '
        'xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">'
        "<Metadata>"
        f'<Identity Language="en-US" Id="toukaide" Version="{version}" '
        f'Publisher="{publisher}" />'
        "</Metadata>"
        "</PackageManifest>"
    ).encode()


def _member_payloads(
    *,
    package_overrides: dict[str, Any] | None = None,
    license_bytes: bytes = ROOT_LICENSE,
) -> dict[str, bytes]:
    package = _package_manifest(**(package_overrides or {}))
    return {
        "[Content_Types].xml": b"<Types />",
        "extension.vsixmanifest": _manifest_xml(
            version=str(package["version"]),
            publisher=str(package["publisher"]),
        ),
        "extension/package.json": json.dumps(
            package,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
        "extension/readme.md": b"# Touka IDE\n",
        "extension/LICENSE.txt": license_bytes,
        "extension/THIRD_PARTY_NOTICES.txt": b"ToukaIDE third-party notices\n",
        "extension/dist/extension.js": b"exports.activate = async () => {};\n",
        "extension/dist/webview/index.html": b"<!doctype html>\n",
        "extension/dist/webview/main.js": b"// webview\n",
        "extension/dist/webview/style.css": b"body {}\n",
    }


def _write_source_vsix(
    path: Path,
    payloads: dict[str, bytes],
    *,
    order: tuple[str, ...] | None = None,
) -> None:
    names = order or tuple(reversed(tuple(payloads)))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(names):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 24, 12, 0, index * 2))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payloads[name])


def test_package_identity_and_universal_member_projection_are_exact() -> None:
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert package["name"] == "toukaide"
    assert package["publisher"] == "touka-ai"
    assert package["version"] == "0.3.0"
    assert package["license"] == "MIT"
    assert package["repository"] == {
        "type": "git",
        "url": "https://github.com/OtohaTouka/Touka.git",
        "directory": "ToukaIDE",
    }
    assert package["bugs"] == {
        "url": "https://github.com/OtohaTouka/Touka/issues",
    }
    assert tuple(package["files"]) == vsix.PACKAGE_FILES
    assert lock["version"] == package["version"]
    assert lock["packages"][""]["version"] == package["version"]
    assert vsix.sha256_digest((REPO_ROOT / "LICENSE").read_bytes()) == (
        vsix.EXPECTED_ROOT_LICENSE_SHA256
    )
    assert not (REPO_ROOT / "ToukaIDE" / "LICENSE").exists()


def test_injects_only_the_committed_root_license_into_external_staging(tmp_path: Path) -> None:
    repo = _license_repo(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()

    digest = vsix.inject_tracked_license(
        repo,
        staging,
        expected_sha256=vsix.sha256_digest(ROOT_LICENSE),
    )

    assert (staging / "LICENSE").read_bytes() == ROOT_LICENSE
    assert digest == vsix.sha256_digest(ROOT_LICENSE)
    assert (repo / "ToukaIDE" / "LICENSE").exists() is False


def test_license_injection_rejects_root_license_digest_drift(tmp_path: Path) -> None:
    repo = _license_repo(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    (repo / "LICENSE").write_bytes(b"dirty worktree license")

    with pytest.raises(vsix.VsixPolicyError, match="root license digest"):
        vsix.inject_tracked_license(
            repo,
            staging,
            expected_sha256=vsix.sha256_digest(ROOT_LICENSE),
        )
    assert not (staging / "LICENSE").exists()


def test_verifier_rejects_an_unsealed_root_license_reference(tmp_path: Path) -> None:
    source = tmp_path / "unsealed-license.vsix"
    _write_source_vsix(source, _member_payloads())

    with pytest.raises(vsix.VsixPolicyError, match="root license digest"):
        vsix.verify_vsix(source, ROOT_LICENSE)


@pytest.mark.parametrize("target_kind", ["repo", "extension", "existing"])
def test_license_injection_rejects_unsafe_or_existing_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    repo = _license_repo(tmp_path)
    if target_kind == "repo":
        staging = repo
    elif target_kind == "extension":
        staging = repo / "ToukaIDE"
    else:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "LICENSE").write_bytes(b"do not overwrite")

    with pytest.raises(vsix.VsixPolicyError):
        vsix.inject_tracked_license(
            repo,
            staging,
            expected_sha256=vsix.sha256_digest(ROOT_LICENSE),
        )


def test_normalization_is_byte_deterministic_and_emits_canonical_manifest(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "source-a.vsix"
    source_b = tmp_path / "source-b.vsix"
    output_a = tmp_path / "normalized-a.vsix"
    output_b = tmp_path / "normalized-b.vsix"
    payloads = _member_payloads()
    _write_source_vsix(source_a, payloads)
    _write_source_vsix(source_b, payloads, order=tuple(payloads))

    report_a = _normalize_vsix(source_a, output_a)
    report_b = _normalize_vsix(source_b, output_b)

    assert output_a.read_bytes() == output_b.read_bytes()
    assert report_a.archive_sha256 == report_b.archive_sha256
    assert report_a.canonical_manifest_bytes() == report_b.canonical_manifest_bytes()
    assert tuple(member.path for member in report_a.members) == vsix.EXPECTED_MEMBERS
    verified = _verify_vsix(output_a, require_deterministic=True)
    assert verified == report_a


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing", "member set"),
        ("extra", "member set"),
        ("license", "license"),
        ("identity", "package identity"),
    ],
)
def test_verifier_rejects_member_and_identity_drift(
    tmp_path: Path,
    mutation: str,
    expected_reason: str,
) -> None:
    payloads = _member_payloads(
        package_overrides={"publisher": "hostile"} if mutation == "identity" else None,
        license_bytes=b"different" if mutation == "license" else ROOT_LICENSE,
    )
    if mutation == "missing":
        payloads.pop("extension/dist/webview/style.css")
    if mutation == "extra":
        payloads["extension/source.ts"] = b"must not ship"
    source = tmp_path / f"{mutation}.vsix"
    _write_source_vsix(source, payloads)

    with pytest.raises(vsix.VsixPolicyError, match=expected_reason):
        _verify_vsix(source)


def test_member_inventory_diagnostics_are_sorted_and_bounded(tmp_path: Path) -> None:
    payloads = _member_payloads()
    payloads.pop("extension/dist/webview/main.js")
    payloads.pop("extension/dist/webview/style.css")
    for index in range(12):
        payloads[f"extension/extra/{index:02}.txt"] = b"unexpected"
    source = tmp_path / "inventory-drift.vsix"
    _write_source_vsix(source, payloads)

    with pytest.raises(vsix.VsixPolicyError) as exc_info:
        _verify_vsix(source)

    message = str(exc_info.value)
    assert (
        'missing=["extension/dist/webview/main.js", "extension/dist/webview/style.css"]'
    ) in message
    for index in range(8):
        assert f"extension/extra/{index:02}.txt" in message
    for index in range(8, 12):
        assert f"extension/extra/{index:02}.txt" not in message
    assert "...(+4 more)" in message
    assert len(message) <= 2048


def test_member_inventory_diagnostics_escape_and_truncate_untrusted_names(
    tmp_path: Path,
) -> None:
    payloads = _member_payloads()
    untrusted_name = "extension/\x1b[31m\u202e" + ("x" * 1024) + ".txt"
    payloads[untrusted_name] = b"unexpected"
    source = tmp_path / "unsafe-display-name.vsix"
    _write_source_vsix(source, payloads)

    with pytest.raises(vsix.VsixPolicyError) as exc_info:
        _verify_vsix(source)

    message = str(exc_info.value)
    assert "\x1b" not in message
    assert "\u202e" not in message
    assert "\\u001b" in message
    assert "\\u202e" in message
    assert untrusted_name not in message
    assert len(message) <= 2048


def test_verifier_rejects_duplicate_unsafe_and_symlink_members(tmp_path: Path) -> None:
    payloads = _member_payloads()

    duplicate = tmp_path / "duplicate.vsix"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
            archive.writestr("extension/LICENSE.txt", ROOT_LICENSE)
    with pytest.raises(vsix.VsixPolicyError, match="duplicate"):
        _verify_vsix(duplicate)

    unsafe = tmp_path / "unsafe.vsix"
    unsafe_payloads = dict(payloads)
    unsafe_payloads["extension/../escape"] = unsafe_payloads.pop(
        "extension/dist/webview/style.css",
    )
    _write_source_vsix(unsafe, unsafe_payloads)
    with pytest.raises(vsix.VsixPolicyError, match="unsafe"):
        _verify_vsix(unsafe)

    symlink = tmp_path / "symlink.vsix"
    with zipfile.ZipFile(symlink, "w") as archive:
        for name, content in payloads.items():
            info = zipfile.ZipInfo(name)
            if name == "extension/LICENSE.txt":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, content)
    with pytest.raises(vsix.VsixPolicyError, match="symlink"):
        _verify_vsix(symlink)


def test_verifier_rejects_identity_markup_spoofed_inside_a_comment(
    tmp_path: Path,
) -> None:
    payloads = _member_payloads()
    payloads["extension.vsixmanifest"] = (
        b'<!-- <Identity Id="toukaide" Publisher="touka-ai" Version="0.3.0" /> -->'
        b"<PackageManifest />"
    )
    source = tmp_path / "comment-spoof.vsix"
    _write_source_vsix(source, payloads)

    with pytest.raises(vsix.VsixPolicyError, match="forbidden declarations"):
        _verify_vsix(source)


def test_deterministic_verification_rejects_unsealed_zip_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.vsix"
    _write_source_vsix(source, _member_payloads())

    _verify_vsix(source, require_deterministic=False)
    with pytest.raises(vsix.VsixPolicyError, match="deterministic"):
        _verify_vsix(source, require_deterministic=True)


def test_normalizer_never_overwrites_an_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.vsix"
    output = tmp_path / "output.vsix"
    _write_source_vsix(source, _member_payloads())
    output.write_bytes(b"owner content")

    with pytest.raises(vsix.VsixPolicyError, match="already exists"):
        _normalize_vsix(source, output)
    assert output.read_bytes() == b"owner content"

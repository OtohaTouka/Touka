"""Create and verify byte-deterministic universal ToukaIDE VSIX archives.

The script never downloads tools, packages an extension, or contacts a registry.
It normalizes an already-created VSIX into a fixed, stored ZIP representation and
injects the repository-root license only into an explicit external staging root.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


EXPECTED_EXTENSION_NAME = "toukaide"
EXPECTED_PUBLISHER = "touka-ai"
EXPECTED_VERSION = "0.3.0"
EXPECTED_LICENSE = "MIT"
EXPECTED_EXTENSION_ID = f"{EXPECTED_PUBLISHER}.{EXPECTED_EXTENSION_NAME}"
EXPECTED_LICENSE_MEMBER = "extension/LICENSE.txt"
EXPECTED_ROOT_LICENSE_SHA256 = (
    "sha256:1059380d76a5925c5d2bd7dcad7efa3118994a1455c977cf989a3012c8d3d69e"
)
EXPECTED_REPOSITORY = {
    "type": "git",
    "url": "https://github.com/OtohaTouka/Touka.git",
    "directory": "ToukaIDE",
}
EXPECTED_BUGS = {"url": "https://github.com/OtohaTouka/Touka/issues"}
PACKAGE_FILES = (
    "dist/extension.js",
    "dist/webview/index.html",
    "dist/webview/main.js",
    "dist/webview/style.css",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.txt",
)
EXPECTED_MEMBERS = (
    "[Content_Types].xml",
    "extension.vsixmanifest",
    "extension/package.json",
    "extension/readme.md",
    EXPECTED_LICENSE_MEMBER,
    "extension/THIRD_PARTY_NOTICES.txt",
    "extension/dist/extension.js",
    "extension/dist/webview/index.html",
    "extension/dist/webview/main.js",
    "extension/dist/webview/style.css",
)
DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
DETERMINISTIC_MODE = stat.S_IFREG | 0o644
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_DIAGNOSTIC_MEMBERS = 8
MAX_DIAGNOSTIC_MEMBER_CHARS = 160
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY_PATTERN = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?Identity\b([^>]*)/?>")
_ATTRIBUTE_PATTERN = re.compile(
    r"([A-Za-z_][\w.:-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
)


class VsixPolicyError(RuntimeError):
    """The candidate violates the deterministic universal VSIX policy."""


def _fail(reason: str, *, cause: BaseException | None = None) -> NoReturn:
    """Raise a policy violation while retaining an optional low-level cause."""
    if cause is None:
        raise VsixPolicyError(reason)
    raise VsixPolicyError(reason) from cause


@dataclass(frozen=True, slots=True)
class VsixMember:
    """Content identity for one canonical VSIX member."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VsixReport:
    """Canonical identity for a verified VSIX archive."""

    archive_sha256: str
    extension_id: str
    version: str
    members: tuple[VsixMember, ...]

    def canonical_manifest_bytes(self) -> bytes:
        """Serialize the report without timestamps or host-specific fields."""
        payload = {
            "archive_sha256": self.archive_sha256,
            "extension_id": self.extension_id,
            "members": [asdict(member) for member in self.members],
            "schema": "touka.ide.vsix-candidate.v1",
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


def sha256_digest(content: bytes) -> str:
    """Return the canonical prefixed SHA-256 identity for bytes."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _is_redirected(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _require_digest(value: str, *, label: str) -> str:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        _fail(f"{label} must be a canonical SHA-256 digest")
    return value


def _read_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    if _is_redirected(path) or not path.is_file():
        _fail(f"{label} must be a regular file")
    size = path.stat().st_size
    if size < 0 or size > limit:
        _fail(f"{label} exceeds the bounded size")
    try:
        return path.read_bytes()
    except OSError as exc:
        _fail(f"{label} is unreadable", cause=exc)


def _read_hash_bound_root_license(
    repo_root: Path,
    expected_sha256: str,
) -> bytes:
    expected = _require_digest(expected_sha256, label="expected root license digest")
    repo = repo_root.resolve(strict=True)
    if _is_redirected(repo) or not repo.is_dir():
        _fail("repository root must be a real directory")
    content = _read_regular_file(
        repo / "LICENSE",
        label="repository root LICENSE",
        limit=256 * 1024,
    )
    if sha256_digest(content) != expected:
        _fail("root license digest does not match the sealed policy")
    return content


def inject_tracked_license(
    repo_root: Path,
    staging_root: Path,
    *,
    expected_sha256: str = EXPECTED_ROOT_LICENSE_SHA256,
) -> str:
    """Inject the hash-bound root LICENSE into an external staging directory.

    The destination must already exist outside the repository and must not
    contain a LICENSE entry. The source tree is never modified.
    """
    repo = repo_root.resolve(strict=True)
    stage = staging_root.resolve(strict=True)
    if _is_redirected(stage) or not stage.is_dir():
        _fail("staging root must be a real directory")
    if stage == repo or stage.is_relative_to(repo):
        _fail("staging root must be outside the repository")
    content = _read_hash_bound_root_license(repo, expected_sha256)
    destination = stage / "LICENSE"
    if destination.exists() or _is_redirected(destination):
        _fail("staging LICENSE already exists")

    created = False
    try:
        with destination.open("xb") as handle:
            created = True
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o644)
    except OSError as exc:
        if created:
            destination.unlink(missing_ok=True)
        _fail("staging LICENSE injection failed", cause=exc)
    return sha256_digest(content)


def _validate_member_path(path: str) -> str:
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or ":" in path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(character in path for character in "\x00\r\n\t")
    ):
        _fail(f"unsafe VSIX member path: {path!r}")
    return pure.as_posix()


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    if info.create_system != 3:
        return False
    return stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK


def _bounded_member_diagnostics(paths: set[str]) -> str:
    ordered = sorted(paths)
    visible = ordered[:MAX_DIAGNOSTIC_MEMBERS]
    encoded: list[str] = []
    for path in visible:
        bounded = (
            path
            if len(path) <= MAX_DIAGNOSTIC_MEMBER_CHARS
            else f"{path[: MAX_DIAGNOSTIC_MEMBER_CHARS - 3]}..."
        )
        encoded.append(json.dumps(bounded, ensure_ascii=True))
    remaining = len(ordered) - len(visible)
    if remaining:
        encoded.append(f"...(+{remaining} more)")
    return f"[{', '.join(encoded)}]"


def _validate_archive_inventory(
    archive: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    total_size = 0
    for info in infos:
        path = _validate_member_path(info.filename)
        folded = path.casefold()
        if path in seen or folded in seen_casefold:
            _fail(f"duplicate VSIX member: {path}")
        seen.add(path)
        seen_casefold.add(folded)
        if info.is_dir():
            _fail(f"directory VSIX member is forbidden: {path}")
        if _is_zip_symlink(info):
            _fail(f"symlink VSIX member is forbidden: {path}")
        if info.flag_bits & 0x1:
            _fail(f"encrypted VSIX member is forbidden: {path}")
        if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
            _fail(f"VSIX member exceeds the bounded size: {path}")
        total_size += info.file_size
        if total_size > MAX_TOTAL_BYTES:
            _fail("VSIX uncompressed content exceeds the bounded size")
        if info.compress_size == 0:
            if info.file_size != 0:
                _fail(f"invalid compressed size for VSIX member: {path}")
        elif info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            _fail(f"VSIX member compression ratio is unsafe: {path}")

    expected = set(EXPECTED_MEMBERS)
    if seen != expected or len(infos) != len(EXPECTED_MEMBERS):
        missing = _bounded_member_diagnostics(expected - seen)
        extra = _bounded_member_diagnostics(seen - expected)
        _fail(
            f"VSIX member set does not match the exact allowlist: missing={missing}; extra={extra}",
        )
    return infos


def _validate_package_manifest(content: bytes) -> Mapping[str, object]:
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("extension package manifest is invalid", cause=exc)
    if not isinstance(manifest, dict):
        _fail("extension package manifest must be an object")
    expected_fields: tuple[tuple[str, object], ...] = (
        ("name", EXPECTED_EXTENSION_NAME),
        ("publisher", EXPECTED_PUBLISHER),
        ("version", EXPECTED_VERSION),
        ("license", EXPECTED_LICENSE),
        ("main", "./dist/extension.js"),
        ("repository", EXPECTED_REPOSITORY),
        ("bugs", EXPECTED_BUGS),
        ("files", list(PACKAGE_FILES)),
    )
    for field, expected in expected_fields:
        if manifest.get(field) != expected:
            _fail(f"package identity or policy field mismatch: {field}")
    engines = manifest.get("engines")
    if not isinstance(engines, dict) or engines != {
        "node": ">=20",
        "vscode": "^1.90.0",
    }:
        _fail("package identity or policy field mismatch: engines")
    return manifest


def _validate_vsix_manifest(content: bytes) -> None:
    try:
        source = content.decode("utf-8")
    except UnicodeError as exc:
        _fail("VSIX identity manifest is not UTF-8", cause=exc)
    if "<!" in source:
        _fail("VSIX identity manifest contains forbidden declarations")
    in_tag = False
    quote: str | None = None
    for character in source:
        if not in_tag:
            if character == "<":
                in_tag = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "<":
                _fail("VSIX identity manifest contains nested markup")
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "<":
            _fail("VSIX identity manifest contains nested markup")
        elif character == ">":
            in_tag = False
    if in_tag or quote is not None:
        _fail("VSIX identity manifest has unterminated markup")

    matches = _IDENTITY_PATTERN.findall(source)
    if len(matches) != 1:
        _fail("VSIX identity manifest must contain one Identity")
    attributes: dict[str, str] = {}
    for match in _ATTRIBUTE_PATTERN.finditer(matches[0]):
        name, double_quoted, single_quoted = match.groups()
        if name in attributes:
            _fail("VSIX identity manifest has duplicate attributes")
        attributes[name] = double_quoted or single_quoted
    expected = {
        "Id": EXPECTED_EXTENSION_NAME,
        "Publisher": EXPECTED_PUBLISHER,
        "Version": EXPECTED_VERSION,
    }
    if any(attributes.get(name) != value for name, value in expected.items()):
        _fail("VSIX identity manifest does not match the package identity")


def _require_deterministic_metadata(
    archive: zipfile.ZipFile,
    infos: Sequence[zipfile.ZipInfo],
) -> None:
    if archive.comment:
        _fail("VSIX deterministic metadata has an archive comment")
    if tuple(info.filename for info in infos) != EXPECTED_MEMBERS:
        _fail("VSIX deterministic member order mismatch")
    for info in infos:
        mode = (info.external_attr >> 16) & 0xFFFF
        if (
            info.date_time != DETERMINISTIC_TIMESTAMP
            or info.compress_type != zipfile.ZIP_STORED
            or info.create_system != 3
            or mode != DETERMINISTIC_MODE
            or info.internal_attr != 0
            or info.extra
            or info.comment
            or info.flag_bits != 0
        ):
            _fail(f"VSIX deterministic metadata mismatch: {info.filename}")


def _read_verified_members(
    vsix_path: Path,
    root_license: bytes,
    *,
    expected_license_sha256: str,
    require_deterministic: bool,
) -> tuple[tuple[VsixMember, ...], dict[str, bytes], bytes]:
    expected_license = _require_digest(
        expected_license_sha256,
        label="expected root license digest",
    )
    if sha256_digest(root_license) != expected_license:
        _fail("root license digest does not match the sealed policy")
    raw_archive = _read_regular_file(
        vsix_path,
        label="VSIX archive",
        limit=MAX_ARCHIVE_BYTES,
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw_archive), "r") as archive:
            infos = _validate_archive_inventory(archive)
            if require_deterministic:
                _require_deterministic_metadata(archive, infos)
            payloads: dict[str, bytes] = {}
            for info in infos:
                try:
                    payloads[info.filename] = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    _fail(
                        f"VSIX member cannot be verified: {info.filename}",
                        cause=exc,
                    )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        _fail("invalid VSIX archive", cause=exc)

    if payloads[EXPECTED_LICENSE_MEMBER] != root_license:
        _fail("VSIX license is not the exact root LICENSE copy")
    _validate_package_manifest(payloads["extension/package.json"])
    _validate_vsix_manifest(payloads["extension.vsixmanifest"])
    forbidden_suffixes = (".node", ".dll", ".so", ".dylib")
    if any(
        member.casefold().endswith(forbidden_suffixes)
        or "/node_modules/" in f"/{member.casefold()}/"
        for member in payloads
    ):
        _fail("universal VSIX contains platform-specific content")
    members = tuple(
        VsixMember(
            path=path,
            size=len(payloads[path]),
            sha256=sha256_digest(payloads[path]),
        )
        for path in EXPECTED_MEMBERS
    )
    return members, payloads, raw_archive


def verify_vsix(
    vsix_path: Path,
    root_license: bytes,
    *,
    expected_license_sha256: str = EXPECTED_ROOT_LICENSE_SHA256,
    require_deterministic: bool = False,
) -> VsixReport:
    """Verify exact members, identity, license, bounds, and optional ZIP sealing."""
    members, _payloads, archive_content = _read_verified_members(
        vsix_path,
        root_license,
        expected_license_sha256=expected_license_sha256,
        require_deterministic=require_deterministic,
    )
    return VsixReport(
        archive_sha256=sha256_digest(archive_content),
        extension_id=EXPECTED_EXTENSION_ID,
        version=EXPECTED_VERSION,
        members=members,
    )


def _deterministic_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=DETERMINISTIC_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = DETERMINISTIC_MODE << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    info.flag_bits = 0
    return info


def normalize_vsix(
    source_path: Path,
    output_path: Path,
    root_license: bytes,
    *,
    expected_license_sha256: str = EXPECTED_ROOT_LICENSE_SHA256,
) -> VsixReport:
    """Repack an exact allowlisted VSIX into deterministic stored ZIP bytes."""
    source = source_path.resolve(strict=True)
    output = output_path.resolve(strict=False)
    if source == output:
        _fail("normalized VSIX output must differ from the source")
    if not output.parent.is_dir() or _is_redirected(output.parent):
        _fail("normalized VSIX output parent is unavailable")
    if output.exists() or _is_redirected(output):
        _fail("normalized VSIX output already exists")
    _members, payloads, _raw_archive = _read_verified_members(
        source,
        root_license,
        expected_license_sha256=expected_license_sha256,
        require_deterministic=False,
    )

    created = False
    try:
        with zipfile.ZipFile(
            output,
            "x",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            created = True
            archive.comment = b""
            for path in EXPECTED_MEMBERS:
                archive.writestr(_deterministic_info(path), payloads[path])
        return verify_vsix(
            output,
            root_license,
            expected_license_sha256=expected_license_sha256,
            require_deterministic=True,
        )
    except VsixPolicyError:
        if created:
            output.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if created:
            output.unlink(missing_ok=True)
        _fail("deterministic VSIX normalization failed", cause=exc)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inject = subparsers.add_parser("inject-license")
    inject.add_argument("--repo-root", type=Path, required=True)
    inject.add_argument("--staging-root", type=Path, required=True)
    inject.add_argument(
        "--expected-license-sha256",
        default=EXPECTED_ROOT_LICENSE_SHA256,
    )

    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("--repo-root", type=Path, required=True)
    normalize.add_argument("--input", type=Path, required=True)
    normalize.add_argument("--output", type=Path, required=True)
    normalize.add_argument(
        "--expected-license-sha256",
        default=EXPECTED_ROOT_LICENSE_SHA256,
    )

    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--require-deterministic", action="store_true")
    verify.add_argument(
        "--expected-license-sha256",
        default=EXPECTED_ROOT_LICENSE_SHA256,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run an offline deterministic VSIX operation."""
    args = _build_parser().parse_args(argv)
    if args.command == "inject-license":
        digest = inject_tracked_license(
            args.repo_root,
            args.staging_root,
            expected_sha256=args.expected_license_sha256,
        )
        sys.stdout.write(f"{digest}\n")
        return 0

    license_bytes = _read_hash_bound_root_license(
        args.repo_root,
        args.expected_license_sha256,
    )
    if args.command == "normalize":
        report = normalize_vsix(
            args.input,
            args.output,
            license_bytes,
            expected_license_sha256=args.expected_license_sha256,
        )
    else:
        report = verify_vsix(
            args.input,
            license_bytes,
            expected_license_sha256=args.expected_license_sha256,
            require_deterministic=args.require_deterministic,
        )
    sys.stdout.buffer.write(report.canonical_manifest_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

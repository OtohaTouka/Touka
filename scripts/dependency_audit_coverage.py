"""Account for every frozen input before querying public advisory identities.

First-party source review and binary qualification are separate from a public
package advisory query. Official CPU variants retain their actual identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, NoReturn
from urllib.parse import unquote, urldefrag, urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

PYPI_REGISTRY = "https://pypi.org/simple"
TORCH_CPU_REGISTRY = "https://download.pytorch.org/whl/cpu"
TORCH_WHEEL_HOSTS = frozenset({"download.pytorch.org", "download-r2.pytorch.org"})


class CoverageError(ValueError):
    """An input cannot be assigned a complete, verified advisory identity."""


def _fail(message: str) -> NoReturn:
    raise CoverageError(message)


@dataclass(frozen=True)
class AuditProjection:
    """The immutable query identities and their original scope accounting."""

    query_text: str
    expected: tuple[tuple[str, str], ...]
    manifest: dict[str, Any]


class _IndexEntry(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.metadata_hashes: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (href := dict(attrs).get("href")):
            self.links.append(href)
            self.metadata_hashes.append(dict(attrs).get("data-core-metadata"))


def _package_rows(lock: dict[str, Any]) -> list[dict[str, Any]]:
    rows = lock.get("package")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        _fail("coverage lock has no valid package rows")
    return rows


def _workspace_inventory(
    rows: list[dict[str, Any]], projects: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    root = projects.get(".", {})
    members = root.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    if not isinstance(members, list) or set(projects) != {".", *members}:
        _fail("coverage workspace project set is inconsistent")
    inventory = []
    for path, metadata in projects.items():
        if path != "." and (
            not re.fullmatch(r"[A-Za-z0-9_.-]+", path)
            or path.casefold() in {"..", "data", ".gitignore"}
        ):
            _fail("coverage workspace path is unsafe")
        project = metadata.get("project", {})
        name, version = project.get("name"), project.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str):
            _fail("coverage workspace identity is invalid")
        name = canonicalize_name(name)
        matches = [row for row in rows if row.get("name") == name]
        if len(matches) != 1:
            _fail("coverage workspace identity is ambiguous in lock")
        row = matches[0]
        if row.get("version") != version or row.get("source") not in (
            {"virtual": path},
            {"editable": path},
        ):
            _fail("coverage workspace source or version differs from lock")
        inventory.append(
            {"name": name, "version": version, "path": path, "coverage": "FIRST_PARTY_SOURCE"}
        )
    local_rows = [
        row
        for row in rows
        if set(row.get("source", {})) & {"editable", "virtual", "directory", "path"}
    ]
    if {row["name"] for row in local_rows} != {row["name"] for row in inventory}:
        _fail("coverage lock contains an unowned local workspace source")
    return inventory


def _torch_artifacts(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    public = policy.get("public_version")
    commit = policy.get("upstream_commit")
    if (
        policy.get("schema_version") != 1
        or policy.get("package") != "torch"
        or policy.get("registry") != TORCH_CPU_REGISTRY
        or not isinstance(public, str)
        or str(Version(public)) != public
        or Version(public).local is not None
        or not isinstance(commit, str)
        or re.fullmatch(r"[a-f0-9]{40}", commit) is None
        or policy.get("release_url") != f"https://github.com/pytorch/pytorch/releases/tag/v{public}"
        or policy.get("commit_url") != f"https://github.com/pytorch/pytorch/commit/{commit}"
    ):
        _fail("coverage Torch public release evidence is inconsistent")
    artifacts = policy.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        _fail("coverage Torch official artifact evidence is missing")
    result = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            _fail("coverage Torch artifact evidence is invalid")
        url, digest = artifact.get("url"), artifact.get("sha256")
        if (
            not isinstance(url, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            _fail("coverage Torch artifact digest is invalid")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in TORCH_WHEEL_HOSTS
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/whl/cpu/")
        ):
            _fail("coverage Torch wheel has a non-official CPU source")
        name, version, _build, tags = parse_wheel_filename(unquote(parsed.path.rsplit("/", 1)[-1]))
        if (
            name != "torch"
            or str(version) not in {public, f"{public}+cpu"}
            or artifact.get("version") != str(version)
        ):
            _fail("coverage Torch wheel version is not the reviewed CPU release")
        if not tags or any(tag.interpreter != "cp312" or tag.abi != "cp312" for tag in tags):
            _fail("coverage Torch wheel is not the selected Python ABI")
        entry = artifact.get("official_index_entry")
        if not isinstance(entry, str):
            _fail("coverage Torch captured official index entry is missing")
        parser = _IndexEntry()
        parser.feed(entry)
        if len(parser.links) != 1:
            _fail("coverage Torch official index entry is ambiguous")
        source_url, fragment = urldefrag(parser.links[0])
        if unquote(source_url) != unquote(url) or fragment != f"sha256={digest}":
            _fail("coverage Torch artifact differs from captured official index evidence")
        metadata_digest = artifact.get("metadata_sha256")
        if (
            not isinstance(metadata_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", metadata_digest) is None
            or parser.metadata_hashes != [f"sha256={metadata_digest}"]
        ):
            _fail("coverage Torch metadata differs from captured official index evidence")
        key = unquote(url)
        if key in result:
            _fail("coverage Torch artifact evidence is duplicated")
        result[key] = artifact
    return result


def verify_installed_torch(
    policy: dict[str, Any], version: str, git_commit: str, metadata_sha256: str
) -> None:
    """Cross-check installed version/commit/raw metadata with official evidence."""
    artifacts = _torch_artifacts(policy)
    if git_commit != policy["upstream_commit"] or not any(
        row["version"] == version and row.get("metadata_sha256") == metadata_sha256
        for row in artifacts.values()
    ):
        _fail("coverage installed Torch differs from reviewed release or official metadata")


def _query_version(row: dict[str, Any], policy: dict[str, Any]) -> str:
    name, version, source = row["name"], row["version"], row.get("source")
    if name == "torch":
        artifacts = _torch_artifacts(policy)
        public = policy["public_version"]
        if source != {"registry": TORCH_CPU_REGISTRY} or version not in {public, f"{public}+cpu"}:
            _fail("coverage Torch source or local version is not reviewed")
        wheels = row.get("wheels")
        if not isinstance(wheels, list) or not wheels or "sdist" in row:
            _fail("coverage Torch lock has no exact wheel evidence")
        seen = set()
        for wheel in wheels:
            expected = artifacts.get(unquote(wheel.get("url", "")))
            if (
                expected is None
                or expected["version"] != version
                or wheel.get("hash") != f"sha256:{expected['sha256']}"
            ):
                _fail("coverage Torch lock artifact differs from official evidence")
            seen.add(unquote(wheel["url"]))
        required = {url for url, item in artifacts.items() if item["version"] == version}
        if seen != required or len(seen) != len(wheels):
            _fail("coverage Torch lock artifact set differs from reviewed release")
        return public
    if Version(version).local is not None:
        _fail("coverage unreviewed local version cannot use a public advisory identity")
    if source != {"registry": PYPI_REGISTRY}:
        _fail("coverage dependency is not a verified public PyPI source")
    return version


def _pinned_requirement(line: str) -> tuple[Requirement, str]:
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        _fail("coverage invalid requirement syntax")
    specifiers = list(requirement.specifier)
    if (
        requirement.url
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        _fail("coverage requires an exact pinned package version, without URL or wildcard")
    return requirement, str(Version(specifiers[0].version))


def query_identities(requirements: str) -> tuple[tuple[str, str], ...]:
    """Read the exact public query set; never evaluate host markers here."""
    identities = []
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement, version = _pinned_requirement(line)
        if requirement.marker is not None or requirement.extras:
            _fail("coverage query input contains unresolved marker or extras")
        identity = (canonicalize_name(requirement.name), version)
        if identity in identities:
            _fail("coverage query input contains duplicate identities")
        identities.append(identity)
    if not identities:
        _fail("coverage query input is empty")
    return tuple(sorted(identities))


def prepare_projection(
    requirements: str,
    lock: dict[str, Any],
    projects: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> AuditProjection:
    """Build a conservative all-platform query set from the original uv export."""
    rows = _package_rows(lock)
    first_party = _workspace_inventory(rows, projects)
    local_by_path = {row["path"]: row for row in first_party}
    actual: set[tuple[str, str]] = set()
    queries: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e "):
            path = line[3:].strip()
            path = path[2:] if path.startswith("./") else path
            local = local_by_path.get(path)
            if local is None:
                _fail("coverage export has an unknown editable workspace path")
            records.append({**local, "original": line})
            continue
        requirement, version = _pinned_requirement(line)
        name = canonicalize_name(requirement.name)
        matches = [row for row in rows if row.get("name") == name and row.get("version") == version]
        if len(matches) != 1:
            _fail("coverage exported identity is missing or ambiguous in frozen lock")
        query_version = _query_version(matches[0], policy)
        if name in queries and queries[name] != query_version:
            _fail("coverage needs separate queries for multiple public versions")
        queries[name] = query_version
        actual.add((name, version))
        records.append(
            {
                "name": name,
                "actual_version": version,
                "query_version": query_version,
                "marker": str(requirement.marker) if requirement.marker else None,
                "source": matches[0]["source"],
                "original": line,
                "coverage": "UPSTREAM_ADVISORY_IDENTITY" if name == "torch" else "PUBLIC_ADVISORY",
            }
        )
    for local in first_party:
        metadata = projects[local["path"]]
        for build_requirement in metadata.get("build-system", {}).get("requires", []):
            requirement, version = _pinned_requirement(build_requirement)
            if (canonicalize_name(requirement.name), version) not in actual:
                _fail("coverage build backend dependency is missing from the query input")
        row = next(row for row in rows if row.get("name") == local["name"])
        for dependency in row.get("dependencies", []):
            name = dependency["name"]
            if name not in {item["name"] for item in first_party} and not any(
                identity[0] == name
                and ("version" not in dependency or identity[1] == dependency["version"])
                for identity in actual
            ):
                _fail("coverage first-party dependency is missing from the query input")
    expected = tuple(sorted(queries.items()))
    if not expected:
        _fail("coverage scope has no public advisory identities")
    return AuditProjection(
        query_text="".join(f"{name}=={version}\n" for name, version in expected),
        expected=expected,
        manifest={
            "first_party": first_party,
            "requirements": records,
            "query_identities": [{"name": name, "version": version} for name, version in expected],
            "marker_policy": "all exported branches queried; no host filtering",
            "limits": (
                "Known upstream package advisories; not binary equivalence "
                "or first-party source qualification"
            ),
        },
    )

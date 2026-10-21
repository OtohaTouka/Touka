"""Reconstruct fixed braces installed bytes and audit coverage without filesystem I/O."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

BRACES_SOURCE_PREFIX = "ToukaChat/container-security/braces-backport/"
BRACES_HELPER_PATH = BRACES_SOURCE_PREFIX + "backport.mjs"
BRACES_POLICY_PATH = BRACES_SOURCE_PREFIX + "manifest.json"
BRACES_PATCH_PATH = BRACES_SOURCE_PREFIX + "braces-3.0.3-depth-limit.patch"
BRACES_SOURCE_PATHS = (BRACES_HELPER_PATH, BRACES_POLICY_PATH, BRACES_PATCH_PATH)
BRACES_READER_PATH = "scripts/braces_backport_evidence.py"
BRACES_VERIFY_TIMEOUT = 30
LEGACY_NODE_PROFILE = "lock-only-v1"
BRACES_NODE_PROFILE = "node-lock-and-braces-backport-v1"
MAX_JSON_BYTES = 1024**2
MAX_PACKAGE_FILES = 64
MAX_PACKAGE_BYTES = 262144
MAX_LOCK_NODES = 16384
PATCH_FILES = (
    "lib/compile.js",
    "lib/constants.js",
    "lib/expand.js",
    "lib/parse.js",
    "lib/stringify.js",
)
BRACES_ADVISORY = {
    "source": 1240992,
    "name": "braces",
    "dependency": "braces",
    "title": (
        "braces vulnerable to stack-exhaustion denial of service through deeply nested patterns"
    ),
    "url": "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm",
    "severity": "high",
    "cwe": ["CWE-674"],
    "cvss": {"score": 7.5, "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"},
    "range": "<=3.0.3",
}
_LIMITS = {
    "json_bytes": MAX_JSON_BYTES,
    "file_bytes": MAX_JSON_BYTES,
    "package_files": MAX_PACKAGE_FILES,
    "package_bytes": MAX_PACKAGE_BYTES,
    "lock_nodes": MAX_LOCK_NODES,
    "installed_packages": MAX_LOCK_NODES,
    "topology_depth": 64,
    "deadline_ms": 25000,
}
_PACKAGE_PATH = re.compile(
    r"(?:node_modules/(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)(?:/node_modules/(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+)*"
)


def _check(value: object, message: str) -> None:
    if not value:
        raise ValueError(message)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _check(key not in result, "duplicate braces JSON key")
        result[key] = value
    return result


def _json(raw: bytes, *, canonical: bool = False, limit: int = MAX_JSON_BYTES) -> dict[str, Any]:
    _check(type(raw) is bytes and 0 < len(raw) <= limit, "braces JSON size/type")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        message = "braces JSON invalid"
        raise ValueError(message) from exc
    _check(type(value) is dict, "braces JSON object required")
    if canonical:
        _check(
            raw
            == (
                json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode(),
            "braces JSON is not canonical",
        )
    return value


def _keys(value: object, keys: set[str]) -> None:
    _check(type(value) is dict and set(value) == keys, "braces closed schema mismatch")


def _file_path(value: object) -> str:
    _check(type(value) is str and bool(value) and "\\" not in value, "braces file path")
    path = PurePosixPath(value)
    _check(
        str(path) == value
        and not path.is_absolute()
        and not any(part in {".", "..", ""} for part in path.parts)
        and all(32 < ord(char) < 127 for char in value),
        "braces file path",
    )
    return value


def _identity(value: object) -> None:
    _keys(value, {"bytes", "sha256"})
    _check(
        type(value["bytes"]) is int
        and 0 <= value["bytes"] <= MAX_JSON_BYTES
        and type(value["sha256"]) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["sha256"]),
        "braces file identity",
    )


def _rows(rows: object, *, maximum: int) -> dict[str, dict[str, Any]]:
    _check(type(rows) is list and 0 < len(rows) <= maximum, "braces inventory count")
    result = {}
    for row in rows:
        _check(type(row) is dict and "path" in row, "braces inventory row")
        name = _file_path(row["path"])
        _check(name not in result, "duplicate braces inventory path")
        result[name] = row
    _check(list(result) == sorted(result), "braces inventory order")
    return result


def _policy(source: Mapping[str, bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = _json(source[BRACES_POLICY_PATH], canonical=True)
    _keys(
        policy, {"schema", "package", "projects", "upstream", "patch", "depth", "files", "limits"}
    )
    _check(
        policy["schema"] == "touka.braces-backport.policy.v1"
        and policy["projects"] == {"ToukaChat": "frontend", "ToukaIDE": "toukaide"},
        "braces policy profile",
    )
    _check(
        policy["upstream"]
        == {
            "repository": "micromatch/braces",
            "pr": 72,
            "base": "e53730e6f935498326c72d768889ac194eedc0e0",
            "head": "28d440b5dd449dbf1fe6f3506cf94ecca4d02660",
        }
        and type(policy["upstream"]["pr"]) is int,
        "braces upstream identity",
    )
    _check(
        policy["depth"] == {"default": 100, "maximum": 100}
        and all(type(v) is int for v in policy["depth"].values()),
        "braces depth policy",
    )
    _check(
        policy["limits"] == _LIMITS and all(type(v) is int for v in policy["limits"].values()),
        "braces limits",
    )
    package = policy["package"]
    _keys(package, {"name", "version", "url", "bytes", "sha256", "integrity"})
    _identity({key: package[key] for key in ("bytes", "sha256")})
    _check(
        package["name"] == "braces"
        and package["version"] == "3.0.3"
        and package["url"] == "https://registry.npmjs.org/braces/-/braces-3.0.3.tgz"
        and type(package["integrity"]) is str
        and re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", package["integrity"]),
        "braces official package identity",
    )
    patch = policy["patch"]
    _keys(patch, {"path", "bytes", "sha256", "files"})
    _identity({key: patch[key] for key in ("bytes", "sha256")})
    _check(
        patch["path"] == "braces-3.0.3-depth-limit.patch"
        and patch["files"] == list(PATCH_FILES)
        and patch["bytes"] == len(source[BRACES_PATCH_PATH])
        and patch["sha256"] == _sha(source[BRACES_PATCH_PATH]),
        "braces fixed patch identity",
    )
    files = _rows(policy["files"], maximum=MAX_PACKAGE_FILES)
    _check(
        {*PATCH_FILES, "package.json", "LICENSE"} <= set(files), "braces complete policy inventory"
    )
    for name, row in files.items():
        _keys(row, {"path", "before", "after"})
        _identity(row["before"])
        _identity(row["after"])
        _check(
            (row["before"] != row["after"]) == (name in PATCH_FILES),
            "braces unselected source mutation",
        )
    _check(
        sum(row["after"]["bytes"] for row in files.values()) <= MAX_PACKAGE_BYTES,
        "braces package budget",
    )
    return policy, files


def _package_name(path: str) -> str:
    return path.rsplit("node_modules/", 1)[-1]


def _resolve(packages: Mapping[str, Any], parent: str, dependency: str) -> str | None:
    parts = list(PurePosixPath(parent).parts) if parent else []
    for count in range(len(parts), -1, -1):
        prefix = parts[:count]
        if prefix and prefix[-1] == "node_modules":
            continue
        candidate = "/".join([*prefix, "node_modules", dependency])
        if candidate in packages:
            return candidate
    return None


def _dependencies(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in ("dependencies", "optionalDependencies"):
        values = metadata.get(key, {})
        _check(
            type(values) is dict
            and all(type(k) is str and type(v) is str for k, v in values.items()),
            "braces lock dependencies",
        )
        result.update(values)
    return result


def _lock(raw: bytes, policy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    document = _json(raw)
    packages = document.get("packages")
    _check(
        type(packages) is dict and 0 < len(packages) <= MAX_LOCK_NODES and "" in packages,
        "braces lock topology",
    )
    nodes: dict[str, list[str]] = {}
    for name, value in packages.items():
        _check(
            type(name) is str
            and (name == "" or _PACKAGE_PATH.fullmatch(name))
            and type(value) is dict,
            "braces lock package path",
        )
        _dependencies(value)
        if name and (
            value.get("name") == "braces" or value.get("resolved") == policy["package"]["url"]
        ):
            _check(_package_name(name) == "braces", "braces aliased package")
        if name and _package_name(name) == "braces":
            _check(
                value.get("version") == "3.0.3"
                and value.get("resolved") == policy["package"]["url"]
                and value.get("integrity") == policy["package"]["integrity"]
                and value.get("dev") is True
                and not value.get("link", False),
                "braces lock identity/classification",
            )
            nodes[name] = []
    _check(nodes, "braces lock nodes missing")
    for parent, value in packages.items():
        for dependency in _dependencies(value):
            node = _resolve(packages, parent, dependency)
            if node in nodes:
                _check(dependency == "braces", "braces aliased dependency")
                nodes[node].append(parent)
    return packages, {name: sorted(set(parents)) for name, parents in sorted(nodes.items())}


@dataclass(frozen=True)
class BracesInstalledEvidence:
    """Content facts only; the caller supplies physical and execution authority."""

    project: str
    node_paths: tuple[str, ...]
    packages: dict[str, Any]
    proof_sha256: str
    policy_sha256: str
    patch_sha256: str


@dataclass(frozen=True)
class BracesAuditAssessment:
    """Retain scanner counts separately from verified source remediation coverage."""

    raw_blocking: int
    covered_blocking: int
    unresolved_blocking: int
    proof_sha256: str


def consume_braces_installed(
    raw: bytes, *, project: str, source_files: Mapping[str, bytes]
) -> BracesInstalledEvidence:
    """Accept only complete full-installation bytes against trusted source inputs."""
    try:
        policy, files = _policy(source_files)
        _check(project in policy["projects"], "braces project")
        package_raw = source_files[f"{project}/package.json"]
        lock_raw = source_files[f"{project}/package-lock.json"]
        _check(
            _json(package_raw).get("name") == policy["projects"][project],
            "braces project package identity",
        )
        packages, nodes = _lock(lock_raw, policy)
        proof = _json(raw, canonical=True)
        _keys(
            proof,
            {
                "schema",
                "project",
                "scope",
                "package_sha256",
                "lock_sha256",
                "policy_sha256",
                "helper_sha256",
                "patch_sha256",
                "nodes",
            },
        )
        _check(
            proof["schema"] == "touka.braces-backport.installed-bytes.v1"
            and proof["project"] == project
            and proof["scope"] == "full",
            "braces full installed proof required",
        )
        for key, body in {
            "package_sha256": package_raw,
            "lock_sha256": lock_raw,
            "policy_sha256": source_files[BRACES_POLICY_PATH],
            "helper_sha256": source_files[BRACES_HELPER_PATH],
            "patch_sha256": source_files[BRACES_PATCH_PATH],
        }.items():
            _check(proof[key] == _sha(body), "braces source identity mismatch")
        rows = _rows(proof["nodes"], maximum=MAX_LOCK_NODES)
        _check(list(rows) == list(nodes), "braces installed node coverage")
        for node, row in rows.items():
            _keys(row, {"path", "parents", "files"})
            _check(row["parents"] == nodes[node], "braces parent coverage")
            installed = _rows(row["files"], maximum=MAX_PACKAGE_FILES)
            _check(list(installed) == list(files), "braces complete installed inventory")
            for name, item in installed.items():
                _keys(item, {"path", "bytes", "sha256", "body_base64"})
                identity = {key: item[key] for key in ("bytes", "sha256")}
                _identity(identity)
                _check(
                    identity == files[name]["after"] and type(item["body_base64"]) is str,
                    "braces installed identity",
                )
                body = base64.b64decode(item["body_base64"], validate=True)
                _check(
                    base64.b64encode(body).decode() == item["body_base64"]
                    and len(body) == item["bytes"]
                    and _sha(body) == item["sha256"],
                    "braces installed body mismatch",
                )
                if name == "package.json":
                    package = _json(body)
                    _check(
                        package.get("name") == "braces" and package.get("version") == "3.0.3",
                        "braces installed version relabel",
                    )
        return BracesInstalledEvidence(
            project,
            tuple(nodes),
            packages,
            _sha(raw),
            proof["policy_sha256"],
            proof["patch_sha256"],
        )
    except (KeyError, TypeError, binascii.Error) as exc:
        message = "braces installed originals invalid"
        raise ValueError(message) from exc


def assess_braces_audit(
    raw: bytes, *, scope: str, raw_blocking: int, installed: BracesInstalledEvidence
) -> BracesAuditAssessment:
    """Keep raw counts; admit only the closed full-scope fixed-advisory graph."""
    _check(
        scope in {"full", "runtime"} and type(raw_blocking) is int and raw_blocking >= 0,
        "braces audit scope/count",
    )
    if scope == "runtime" or raw_blocking == 0:
        return BracesAuditAssessment(raw_blocking, 0, raw_blocking, installed.proof_sha256)
    document = _json(raw)
    findings = document.get("vulnerabilities")
    _check(type(findings) is dict and len(findings) <= MAX_LOCK_NODES, "braces audit findings")
    high = {
        name: row
        for name, row in findings.items()
        if type(row) is dict and row.get("severity") in {"high", "critical"}
    }
    _check(len(high) == raw_blocking and "braces" in high, "braces blocking graph coverage")
    packages = installed.packages
    reached = set(installed.node_paths)
    dependencies = {
        path: {_resolve(packages, path, name) for name in _dependencies(value)}
        for path, value in packages.items()
        if path
    }
    while True:
        expanded = reached | {path for path, targets in dependencies.items() if targets & reached}
        if expanded == reached:
            break
        reached = expanded
    expected_names = {_package_name(path) for path in reached}
    _check(set(high) == expected_names, "braces transitive finding coverage")
    edges = {
        name: {
            _package_name(target)
            for parent in reached
            if _package_name(parent) == name
            for target in dependencies[parent] & reached
        }
        for name in expected_names
    }
    root = packages[""]
    declared = {*_dependencies(root), *root.get("devDependencies", {})}
    for name, row in high.items():
        _check(
            row.get("name") == name and row.get("severity") == "high",
            "braces unrelated/critical finding",
        )
        paths = row.get("nodes")
        expected = sorted(path for path in reached if _package_name(path) == name)
        _check(
            type(paths) is list
            and all(type(item) is str for item in paths)
            and len(paths) == len(set(paths))
            and sorted(paths) == expected
            and expected,
            "braces audit node coverage",
        )
        _check(
            type(row.get("isDirect")) is bool
            and row["isDirect"] == (name in declared and _resolve(packages, "", name) in expected),
            "braces audit direct parent",
        )
        via = row.get("via")
        _check(type(via) is list and via, "braces missing audit cause")
        if name == "braces":
            _check(
                via == [BRACES_ADVISORY]
                and type(via[0]["source"]) is int
                and tuple(expected) == installed.node_paths
                and not edges[name],
                "braces advisory descriptor",
            )
        else:
            _check(
                all(type(child) is str and child in high and child != name for child in via)
                and len(via) == len(set(via)),
                "braces unknown audit cause",
            )
            _check(set(via) == edges[name], "braces audit cause coverage")
    for name, row in high.items():
        effects = row.get("effects")
        expected = sorted(parent for parent, children in edges.items() if name in children)
        _check(
            type(effects) is list
            and all(type(item) is str for item in effects)
            and len(effects) == len(set(effects))
            and sorted(effects) == expected,
            "braces audit effects mismatch",
        )
    resolved = {"braces"}
    while len(resolved) < len(edges):
        next_resolved = resolved | {
            name for name, children in edges.items() if children and children <= resolved
        }
        _check(next_resolved != resolved, "braces unresolved audit graph")
        resolved = next_resolved
    return BracesAuditAssessment(raw_blocking, len(high), 0, installed.proof_sha256)

"""Pure retained-content contracts; no npm, Node or vulnerability reproduction."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
from typing import Any

import pytest

PREFIX = "ToukaChat/container-security/braces-backport/"
PATCHED = [
    "lib/compile.js",
    "lib/constants.js",
    "lib/expand.js",
    "lib/parse.js",
    "lib/stringify.js",
]


def _json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _identity(raw: bytes) -> dict[str, Any]:
    return {"bytes": len(raw), "sha256": _sha(raw)}


@pytest.fixture
def reader() -> Any:
    spec = importlib.util.find_spec("braces_backport_evidence")
    assert spec is not None, "retained installed-byte reader is required"
    return importlib.import_module("braces_backport_evidence")


@pytest.fixture
def case() -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    archive = b"synthetic official archive identity"
    patch = b"synthetic fixed patch\n"
    package = {"name": "frontend", "version": "1.0.0", "devDependencies": {"micromatch": "4.0.8"}}
    lock = {
        "name": "frontend",
        "lockfileVersion": 3,
        "packages": {
            "": package,
            "node_modules/micromatch": {
                "version": "4.0.8",
                "dev": True,
                "dependencies": {"braces": "^3.0.3"},
            },
            "node_modules/braces": {
                "version": "3.0.3",
                "dev": True,
                "resolved": "https://registry.npmjs.org/braces/-/braces-3.0.3.tgz",
                "integrity": "sha512-"
                + base64.b64encode(hashlib.sha512(archive).digest()).decode(),
            },
        },
    }
    contents = dict.fromkeys(PATCHED, b"fixed normal source\n")
    contents["LICENSE"] = b"MIT\n"
    contents["package.json"] = _json({"name": "braces", "version": "3.0.3"})
    policy = {
        "schema": "touka.braces-backport.policy.v1",
        "package": {
            "name": "braces",
            "version": "3.0.3",
            "url": lock["packages"]["node_modules/braces"]["resolved"],
            **_identity(archive),
            "integrity": lock["packages"]["node_modules/braces"]["integrity"],
        },
        "projects": {"ToukaChat": "frontend", "ToukaIDE": "toukaide"},
        "upstream": {
            "repository": "micromatch/braces",
            "pr": 72,
            "base": "e53730e6f935498326c72d768889ac194eedc0e0",
            "head": "28d440b5dd449dbf1fe6f3506cf94ecca4d02660",
        },
        "patch": {"path": "braces-3.0.3-depth-limit.patch", **_identity(patch), "files": PATCHED},
        "depth": {"default": 100, "maximum": 100},
        "files": [
            {
                "path": name,
                "before": _identity(b"original\n" if name in PATCHED else raw),
                "after": _identity(raw),
            }
            for name, raw in sorted(contents.items())
        ],
        "limits": {
            "json_bytes": 1048576,
            "file_bytes": 1048576,
            "package_files": 64,
            "package_bytes": 262144,
            "lock_nodes": 16384,
            "installed_packages": 16384,
            "topology_depth": 64,
            "deadline_ms": 25000,
        },
    }
    sources = {
        PREFIX + "manifest.json": _json(policy),
        PREFIX + "backport.mjs": b"trusted inert helper source\n",
        PREFIX + "braces-3.0.3-depth-limit.patch": patch,
        "ToukaChat/package.json": _json(package),
        "ToukaChat/package-lock.json": _json(lock),
    }
    proof = {
        "schema": "touka.braces-backport.installed-bytes.v1",
        "project": "ToukaChat",
        "scope": "full",
        "package_sha256": _sha(sources["ToukaChat/package.json"]),
        "lock_sha256": _sha(sources["ToukaChat/package-lock.json"]),
        "policy_sha256": _sha(sources[PREFIX + "manifest.json"]),
        "helper_sha256": _sha(sources[PREFIX + "backport.mjs"]),
        "patch_sha256": _sha(patch),
        "nodes": [
            {
                "path": "node_modules/braces",
                "parents": ["node_modules/micromatch"],
                "files": [
                    {"path": name, **_identity(raw), "body_base64": base64.b64encode(raw).decode()}
                    for name, raw in sorted(contents.items())
                ],
            }
        ],
    }
    return sources, proof, lock


def test_complete_installed_bytes_are_independently_reconstructed(reader: Any, case: tuple) -> None:
    sources, proof, _ = case
    result = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    assert result.proof_sha256 == _sha(_json(proof))
    assert result.policy_sha256 == _sha(sources[PREFIX + "manifest.json"])
    assert result.node_paths == ("node_modules/braces",)


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "project",
        "helper",
        "body",
        "missing",
        "extra",
        "parent",
        "node",
        "digest",
        "boolean",
        "duplicate",
        "noncanonical",
    ],
)
def test_installed_proof_cannot_substitute_copied_claims(
    reader: Any, case: tuple, fault: str
) -> None:
    sources, proof, _ = case
    if fault == "scope":
        proof["scope"] = "runtime"
        proof["nodes"][0]["files"] = []
    elif fault == "project":
        proof["project"] = "ToukaIDE"
    elif fault == "helper":
        proof["helper_sha256"] = _sha(b"other")
    elif fault == "body":
        proof["nodes"][0]["files"][0]["body_base64"] = base64.b64encode(b"changed").decode()
    elif fault == "missing":
        proof["nodes"][0]["files"].pop()
    elif fault == "extra":
        proof["status"] = "PASS"
    elif fault == "parent":
        proof["nodes"][0]["parents"] = []
    elif fault == "node":
        proof["nodes"].append(copy.deepcopy(proof["nodes"][0]))
    elif fault == "digest":
        proof["policy_sha256"] = proof["policy_sha256"].removeprefix("sha256:")
    elif fault == "boolean":
        proof["nodes"][0]["files"][0]["bytes"] = True
    raw = _json(proof)
    if fault == "duplicate":
        raw = raw.replace(b'{"helper_sha256":', b'{"scope":"full","helper_sha256":', 1)
    elif fault == "noncanonical":
        raw = json.dumps(proof).encode()
    with pytest.raises(ValueError):
        reader.consume_braces_installed(raw, project="ToukaChat", source_files=sources)


def _audit(reader: Any) -> dict[str, Any]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "braces": {
                "name": "braces",
                "severity": "high",
                "isDirect": False,
                "via": [copy.deepcopy(reader.BRACES_ADVISORY)],
                "effects": ["micromatch"],
                "range": "*",
                "nodes": ["node_modules/braces"],
                "fixAvailable": False,
            },
            "micromatch": {
                "name": "micromatch",
                "severity": "high",
                "isDirect": True,
                "via": ["braces"],
                "effects": [],
                "range": "*",
                "nodes": ["node_modules/micromatch"],
                "fixAvailable": False,
            },
        },
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": 2,
                "critical": 0,
                "total": 2,
            }
        },
    }


def test_exact_advisory_graph_keeps_raw_high_and_separate_coverage(
    reader: Any, case: tuple
) -> None:
    sources, proof, _ = case
    installed = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    raw = _json(_audit(reader))
    result = reader.assess_braces_audit(raw, scope="full", raw_blocking=2, installed=installed)
    assert (result.raw_blocking, result.covered_blocking, result.unresolved_blocking) == (2, 2, 0)
    assert raw == _json(_audit(reader))
    runtime = reader.assess_braces_audit(raw, scope="runtime", raw_blocking=2, installed=installed)
    assert (runtime.covered_blocking, runtime.unresolved_blocking) == (0, 2)


@pytest.mark.parametrize(
    "fault",
    [
        "advisory",
        "critical",
        "extra-advisory",
        "missing-via",
        "effects",
        "node",
        "unresolved-edge",
        "direct",
    ],
)
def test_unproven_findings_remain_blocked(reader: Any, case: tuple, fault: str) -> None:
    sources, proof, lock = case
    doc = _audit(reader)
    if fault == "advisory":
        doc["vulnerabilities"]["braces"]["via"][0]["source"] += 1
    elif fault == "critical":
        doc["vulnerabilities"]["braces"]["severity"] = "critical"
    elif fault == "extra-advisory":
        doc["vulnerabilities"]["micromatch"]["via"].append(
            {"url": "https://github.com/advisories/other"}
        )
    elif fault == "missing-via":
        doc["vulnerabilities"]["micromatch"]["via"] = []
    elif fault == "effects":
        doc["vulnerabilities"]["braces"]["effects"] = []
    elif fault == "node":
        doc["vulnerabilities"]["braces"]["nodes"].append("node_modules/unknown")
    elif fault == "unresolved-edge":
        lock["packages"]["node_modules/micromatch"]["dependencies"] = {}
        sources["ToukaChat/package-lock.json"] = _json(lock)
        proof["lock_sha256"] = _sha(sources["ToukaChat/package-lock.json"])
        proof["nodes"][0]["parents"] = []
    elif fault == "direct":
        doc["vulnerabilities"]["micromatch"]["isDirect"] = False
    installed = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    with pytest.raises(ValueError):
        reader.assess_braces_audit(_json(doc), scope="full", raw_blocking=2, installed=installed)


def test_omitted_dependent_finding_cannot_shrink_the_closed_lock_graph(
    reader: Any, case: tuple
) -> None:
    sources, proof, lock = case
    lock["packages"]["node_modules/tool"] = {
        "version": "1.0.0",
        "dev": True,
        "dependencies": {"micromatch": "^4.0.8"},
    }
    sources["ToukaChat/package-lock.json"] = _json(lock)
    proof["lock_sha256"] = _sha(sources["ToukaChat/package-lock.json"])
    installed = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    with pytest.raises(ValueError, match="coverage"):
        reader.assess_braces_audit(
            _json(_audit(reader)), scope="full", raw_blocking=2, installed=installed
        )


def test_alias_package_cannot_hide_an_uncovered_braces_node(reader: Any, case: tuple) -> None:
    sources, proof, lock = case
    lock["packages"]["node_modules/alias"] = copy.deepcopy(lock["packages"]["node_modules/braces"])
    lock["packages"]["node_modules/alias"]["name"] = "braces"
    sources["ToukaChat/package-lock.json"] = _json(lock)
    proof["lock_sha256"] = _sha(sources["ToukaChat/package-lock.json"])
    with pytest.raises(ValueError, match="alias"):
        reader.consume_braces_installed(_json(proof), project="ToukaChat", source_files=sources)


def test_policy_pr_identity_rejects_float_substitution(reader: Any, case: tuple) -> None:
    sources, proof, _ = case
    name = reader.BRACES_POLICY_PATH
    policy = json.loads(sources[name])
    policy["upstream"]["pr"] = 72.0
    sources[name] = _json(policy)
    proof["policy_sha256"] = _sha(sources[name])
    with pytest.raises(ValueError, match="upstream"):
        reader.consume_braces_installed(_json(proof), project="ToukaChat", source_files=sources)


def test_advisory_identity_rejects_float_substitution(reader: Any, case: tuple) -> None:
    sources, proof, _ = case
    installed = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    document = _audit(reader)
    document["vulnerabilities"]["braces"]["via"][0]["source"] = float(
        reader.BRACES_ADVISORY["source"]
    )
    with pytest.raises(ValueError, match="advisory"):
        reader.assess_braces_audit(
            _json(document), scope="full", raw_blocking=2, installed=installed
        )


def _graph_row(
    name: str, nodes: list[str], via: list[Any], effects: list[str], *, direct: bool = False
) -> dict[str, Any]:
    return {
        "name": name,
        "severity": "high",
        "nodes": nodes,
        "via": via,
        "effects": effects,
        "isDirect": direct,
    }


def _graph_inputs(
    case: tuple,
    additions: dict[str, dict[str, Any]],
    roots: list[str],
    braces_parents: list[str],
) -> tuple[dict[str, bytes], dict[str, Any]]:
    sources, proof, lock = case
    lock["packages"].update(additions)
    lock["packages"][""]["devDependencies"].update(dict.fromkeys(roots, "1.0.0"))
    sources["ToukaChat/package.json"] = _json(lock["packages"][""])
    sources["ToukaChat/package-lock.json"] = _json(lock)
    proof["package_sha256"] = _sha(sources["ToukaChat/package.json"])
    proof["lock_sha256"] = _sha(sources["ToukaChat/package-lock.json"])
    proof["nodes"][0]["parents"] = sorted(braces_parents)
    return sources, proof


def _graph_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {row["name"]: row for row in rows},
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": len(rows),
                "critical": 0,
                "total": len(rows),
            }
        },
    }


def _assess_graph(reader: Any, sources: dict, proof: dict, document: dict) -> Any:
    installed = reader.consume_braces_installed(
        _json(proof), project="ToukaChat", source_files=sources
    )
    raw = _json(document)
    assessment = reader.assess_braces_audit(
        raw, scope="full", raw_blocking=len(document["vulnerabilities"]), installed=installed
    )
    assert raw == _json(document)
    assert (
        assessment.raw_blocking == assessment.covered_blocking == len(document["vulnerabilities"])
    )
    assert assessment.unresolved_blocking == 0
    return assessment


def _same_name_graph(reader: Any, case: tuple, topology: str) -> tuple[dict, dict, dict]:
    root = "node_modules/micromatch"
    nested = "node_modules/tool/node_modules/micromatch"
    root_affected = topology != "nested-only"
    nested_affected = topology != "root-only"
    additions = {
        root: {
            "version": "4.0.8",
            "dev": True,
            "dependencies": {"braces": "3.0.3"} if root_affected else {},
        },
        nested: {
            "version": "4.0.8",
            "dev": True,
            "dependencies": {"braces": "3.0.3"} if nested_affected else {},
        },
        "node_modules/tool": {
            "version": "1.0.0",
            "dev": True,
            "dependencies": {"micromatch": "4.0.8"},
        },
    }
    nodes = [
        path for path, affected in ((root, root_affected), (nested, nested_affected)) if affected
    ]
    sources, proof = _graph_inputs(case, additions, ["tool"], nodes)
    rows = [
        _graph_row(
            "braces",
            ["node_modules/braces"],
            [copy.deepcopy(reader.BRACES_ADVISORY)],
            ["micromatch"],
        ),
        _graph_row(
            "micromatch",
            nodes,
            ["braces"],
            ["tool"] if nested_affected else [],
            direct=root_affected,
        ),
    ]
    if nested_affected:
        rows.append(_graph_row("tool", ["node_modules/tool"], ["micromatch"], [], direct=True))
    return sources, proof, _graph_report(rows)


@pytest.mark.parametrize("topology", ["root-only", "nested-only", "both"])
def test_affected_same_name_nodes_and_root_edge_are_exact(
    reader: Any, case: tuple, topology: str
) -> None:
    _assess_graph(reader, *_same_name_graph(reader, case, topology))


@pytest.mark.parametrize("fault", ["missing-root", "missing-nested", "insert-unaffected"])
def test_affected_same_name_nodes_cannot_be_omitted_or_expanded(
    reader: Any, case: tuple, fault: str
) -> None:
    sources, proof, document = _same_name_graph(
        reader, case, "root-only" if fault == "insert-unaffected" else "both"
    )
    nodes = document["vulnerabilities"]["micromatch"]["nodes"]
    if fault == "insert-unaffected":
        nodes.append("node_modules/tool/node_modules/micromatch")
    else:
        nodes.remove(
            "node_modules/micromatch"
            if fault == "missing-root"
            else "node_modules/tool/node_modules/micromatch"
        )
    with pytest.raises(ValueError, match="braces audit node coverage"):
        _assess_graph(reader, sources, proof, document)


@pytest.mark.parametrize("topology", ["root-only", "nested-only"])
def test_direct_flag_follows_the_affected_root_edge(
    reader: Any, case: tuple, topology: str
) -> None:
    sources, proof, document = _same_name_graph(reader, case, topology)
    finding = document["vulnerabilities"]["micromatch"]
    finding["isDirect"] = not finding["isDirect"]
    with pytest.raises(ValueError, match="braces audit direct parent"):
        _assess_graph(reader, sources, proof, document)


def test_different_affected_copies_preserve_the_complete_resolved_edge_union(
    reader: Any, case: tuple
) -> None:
    sources, proof = _graph_inputs(
        case,
        {
            "node_modules/tool": {
                "version": "1.0.0",
                "dev": True,
                "dependencies": {"micromatch": "4.0.8"},
            },
            "node_modules/host": {
                "version": "1.0.0",
                "dev": True,
                "dependencies": {"tool": "1.0.0"},
            },
            "node_modules/host/node_modules/tool": {
                "version": "1.0.0",
                "dev": True,
                "dependencies": {"braces": "3.0.3"},
            },
        },
        ["tool", "host"],
        ["node_modules/micromatch", "node_modules/host/node_modules/tool"],
    )
    document = _graph_report(
        [
            _graph_row(
                "braces",
                ["node_modules/braces"],
                [copy.deepcopy(reader.BRACES_ADVISORY)],
                ["micromatch", "tool"],
            ),
            _graph_row(
                "micromatch", ["node_modules/micromatch"], ["braces"], ["tool"], direct=True
            ),
            _graph_row(
                "tool",
                ["node_modules/tool", "node_modules/host/node_modules/tool"],
                ["braces", "micromatch"],
                ["host"],
                direct=True,
            ),
            _graph_row("host", ["node_modules/host"], ["tool"], [], direct=True),
        ]
    )
    _assess_graph(reader, sources, proof, document)


@pytest.mark.parametrize("fault", ["complete", "omit-edge-and-effect", "insert-unaffected-edge"])
def test_causal_edges_and_reverse_effects_match_the_trusted_graph(
    reader: Any, case: tuple, fault: str
) -> None:
    additions = {
        "node_modules/tool": {
            "version": "1.0.0",
            "dev": True,
            "dependencies": {"micromatch": "4.0.8", "other": "1.0.0"},
        },
        "node_modules/other": {
            "version": "1.0.0",
            "dev": True,
            "dependencies": {"micromatch": "4.0.8"},
        },
    }
    rows = [
        _graph_row(
            "braces",
            ["node_modules/braces"],
            [copy.deepcopy(reader.BRACES_ADVISORY)],
            ["micromatch"],
        ),
        _graph_row(
            "micromatch", ["node_modules/micromatch"], ["braces"], ["other", "tool"], direct=True
        ),
        _graph_row("tool", ["node_modules/tool"], ["micromatch", "other"], [], direct=True),
        _graph_row("other", ["node_modules/other"], ["micromatch"], ["tool"]),
    ]
    roots = ["tool"]
    if fault == "omit-edge-and-effect":
        rows[2]["via"].remove("other")
        rows[3]["effects"].remove("tool")
    elif fault == "insert-unaffected-edge":
        additions["node_modules/other"]["dependencies"] = {}
        additions["node_modules/host"] = {
            "version": "1.0.0",
            "dev": True,
            "dependencies": {"other": "1.0.0"},
        }
        additions["node_modules/host/node_modules/other"] = {
            "version": "1.0.0",
            "dev": True,
            "dependencies": {"micromatch": "4.0.8"},
        }
        roots.append("host")
        rows[3]["nodes"] = ["node_modules/host/node_modules/other"]
        rows[3]["effects"].append("host")
        rows.append(_graph_row("host", ["node_modules/host"], ["other"], [], direct=True))
    sources, proof = _graph_inputs(case, additions, roots, ["node_modules/micromatch"])
    document = _graph_report(rows)
    if fault == "complete":
        _assess_graph(reader, sources, proof, document)
    else:
        with pytest.raises(ValueError, match="braces audit cause coverage"):
            _assess_graph(reader, sources, proof, document)

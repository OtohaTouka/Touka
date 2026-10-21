"""Coverage accounting across workspace sources and public advisory identities."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import dependency_audit_coverage as coverage  # noqa: E402


@pytest.fixture
def inventory() -> tuple[dict, dict, dict, str]:
    wheel_url = (
        "https://download-r2.pytorch.org/whl/cpu/torch-2.13.0%2Bcpu-cp312-cp312-win_amd64.whl"
    )
    policy = {
        "schema_version": 1,
        "package": "torch",
        "public_version": "2.13.0",
        "registry": "https://download.pytorch.org/whl/cpu",
        "upstream_commit": "b" * 40,
        "release_url": "https://github.com/pytorch/pytorch/releases/tag/v2.13.0",
        "commit_url": "https://github.com/pytorch/pytorch/commit/" + "b" * 40,
        "artifacts": [
            {
                "url": wheel_url,
                "sha256": "a" * 64,
                "version": "2.13.0+cpu",
                "metadata_sha256": "c" * 64,
                "official_index_entry": '<a href="'
                + wheel_url
                + "#sha256="
                + "a" * 64
                + '" data-core-metadata="sha256='
                + "c" * 64
                + '">wheel</a>',
            }
        ],
    }
    projects = {
        ".": {
            "project": {"name": "example-root", "version": "0.1.0"},
            "tool": {"uv": {"workspace": {"members": ["Member"]}}},
        },
        "Member": {
            "project": {"name": "example-member", "version": "0.1.0"},
            "build-system": {"requires": ["setuptools==83.0.0"]},
        },
    }
    lock = {
        "package": [
            {"name": "example-root", "version": "0.1.0", "source": {"virtual": "."}},
            {
                "name": "example-member",
                "version": "0.1.0",
                "source": {"editable": "Member"},
                "dependencies": [{"name": "crypto"}],
            },
            {
                "name": "crypto",
                "version": "5.0.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "setuptools",
                "version": "83.0.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "torch",
                "version": "2.13.0+cpu",
                "source": {"registry": "https://download.pytorch.org/whl/cpu"},
                "wheels": [{"url": wheel_url, "hash": "sha256:" + "a" * 64}],
            },
        ]
    }
    original = (
        "-e ./Member\ncrypto==5.0.0 ; sys_platform == 'linux'\n"
        "setuptools==83.0.0\ntorch==2.13.0+cpu\n"
    )
    return lock, projects, policy, original


def test_workspace_sources_and_all_marker_branches_remain_accounted(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    projection = coverage.prepare_projection(original, lock, projects, policy)

    assert projection.query_text == "crypto==5.0.0\nsetuptools==83.0.0\ntorch==2.13.0\n"
    assert projection.expected == (
        ("crypto", "5.0.0"),
        ("setuptools", "83.0.0"),
        ("torch", "2.13.0"),
    )
    assert {row["name"] for row in projection.manifest["first_party"]} == {
        "example-root",
        "example-member",
    }
    records = projection.manifest["requirements"]
    assert any(row.get("marker") == 'sys_platform == "linux"' for row in records)
    assert any(
        row.get("actual_version") == "2.13.0+cpu" and row["query_version"] == "2.13.0"
        for row in records
    )
    assert projection.manifest["first_party"][0]["coverage"] == "FIRST_PARTY_SOURCE"


@pytest.mark.parametrize(
    "local", ["-e ./Other", "-e ../Member", "thing @ https://example.invalid/a.whl", "-r other.txt"]
)
def test_unowned_local_and_url_requirements_are_not_silently_exempted(
    inventory: tuple, local: str
) -> None:
    lock, projects, policy, original = inventory
    with pytest.raises(coverage.CoverageError):
        coverage.prepare_projection(original + local + "\n", lock, projects, policy)


@pytest.mark.parametrize("field,value", [("name", "foreign"), ("version", "0.2.0")])
def test_workspace_identity_must_match_the_lock(inventory: tuple, field: str, value: str) -> None:
    lock, projects, policy, original = inventory
    projects["Member"]["project"][field] = value
    with pytest.raises(coverage.CoverageError, match="workspace"):
        coverage.prepare_projection(original, lock, projects, policy)


def test_build_backend_dependencies_cannot_disappear(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    with pytest.raises(coverage.CoverageError, match="build"):
        coverage.prepare_projection(
            original.replace("setuptools==83.0.0\n", ""), lock, projects, policy
        )


def test_first_party_transitive_dependency_cannot_disappear(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    with pytest.raises(coverage.CoverageError, match="dependency"):
        coverage.prepare_projection(
            original.replace("crypto==5.0.0 ; sys_platform == 'linux'\n", ""),
            lock,
            projects,
            policy,
        )


@pytest.mark.parametrize(
    "mutation", ["suffix", "registry", "wheel_hash", "wheel_host", "evidence_hash", "commit"]
)
def test_torch_mapping_rejects_variant_and_source_drift(inventory: tuple, mutation: str) -> None:
    lock, projects, policy, original = inventory
    torch = lock["package"][-1]
    if mutation == "suffix":
        torch["version"] = "2.13.0+custom"
        original = original.replace("+cpu", "+custom")
    elif mutation == "registry":
        torch["source"]["registry"] = "https://example.invalid/cpu"
    elif mutation == "wheel_hash":
        torch["wheels"][0]["hash"] = "sha256:" + "d" * 64
    elif mutation == "wheel_host":
        torch["wheels"][0]["url"] = torch["wheels"][0]["url"].replace(
            "download-r2.pytorch.org", "example.invalid"
        )
    elif mutation == "evidence_hash":
        policy["artifacts"][0]["official_index_entry"] = policy["artifacts"][0][
            "official_index_entry"
        ].replace("a" * 64, "d" * 64)
    else:
        policy["upstream_commit"] = "d" * 40
    with pytest.raises(coverage.CoverageError):
        coverage.prepare_projection(original, lock, projects, policy)


def test_unrelated_local_version_cannot_borrow_torch_mapping(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    lock["package"][2]["version"] = "5.0.0+cpu"
    with pytest.raises(coverage.CoverageError, match="local"):
        coverage.prepare_projection(
            original.replace("crypto==5.0.0", "crypto==5.0.0+cpu"), lock, projects, policy
        )


def test_torch_public_and_cpu_variants_share_one_explicit_query(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    row = copy.deepcopy(lock["package"][-1])
    row["version"] = "2.13.0"
    artifact = copy.deepcopy(policy["artifacts"][0])
    artifact["version"] = "2.13.0"
    artifact["url"] = (
        artifact["url"].replace("%2Bcpu", "").replace("win_amd64", "macosx_14_0_arm64")
    )
    artifact["official_index_entry"] = (
        '<a href="'
        + artifact["url"]
        + "#sha256="
        + artifact["sha256"]
        + '" data-core-metadata="sha256='
        + artifact["metadata_sha256"]
        + '">wheel</a>'
    )
    row["wheels"] = [{"url": artifact["url"], "hash": "sha256:" + artifact["sha256"]}]
    lock["package"].append(row)
    policy["artifacts"].append(artifact)
    projection = coverage.prepare_projection(
        original + "torch==2.13.0 ; sys_platform == 'darwin'\n", lock, projects, policy
    )
    assert projection.query_text.count("torch==") == 1
    assert (
        len([record for record in projection.manifest["requirements"] if record["name"] == "torch"])
        == 2
    )


@pytest.mark.parametrize("git_commit,metadata_hash", [("d" * 40, "c" * 64), ("b" * 40, "d" * 64)])
def test_installed_torch_requires_matching_release_and_metadata(
    inventory: tuple, git_commit: str, metadata_hash: str
) -> None:
    _lock, _projects, policy, _original = inventory
    with pytest.raises(coverage.CoverageError, match="installed"):
        coverage.verify_installed_torch(policy, "2.13.0+cpu", git_commit, metadata_hash)


def test_installed_torch_accepts_the_captured_official_metadata(inventory: tuple) -> None:
    _lock, _projects, policy, _original = inventory
    coverage.verify_installed_torch(policy, "2.13.0+cpu", "b" * 40, "c" * 64)


def test_metadata_digest_must_match_the_official_index_not_only_policy(inventory: tuple) -> None:
    lock, projects, policy, original = inventory
    policy["artifacts"][0]["official_index_entry"] = policy["artifacts"][0][
        "official_index_entry"
    ].replace("c" * 64, "d" * 64)
    with pytest.raises(coverage.CoverageError, match="metadata"):
        coverage.prepare_projection(original, lock, projects, policy)


def test_invalid_requirement_error_does_not_echo_credentials() -> None:
    credential_marker = "synthetic-audit-password"
    with pytest.raises(coverage.CoverageError) as error:
        coverage.query_identities(
            f"--index-url https://user:{credential_marker}@example.invalid/simple"
        )
    assert credential_marker not in str(error.value)
    assert "example.invalid" not in str(error.value)


@pytest.mark.parametrize("requirements", ["", " \n\t", "# exported requirements\n# comment\n"])
def test_empty_query_is_not_valid_coverage(requirements: str) -> None:
    with pytest.raises(coverage.CoverageError, match="empty"):
        coverage.query_identities(requirements)

"""P11 captured-advisory version floors; these checks do not replace an audit."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_FLOORS = (
    ("aiohttp", "3.14.3"),
    ("cryptography", "50.0.0"),
    ("h2", "4.4.1"),
    ("setuptools", "83.0.0"),
    ("mkdocs-material", "9.7.7"),
    ("pip", "26.2"),
    ("pymdown-extensions", "11.0.1"),
    ("sentence-transformers", "6.0.0"),
    ("pyjwt", "2.15.1"),
    ("urllib3", "2.8.0"),
    ("opentelemetry-api", "1.45.1"),
)


def _assert_safe_lock_rows(lock: dict[str, Any], package: str, minimum: str) -> None:
    rows = [
        row
        for row in lock.get("package", [])
        if canonicalize_name(row["name"]) == canonicalize_name(package)
    ]
    assert rows, f"{package}: missing from lock"
    for row in rows:
        version = Version(row["version"])
        assert not version.is_prerelease, f"{package}: prerelease {version} is not qualified"
        assert version >= Version(minimum), f"{package}: {version} is below {minimum}"


def _assert_declaration(requirements: list[str], expected: str) -> None:
    target = Requirement(expected)
    matches = [
        requirement
        for value in requirements
        if canonicalize_name((requirement := Requirement(value)).name)
        == canonicalize_name(target.name)
    ]
    assert matches, f"{target.name}: missing declaration"
    assert all(requirement == target for requirement in matches), matches


@pytest.mark.parametrize(("package", "minimum"), SECURITY_FLOORS)
def test_every_locked_variant_meets_security_floor(package: str, minimum: str) -> None:
    """A safe platform variant must not hide an older row for the same package."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    _assert_safe_lock_rows(lock, package, minimum)


def test_crypto_test_declaration_requires_fixed_series() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    _assert_declaration(document["dependency-groups"]["dev"], "cryptography~=50.0.0")


@pytest.mark.parametrize(
    "expected", ["mkdocs-material~=9.7.7", "pip~=26.2.0", "pymdown-extensions~=11.0.1"]
)
def test_build_tool_declarations_require_fixed_series(expected: str) -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    _assert_declaration(document["dependency-groups"]["dev"], expected)


def _synthetic_lock(versions: tuple[str, ...]) -> dict[str, Any]:
    document = '[[package]]\nname = "unrelated"\nversion = "1.0.0"\n'
    for version in versions:
        document += f'\n[[package]]\nname = "aiohttp"\nversion = "{version}"\n'
    return tomllib.loads(document)


@pytest.mark.parametrize(
    ("versions", "reason"),
    (
        (("3.14.3", "3.14.1"), "below"),
        ((), "missing"),
        (("3.15.0rc1",), "prerelease"),
    ),
    ids=("fixed-variant-before-older-variant", "missing-package", "future-prerelease"),
)
def test_unsafe_synthetic_lock_is_rejected(versions: tuple[str, ...], reason: str) -> None:
    """Reject incomplete coverage and prereleases even above the numeric floor."""
    with pytest.raises(AssertionError, match=reason):
        _assert_safe_lock_rows(_synthetic_lock(versions), "aiohttp", "3.14.3")


def test_stable_fixed_and_newer_variants_are_allowed() -> None:
    _assert_safe_lock_rows(_synthetic_lock(("3.14.3", "3.15.0")), "aiohttp", "3.14.3")


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("ToukaProxy/pyproject.toml", "sentence-transformers~=6.0.0"),
        ("ToukaMemory/pyproject.toml", "sentence-transformers~=6.0.0"),
        ("ToukaProxy/pyproject.toml", "PyJWT~=2.15.1"),
    ],
)
def test_security_runtime_dependencies_are_direct_and_require_fixed_series(
    manifest: str, expected: str
) -> None:
    document = tomllib.loads((REPO_ROOT / manifest).read_text(encoding="utf-8"))
    _assert_declaration(document["project"]["dependencies"], expected)


def test_urllib3_fixed_version_is_a_workspace_constraint() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    _assert_declaration(document["tool"]["uv"]["constraint-dependencies"], "urllib3==2.8.0")

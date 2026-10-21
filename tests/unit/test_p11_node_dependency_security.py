"""Captured npm advisory floors across all lock entries, not a live audit."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECURITY_FLOORS = {
    "fast-uri": "3.1.7",
    "nanoid": "3.3.18",
    "browserslist": "4.28.7",
    "ip-address": "10.3.1",
    "js-yaml": "4.3.1",
    "undici": "7.29.1",
    "hono": "4.12.34",
    "qs": "6.16.0",
    "@humanfs/node": "0.16.8",
    "postcss-selector-parser": "7.1.3",
    "smol-toml": "1.7.1",
    "engine.io": "6.6.10",
    "next": "16.3.8",
}
PROJECT_PACKAGES = {
    "ToukaChat": tuple(SECURITY_FLOORS),
    "ToukaIDE": (
        "fast-uri",
        "nanoid",
        "browserslist",
        "ip-address",
        "js-yaml",
        "undici",
        "hono",
        "qs",
    ),
}


def _stable_version(value: object, path: str) -> tuple[int, ...]:
    assert isinstance(value, str) and re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value
    ), f"{path}: malformed or unqualified version {value!r}"
    return tuple(int(part) for part in value.split("."))


def _assert_safe_entries(
    lock: dict[str, Any], name: str, minimum: str, *, allow_absent: bool = False
) -> None:
    packages = lock.get("packages")
    assert isinstance(packages, dict), "missing lock packages"
    rows = [
        (path, metadata)
        for path, metadata in packages.items()
        if path.startswith("node_modules/") and path.rsplit("node_modules/", 1)[-1] == name
    ]
    assert rows or allow_absent, f"{name}: missing from lock"
    floor = _stable_version(minimum, name)
    for path, metadata in rows:
        assert isinstance(metadata, dict), f"{path}: malformed package metadata"
        version = _stable_version(metadata.get("version"), path)
        # A different major (or a 0.x minor) needs its own advisory qualification.
        branch_length = 2 if floor[0] == 0 else 1
        assert version[:branch_length] == floor[:branch_length], (
            f"{path}: {version} is outside the reviewed release branch"
        )
        assert version >= floor, f"{path}: {metadata['version']} is below {minimum}"


@pytest.mark.parametrize(
    ("project", "name"),
    [(project, name) for project, names in PROJECT_PACKAGES.items() for name in names],
)
def test_every_locked_occurrence_meets_captured_security_floor(project: str, name: str) -> None:
    lock = json.loads((ROOT / project / "package-lock.json").read_text(encoding="utf-8"))
    _assert_safe_entries(
        lock,
        name,
        SECURITY_FLOORS[name],
        allow_absent=(project, name) == ("ToukaIDE", "ip-address"),
    )


@pytest.mark.parametrize("depth", (1, 2, 3))
def test_safe_top_level_does_not_hide_vulnerable_nested_entry(depth: int) -> None:
    nested = "node_modules/parent/" * depth + "node_modules/fast-uri"
    lock = {
        "packages": {
            "node_modules/fast-uri": {"version": "3.1.6"},
            nested: {"version": "3.1.5"},
        }
    }
    with pytest.raises(AssertionError, match=re.escape(nested) + ": 3.1.5 is below"):
        _assert_safe_entries(lock, "fast-uri", "3.1.6")


def test_scoped_nested_package_cannot_hide_behind_safe_root() -> None:
    lock = {
        "packages": {
            "node_modules/@humanfs/node": {"version": "0.16.8"},
            "node_modules/eslint/node_modules/@humanfs/node": {"version": "0.16.7"},
        }
    }
    with pytest.raises(AssertionError, match="eslint/node_modules/@humanfs/node: 0.16.7 is below"):
        _assert_safe_entries(lock, "@humanfs/node", "0.16.8")


def test_missing_package_cannot_pass_vacuously() -> None:
    with pytest.raises(AssertionError, match="fast-uri: missing from lock"):
        _assert_safe_entries(
            {"packages": {"node_modules/other": {"version": "3.1.6"}}}, "fast-uri", "3.1.6"
        )


def test_retired_ide_ip_address_may_be_absent() -> None:
    _assert_safe_entries({"packages": {}}, "ip-address", "10.3.1", allow_absent=True)


@pytest.mark.parametrize("depth", (1, 2))
def test_retired_package_reintroduced_below_floor_still_fails(depth: int) -> None:
    path = "node_modules/parent/" * depth + "node_modules/ip-address"
    lock = {"packages": {path: {"version": "10.3.0"}}}
    with pytest.raises(AssertionError, match="below 10.3.1"):
        _assert_safe_entries(lock, "ip-address", "10.3.1", allow_absent=True)


@pytest.mark.parametrize("version", (None, 3, "3.1", "03.1.6", "3.1.6-rc.1", "3.1.6+build"))
def test_malformed_or_unqualified_nested_version_is_rejected(version: object) -> None:
    lock = {"packages": {"node_modules/parent/node_modules/fast-uri": {"version": version}}}
    with pytest.raises(AssertionError, match="malformed or unqualified version"):
        _assert_safe_entries(lock, "fast-uri", "3.1.6")


def test_unreviewed_release_branch_is_not_assumed_safe() -> None:
    lock = {"packages": {"node_modules/@humanfs/node": {"version": "0.17.0"}}}
    with pytest.raises(AssertionError, match="outside the reviewed release branch"):
        _assert_safe_entries(lock, "@humanfs/node", "0.16.8")


def test_repaired_nested_entry_and_newer_safe_patch_are_accepted() -> None:
    lock = {
        "packages": {
            "": {"version": "0.0.1"},
            "node_modules/js-yaml": {"version": "4.3.1"},
            "node_modules/parent/node_modules/js-yaml": {"version": "4.3.2"},
            "node_modules/unrelated": {"version": "0.0.0"},
        }
    }
    _assert_safe_entries(lock, "js-yaml", "4.3.1")


BRACE_BRANCH_FLOORS = {1: "1.1.20", 2: "2.1.6", 5: "5.0.11"}


def _assert_safe_brace_entries(lock: dict[str, Any]) -> None:
    packages = lock.get("packages")
    assert isinstance(packages, dict), "missing lock packages"
    rows = [
        (path, metadata)
        for path, metadata in packages.items()
        if path.startswith("node_modules/")
        and path.rsplit("node_modules/", 1)[-1] == "brace-expansion"
    ]
    assert rows, "brace-expansion: missing from lock"
    for path, metadata in rows:
        assert isinstance(metadata, dict), f"{path}: malformed package metadata"
        version = _stable_version(metadata.get("version"), path)
        assert version[0] in BRACE_BRANCH_FLOORS, f"{path}: unreviewed brace release branch"
        floor_text = BRACE_BRANCH_FLOORS[version[0]]
        assert version >= _stable_version(floor_text, path), (
            f"{path}: {metadata['version']} is below {floor_text}"
        )


@pytest.mark.parametrize("project", ("ToukaChat", "ToukaIDE"))
def test_every_brace_branch_meets_its_own_reviewed_floor(project: str) -> None:
    lock = json.loads((ROOT / project / "package-lock.json").read_text(encoding="utf-8"))
    _assert_safe_brace_entries(lock)


@pytest.mark.parametrize("depth", (1, 2, 3))
@pytest.mark.parametrize("version", ("1.1.19", "2.1.5", "5.0.10"))
def test_safe_brace_root_cannot_hide_weak_nested_branch(depth: int, version: str) -> None:
    nested = "node_modules/parent/" * depth + "node_modules/brace-expansion"
    lock = {
        "packages": {
            "node_modules/brace-expansion": {"version": "5.0.11"},
            nested: {"version": version},
        }
    }
    with pytest.raises(AssertionError, match=re.escape(nested) + ": .* is below"):
        _assert_safe_brace_entries(lock)


@pytest.mark.parametrize("version", ("0.1.0", "3.0.0", "4.0.0", "6.0.0"))
def test_unreviewed_brace_branch_is_rejected(version: str) -> None:
    with pytest.raises(AssertionError, match="unreviewed brace release branch"):
        _assert_safe_brace_entries(
            {"packages": {"node_modules/brace-expansion": {"version": version}}}
        )


@pytest.mark.parametrize("version", (None, 5, "5.0", "05.0.11", "5.0.11-rc.1"))
def test_malformed_brace_version_is_rejected(version: object) -> None:
    with pytest.raises(AssertionError, match="malformed or unqualified version"):
        _assert_safe_brace_entries(
            {"packages": {"node_modules/brace-expansion": {"version": version}}}
        )


def test_three_reviewed_brace_branches_coexist_without_major_override() -> None:
    lock = {
        "packages": {
            "node_modules/brace-expansion": {"version": "1.1.20"},
            "node_modules/two/node_modules/brace-expansion": {"version": "2.1.6"},
            "node_modules/five/node_modules/brace-expansion": {"version": "5.0.11"},
        }
    }
    _assert_safe_brace_entries(lock)


def test_missing_brace_is_not_vacuous_success() -> None:
    with pytest.raises(AssertionError, match="brace-expansion: missing from lock"):
        _assert_safe_brace_entries({"packages": {}})

"""Regression coverage for the Next.js August 2026 security release."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "ToukaChat"
PATCHED_NEXT_VERSION = "16.4.2"
PATCHED_SHARP_RANGE = "^0.35.5"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_cve_2026_75604_nextjs_windows_rce_is_patched() -> None:
    """Keep the direct and resolved Next.js versions on the patched release."""
    package = _read_json(CHAT / "package.json")
    lock_packages = _read_json(CHAT / "package-lock.json")["packages"]

    assert package["dependencies"]["next"] == PATCHED_NEXT_VERSION
    assert lock_packages[""]["dependencies"]["next"] == PATCHED_NEXT_VERSION
    assert lock_packages["node_modules/next"]["version"] == PATCHED_NEXT_VERSION


def test_nextjs_eslint_config_is_exactly_aligned_with_patched_release() -> None:
    """Prevent the Next.js runtime and lint preset from splitting versions."""
    package = _read_json(CHAT / "package.json")
    lock_packages = _read_json(CHAT / "package-lock.json")["packages"]

    assert package["devDependencies"]["eslint-config-next"] == PATCHED_NEXT_VERSION
    assert lock_packages[""]["devDependencies"]["eslint-config-next"] == PATCHED_NEXT_VERSION
    assert lock_packages["node_modules/eslint-config-next"]["version"] == PATCHED_NEXT_VERSION
    assert (
        lock_packages["node_modules/eslint-config-next"]["dependencies"]["@next/eslint-plugin-next"]
        == PATCHED_NEXT_VERSION
    )
    assert lock_packages["node_modules/@next/eslint-plugin-next"]["version"] == PATCHED_NEXT_VERSION


def test_ghsa_2xp9_avif_rce_uses_patched_upstream_sharp_range() -> None:
    """Retire the project override once Next.js carries the patched sharp line."""
    package = _read_json(CHAT / "package.json")
    lock_packages = _read_json(CHAT / "package-lock.json")["packages"]
    sharp_version = lock_packages["node_modules/sharp"]["version"]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", sharp_version)

    assert "sharp" not in package.get("overrides", {})
    assert (
        lock_packages["node_modules/next"]["optionalDependencies"]["sharp"] == PATCHED_SHARP_RANGE
    )
    assert match is not None
    assert (0, 35, 4) <= tuple(int(part) for part in match.groups()) < (0, 36, 0)

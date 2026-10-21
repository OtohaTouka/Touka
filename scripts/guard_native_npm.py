# ruff: noqa: T201
"""Guard #25 — Native NPM Module Build Environment Guard.

Detects npm packages in the frontend's runtime dependencies that require
native compilation via node-gyp, and verifies the corresponding Dockerfile
has the necessary build tools installed.

SSOT: KNOWN_NATIVE_MODULES and REQUIRED_BUILD_TOOLS are the single source
of truth for which packages need compilation and which tools are required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── SSOT Constants ────────────────────────────────────────────────────────────

# Known npm packages that require native compilation (node-gyp).
# Add new entries here when introducing native modules to any frontend project.
KNOWN_NATIVE_MODULES: frozenset[str] = frozenset(
    {
        "node-pty",
        "canvas",
        "sharp",
        "node-sqlite3",
        "better-sqlite3",
        "bcrypt",
        "fsevents",
    }
)

# Build tools that must appear in the Dockerfile to support native compilation.
# build-essential is treated as an alternative that covers make and g++.
REQUIRED_BUILD_TOOLS: frozenset[str] = frozenset({"python3", "make", "g++"})

# Only runtime dependencies trigger the check; devDependencies are install-time
# only and are typically excluded from Docker images via --omit=dev.
_RUNTIME_DEPS_KEY = "dependencies"


# ── Pure Functions (SOLID SRP) ────────────────────────────────────────────────


def find_native_modules(package_json: dict) -> set[str]:
    """Return the set of known native modules found in runtime dependencies.

    Args:
        package_json: Parsed package.json content.

    Returns:
        Set of native module names present in ``dependencies`` (not devDependencies).
    """
    deps: dict = package_json.get(_RUNTIME_DEPS_KEY, {})
    return {pkg for pkg in deps if pkg in KNOWN_NATIVE_MODULES}


def find_missing_build_tools(dockerfile_text: str) -> set[str]:
    """Return the set of required build tools absent from the Dockerfile.

    Recognises both individual tool names (python3, make, g++) and the
    Debian meta-package ``build-essential`` which implies make and g++.

    Args:
        dockerfile_text: Raw text content of a Dockerfile.

    Returns:
        Set of tool names that are required but not found.
    """
    has_build_essential = "build-essential" in dockerfile_text
    missing: set[str] = set()
    for tool in REQUIRED_BUILD_TOOLS:
        if tool in ("make", "g++") and has_build_essential:
            continue
        if tool not in dockerfile_text:
            missing.add(tool)
    return missing


def check_project(
    project_root: Path,
    frontend_dir: str = "ToukaChat",
) -> list[dict]:
    """Check whether the frontend Dockerfile has build tools for native modules.

    Args:
        project_root: Root directory of the Touka repository.
        frontend_dir: Subdirectory name of the frontend project.

    Returns:
        List of violation dicts, each with keys ``native_modules`` and
        ``missing_tools``. Empty list means the project passes.
    """
    package_json_path = project_root / frontend_dir / "package.json"
    # Check all Dockerfiles in the frontend directory (both dev and prod variants)
    dockerfile_paths = [
        project_root / frontend_dir / "Dockerfile",
        project_root / frontend_dir / "Dockerfile.prod",
    ]

    # Missing package.json → nothing to check
    if not package_json_path.exists():
        return []

    try:
        package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    native_modules = find_native_modules(package_json)

    # No native runtime dependencies → no check required
    if not native_modules:
        return []

    # Native modules present → check each existing Dockerfile for build tools
    violations: list[dict] = []
    found_any = False
    for dockerfile_path in dockerfile_paths:
        if not dockerfile_path.exists():
            continue
        found_any = True
        dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
        missing_tools = find_missing_build_tools(dockerfile_text)
        if missing_tools:
            violations.append(
                {
                    "dockerfile": dockerfile_path.name,
                    "native_modules": sorted(native_modules),
                    "missing_tools": sorted(missing_tools),
                }
            )

    if not found_any:
        return [
            {
                "native_modules": sorted(native_modules),
                "missing_tools": sorted(REQUIRED_BUILD_TOOLS),
                "note": "No Dockerfile found",
            }
        ]

    return violations


# ── Guard Entry Point ─────────────────────────────────────────────────────────


def main() -> int:
    """Run the guard and return an exit code (0 = pass, 1 = fail)."""
    # Reconfigure stdout for Windows UTF-8 compatibility
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    line = "=" * 60
    print(line)
    print("Native NPM Module Build Environment Guard (Guard #25)")
    print(line)

    project_root = Path(__file__).resolve().parents[1]
    frontend_dir = "ToukaChat"
    package_json_path = project_root / frontend_dir / "package.json"

    violations = check_project(project_root, frontend_dir)

    # Report how many native modules were found (informational)
    try:
        pkg = json.loads(package_json_path.read_text(encoding="utf-8"))
        found = find_native_modules(pkg)
    except (OSError, json.JSONDecodeError):
        found = set()

    print(
        f"Scanned {frontend_dir}/package.json: "
        f"{len(found)} native module(s) found."
    )

    if not violations:
        if found:
            print(
                "[OK] All native modules have required build tools in all Dockerfiles."
            )
        else:
            print("[OK] No native modules detected — no Dockerfile check needed.")
        return 0

    print("\n[FAIL] Native modules detected but Dockerfile(s) missing build tools:")
    for v in violations:
        dockerfile_name = v.get("dockerfile", "Dockerfile")
        modules_str = ", ".join(v["native_modules"])
        tools_str = ", ".join(v["missing_tools"])
        print(f"  Dockerfile     : {frontend_dir}/{dockerfile_name}")
        print(f"  Native modules : {modules_str}")
        print(f"  Missing tools  : {tools_str}")
        if "note" in v:
            print(f"  Note           : {v['note']}")
        print(
            f"  Fix            : Add missing tools to "
            f"{frontend_dir}/{dockerfile_name}"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())

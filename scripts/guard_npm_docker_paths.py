# ruff: noqa: T201
"""Guard #26 — npm Script Docker Path Guard.

Scans the frontend package.json scripts for parent-directory relative paths
(``../filename``), then verifies that each referenced file is volume-mounted
into the corresponding docker-compose service.

This prevents the class of Docker startup failures where an npm script reads
a file outside its container workdir (e.g. ``../openapi.json``) but the file
has no matching volume mount.

SSOT:
    PARENT_PATH_PATTERN  — regex for detecting ``../file`` in script strings
    FRONTEND_SERVICE     — docker-compose service name for the frontend
    FRONTEND_WORKDIR     — container working directory (used to resolve paths)
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from pathlib import Path

# ── SSOT Constants ────────────────────────────────────────────────────────────

# Matches ../filename.ext (stop at whitespace, quotes, or other delimiters)
PARENT_PATH_PATTERN: re.Pattern[str] = re.compile(r"\.\./([^\s'\"&|;,]+)")

# docker-compose service name for the frontend project
FRONTEND_SERVICE: str = "touka-chat"

# Container working directory (matches ToukaChat/Dockerfile WORKDIR)
FRONTEND_WORKDIR: str = "/app"


# ── Pure Functions (SOLID SRP) ────────────────────────────────────────────────


def find_parent_path_refs(scripts: dict[str, str]) -> set[str]:
    """Return filenames referenced via ``../filename`` in any npm script.

    Args:
        scripts: The ``scripts`` object from package.json.

    Returns:
        Set of bare filenames (e.g. ``{"openapi.json"}``) found in any script.
    """
    refs: set[str] = set()
    for cmd in scripts.values():
        if not isinstance(cmd, str):
            continue
        for match in PARENT_PATH_PATTERN.finditer(cmd):
            refs.add(match.group(1))
    return refs


def find_mounted_container_paths(
    compose: dict,
    service: str,
    workdir: str = FRONTEND_WORKDIR,  # noqa: ARG001 — reserved for future path resolution
) -> set[str]:
    """Return the set of container paths exposed via volume mounts for *service*.

    Only the short string volume syntax is parsed (``host:container[:mode]``).
    Long-form dict volumes are ignored for simplicity.

    Args:
        compose: Parsed docker-compose.yml content.
        service:  Service name to inspect (e.g. ``"touka-chat"``).
        workdir:  Container workdir (unused currently, reserved for future use).

    Returns:
        Set of absolute container paths available through volume mounts.
    """
    services = compose.get("services", {})
    svc = services.get(service, {})
    volumes: list = svc.get("volumes", []) or []

    mounted: set[str] = set()
    for vol in volumes:
        if not isinstance(vol, str):
            continue
        # Short syntax: host:container or host:container:mode
        parts = vol.split(":")
        if len(parts) >= 2:
            container_path = parts[1]
            mounted.add(container_path)
    return mounted


def check_project(
    project_root: Path,
    frontend_dir: str = "ToukaChat",
) -> list[dict]:
    """Check that parent-path npm script references are volume-mounted.

    Args:
        project_root: Root of the Touka repository.
        frontend_dir: Subdirectory containing the frontend project.

    Returns:
        List of violation dicts. Empty list means the check passes.
    """
    package_json_path = project_root / frontend_dir / "package.json"
    compose_path = project_root / "docker-compose.yml"

    if not package_json_path.exists():
        return []

    try:
        pkg = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    scripts = pkg.get("scripts", {})
    parent_refs = find_parent_path_refs(scripts)

    if not parent_refs:
        return []  # No parent-dir references → nothing to verify

    # We have references; must verify docker-compose mounts them
    if not compose_path.exists():
        return [
            {
                "unreferenced_files": sorted(parent_refs),
                "note": "docker-compose.yml not found — cannot verify volume mounts",
            }
        ]

    try:
        import yaml  # noqa: PLC0415 — PyYAML available in dev environment

        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (ImportError, Exception):
        # Fallback: minimal YAML parsing for simple volume strings
        compose = _parse_compose_minimal(compose_path)

    mounted = find_mounted_container_paths(compose, FRONTEND_SERVICE, FRONTEND_WORKDIR)

    # For each parent-dir ref, compute the resolved container path and check.
    # Use posixpath (not pathlib) to avoid Windows drive-letter contamination.
    violations: list[dict] = []

    for filename in sorted(parent_refs):
        # ../filename from /app → posixpath.normpath("/app/../filename") = "/filename"
        container_path = posixpath.normpath(
            posixpath.join(FRONTEND_WORKDIR, "..", filename)
        )
        if container_path not in mounted:
            violations.append(
                {
                    "file": filename,
                    "npm_ref": f"../{filename}",
                    "expected_container_path": container_path,
                    "fix": (
                        f"Add to docker-compose.yml touka-chat volumes: "
                        f"./{filename}:{container_path}:ro"
                    ),
                }
            )

    return violations


def _parse_compose_minimal(compose_path: Path) -> dict:
    """Minimal fallback YAML parser for docker-compose volumes (no PyYAML).

    Only handles top-level structure and short-syntax volume strings.
    """
    text = compose_path.read_text(encoding="utf-8")
    # Very basic: look for volumes: lines under touka-chat service block
    services: dict = {}
    current_service: str | None = None
    in_volumes = False
    volumes: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("services:"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and stripped.endswith(":"):
            if current_service and volumes:
                services[current_service] = {"volumes": volumes}
            current_service = stripped[:-1]
            volumes = []
            in_volumes = False
        elif stripped == "volumes:":
            in_volumes = True
        elif in_volumes and stripped.startswith("- "):
            volumes.append(stripped[2:])
        elif indent <= 4 and not stripped.startswith("- ") and in_volumes:
            in_volumes = False

    if current_service and volumes:
        services[current_service] = {"volumes": volumes}

    return {"services": services}


# ── Guard Entry Point ─────────────────────────────────────────────────────────


def main() -> int:
    """Run the guard and return an exit code (0 = pass, 1 = fail)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    line = "=" * 60
    print(line)
    print("npm Script Docker Path Guard (Guard #26)")
    print(line)

    project_root = Path(__file__).resolve().parents[1]
    violations = check_project(project_root)

    if not violations:
        print("[OK] All npm script parent-path references are volume-mounted.")
        return 0

    print("\n[FAIL] npm scripts reference parent-dir files not mounted in Docker:")
    for v in violations:
        if "note" in v:
            print(f"  Files     : {', '.join(v.get('unreferenced_files', []))}")
            print(f"  Note      : {v['note']}")
        else:
            print(f"  File      : {v['file']}")
            print(f"  npm ref   : {v['npm_ref']}")
            print(f"  Container : {v['expected_container_path']}")
            print(f"  Fix       : {v['fix']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

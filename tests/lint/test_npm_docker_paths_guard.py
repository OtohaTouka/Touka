"""TDD tests for Guard #24 — npm Script Docker Path Guard.

Tests that guard_npm_docker_paths.py correctly detects parent-directory
relative paths in npm scripts and verifies they are volume-mounted in
docker-compose.yml for the corresponding service.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# ── 動態載入守衛模組 ──────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "guard_npm_docker_paths",
    _ROOT / "scripts" / "guard_npm_docker_paths.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

find_parent_path_refs = _mod.find_parent_path_refs
find_mounted_container_paths = _mod.find_mounted_container_paths
check_project = _mod.check_project


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_package_json(directory: Path, scripts: dict) -> None:
    data = {"name": "test", "version": "0.1.0", "scripts": scripts}
    (directory / "package.json").write_text(json.dumps(data), encoding="utf-8")


def _write_compose(directory: Path, volumes: list[str] | None = None) -> None:
    """Write a minimal docker-compose.yml as a YAML string (no yaml import needed)."""
    if volumes:
        vol_lines = "".join(f"\n      - {v}" for v in volumes)
        content = f"services:\n  touka-chat:\n    image: node:20\n    volumes:{vol_lines}\n"
    else:
        content = "services:\n  touka-chat:\n    image: node:20\n"
    (directory / "docker-compose.yml").write_text(content, encoding="utf-8")


# ── find_parent_path_refs ─────────────────────────────────────────────────────

def test_find_refs_detects_parent_json():
    """../openapi.json must be detected in a script command."""
    scripts = {"sync-api": "npx openapi-typescript ../openapi.json -o src/types/api.ts"}
    refs = find_parent_path_refs(scripts)
    assert "openapi.json" in refs


def test_find_refs_ignores_relative_same_dir():
    """./local.json (same dir) must NOT be detected."""
    scripts = {"cmd": "node ./local.json"}
    refs = find_parent_path_refs(scripts)
    assert len(refs) == 0


def test_find_refs_ignores_plain_commands():
    """Plain commands without path references must return empty set."""
    scripts = {"dev": "next dev -p 3001", "build": "next build"}
    refs = find_parent_path_refs(scripts)
    assert len(refs) == 0


def test_find_refs_detects_multiple():
    """Multiple ../file references in different scripts must all be found."""
    scripts = {
        "sync-api": "cmd ../openapi.json",
        "sync-cfg": "cmd ../config.yaml",
    }
    refs = find_parent_path_refs(scripts)
    assert "openapi.json" in refs
    assert "config.yaml" in refs


def test_find_refs_empty_scripts():
    """Empty scripts dict must return empty set."""
    assert find_parent_path_refs({}) == set()


# ── find_mounted_container_paths ──────────────────────────────────────────────

def test_find_mounted_detects_simple_volume():
    """Short volume syntax ./host:/container:ro must be detected."""
    compose = {
        "services": {
            "touka-chat": {
                "volumes": ["./openapi.json:/openapi.json:ro"]
            }
        }
    }
    paths = find_mounted_container_paths(compose, "touka-chat", "/app")
    assert "/openapi.json" in paths


def test_find_mounted_no_volumes():
    """Service with no volumes must return empty set."""
    compose = {"services": {"touka-chat": {"image": "node:20"}}}
    paths = find_mounted_container_paths(compose, "touka-chat", "/app")
    assert len(paths) == 0


def test_find_mounted_missing_service():
    """Missing service must return empty set (no crash)."""
    compose = {"services": {}}
    paths = find_mounted_container_paths(compose, "touka-chat", "/app")
    assert len(paths) == 0


# ── check_project (integration) ───────────────────────────────────────────────

def test_parent_path_mounted_passes(tmp_path):
    """npm script has ../openapi.json AND docker-compose mounts it → no violations."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {
        "sync-api": "npx openapi-typescript ../openapi.json -o src/types/api.ts"
    })
    _write_compose(tmp_path, volumes=["./openapi.json:/openapi.json:ro"])

    violations = check_project(tmp_path)
    assert violations == []


def test_parent_path_not_mounted_fails(tmp_path):
    """npm script has ../openapi.json but docker-compose has NO volumes → violation."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {
        "sync-api": "npx openapi-typescript ../openapi.json -o src/types/api.ts"
    })
    _write_compose(tmp_path)  # no volumes

    violations = check_project(tmp_path)
    assert len(violations) > 0
    violation_text = " ".join(str(v) for v in violations)
    assert "openapi.json" in violation_text


def test_no_parent_paths_passes(tmp_path):
    """npm scripts with no ../refs → no violations, docker-compose not checked."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {"dev": "next dev -p 3001"})
    # No docker-compose.yml — if guard tries to read it, it should not crash

    violations = check_project(tmp_path)
    assert violations == []


def test_missing_package_json_skips_gracefully(tmp_path):
    """Missing package.json must return no violations (safe fallback)."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()

    violations = check_project(tmp_path)
    assert violations == []


def test_missing_compose_with_parent_path_fails(tmp_path):
    """npm script has ../file.json but docker-compose.yml doesn't exist → violation."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {
        "sync-api": "npx openapi-typescript ../openapi.json -o api.ts"
    })
    # No docker-compose.yml created

    violations = check_project(tmp_path)
    assert len(violations) > 0
    violation_text = " ".join(str(v) for v in violations)
    assert "openapi.json" in violation_text


def test_real_project_passes():
    """The actual Touka project must pass the guard (regression test)."""
    violations = check_project(_ROOT)
    assert violations == [], (
        f"Real project has npm docker path violations: {violations}"
    )

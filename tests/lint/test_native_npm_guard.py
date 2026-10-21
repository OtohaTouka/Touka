"""TDD tests for Guard #23 — Native NPM Module Build Environment Guard.

Tests the guard_native_npm.py script which detects native npm modules in
package.json and verifies the Dockerfile has the required build tools.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# ── 動態載入守衛模組 ──────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "guard_native_npm",
    _ROOT / "scripts" / "guard_native_npm.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

find_native_modules = _mod.find_native_modules
find_missing_build_tools = _mod.find_missing_build_tools
check_project = _mod.check_project


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_package_json(directory: Path, deps: dict, dev_deps: dict | None = None) -> None:
    data: dict = {"name": "test", "version": "0.1.0"}
    if deps:
        data["dependencies"] = deps
    if dev_deps:
        data["devDependencies"] = dev_deps
    (directory / "package.json").write_text(json.dumps(data), encoding="utf-8")


def _write_dockerfile(directory: Path, content: str) -> None:
    (directory / "Dockerfile").write_text(content, encoding="utf-8")


_DOCKERFILE_WITH_TOOLS = """\
FROM node:20-slim
RUN apt-get update && \\
    apt-get install -y --no-install-recommends python3 make g++ && \\
    rm -rf /var/lib/apt/lists/*
COPY package*.json ./
RUN npm install
"""

_DOCKERFILE_WITHOUT_TOOLS = """\
FROM node:20-slim
COPY package*.json ./
RUN npm install
"""

_DOCKERFILE_PARTIAL_TOOLS = """\
FROM node:20-slim
RUN apt-get update && apt-get install -y python3 && rm -rf /var/lib/apt/lists/*
COPY package*.json ./
RUN npm install
"""


# ── find_native_modules ───────────────────────────────────────────────────────

def test_find_native_modules_detects_node_pty():
    """node-pty in dependencies must be detected."""
    pkg = {"dependencies": {"node-pty": "^1.1.0", "react": "^19.0.0"}}
    result = find_native_modules(pkg)
    assert "node-pty" in result


def test_find_native_modules_ignores_dev_dependencies():
    """node-pty in devDependencies only must NOT be detected."""
    pkg = {
        "dependencies": {"react": "^19.0.0"},
        "devDependencies": {"node-pty": "^1.1.0"},
    }
    result = find_native_modules(pkg)
    assert len(result) == 0


def test_find_native_modules_empty_deps_returns_empty():
    """Empty dependencies must return empty set."""
    pkg = {"dependencies": {}}
    result = find_native_modules(pkg)
    assert result == set()


def test_find_native_modules_no_deps_key_returns_empty():
    """Missing dependencies key must return empty set (no crash)."""
    pkg = {"name": "test"}
    result = find_native_modules(pkg)
    assert result == set()


# ── find_missing_build_tools ──────────────────────────────────────────────────

def test_find_missing_build_tools_all_present():
    """Dockerfile with python3 make g++ must return empty set."""
    result = find_missing_build_tools(_DOCKERFILE_WITH_TOOLS)
    assert result == set()


def test_find_missing_build_tools_all_missing():
    """Dockerfile without any build tools must report all three."""
    result = find_missing_build_tools(_DOCKERFILE_WITHOUT_TOOLS)
    assert "python3" in result
    assert "make" in result
    assert "g++" in result


def test_find_missing_build_tools_partial():
    """Dockerfile with only python3 must report make and g++ as missing."""
    result = find_missing_build_tools(_DOCKERFILE_PARTIAL_TOOLS)
    assert "python3" not in result
    assert "make" in result
    assert "g++" in result


def test_find_missing_build_tools_build_essential_alternative():
    """build-essential implies make and g++; should count as present."""
    dockerfile = "RUN apt-get install -y python3 build-essential"
    result = find_missing_build_tools(dockerfile)
    assert result == set()


# ── check_project (integration) ───────────────────────────────────────────────

def test_native_module_without_build_tools_fails(tmp_path):
    """package.json has node-pty but Dockerfile lacks build tools → violations."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {"node-pty": "^1.1.0", "react": "^19.0.0"})
    _write_dockerfile(frontend, _DOCKERFILE_WITHOUT_TOOLS)

    violations = check_project(tmp_path)
    assert len(violations) > 0
    violation_text = " ".join(str(v) for v in violations)
    assert "node-pty" in violation_text


def test_native_module_with_build_tools_passes(tmp_path):
    """package.json has node-pty and Dockerfile has all build tools → no violations."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {"node-pty": "^1.1.0"})
    _write_dockerfile(frontend, _DOCKERFILE_WITH_TOOLS)

    violations = check_project(tmp_path)
    assert violations == []


def test_no_native_modules_passes(tmp_path):
    """No native modules in dependencies → no violations, Dockerfile not required."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {"react": "^19.0.0", "next": "^16.0.0"})
    # No Dockerfile created — guard must not crash

    violations = check_project(tmp_path)
    assert violations == []


def test_native_module_in_dev_dependencies_ignored(tmp_path):
    """node-pty in devDependencies only must not trigger Dockerfile check."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(
        frontend,
        deps={"react": "^19.0.0"},
        dev_deps={"node-pty": "^1.1.0"},
    )
    # No Dockerfile — if guard checks, it will error; if it skips, it passes
    violations = check_project(tmp_path)
    assert violations == []


def test_partial_missing_tools_reports_specific_items(tmp_path):
    """Dockerfile with python3 only must report make and g++ specifically."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    _write_package_json(frontend, {"node-pty": "^1.1.0"})
    _write_dockerfile(frontend, _DOCKERFILE_PARTIAL_TOOLS)

    violations = check_project(tmp_path)
    assert len(violations) > 0
    violation_text = " ".join(str(v) for v in violations)
    assert "make" in violation_text
    assert "g++" in violation_text
    assert "python3" not in violation_text


def test_missing_package_json_skips_gracefully(tmp_path):
    """Missing package.json must not raise exception, just skip."""
    frontend = tmp_path / "ToukaChat"
    frontend.mkdir()
    # No package.json

    violations = check_project(tmp_path)
    assert violations == []


def test_real_project_passes():
    """The actual ToukaChat project must pass the guard (regression test)."""
    violations = check_project(_ROOT)
    assert violations == [], (
        f"Real ToukaChat project has native module guard violations: {violations}"
    )

"""P8-B RED 1: immutable frontend supply chain and production image binding."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from hermetic_gate import npm_offline_gate

ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "ToukaChat"
IDE = ROOT / "ToukaIDE"
NODE_VERSION = "24.22.1"
NPM_VERSION = "11.19.1"
# 映像自帶 npm 不保證符合 engine-strict 版本；唯一允許的 npm install 是此精確自我釘版。
NPM_SELF_PIN = f"npm install -g --ignore-scripts --no-audit --no-fund npm@{NPM_VERSION}"
NEXT_VERSION = "16.4.2"
NEXT_SHARP_RANGE = "^0.35.5"
NODE_INDEX = "sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea"
PYTHON_IMAGE = (
    "python:3.12-slim-trixie@"
    "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
UV_CHILD = "sha256:f6edac8c6d516a01a5387a8655c97481e0db46dd0a62ab62f41e5b2294376f6e"
DEV_ENGINES = {
    "runtime": {
        "name": "node",
        "version": NODE_VERSION,
        "onFail": "error",
    },
    "packageManager": {
        "name": "npm",
        "version": NPM_VERSION,
        "onFail": "error",
    },
}
AUDIT_SAFE_DIRECT_DEPENDENCIES = {
    "dompurify": "3.4.17",
    "mermaid": "11.16.1",
}
AUDIT_SAFE_OVERRIDES = {
    "fast-uri": "3.1.7",
    "nanoid": "3.3.18",
    "socket.io-parser": "4.2.7",
    "undici": "7.29.1",
}


def _package_json() -> dict[str, Any]:
    return json.loads((CHAT / "package.json").read_text(encoding="utf-8"))


def _from_refs(path: Path) -> list[str]:
    return re.findall(r"(?m)^FROM\s+(\S+)", path.read_text(encoding="utf-8"))


def _run_instructions(text: str) -> str:
    joined = re.sub(r"\\\r?\n\s*", " ", text)
    return "\n".join(re.findall(r"(?m)^RUN\s+(.+)$", joined))


def _assert_debian_node_from_official_image(text: str) -> None:
    """Node/npm come only from the digest-pinned official node image, never apt/npm."""
    assert f"FROM {PYTHON_IMAGE}" in text
    donor = re.search(rf"(?m)^FROM node@{NODE_INDEX} AS ([\w-]+)$", text)
    assert donor is not None
    copies = re.findall(rf"(?m)^COPY --from={re.escape(donor[1])}\s+(.+)$", text)
    assert copies == [
        "/usr/local/bin/node /usr/local/bin/node",
        "/usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm",
    ]
    runs = _run_instructions(text)
    assert "npm install" not in runs.replace(NPM_SELF_PIN, "")
    assert "apt-get update" in runs
    assert "apk " not in runs
    assert "--allow-untrusted" not in runs
    installs = " ".join(re.findall(r"\bapt-get\s+install\b([^;&]*)", runs))
    assert re.search(r"\bbash\b", installs)
    assert "libstdc++6" in installs
    assert not re.search(r"\b(?:nodejs|npm)\b", installs)
    assert f"test \"$(node --version)\" = 'v{NODE_VERSION}'" in runs
    assert "npm --version" in runs
    assert re.search(r"groupadd\b[^;\n]*\s(?:--gid|-g)\s+1001\b", runs)
    assert re.search(r"useradd\b[^;\n]*\s(?:--uid|-u)\s+1001\b", runs)
    assert "USER appuser" in text


def test_committed_npm_policy_is_fail_closed() -> None:
    assert npm_offline_gate.load_npm_policy(CHAT) == {
        "audit": "false",
        "engine-strict": "true",
        "fund": "false",
        "ignore-scripts": "true",
        "registry": "https://registry.npmjs.org/",
        "update-notifier": "false",
    }


def test_frontend_runtime_and_lifecycle_are_exact() -> None:
    package = _package_json()
    assert package.get("engines") == {
        "node": NODE_VERSION,
        "npm": NPM_VERSION,
    }
    assert package.get("packageManager") == f"npm@{NPM_VERSION}"
    assert package.get("devEngines") == DEV_ENGINES
    assert package["devDependencies"]["@types/node"] == "^24.13.3"
    scripts = package["scripts"]
    assert "postinstall" not in scripts
    assert "prebuild" not in scripts
    assert (
        scripts["gen:artifact-runtime"]
        == "npm run braces:verify && node scripts/build-artifact-runtime.mjs"
    )
    assert scripts["build"] == "npm run braces:verify && next build"


def test_toukaide_uses_exact_build_toolchain_without_widening_host_api() -> None:
    package = json.loads((IDE / "package.json").read_text(encoding="utf-8"))

    assert package.get("engines") == {"node": ">=20", "vscode": "^1.90.0"}
    assert package.get("packageManager") == f"npm@{NPM_VERSION}"
    assert package.get("devEngines") == DEV_ENGINES
    assert package["devDependencies"]["@types/node"] == "20.19.30"


def test_chat_build_has_no_remote_google_font_dependency() -> None:
    layout = (CHAT / "src" / "app" / "layout.tsx").read_text(encoding="utf-8")
    globals_css = (CHAT / "src" / "app" / "globals.css").read_text(encoding="utf-8")
    tokens = (CHAT / "src" / "lib" / "design-system" / "tokens.ts").read_text(encoding="utf-8")
    system_stack = '"Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif'

    assert "next/font/google" not in layout
    assert "fonts.googleapis.com/css2?family=Inter" not in layout
    assert "inter.className" not in layout
    assert f"--font-display: {system_stack};" in globals_css
    assert f"display: '{system_stack}'" in tokens


def test_lockfile_dependencies_are_integrity_bound_to_official_registry() -> None:
    lock = json.loads((CHAT / "package-lock.json").read_text(encoding="utf-8"))
    bundled = {
        path: metadata
        for path, metadata in lock["packages"].items()
        if metadata.get("inBundle") is True
    }
    assert len(bundled) == 6
    assert all("resolved" not in row and "integrity" not in row for row in bundled.values())
    npm_offline_gate.validate_lockfile(CHAT)


def test_inbundle_metadata_does_not_bypass_parent_integrity_binding(tmp_path: Path) -> None:
    package_path = "node_modules/parent/node_modules/bundled"
    metadata = {"version": "1.0.0", "inBundle": True}
    packages: dict[str, Any] = {
        "node_modules/parent": {
            "resolved": "https://registry.npmjs.org/parent/-/parent-1.0.0.tgz",
            "integrity": "sha512-parent",
            "bundleDependencies": ["bundled"],
        }
    }
    packages[package_path] = metadata
    lock_path = tmp_path / "package-lock.json"
    lock_path.write_text(json.dumps({"packages": packages}), encoding="utf-8")
    npm_offline_gate.validate_lockfile(tmp_path)
    packages["node_modules/parent"]["integrity"] = "sha256-untrusted"
    lock_path.write_text(json.dumps({"packages": packages}), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        npm_offline_gate.validate_lockfile(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate",
        "empty-key",
        "missing",
        "extra",
        "invalid-line",
        "foreign",
        "wrong-value",
        "legacy-three",
    ),
)
def test_offline_npm_policy_rejects_malformed_or_drifted_keys(
    tmp_path: Path,
    mutation: str,
) -> None:
    policy = (CHAT / ".npmrc").read_text(encoding="utf-8")
    replacements = {
        "duplicate": policy + "audit=false\n",
        "empty-key": policy + "=false\n",
        "missing": policy.replace("audit=false\n", ""),
        "extra": policy + "progress=false\n",
        "invalid-line": policy + "invalid-line\n",
        "foreign": policy.replace("https://registry.npmjs.org/", "https://foreign.invalid/"),
        "wrong-value": policy.replace("audit=false", "audit=true"),
        "legacy-three": "engine-strict=true\nignore-scripts=true\nregistry=https://registry.npmjs.org/\n",
    }
    (tmp_path / ".npmrc").write_text(replacements[mutation], encoding="utf-8")
    with pytest.raises(ValueError):
        npm_offline_gate.load_npm_policy(tmp_path)


@pytest.mark.parametrize(
    "parent",
    (
        None,
        {"resolved": "https://registry.npmjs.org/parent.tgz", "bundleDependencies": ["bundled"]},
        {
            "resolved": "https://registry.npmjs.org/parent.tgz",
            "integrity": "sha512-parent",
            "bundleDependencies": [],
        },
        {
            "resolved": "https://foreign.invalid/parent.tgz",
            "integrity": "sha512-parent",
            "bundleDependencies": ["bundled"],
        },
    ),
    ids=("missing", "unbound", "undeclared", "foreign"),
)
def test_offline_bundle_requires_declaring_integrity_bound_parent(
    tmp_path: Path,
    parent: dict[str, Any] | None,
) -> None:
    packages = {"node_modules/parent/node_modules/bundled": {"inBundle": True}}
    if parent is not None:
        packages["node_modules/parent"] = parent
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        npm_offline_gate.validate_lockfile(tmp_path)


@pytest.mark.parametrize(
    "child_fields",
    (
        {"resolved": "https://foreign.invalid/bundled.tgz"},
        {"resolved": None},
        {"integrity": "sha256-untrusted"},
        {"integrity": None},
    ),
    ids=("foreign-resolved", "null-resolved", "wrong-integrity", "null-integrity"),
)
def test_offline_bundle_rejects_explicit_invalid_child_fields(
    tmp_path: Path,
    child_fields: dict[str, Any],
) -> None:
    packages = {
        "node_modules/parent": {
            "resolved": "https://registry.npmjs.org/parent.tgz",
            "integrity": "sha512-parent",
            "bundleDependencies": ["bundled"],
        },
        "node_modules/parent/node_modules/bundled": {"inBundle": True, **child_fields},
    }
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        npm_offline_gate.validate_lockfile(tmp_path)


@pytest.mark.parametrize(
    "lock",
    (
        None,
        [],
        {},
        {"packages": None},
        {"packages": []},
        {"packages": {"node_modules/bad": None}},
        {"packages": {"node_modules/bad": []}},
        {"packages": {"": None}},
    ),
    ids=(
        "null-root",
        "array-root",
        "missing-packages",
        "null-packages",
        "array-packages",
        "null-row",
        "array-row",
        "null-project-row",
    ),
)
def test_offline_lockfile_rejects_malformed_shapes(tmp_path: Path, lock: object) -> None:
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError):
        npm_offline_gate.validate_lockfile(tmp_path)


@pytest.mark.parametrize("inherited_telemetry", (None, "0", "1"))
def test_offline_child_always_disables_next_telemetry(
    tmp_path: Path,
    inherited_telemetry: str | None,
) -> None:
    inherited = {"UNRELATED_SETTING": "must-not-propagate"}
    if inherited_telemetry is not None:
        inherited["NEXT_TELEMETRY_DISABLED"] = inherited_telemetry
    environment = npm_offline_gate.build_clean_environment(
        inherited,
        cache_dir=tmp_path / "cache",
        global_config=tmp_path / "global.npmrc",
        home_dir=tmp_path / "home",
        user_config=tmp_path / ".npmrc",
    )
    assert environment.get("NEXT_TELEMETRY_DISABLED") == "1"
    assert "UNRELATED_SETTING" not in environment


def test_live_audit_packages_resolve_to_exact_safe_versions() -> None:
    package = _package_json()
    lock = json.loads((CHAT / "package-lock.json").read_text(encoding="utf-8"))
    lock_packages = lock["packages"]
    violations: list[str] = []

    for name, expected in AUDIT_SAFE_DIRECT_DEPENDENCIES.items():
        actual = package["dependencies"].get(name)
        if actual != expected:
            violations.append(f"dependencies.{name}: expected {expected}, got {actual}")

    for name, expected in AUDIT_SAFE_OVERRIDES.items():
        actual = package["overrides"].get(name)
        if actual != expected:
            violations.append(f"overrides.{name}: expected {expected}, got {actual}")

    for name, expected in {
        **AUDIT_SAFE_DIRECT_DEPENDENCIES,
        **AUDIT_SAFE_OVERRIDES,
    }.items():
        actual = lock_packages[f"node_modules/{name}"]["version"]
        if actual != expected:
            violations.append(f"lock {name}: expected {expected}, got {actual}")

    assert not violations, "\n".join(violations)


def test_next_security_release_and_framework_tooling_are_lock_coherent() -> None:
    package = _package_json()
    lock_packages = json.loads((CHAT / "package-lock.json").read_text(encoding="utf-8"))["packages"]

    assert package["dependencies"]["next"] == NEXT_VERSION
    assert package["devDependencies"]["eslint-config-next"] == NEXT_VERSION
    assert lock_packages[""]["dependencies"]["next"] == NEXT_VERSION
    assert lock_packages[""]["devDependencies"]["eslint-config-next"] == NEXT_VERSION
    assert lock_packages["node_modules/next"]["version"] == NEXT_VERSION
    assert lock_packages["node_modules/@next/env"]["version"] == NEXT_VERSION
    assert lock_packages["node_modules/eslint-config-next"]["version"] == NEXT_VERSION
    assert lock_packages["node_modules/@next/eslint-plugin-next"]["version"] == NEXT_VERSION

    swc_versions = {
        metadata["version"]
        for path, metadata in lock_packages.items()
        if path.startswith("node_modules/@next/swc-")
    }
    assert swc_versions == {NEXT_VERSION}
    assert "sharp" not in package.get("overrides", {})
    assert lock_packages["node_modules/next"]["optionalDependencies"]["sharp"] == NEXT_SHARP_RANGE


def test_chat_production_image_uses_pinned_node_and_explicit_commands() -> None:
    dockerfile = CHAT / "Dockerfile.prod"
    text = dockerfile.read_text(encoding="utf-8")
    runner = text[text.index("FROM node-runtime AS runner") :]
    runner_runs = _run_instructions(runner)
    assert not re.search(r"\bapt-get\s+install\b", runner_runs)
    assert runner_runs.index("apt-get update") < runner_runs.index("apt-get upgrade -y")
    assert runner_runs.index("apt-get upgrade -y") < runner_runs.index(
        "rm -rf /var/lib/apt/lists/*"
    )
    _assert_debian_node_from_official_image(text)
    assert {ref for ref in _from_refs(dockerfile) if "@" in ref} == {
        f"node@{NODE_INDEX}",
        PYTHON_IMAGE,
    }
    installs = " ".join(re.findall(r"\bapt-get\s+install\b([^;&]*)", _run_instructions(text)))
    assert re.search(r"\bmake\b", installs) and "g++" in installs
    assert text.count(NPM_SELF_PIN) == 2
    assert "npm install" not in text.replace(NPM_SELF_PIN, "")
    assert text.count("npm ci --ignore-scripts --no-audit --no-fund") == 1
    assert text.count("npm ci --omit=dev --ignore-scripts --no-audit --no-fund") == 1
    telemetry = text.index("ENV NEXT_TELEMETRY_DISABLED=1")
    generator = text.index("npm run gen:artifact-runtime --ignore-scripts")
    build = text.index("npm run build --ignore-scripts")
    assert telemetry < generator < build
    assert "ENV NO_UPDATE_CHECK=1" in text[text.index(" AS runner") :]
    assert 'CMD ["./node_modules/.bin/serve", "-s", "out", "-l", "3001"]' in text


def test_chat_development_image_uses_same_pinned_node_and_lock_install() -> None:
    dockerfile = CHAT / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    _assert_debian_node_from_official_image(text)
    assert {ref for ref in _from_refs(dockerfile) if "@" in ref} == {
        f"node@{NODE_INDEX}",
        PYTHON_IMAGE,
    }
    installs = " ".join(re.findall(r"\bapt-get\s+install\b([^;&]*)", _run_instructions(text)))
    assert re.search(r"\bmake\b", installs) and "g++" in installs
    assert text.count(NPM_SELF_PIN) == 1
    assert "npm install" not in text.replace(NPM_SELF_PIN, "")
    assert "ENV NEXT_TELEMETRY_DISABLED=1" in text
    assert text.count("npm ci --ignore-scripts --no-audit --no-fund") == 1
    assert (
        text.count(
            "npm rebuild node-pty --ignore-scripts=false --foreground-scripts --no-audit --no-fund"
        )
        == 1
    )


def test_single_proxy_image_uses_pinned_python_uv_and_node_references() -> None:
    dockerfile = ROOT / "ToukaProxy" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    refs = _from_refs(dockerfile)
    assert {ref for ref in refs if "@" in ref} == {
        f"node@{NODE_INDEX}",
        f"ghcr.io/astral-sh/uv@{UV_CHILD}",
        PYTHON_IMAGE,
    }
    _assert_debian_node_from_official_image(text)
    assert "UV_PYTHON_DOWNLOADS=never" in text
    assert "uv sync --locked --no-dev" in text
    assert 'ENV PATH="/app/.venv/bin:$PATH"' in text  # noqa: PATH_LITERAL_SSOT - Dockerfile contract
    assert "uv:latest" not in text


def test_repo_owned_offline_npm_gate_exists() -> None:
    gate = ROOT / "scripts" / "hermetic_gate" / "npm_offline_gate.py"
    assert gate.is_file(), "cache/env/lifecycle enforcement must be repo-owned"


def test_chat_build_context_excludes_host_artifacts_and_secrets() -> None:
    dockerignore = CHAT / ".dockerignore"
    assert dockerignore.is_file(), "ToukaChat build context needs an explicit allow boundary"
    entries = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".env",
        ".env.*",
        ".git",
        ".next",
        "*.log",
        "coverage",
        "data",
        "node_modules",
        "out",
        "playwright-report",
        "test-results",
    } <= entries

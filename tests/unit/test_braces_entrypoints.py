"""Controlled source entrypoints verify bytes without running installed tools."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = "container-security/braces-backport/backport.mjs"


@pytest.mark.parametrize(
    "project,dev_scripts",
    [
        (
            "ToukaChat",
            (
                "dev",
                "build",
                "lint",
                "sync-api",
                "gen:artifact-runtime",
                "test",
                "test:e2e",
                "guard:typecheck",
            ),
        ),
        ("ToukaIDE", ("test", "typecheck", "compile", "watch", "package")),
    ],
)
def test_package_scripts_have_explicit_install_and_readonly_use(project, dev_scripts):
    scripts = json.loads((ROOT / project / "package.json").read_bytes())["scripts"]
    helper = ("../ToukaChat/" if project == "ToukaIDE" else "") + HELPER
    assert scripts["braces:apply"] == f"node {helper} apply --project-root ."
    assert scripts["braces:verify"] == f"node {helper} verify --project-root . --scope full"
    assert (
        scripts["install:controlled"]
        == "npm ci --ignore-scripts && npm run braces:apply && npm run braces:verify"
    )
    assert all(scripts[name].startswith("npm run braces:verify && ") for name in dev_scripts)
    assert not ({"preinstall", "install", "postinstall", "prepare", "prebuild"} & scripts.keys())


@pytest.mark.parametrize(
    "module_name,entry",
    [
        ("guard_eslint_ratchet", "collect_violations"),
        ("list_theme_violations", "list_violations"),
        ("summarize_theme_violations", "list_violations_summary"),
    ],
)
@pytest.mark.parametrize("reject", [False, True])
def test_python_direct_tools_stop_when_braces_verify_fails(monkeypatch, module_name, entry, reject):
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def run(command, **options):
        calls.append(command)
        if isinstance(command, list) and "verify" in command:
            assert options["check"] is True and options["timeout"] == 30
            assert "--scope" in command and command[-1] == "full"
            if reject:
                raise subprocess.CalledProcessError(2, command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(
        module,
        "subprocess",
        SimpleNamespace(run=run, CalledProcessError=subprocess.CalledProcessError),
    )
    monkeypatch.setattr(
        module, "shutil", SimpleNamespace(which=lambda name: str(ROOT / "synthetic-node"))
    )
    if reject:
        with pytest.raises(subprocess.CalledProcessError):
            getattr(module, entry)()
        assert len(calls) == 1
    else:
        getattr(module, entry)()
        assert len(calls) == 2
    assert isinstance(calls[0], list) and "verify" in calls[0]
    assert Path(calls[0][1]) == ROOT / "ToukaChat" / HELPER


def test_knip_uses_scoped_preload_after_installed_byte_verification() -> None:
    """The package entry keeps verification and changes only the owned CLI cache policy."""
    scripts = json.loads((ROOT / "ToukaChat/package.json").read_bytes())["scripts"]
    verify, command = scripts["guard:prune"].split(" && ")
    assert verify == "npm run braces:verify"
    assert command.split() == [
        "node",
        "--import",
        "./scripts/knip-no-cache.mjs",
        "./node_modules/knip/bin/knip.js",
        "--no-gitignore",
        "--reporter",
        "compact",
    ]
    preload = ROOT / "ToukaChat/scripts/knip-no-cache.mjs"
    assert preload.is_file() and not preload.is_symlink()
    assert preload.read_text(encoding="utf-8").strip() == 'process.env.JITI_FS_CACHE = "false";'

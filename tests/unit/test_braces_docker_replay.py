"""Source and mocked offline commands enforce the installed braces boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermetic_gate import npm_offline_gate as gate

ROOT = Path(__file__).resolve().parents[2]
HELPER = "container-security/braces-backport/backport.mjs"


@pytest.mark.parametrize("runtime", [False, True])
def test_offline_sequence_checks_braces_before_tools(runtime):
    commands = gate.npm_command_sequence(Path("npm"), runtime=runtime)
    verify = [
        "node",
        HELPER,
        "verify",
        "--project-root",
        ".",
        "--scope",
        "runtime" if runtime else "full",
    ]
    if runtime:
        assert commands == (["npm", "ci", "--omit=dev", "--offline", "--ignore-scripts"], verify)
    else:
        assert len(commands) == 6
        assert commands[1:3] == (["node", HELPER, "apply", "--project-root", "."], verify)
        assert commands[3][1] == "scripts/ensure-critical-native.mjs"


def _gate_case(tmp_path, monkeypatch, *, runtime, reject_verify=False, missing_node=False):
    project = tmp_path / "project"
    project.mkdir()
    npm, node, config = (tmp_path / name for name in ("npm", "node", "global.npmrc"))
    for file in (npm, node, config):
        file.touch()
    for name in ("cache", "home"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(gate, "load_npm_policy", lambda root: None)
    monkeypatch.setattr(gate, "validate_lockfile", lambda root: None)
    monkeypatch.setattr(gate, "build_clean_environment", lambda *a, **k: {"PATH": str(tmp_path)})
    monkeypatch.setattr(
        gate, "shutil", SimpleNamespace(which=lambda *a, **k: None if missing_node else str(node))
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if reject_verify and "verify" in command:
            message = "synthetic verify refusal"
            raise gate.NpmExecutionError(message)
        stdout = (
            "https://registry.npmjs.org/\n"
            if command[1:4] == ["config", "get", "registry"]
            else "fixture"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(gate, "_run", run)
    args = SimpleNamespace(
        project=project,
        npm=npm,
        global_config=config,
        cache=tmp_path / "cache",
        home=tmp_path / "home",
        validate_only=False,
        runtime=runtime,
    )
    return args, calls, node


@pytest.mark.parametrize("runtime", [False, True])
def test_offline_actual_and_canonical_commands_have_same_order(tmp_path, monkeypatch, runtime):
    args, calls, node = _gate_case(tmp_path, monkeypatch, runtime=runtime)
    proof = gate.execute_gate(args)
    commands = gate.npm_command_sequence(Path("npm"), runtime=runtime)
    assert len(proof["commands"]) == (2 if runtime else 6)
    assert [item["argv"] for item in proof["commands"]] == [command[1:] for command in commands]
    verify = next(command for command, _ in calls if "verify" in command)
    assert verify[0] == str(node)
    assert all(options["cwd"] == args.project for _, options in calls)


def test_offline_verify_refusal_prevents_native_generator_and_build(tmp_path, monkeypatch):
    args, calls, _ = _gate_case(tmp_path, monkeypatch, runtime=False, reject_verify=True)
    with pytest.raises(gate.NpmExecutionError, match="synthetic verify refusal"):
        gate.execute_gate(args)
    assert calls[-1][0][2] == "verify"
    assert not any(
        "scripts/ensure-critical-native.mjs" in command or "run" in command for command, _ in calls
    )


def test_runtime_requires_resolved_node(tmp_path, monkeypatch):
    args, calls, _ = _gate_case(tmp_path, monkeypatch, runtime=True, missing_node=True)
    with pytest.raises(gate.NpmPolicyError, match="NODE_EXECUTABLE_REQUIRED"):
        gate.execute_gate(args)
    assert not any("ci" in command for command, _ in calls)


@pytest.mark.parametrize("relative", ["ToukaChat/Dockerfile", "ToukaChat/Dockerfile.prod"])
def test_docker_full_install_verifies_before_first_tool(relative):
    source = (ROOT / relative).read_text("utf-8")
    for name in ("backport.mjs", "manifest.json", "braces-3.0.3-depth-limit.patch"):
        assert f"COPY container-security/braces-backport/{name} " in source
    ci = source.index("npm ci")
    apply = source.index(f"node {HELPER} apply --project-root .", ci)
    verify = source.index(f"node {HELPER} verify --project-root . --scope full", apply)
    tool = source.index(
        "npm rebuild node-pty"
        if relative.endswith("Chat/Dockerfile")
        else "node scripts/ensure-critical-native.mjs",
        ci,
    )
    assert ci < apply < verify < tool


def test_shipping_runtime_verifies_absence_without_apply():
    source = (
        (ROOT / "ToukaChat/Dockerfile.prod")
        .read_text("utf-8")
        .split("FROM node-runtime AS runner", 1)[1]
    )
    ci = source.index("npm ci --omit=dev --ignore-scripts")
    assert ci < source.index(f"node {HELPER} verify --project-root . --scope runtime")
    assert f"node {HELPER} apply" not in source

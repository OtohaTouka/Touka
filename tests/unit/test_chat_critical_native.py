"""Critical Linux native installation uses the committed lock and explicit build gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hermetic_gate import npm_offline_gate

ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "ToukaChat"
HELPER = CHAT / "scripts" / "ensure-critical-native.mjs"
LOCK = json.loads((CHAT / "package-lock.json").read_text("utf-8"))


def _node(script: str, tmp_path: Path) -> Any:
    assert HELPER.is_file(), "explicit critical native build helper is required"
    executable = shutil.which("node")
    assert executable, "test runner must prepend pinned Node runtime"
    program = tmp_path / "case.mjs"
    program.write_text(
        "import assert from 'node:assert/strict';\n"
        "import fs from 'node:fs'; import path from 'node:path';\n"
        f"import * as gate from {json.dumps(HELPER.as_uri())};\n"
        f"const lock = {json.dumps(LOCK)};\n" + script,
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()  # noqa: ENV_SSOT_VIOLATION - CLI test boundary
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    result = subprocess.run(  # noqa: S603 - pinned Node and owned synthetic fixture
        [executable, str(program)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_q44_required_projection_keeps_exact_lock_identity_and_drops_optional(tmp_path):
    result = _node("const s = gate.requiredLock(lock); console.log(JSON.stringify(s));", tmp_path)
    name = "lightningcss-linux-x64-gnu"
    original = LOCK["packages"]["node_modules/" + name]
    assert result["name"] == name
    assert result["packageJson"]["dependencies"] == {name: original["version"]}
    selected = result["lockfile"]["packages"]["node_modules/" + name]
    for field in ("version", "resolved", "integrity"):
        assert selected[field] == original[field]
    assert not {"optional", "dev", "devOptional"} & selected.keys()
    assert set(result["lockfile"]["packages"]) == {"", "node_modules/" + name}


@pytest.mark.parametrize(
    "change",
    (
        "delete lock.packages['node_modules/lightningcss-linux-x64-gnu']",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].version='0.0.0'",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].resolved='https://example.invalid/x.tgz'",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].resolved='https://registry.npmjs.org@evil.invalid/x.tgz'",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].integrity='sha512-invalid'",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].cpu=['arm64']",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].os=['win32']",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].dependencies={evil:'*'}",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].link=true",
        "lock.packages['node_modules/lightningcss-linux-x64-gnu'].inBundle=true",
        "lock.packages['node_modules/lightningcss'].optionalDependencies['lightningcss-linux-x64-gnu']='*'",
    ),
)
def test_q44_required_projection_rejects_unlocked_or_unsupported_records(tmp_path, change):
    result = _node(
        change
        + "; let code; try { gate.requiredLock(lock); } catch(e) { code=e.code; }"
        + " assert.equal(code,'NATIVE_LOCK_INVALID'); console.log(JSON.stringify({code}));",
        tmp_path,
    )
    assert result["code"] == "NATIVE_LOCK_INVALID"


@pytest.mark.parametrize("offline", [False, True])
def test_q44_required_install_has_closed_once_only_command(tmp_path, offline):
    result = _node(
        "console.log(JSON.stringify(gate.npmArguments({npmCli:'npm-cli.js',cache:'cache',offline:"
        + json.dumps(offline)
        + "})));",
        tmp_path,
    )
    assert result[:2] == ["npm-cli.js", "ci"]
    assert "--ignore-scripts" in result
    assert "--fetch-retries=0" in result
    assert "--fetch-timeout=30000" in result
    assert ("--offline" in result) is offline
    assert not any(value in result for value in ("install", "--force", "--no-optional"))


@pytest.mark.parametrize(
    "code,expected",
    [
        ("MODULE_NOT_FOUND", "MODULE_NOT_FOUND"),
        ("ERR_DLOPEN_FAILED", "ERR_DLOPEN_FAILED"),
        ("secret-path-token", "UNKNOWN"),
    ],
)
def test_q44_diagnostic_does_not_echo_arbitrary_error_text(tmp_path, code, expected):
    result = _node(
        f"console.log(JSON.stringify({{code:gate.closedNodeCode({{code:{json.dumps(code)},message:'secret'}})}}));",
        tmp_path,
    )
    assert result == {"code": expected}


def _package_fixture(tmp_path: Path, *, native: bool) -> Path:
    project = tmp_path / "project"
    parent = project / "node_modules/lightningcss/node"
    parent.mkdir(parents=True)
    (project / "package.json").write_text('{"private":true}', encoding="utf-8")
    (parent.parent / "package.json").write_text(
        '{"name":"lightningcss","version":"1.31.1","main":"node/index.js"}',
        encoding="utf-8",
    )
    (parent / "index.js").write_text("module.exports = {};", encoding="utf-8")
    if native:
        directory = project / "node_modules/lightningcss-linux-x64-gnu"
        directory.mkdir()
        (directory / "package.json").write_text(
            '{"name":"lightningcss-linux-x64-gnu","version":"1.31.1",'
            '"main":"lightningcss.linux-x64-gnu.node"}',
            encoding="utf-8",
        )
        (directory / "lightningcss.linux-x64-gnu.node").write_bytes(b"synthetic invalid binary")
    return project


def test_q44_probe_distinguishes_absence_from_present_unloadable_binary(tmp_path):
    project = _package_fixture(tmp_path, native=True)
    result = _node(
        f"const project={json.dumps(str(project))}; let code,primary;"
        "try { gate.inspectNative(project,gate.requiredLock(lock)); }"
        " catch(e) { code=e.code;primary=e.primary; }"
        "assert.equal(code,'NATIVE_LOAD_FAILED'); assert.equal(primary,'ERR_DLOPEN_FAILED');"
        "console.log(JSON.stringify({code,primary}));",
        tmp_path,
    )
    assert result["code"] == "NATIVE_LOAD_FAILED"


def test_q44_probe_reports_only_definitely_absent_native_as_missing(tmp_path):
    project = _package_fixture(tmp_path, native=False)
    result = _node(
        f"console.log(JSON.stringify(gate.inspectNative({json.dumps(str(project))},gate.requiredLock(lock))));",
        tmp_path,
    )
    assert result == {"missing": True, "primary": "MODULE_NOT_FOUND"}


def test_q44_probe_rejects_ancestor_module_resolution(tmp_path):
    project = _package_fixture(tmp_path, native=False)
    child = project / "nested"
    child.mkdir()
    (child / "package.json").write_text('{"private":true}', encoding="utf-8")
    result = _node(
        f"let code;try {{gate.inspectNative({json.dumps(str(child))},gate.requiredLock(lock));}}"
        "catch(e){code=e.code;} assert.equal(code,'NATIVE_LAYOUT_INVALID');"
        " console.log(JSON.stringify({code}));",
        tmp_path,
    )
    assert result["code"] == "NATIVE_LAYOUT_INVALID"


def test_q44_probe_rejects_ancestor_native_even_when_local_target_is_missing(tmp_path):
    ancestor = _package_fixture(tmp_path, native=True)
    child = ancestor / "nested"
    parent = child / "node_modules/lightningcss/node"
    parent.mkdir(parents=True)
    (child / "package.json").write_text('{"private":true}', encoding="utf-8")
    for relative in ("package.json", "node/index.js"):
        original = ancestor / "node_modules/lightningcss" / relative
        (parent.parent / relative).write_bytes(original.read_bytes())
    result = _node(
        f"let code;try {{gate.inspectNative({json.dumps(str(child))},gate.requiredLock(lock));}}"
        "catch(e){code=e.code;} assert.equal(code,'NATIVE_LAYOUT_INVALID');"
        " console.log(JSON.stringify({code}));",
        tmp_path,
    )
    assert result["code"] == "NATIVE_LAYOUT_INVALID"


def test_q44_offline_sequence_uses_explicit_node_and_cache(tmp_path):
    npm = tmp_path / "npm/bin/npm-cli.js"
    node = tmp_path / "node"
    cache = tmp_path / "cache"
    commands = npm_offline_gate.npm_command_sequence(
        npm, runtime=False, node_executable=node, cache_dir=cache
    )
    assert [command[1:3] for command in commands] == [
        ["ci", "--offline"],
        ["scripts/ensure-critical-native.mjs", "--npm"],
        ["run", "gen:artifact-runtime"],
        ["run", "build"],
    ]
    assert commands[1] == [
        str(node),
        "scripts/ensure-critical-native.mjs",
        "--npm",
        str(npm),
        "--cache",
        str(cache),
        "--offline",
    ]
    assert npm_offline_gate.npm_command_sequence(npm, runtime=True) == (
        [str(npm), "ci", "--omit=dev", "--offline", "--ignore-scripts"],
    )


def test_q44_offline_proof_maps_actual_execution_to_canonical_argv(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("package-lock.json", ".npmrc"):
        (project / name).write_bytes((CHAT / name).read_bytes())
    npm, node, global_config = (tmp_path / name for name in ("npm", "node", "global.npmrc"))
    for item in (npm, node, global_config):
        item.touch()
    cache, home = tmp_path / "cache-dir", tmp_path / "home"
    cache.mkdir()
    home.mkdir()
    monkeypatch.setattr(
        npm_offline_gate, "build_clean_environment", lambda *a, **k: {"PATH": str(tmp_path)}
    )
    monkeypatch.setattr(npm_offline_gate.shutil, "which", lambda *a, **k: str(node))
    calls = []

    def observe(command, **kwargs):
        calls.append((command, kwargs))
        output = (
            "https://registry.npmjs.org/\n"
            if command[1:] == ["config", "get", "registry"]
            else json.dumps(command)
        )
        return subprocess.CompletedProcess(command, 0, output, "synthetic-stderr")

    monkeypatch.setattr(npm_offline_gate, "_run", observe)
    result = npm_offline_gate.execute_gate(
        SimpleNamespace(
            project=project,
            npm=npm,
            global_config=global_config,
            cache=cache,
            home=home,
            validate_only=False,
            runtime=False,
        )
    )
    assert len(calls) == 6
    actual = calls[3][0]
    assert actual == [
        str(node),
        "scripts/ensure-critical-native.mjs",
        "--npm",
        str(npm),
        "--cache",
        str(cache),
        "--offline",
    ]
    proof = result["commands"][1]
    assert proof["argv"] == [
        "scripts/ensure-critical-native.mjs",
        "--npm",
        "npm",
        "--cache",
        "cache",
        "--offline",
    ]
    assert proof["stdout_sha256"] == hashlib.sha256(json.dumps(actual).encode()).hexdigest()
    assert proof["stderr_sha256"] == hashlib.sha256(b"synthetic-stderr").hexdigest()


def test_q44_materialization_never_overwrites_existing_target(tmp_path):
    source = tmp_path / "scratch-package"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "owned-by-other"
    marker.write_bytes(b"retain")
    result = _node(
        f"let code;try {{gate.materializeNative({json.dumps(str(source))},"
        f"{json.dumps(str(target))},gate.requiredLock(lock));}}"
        "catch(e){code=e.code;} assert.equal(code,'NATIVE_TARGET_EXISTS');"
        " console.log(JSON.stringify({code}));",
        tmp_path,
    )
    assert result["code"] == "NATIVE_TARGET_EXISTS"
    assert marker.read_bytes() == b"retain"


@pytest.mark.parametrize("extra", [False, True])
def test_q44_scratch_contains_only_native_and_optional_hidden_lock(tmp_path, extra):
    scratch = tmp_path / "scratch"
    modules = scratch / "node_modules"
    (modules / "lightningcss-linux-x64-gnu").mkdir(parents=True)
    (modules / ".package-lock.json").write_text("{}", encoding="utf-8")
    if extra:
        (modules / "unreviewed-package").mkdir()
    result = _node(
        f"let code=null;try {{gate.validateScratchPackages({json.dumps(str(scratch))},"
        "gate.requiredLock(lock));}catch(e){code=e.code;}"
        "console.log(JSON.stringify({code}));",
        tmp_path,
    )
    assert result["code"] == ("NATIVE_SCRATCH_LAYOUT_INVALID" if extra else None)


@pytest.mark.parametrize("failure", ["none", "timeout", "stderr", "wrong-result"])
def test_q44_fresh_verifier_is_once_only_closed_and_never_repairs(tmp_path, failure):
    result = _node(
        f"const failure={json.dumps(failure)};const calls=[];let code=null,primary=null;"
        "process.env.Q44_SECRET='never-copy'; process.env.npm_config_proxy='never-copy';"
        "const execute=(exe,args,options)=>{calls.push({exe,args,options});"
        "if(failure==='timeout')throw Object.assign(new Error('private'),{code:'ETIMEDOUT'});"
        "if(failure==='stderr')throw Object.assign(new Error('private'),"
        "{status:1,stderr:'private'});"
        "return Buffer.from(JSON.stringify({schema_version:1,status:'PASS',"
        "phase:failure==='wrong-result'?'other':'native-readiness',"
        "package:'lightningcss-linux-x64-gnu',version:'1.31.1',repaired:false,offline:true}));};"
        "try{gate.verifyInstalled({project:process.cwd(),npm:'npm-cli.js',"
        "cache:'cache',offline:true},gate.requiredLock(lock),execute);}"
        "catch(e){code=e.code;primary=e.primary;}"
        "console.log(JSON.stringify({code,primary,calls}));",
        tmp_path,
    )
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert "--verify-only" in call["args"] and "ci" not in call["args"]
    assert call["options"]["timeout"] == 30000
    assert call["options"]["maxBuffer"] == 1024 * 1024
    assert not {"Q44_SECRET", "npm_config_proxy"} & call["options"]["env"].keys()
    assert result["code"] == (None if failure == "none" else "NATIVE_POST_INSTALL_VERIFY_FAILED")
    if failure == "timeout":
        assert result["primary"] == "ETIMEDOUT"


def test_q44_product_and_offline_call_same_helper_before_build():
    production = (CHAT / "Dockerfile.prod").read_text("utf-8")
    helper = "ensure-critical-native.mjs"
    assert helper in production
    assert production.index("RUN npm ci") < production.index("RUN node scripts/" + helper)
    assert production.index("RUN node scripts/" + helper) < production.index("RUN npm run gen:")
    gate = (ROOT / "scripts/hermetic_gate/npm_offline_gate.py").read_text("utf-8")
    assert helper in gate and '"--offline"' in gate


def _offline_gate_case(tmp_path, monkeypatch, *, runtime, missing_node=False):
    """Mock the gate's host side so execute_gate runs its real command order."""
    project = tmp_path / "project"
    project.mkdir()
    npm, node, config = (tmp_path / name for name in ("npm", "node", "global.npmrc"))
    for file in (npm, node, config):
        file.touch()
    for name in ("cache", "home"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(npm_offline_gate, "load_npm_policy", lambda root: None)
    monkeypatch.setattr(npm_offline_gate, "validate_lockfile", lambda root: None)
    monkeypatch.setattr(
        npm_offline_gate, "build_clean_environment", lambda *a, **k: {"PATH": str(tmp_path)}
    )
    monkeypatch.setattr(
        npm_offline_gate,
        "shutil",
        SimpleNamespace(which=lambda *a, **k: None if missing_node else str(node)),
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        stdout = (
            "https://registry.npmjs.org/\n"
            if command[1:4] == ["config", "get", "registry"]
            else "fixture"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(npm_offline_gate, "_run", run)
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
    args, calls, node = _offline_gate_case(tmp_path, monkeypatch, runtime=runtime)
    proof = npm_offline_gate.execute_gate(args)
    commands = npm_offline_gate.npm_command_sequence(Path("npm"), runtime=runtime)
    assert len(proof["commands"]) == (1 if runtime else 4)
    assert [item["argv"] for item in proof["commands"]] == [command[1:] for command in commands]
    if not runtime:
        native = next(
            command for command, _ in calls if "scripts/ensure-critical-native.mjs" in command
        )
        assert native[0] == str(node)
    assert all(options["cwd"] == args.project for _, options in calls)


def test_runtime_requires_resolved_node(tmp_path, monkeypatch):
    args, calls, _ = _offline_gate_case(tmp_path, monkeypatch, runtime=True, missing_node=True)
    with pytest.raises(npm_offline_gate.NpmPolicyError, match="NODE_EXECUTABLE_REQUIRED"):
        npm_offline_gate.execute_gate(args)
    assert not any("ci" in command for command, _ in calls)

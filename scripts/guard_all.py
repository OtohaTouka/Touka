# ruff: noqa: T201, S602, S603, S607
"""Touka 架構大一統守衛聚合器（43 道守衛的單一執行入口）。

逐道執行 lint_ssot、前端 npm 守衛、依賴、死碼、ADR 完整性 等關卡；每道有逾時保護
（DEFAULT_GUARD_TIMEOUT_SECONDS），逾時即殺進程樹。任一失敗即彙整報告並非零退出。
--non-audit 僅略過兩道需獨立執行的 CVE audit，明示完整 43 道尚未完成。
--audit-evidence-root 保留兩道 CVE audit 的原始證據於全新的專案外目錄。
"""

import argparse
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from dependency_audit_evidence import create_evidence_root

# 每道守衛的逾時上限（秒）。防止網路型守衛（pip-audit / npm audit 等）在受限
# 網路環境下無限期卡死整個套件——逾時即殺整棵進程樹、標記失敗、繼續下一道。
# 個別任務可在其 dict 加 "timeout" 鍵覆寫此預設。
DEFAULT_GUARD_TIMEOUT_SECONDS = 300
NODE_USER_PROFILE_PROBE = (
    "node -e \"const info=require('node:os').userInfo();"
    'if(!info.username||!info.homedir)process.exit(1)"'
)


class FrontendRuntimeConfigError(TypeError):
    """Raised when the committed frontend runtime SSOT has an invalid shape."""


def _kill_process_tree(proc):
    """殺掉 proc 及其所有子孫進程（shell=True 多一層 shell，需連子孫一起殺）。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run_command(
    command: str | Sequence[str],
    cwd: str | Path | None = None,
    name: str = "Task",
    timeout: int = DEFAULT_GUARD_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """執行單一守衛指令；逾時即殺整棵進程樹。回傳 (成功, 輸出)。"""
    print(f"[Run] {name}...")
    # 開新進程群組，逾時時才能連同 shell 子孫一起終止。
    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=isinstance(command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            **popen_kwargs,
        )
    except Exception as e:
        print(f"[ERROR] Error running {name}: {e}")
        return False, str(e)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        proc.communicate()
        print(f"[TIMEOUT] {name} exceeded {timeout}s; process tree killed.")
        msg = (
            f"Guard '{name}' timed out after {timeout}s and was killed. "
            "可能卡在網路呼叫（pip-audit / npm audit 等）；"
            "可在該任務 dict 加 'timeout' 鍵調整。"
        )
        return False, msg

    if proc.returncode == 0:
        print(f"[OK] {name} passed.")
        return True, stdout
    print(f"[FAIL] {name} failed.")
    return False, stdout + stderr


def _load_frontend_runtime_requirements(frontend_dir: Path) -> tuple[str, str]:
    """Read exact Node/npm versions from the frontend package SSOT."""
    package_path = frontend_dir / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    engines = package.get("engines")
    if not isinstance(engines, dict):
        raise FrontendRuntimeConfigError

    node_version = engines.get("node")
    npm_version = engines.get("npm")
    if not isinstance(node_version, str) or not isinstance(npm_version, str):
        raise FrontendRuntimeConfigError
    return node_version, npm_version


def run_frontend_runtime_preflight(
    frontend_dir: Path,
    *,
    command_runner: Callable[..., tuple[bool, str]] = run_command,
) -> tuple[bool, str]:
    """Fail before guard execution when ambient Node/npm drift from package.json."""
    try:
        expected_node, expected_npm = _load_frontend_runtime_requirements(frontend_dir)
    except (OSError, json.JSONDecodeError, FrontendRuntimeConfigError) as exc:
        diagnostic = str(exc) or type(exc).__name__
        return False, f"Frontend runtime SSOT is invalid: {diagnostic}"

    observations = (
        ("Node", "node --version", expected_node, "v"),
        ("npm", "npm --version", expected_npm, ""),
    )
    failures: list[str] = []
    actual_versions: dict[str, str] = {}
    for label, command, expected, prefix in observations:
        success, output = command_runner(
            command,
            cwd=frontend_dir,
            name=f"Frontend {label} Runtime Preflight",
        )
        actual = output.strip()
        if prefix and actual.startswith(prefix):
            actual = actual[len(prefix) :]
        actual_versions[label] = actual or "<unavailable>"
        if not success:
            failures.append(f"{label} version command failed: {output.strip()}")
        elif actual != expected:
            failures.append(f"{label} expected={expected} actual={actual}")

    if failures:
        return False, "; ".join(failures)

    profile_ok, profile_output = command_runner(
        NODE_USER_PROFILE_PROBE,
        cwd=frontend_dir,
        name="Frontend Node User-Profile Capability Preflight",
    )
    if not profile_ok:
        diagnostic = profile_output.strip() or "<no diagnostic>"
        return False, f"Node user-profile capability failed: {diagnostic}"
    return True, f"Node {actual_versions['Node']} / npm {actual_versions['npm']}"


def main(argv: Sequence[str] = ()) -> None:
    """依序執行所有守衛任務，輸出彙整報告，任一失敗則非零退出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--non-audit",
        action="store_true",
        help="Run 41 non-audit guards; report CVE guards #22/#23 as NOT RUN.",
    )
    parser.add_argument(
        "--dependency-cache-only",
        action="store_true",
        help="Require existing dependency caches for Guard #16; never install or rebuild them.",
    )
    parser.add_argument(
        "--audit-evidence-root",
        type=Path,
        help=(
            "Retain audit receipts in a new absolute directory outside the repository; "
            "ignored with --non-audit."
        ),
    )
    options = parser.parse_args(argv)
    # 強制 stdout 使用 UTF-8 避免 Windows 上的 UnicodeEncodeError
    if (sys.stdout.encoding or "").lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    root_dir = Path(__file__).parent.parent.absolute()
    frontend_dir = root_dir / "ToukaChat"

    runtime_ok, runtime_detail = run_frontend_runtime_preflight(frontend_dir)
    if not runtime_ok:
        print("[FAIL] Frontend Runtime Preflight failed.")
        print(runtime_detail)
        print("System check BLOCKED before guard execution.")
        print("Full 43-guard suite: INCOMPLETE")
        sys.exit(1)
    print(f"[OK] Frontend Runtime Preflight passed: {runtime_detail}")

    retained_root = None
    if options.audit_evidence_root is not None and not options.non_audit:
        try:
            retained_root = create_evidence_root(options.audit_evidence_root, root_dir)
        except (OSError, ValueError) as exc:
            print(f"[FAIL] Audit evidence root could not be created: {exc}")
            print("System check BLOCKED before guard execution.")
            print("Full 43-guard suite: INCOMPLETE")
            sys.exit(1)

    tasks = [
        {
            "name": "Backend SSOT Guard (lint_ssot.py) [Guard #1]",
            "command": "python scripts/lint_ssot.py",
            "cwd": root_dir,
        },
        {
            "name": "Frontend API Parity Guard [Guard #2]",
            "command": "npm run guard:api",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Time SSOT Guard [Guard #3]",
            "command": "npm run guard:time",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Design Token Guard [Guard #4]",
            "command": "npm run guard:design",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Theme Guard [Guard #5]",
            "command": "npm run guard:theme",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Hook Sovereignty Guard [Guard #6]",
            "command": "npm run guard:hooks",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Import Integrity Guard [Guard #7]",
            "command": "npm run guard:integrity",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend CSS Integrity Guard [Guard #8]",
            "command": "npm run guard:css",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Error Guard [Guard #9]",
            "command": "npm run guard:errors",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Env Guard [Guard #10]",
            "command": "npm run guard:env",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Fetch SSOT Guard [Guard #11]",
            "command": "npm run guard:fetch",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend localStorage SSOT Guard [Guard #12]",
            "command": "npm run guard:localStorage",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend X-Agent-ID Identity Guard [Guard #13]",
            "command": "npm run guard:xagentid",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend EventSource Token Guard [Guard #14]",
            "command": "npm run guard:eventSource",
            "cwd": frontend_dir,
        },
        {
            "name": "Frontend Console Ban Guard [Guard #15]",
            "command": "npm run guard:console",
            "cwd": frontend_dir,
        },
        {
            "name": "Backend Dependency Guard [Guard #16]",
            "command": "python scripts/guard_dependencies.py"
            + (" --cache-only" if options.dependency_cache_only else ""),
            "cwd": root_dir,
        },
        {
            "name": "Backend API Sync Guard [Guard #17]",
            "command": [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "tests/integration/test_api_schema_sync.py",
            ],
            "cwd": root_dir,
        },
        {
            "name": "Backend Error Sync Guard [Guard #18]",
            "command": "python scripts/sync_errors.py",
            "cwd": root_dir,
        },
        {
            "name": "Backend NOQA Registry Sync Guard [Guard #19]",
            "command": "python scripts/guard_noqa_registry.py",
            "cwd": root_dir,
        },
        {
            "name": "Dead Code Guard (vulture + knip) [Guard #20]",
            "command": "python scripts/guard_dead_code.py",
            "cwd": root_dir,
        },
        {
            "name": "Module Boundary Guard (import-linter) [Guard #21]",
            "command": "python scripts/guard_module_boundaries.py",
            "cwd": root_dir,
        },
        {
            "name": "Python CVE Guard (pip-audit) [Guard #22]",
            "command": "python scripts/guard_python_cve.py",
            "cwd": root_dir,
            "requires_audit": True,
            "audit_evidence_name": "python",
        },
        {
            "name": "Node CVE Guard (Chat + IDE, runtime + full) [Guard #23]",
            "command": "python scripts/guard_node_cve.py",
            "cwd": root_dir,
            "requires_audit": True,
            "audit_evidence_name": "node",
        },
        {
            "name": "Endpoint Auth Dependency Guard [Guard #24]",
            "command": "python scripts/guard_endpoint_auth.py",
            "cwd": root_dir,
        },
        {
            "name": "Native NPM Build Environment Guard [Guard #25]",
            "command": "python scripts/guard_native_npm.py",
            "cwd": root_dir,
        },
        {
            "name": "npm Script Docker Path Guard [Guard #26]",
            "command": "python scripts/guard_npm_docker_paths.py",
            "cwd": root_dir,
        },
        {
            "name": "Frontend TypeScript Type Check Guard [Guard #27]",
            "command": "npm run guard:typecheck",
            "cwd": frontend_dir,
        },
        {
            "name": "ADR Integrity Guard (guard_adr_integrity.py) [Guard #28]",
            "command": "python scripts/guard_adr_integrity.py",
            "cwd": root_dir,
        },
        {
            "name": "Creation Sovereignty Guard [Guard #29]",
            "command": "python scripts/guard_creation_sovereignty.py",
            "cwd": root_dir,
        },
        {
            "name": "Environment Parity Guard [Guard #30]",
            "command": "python scripts/guard_environment_parity.py",
            "cwd": root_dir,
        },
        {
            "name": "Frontend Orphaned Tests Guard [Guard #31]",
            "command": "npm run guard:orphaned-tests",
            "cwd": frontend_dir,
        },
        {
            "name": "Permissions Registry Guard [Guard #32]",
            "command": "python scripts/guard_permissions_registry.py",
            "cwd": root_dir,
        },
        {
            "name": "Frontend i18n Key-Sync Guard [Guard #33]",
            "command": "npm run guard:i18n",
            "cwd": frontend_dir,
        },
        {
            "name": "Config Example Schema Guard [Guard #34]",
            "command": [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "tests/unit/test_config_examples.py",
                "-q",
            ],
            "cwd": root_dir,
        },
        {
            "name": "ESLint Ratchet Guard [Guard #35]",
            "command": "python scripts/guard_eslint_ratchet.py",
            "cwd": root_dir,
        },
        {
            "name": "Provider Parity Guard [Guard #36]",
            "command": "python scripts/guard_provider_parity.py",
            "cwd": root_dir,
        },
        {
            "name": "Skill MCP Consistency Guard [Guard #37]",
            "command": "python scripts/guard_skill_mcp_consistency.py",
            "cwd": root_dir,
        },
        {
            "name": "Tool-Event Envelope Guard [Guard #38]",
            "command": "python scripts/guard_tool_events.py",
            "cwd": root_dir,
        },
        {
            "name": "Context Snapshot Anti-Drift Guard [Guard #39]",
            "command": "python scripts/guard_context_snapshot.py",
            "cwd": root_dir,
        },
        {
            "name": "Harness Context Anti-Drift Guard [Guard #40]",
            "command": "python scripts/guard_harness_context.py",
            "cwd": root_dir,
        },
        {
            "name": "Role Plane Ratchet Guard [Guard #41]",
            "command": "python scripts/guard_role_plane.py",
            "cwd": root_dir,
        },
        # Guard #42（Release Workflow Gate）與 #43（ghcr Compose Pull-Purity）已退役；
        # 編號為穩定識別碼，不重排。
        {
            "name": "Ruff Banned-API Guard [Guard #44]",
            "command": "python scripts/guard_ruff_banned_api.py",
            "cwd": root_dir,
        },
        {
            "name": "Native Tool Contract Guard [Guard #45]",
            "command": "python -B scripts/guard_native_tool_contracts.py",
            "cwd": root_dir,
        },
    ]

    print("=" * 60)
    print("Touka Architectural Grand Unification Guard")
    print("=" * 60)

    results = []
    any_failed = False

    omitted = [task for task in tasks if options.non_audit and task.get("requires_audit")]
    selected = [task for task in tasks if task not in omitted]
    for task in selected:
        command = task["command"]
        if retained_root is not None and task.get("requires_audit"):
            audit_name = task["audit_evidence_name"]
            command = [
                sys.executable,
                "-B",
                str(root_dir / "scripts" / f"guard_{audit_name}_cve.py"),
                "--evidence-dir",
                str(retained_root / audit_name),
            ]
        success, output = run_command(
            command,
            cwd=task["cwd"],
            name=task["name"],
            timeout=task.get("timeout", DEFAULT_GUARD_TIMEOUT_SECONDS),
        )
        results.append({"name": task["name"], "success": success, "output": output})
        if not success:
            any_failed = True

    print("\n" + "=" * 60)
    print("Summary Report")
    print("-" * 60)
    for res in results:
        status = "PASS" if res["success"] else "FAIL"
        print(f"[{status}] | {res['name']}")
    for task in omitted:
        print(f"[NOT RUN] | {task['name']} (--non-audit)")
    if omitted or any_failed:
        print(f"Full {len(tasks)}-guard suite: INCOMPLETE")

    if any_failed:
        print("-" * 60)
        print("Detailed Error Logs:")
        for res in results:
            if not res["success"]:
                print(f"\n[{res['name']} Errors]:")
                print(res["output"])
        print("=" * 60)
        print("System check FAILED. Please fix architectural violations.")
        sys.exit(1)
    else:
        print("=" * 60)
        if omitted:
            print(f"Selected guards PASSED ({len(results)}/{len(selected)}).")
        else:
            print("All architectural checks PASSED! Touka is solid.")
        sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])

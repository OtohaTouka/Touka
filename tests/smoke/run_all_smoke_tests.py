"""Manifest-driven standalone smoke controller (ADR-0214)."""

from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from lib.sandbox_env import build_smoke_environment
from manifest import AUTOMATED_SANDBOXED, MANUAL_LIVE_SPECS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from manifest import SmokeSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_TESTS_DIR = Path(__file__).parent
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
SMOKE_TEST_TIMEOUT_SECONDS = 120
_PRODUCT_IMPORT_PREFIXES = ("src", "memory_server", "ToukaHeartbeat", "awaken")
_RUNNER_PREFIX = "touka_smoke_runner_"
_CLEANUP_ATTEMPTS = 3
_CLEANUP_RETRY_SECONDS = 0.1


class SmokeResult(TypedDict):
    name: str
    success: bool
    duration: float
    output: str


class SmokeRunnerError(RuntimeError):
    """The manifest, sandbox, or cleanup contract is invalid."""


def automated_specs() -> tuple[SmokeSpec, ...]:
    """Return only the explicitly free, sandboxed automatic suite."""
    return AUTOMATED_SANDBOXED


def _import_name(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    return node.names[0].name


def _validate_script_preflight(spec: SmokeSpec) -> Path:
    if spec.execution_class != "AUTOMATED_SANDBOXED" or spec.metered_api:
        message = f"Unsafe manifest entry in automated suite: {spec}"
        raise SmokeRunnerError(message)
    path = SMOKE_TESTS_DIR / spec.path
    if not path.is_file():
        message = f"Manifest smoke script does not exist: {spec.path}"
        raise SmokeRunnerError(message)

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    product_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and _import_name(node).startswith(_PRODUCT_IMPORT_PREFIXES)
    ]
    sandbox_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "enable_smoke_sandbox"
    ]
    if not product_lines or not sandbox_lines or min(sandbox_lines) >= min(product_lines):
        message = f"{spec.path}: enable_smoke_sandbox() must precede every product import"
        raise SmokeRunnerError(message)
    if any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_dotenv"
        for node in ast.walk(tree)
    ):
        message = f"{spec.path}: automated smoke must not load a repository .env"
        raise SmokeRunnerError(message)
    return path


def _validate_manifest_completeness() -> None:
    discovered = {
        path.name
        for path in SMOKE_TESTS_DIR.iterdir()
        if path.is_file() and path.name.startswith("smoke_") and path.suffix == ".py"
    }
    classified = {spec.path for spec in AUTOMATED_SANDBOXED}
    if discovered != classified:
        missing = sorted(discovered - classified)
        stale = sorted(classified - discovered)
        message = f"Smoke manifest mismatch: unclassified={missing}, missing={stale}"
        raise SmokeRunnerError(message)


def _remove_sandbox(path: Path) -> None:
    root = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        root == temp_root
        or not root.is_relative_to(temp_root)
        or not root.name.startswith(_RUNNER_PREFIX)
    ):
        message = f"Refusing to remove unexpected smoke path: {root}"
        raise SmokeRunnerError(message)
    for attempt in range(_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(root)
        except OSError:
            if attempt == _CLEANUP_ATTEMPTS - 1:
                raise
            time.sleep(_CLEANUP_RETRY_SECONDS)
        else:
            break


@contextmanager
def smoke_child_environment(test_name: str) -> Iterator[dict[str, str]]:
    """Yield one sandboxed, credential-scrubbed child environment with strict cleanup."""
    safe_name = Path(test_name).stem.replace("smoke_", "")
    root = Path(tempfile.mkdtemp(prefix=f"{_RUNNER_PREFIX}{safe_name}_")).resolve()
    child_env = build_smoke_environment(root, os.environ, owner="runner")
    child_env["PYTHONPATH"] = os.pathsep.join(
        (
            str(PROXY_DIR),
            str(MEMORY_SRC_DIR),
            str(SMOKE_TESTS_DIR),
            child_env.get("PYTHONPATH", ""),
        )
    )
    try:
        yield child_env
    finally:
        try:
            _remove_sandbox(root)
        except Exception as exc:
            message = f"Smoke sandbox cleanup failed for {root}: {exc}"
            raise SmokeRunnerError(message) from exc
        if root.exists():
            message = f"Smoke sandbox cleanup left residue: {root}"
            raise SmokeRunnerError(message)


def _run_one(spec: SmokeSpec) -> SmokeResult:
    path = _validate_script_preflight(spec)
    start = time.monotonic()
    try:
        with smoke_child_environment(spec.path) as child_env:
            process = subprocess.run(  # noqa: S603
                [sys.executable, str(path)],
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=SMOKE_TEST_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        return {
            "name": spec.path,
            "success": False,
            "duration": time.monotonic() - start,
            "output": f"timeout after {SMOKE_TEST_TIMEOUT_SECONDS}s: {exc}",
        }
    except Exception as exc:
        return {
            "name": spec.path,
            "success": False,
            "duration": time.monotonic() - start,
            "output": f"{type(exc).__name__}: {exc}",
        }
    else:
        duration = time.monotonic() - start
        success = process.returncode == 0
        output = process.stdout if success else f"{process.stderr}\n{process.stdout}"
        return {
            "name": spec.path,
            "success": success,
            "duration": duration,
            "output": output,
        }


def run_all_tests() -> int:
    """Execute only manifest-classified automatic smokes and return a process code."""
    print("[*] Touka automated sandboxed smoke suite starting...")  # noqa: T201
    print(f"[*] Test Directory: {SMOKE_TESTS_DIR}")  # noqa: T201
    print("=" * 72)  # noqa: T201
    _validate_manifest_completeness()
    for spec in MANUAL_LIVE_SPECS:
        print(  # noqa: T201
            f"[NOT RUN — HUMAN APPROVAL REQUIRED] {spec.path} "
            f"(provider={spec.provider_surface}, metered_api={spec.metered_api})"
        )

    results: list[SmokeResult] = []
    total_start = time.monotonic()
    for spec in automated_specs():
        print(f"\n[RUNNING] {spec.path}...")  # noqa: T201
        result = _run_one(spec)
        results.append(result)
        tag = "PASS" if result["success"] else "FAIL"
        print(f"[{tag}] {spec.path} ({result['duration']:.2f}s)")  # noqa: T201
        if not result["success"]:
            print(result["output"][-1000:])  # noqa: T201

    total_duration = time.monotonic() - total_start
    success_count = sum(result["success"] for result in results)
    fail_count = len(results) - success_count
    print("\n" + "=" * 72)  # noqa: T201
    print("SUMMARY - Automated Sandboxed Smoke Suite")  # noqa: T201
    for result in results:
        tag = "OK" if result["success"] else "FAIL"
        print(f"[{tag}] {result['name']:<35} | {result['duration']:>6.2f}s")  # noqa: T201
    print(  # noqa: T201
        f"Total: {len(results)} | Success: {success_count} | Failed: {fail_count} | "
        f"Duration: {total_duration:.2f}s"
    )
    print("Manual live probes remain NOT RUN — HUMAN APPROVAL REQUIRED.")  # noqa: T201
    return 1 if fail_count else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    raise SystemExit(run_all_tests())

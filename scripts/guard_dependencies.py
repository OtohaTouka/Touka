# ruff: noqa: T201, S603, S607, TID251
"""Touka 依賴守衛 (Dependency Guard).

本腳本透過建立隔離的虛擬環境 (Sandbox) 來驗證各 workspace 成員於單一 uv.lock 下的
依賴完整性（ADR-0117）：以 ``uv sync --frozen --no-dev --package <member>`` 佈建
per-member 沙盒 venv 後執行匯入測試——若成員 pyproject 漏列直接依賴（只靠轉依賴
碰巧存在），匯入即失敗。具備緩存機制，僅在 uv.lock 或成員 pyproject 變動時重建。
快取放在 git common dir，所有 worktree 共用，並以成員鎖序列化（ADR-0240）。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pytest_residue_safety import scan_pytest_residue

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# --- 配置 ---
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
SMOKE_SANDBOX_DIR = PROJECT_ROOT / "tests" / "smoke" / "lib"
# 測試覆寫點；None 時於執行期解析（ADR-0240）。
CACHE_DIR: Path | None = None
CACHE_ROOT_NAME = "touka-guard-cache"
GIT_ROOT_QUERY = ("rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
# 沒有 git 時的退回位置（ADR-0240 之前的唯一位置）。
LEGACY_CACHE_DIR = PROJECT_ROOT / "scripts" / ".cache" / "guards"
# 改變 uv sync 配方（argv 以外的建置方式）時遞增，讓所有共用快取失效（ADR-0240）。
CACHE_KEY_SCHEMA = 1
# 每個成員各自計時（不是整次執行）；guard_all 對整個 Guard #16 只給 300 秒，
# 可能在印出原因前就殺掉整次執行，結果同樣是 FAIL。
CACHE_LOCK_TIMEOUT_SECONDS = 240.0
CACHE_LOCK_POLL_SECONDS = 0.5
UV_LOCK = PROJECT_ROOT / "uv.lock"
TARGET_PROJECTS = [
    {
        "name": "ToukaProxy",
        "package": "toukaproxy",
        "path": PROJECT_ROOT / "ToukaProxy",
        "import_test": (
            "import sys; from unittest.mock import MagicMock; "
            "sys.modules['audioop'] = MagicMock(); "
            "from src.models.user import UserBase; "
            "from pydantic import TypeAdapter; "
            "TypeAdapter(UserBase); "  # Force eager evaluation of Pydantic core schema
            "import src.main; "
            "print('ToukaProxy Import OK')"
        ),
    },
    {
        "name": "ToukaHeartbeat",
        "package": "toukaheartbeat",
        "path": PROJECT_ROOT / "ToukaHeartbeat",
        "import_test": "import awaken; print('ToukaHeartbeat Import OK')",
    },
    {
        "name": "ToukaMemory",
        "package": "toukamemory",
        "path": PROJECT_ROOT / "ToukaMemory",
        "import_test": "import src.memory_server; print('ToukaMemory Import OK')",
    },
    {
        "name": "ToukaDispatch",
        "package": "toukadispatch",
        "path": PROJECT_ROOT / "ToukaDispatch",
        # 深層鏈測試：觸發 MissionManager → MissionLock → filelock 完整呼叫鏈
        # 若 ToukaDispatch/pyproject.toml 缺少 filelock，此處即報錯（不再淺層放行）
        "import_test": (
            "from memory_server.services.mission_lock import MissionLock; "
            "from session_manager import SessionManager; "
            "print('ToukaDispatch Import OK')"
        ),
    },
]


def run_command(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """執行命令並回傳結果."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def resolve_cache_root(project_root: Path) -> Path:
    """回傳 git common dir 下、依平台分開的共用快取根；否則退回舊位置（ADR-0240）."""
    try:
        res = run_command(["git", "-C", str(project_root), *GIT_ROOT_QUERY], cwd=project_root)
    except OSError as exc:
        return _legacy_cache_root(f"git could not run ({type(exc).__name__})")
    answer = res.stdout.strip().splitlines()
    if res.returncode != 0 or len(answer) != 2:
        detail = res.stderr.strip().splitlines()[:1] or [f"exit code {res.returncode}"]
        return _legacy_cache_root(f"no git common dir ({detail[0]})")
    toplevel, common_dir = (Path(line.strip()) for line in answer)
    if toplevel.resolve() != project_root.resolve():
        return _legacy_cache_root("checkout is not the git toplevel")
    if not common_dir.is_dir():
        return _legacy_cache_root("git common dir is not a directory")
    return common_dir / CACHE_ROOT_NAME / sys.platform


def _legacy_cache_root(reason: str) -> Path:
    print(f"[Cache] [Guard] {reason}; falling back to legacy cache {LEGACY_CACHE_DIR}")
    return LEGACY_CACHE_DIR


def _cache_root() -> Path:
    return CACHE_DIR if CACHE_DIR is not None else resolve_cache_root(PROJECT_ROOT)


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _member_cache_lock(cache_root: Path, name: str, *, create: bool) -> Iterator[bool]:
    """持有成員快取的 OS 鎖；逾時仍被占用時 yield False.

    鎖檔在成員目錄外，重建時的 rmtree 不會刪到它；行程結束時 OS 自動釋放。
    ``create=False``（cache-only）不建立任何路徑：鎖檔不存在就不上鎖，照舊因快取缺失而 FAIL。
    """
    lock_path = cache_root / f"{name}.lock"
    if create:
        cache_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    else:
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            yield True
            return
    try:
        deadline = time.monotonic() + CACHE_LOCK_TIMEOUT_SECONDS
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(CACHE_LOCK_POLL_SECONDS)
        try:
            yield True
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def verify_dockerfile_parity(project_path: Path) -> bool:
    """驗證 Dockerfile 是否包含了目錄下的所有本地 Python 模組。"""
    dockerfile = project_path / "Dockerfile"
    if not dockerfile.exists():
        return True  # 無 Dockerfile 則跳過

    content = dockerfile.read_text(encoding="utf-8")

    # 若使用通用 COPY . .（可帶 --chown 旗標，ADR-0117）則視為安全 (SSOT)
    if re.search(r"COPY(\s+--chown=\S+)?\s+\.\s+\./?\s*$", content, re.MULTILINE):
        return True

    # root-context 變體（ADR-0117）：`COPY [--chown] <本專案目錄>/ .` 同為「整目錄、非逐檔挑選」，
    # 物理完整性保證與 COPY . . 等價（如 heartbeat 於 repo 根 context build、佈局維持 /app）。
    if re.search(rf"COPY(\s+--chown=\S+)?\s+{re.escape(project_path.name)}/\s+\.", content):
        return True

    # 否則，檢查所有 .py 檔案是否都被單獨 COPY
    py_files = [f.name for f in project_path.glob("*.py") if f.name != "setup.py"]
    missing_files = []
    for py_file in py_files:
        if py_file not in content:
            missing_files.append(py_file)

    if missing_files:
        print(f"[FAIL] [Guard] Dockerfile parity mismatch in {project_path.name}!")
        print(f"       Missing COPY for: {', '.join(missing_files)}")
        print("       Recommendation: Use 'COPY . .' or update Dockerfile explicitly.")
        return False

    return True


def check_project_dependencies(
    project: dict, *, cache_only: bool = False, cache_root: Path | None = None
) -> bool:
    """檢查單一 workspace 成員的依賴完整性（per-member ``uv sync`` 沙盒）."""
    name = project["name"]
    member_pyproject = project["path"] / "pyproject.toml"

    print(f"\n[Scan] [Guard] Checking dependencies for {name}...")

    if not member_pyproject.exists() or not UV_LOCK.exists():
        print(f"[FAIL] [Guard] {name}: 缺 pyproject.toml 或 uv.lock（ADR-0117 workspace）。")
        return False

    root = _cache_root() if cache_root is None else cache_root
    # 鎖住整段（含匯入測試），其他 worktree 不能在途中重建這個 venv（ADR-0240）
    with _member_cache_lock(root, name, create=not cache_only) as ready:
        if not ready:
            print(
                f"[FAIL] [Guard] {name}: another Guard #16 run holds the cache lock "
                f"(waited {CACHE_LOCK_TIMEOUT_SECONDS:g}s); rerun after it finishes."
            )
            return False
        return _check_member_sandbox(project, root / name, cache_only=cache_only)


def _sync_argv(project: dict) -> list[str]:
    # --frozen：純照 uv.lock 安裝、不驗 pyproject 一致（那是 Guard #30 的職責）
    return ["uv", "sync", "--frozen", "--no-dev", "--package", project["package"]]


def _cache_key(project: dict, sync_argv: list[str]) -> str:
    """快取鍵：建置配方（schema／argv／平台）＋ .python-version ＋ uv.lock ＋ 成員 pyproject."""
    recipe = {"schema": CACHE_KEY_SCHEMA, "argv": sync_argv, "platform": sys.platform}
    python_version = PROJECT_ROOT / ".python-version"
    pinned = python_version.read_bytes() if python_version.is_file() else b""
    member_pyproject = project["path"] / "pyproject.toml"
    material = json.dumps(recipe).encode() + pinned + UV_LOCK.read_bytes()
    return hashlib.sha256(material + member_pyproject.read_bytes()).hexdigest()


def _check_member_sandbox(project: dict, project_cache_dir: Path, *, cache_only: bool) -> bool:
    name = project["name"]
    path = project["path"]

    # 1. 緩存檢查（配方、Python 版本宣告、uv.lock、成員 pyproject 任一變動即重建）
    sync_argv = _sync_argv(project)
    current_hash = _cache_key(project, sync_argv)
    hash_file = project_cache_dir / "lock.hash"
    venv_dir = project_cache_dir / "venv"

    needs_rebuild = True
    if hash_file.exists() and venv_dir.exists():
        cached_hash = hash_file.read_text().strip()
        if cached_hash == current_hash:
            print(f"[Cache] [Guard] Cache hit for {name} (Hash: {current_hash[:8]})")
            needs_rebuild = False

    # 2. 建立/更新 Venv（uv sync：只裝該成員的鎖定子集；torch 由鎖檔 CPU index 供給）
    if needs_rebuild:
        if cache_only:
            print(f"[FAIL] [Guard] {name}: cache missing or stale; --cache-only forbids sync.")
            return False
        print(f"[Build]  [Guard] Syncing sandbox venv for {name} (uv sync --package)...")
        if project_cache_dir.exists():
            shutil.rmtree(project_cache_dir)
        project_cache_dir.mkdir(parents=True, exist_ok=True)

        sync_env = os.environ.copy()
        sync_env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)
        res = run_command(sync_argv, cwd=PROJECT_ROOT, env=sync_env)
        if res.returncode != 0:
            print(f"[FAIL] [Guard] uv sync failed for {name}:\n{res.stderr}")
            return False

        # 寫入雜湊值
        hash_file.write_text(current_hash)

    # 3. 執行匯入測試
    print(f"[Test] [Guard] Running import integrity check for {name}...")
    if os.name == "nt":
        python_exe = venv_dir / "Scripts" / "python.exe"
    else:
        python_exe = venv_dir / "bin" / "python"

    # 設置 PYTHONPATH 以包含專案根目錄
    env = os.environ.copy()
    # 嚴格防止宿主機的 PYTHONPATH 污染沙盒
    env.pop("PYTHONPATH", None)

    # ToukaProxy 與 ToukaDispatch 均依賴 ToukaMemory 的內部模組 (整合模式)
    if name in ("ToukaProxy", "ToukaDispatch"):
        memory_path = PROJECT_ROOT / "ToukaMemory"
        env["PYTHONPATH"] = (
            str(path)
            + os.pathsep
            + str(path / "src")
            + os.pathsep
            + str(memory_path)
            + os.pathsep
            + str(memory_path / "src")
        )
    else:
        # 其他微服務只能看到自己的目錄
        env["PYTHONPATH"] = str(path)

    # Reuse the project smoke sandbox before any product import. Keep native Popen,
    # the member interpreter, and normal host identity/tool lookup semantics.
    import_code = (
        "import os, sys; "
        f"sys.path.insert(0, {json.dumps(str(SMOKE_SANDBOX_DIR))}); "
        "from sandbox_env import enable_smoke_sandbox, SMOKE_ROOT_ENV, SMOKE_OWNER_ENV; "
        "os.environ.pop(SMOKE_ROOT_ENV, None); os.environ.pop(SMOKE_OWNER_ENV, None); "
        f"enable_smoke_sandbox({json.dumps('dependency_' + name)}); " + project["import_test"]
    )
    res = run_command([str(python_exe), "-c", import_code], cwd=path, env=env)

    if res.returncode != 0:
        print(f"[FAIL] [Guard] {name} Dependency Check FAILED")
        print(f"Error Detail:\n{res.stderr or res.stdout}")
        return False

    # 4. Docker 物理完整性檢查 (Parity Check)
    if not verify_dockerfile_parity(path):
        return False

    print(f"[PASS] [Guard] {name} Dependency Check PASSED")
    return True


def main(*, cache_only: bool = False) -> None:
    """進入點."""
    print("[Guard]  Touka Dependency Sovereignty Guard Starting...")

    residue_violations = scan_pytest_residue(PROJECT_ROOT)
    if residue_violations:
        print("[FAIL] [Guard] Unsafe repository-local pytest residue detected:")
        for violation in residue_violations:
            print(f"       {violation.relative_path} [{violation.code}]")
        print("[STOP] Dependency cache work was not started.")
        sys.exit(1)

    cache_root = _cache_root()
    print(f"[Cache] [Guard] Cache root: {cache_root}")
    if cache_root != LEGACY_CACHE_DIR and LEGACY_CACHE_DIR.exists():
        print(
            "[Cache] [Guard] Legacy cache scripts/.cache/guards is no longer used "
            "(rebuildable); delete it manually if you want the space back."
        )
    if not cache_only:
        cache_root.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_projects = []

    for project in TARGET_PROJECTS:
        try:
            passed = check_project_dependencies(
                project, cache_only=cache_only, cache_root=cache_root
            )
            if passed:
                success_count += 1
            else:
                failed_projects.append(project["name"])
        except Exception as e:
            print(f"[Error] [Guard] Critical error checking {project['name']}: {e}")
            failed_projects.append(project["name"])

    print("\n" + "=" * 50)
    print(f"[Summary] Summary: {success_count}/{len(TARGET_PROJECTS)} Passed")

    if failed_projects:
        print(f"[Alert] Failed Projects: {', '.join(failed_projects)}")
        sys.exit(1)
    else:
        print("[OK] All systems nominal. Dependency sovereignty verified.")
        sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-only", action="store_true", help="Fail on missing/stale member caches; never sync."
    )
    main(cache_only=parser.parse_args().cache_only)

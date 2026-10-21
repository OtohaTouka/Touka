"""Standalone smoke sandbox isolation (ADR-0103 / ADR-0214).

standalone smoke 用 ``with TestClient(app)`` 觸發完整 lifespan；若用真實 config，啟動的
背景 bots 與動態模型發現會留下任務/執行緒，使 Windows ProactorEventLoop 在 teardown 死鎖。
sandbox（bootstrap=false、停動態發現）→ 啟動輕、teardown 乾淨
（與 pytest conftest 同理）。預設同時封鎖外部網路（``tests/network_guard.py``）。

⚠️ 必須在 import 任何 src 模組之前呼叫 enable_smoke_sandbox()。
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

# 確定性身分種子：DebugAgent→Debugger（AGENTS.md §17 的標準測試身分）。
_SEED_IDENTITY_MAP = {
    "version": None,
    "author": None,
    "tag_rules": {},
    "agent_overrides": {
        "DebugAgent": {"role": "Debugger", "capabilities": ["heartbeat_awakening"]}
    },
    "default_role": "Base",
}

SMOKE_ROOT_ENV = "TOUKA_SMOKE_SANDBOX_ROOT"
SMOKE_OWNER_ENV = "TOUKA_SMOKE_SANDBOX_OWNER"
_RUNNER_OWNER = "runner"
_STANDALONE_OWNER = "standalone"
_CREDENTIAL_NAME_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "CREDENTIALS",
    "BEARER",
    "MASTER_KEY",
    "OAUTH_KEY",
    "ACCESS_KEY",
)
_SANDBOX_FLAGS = {
    "TOUKA_BOOTSTRAP_ENABLED": "false",
    "DISPATCH_AUTOSPAWN": "false",
    "ENABLE_DYNAMIC_MODELS": "false",
    "TOUKA_EVAL_JUDGE_ENABLED": "false",
    "QDRANT_FORCE_DISABLE_MMAP": "true",
    "HF_HUB_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "TOUKA_SMOKE_OFFLINE": "true",
}
_CLEANUP_ATTEMPTS = 3
_CLEANUP_RETRY_SECONDS = 0.1
_TRACKED_SKILL_FIXTURES = (Path("document-generator") / "SKILL.md",)


class SmokeSandboxError(RuntimeError):
    """The sandbox root or cleanup contract is unsafe."""


_NETWORK_GUARD_MODULE = "tests.network_guard"
_NETWORK_GUARD_PATH = Path(__file__).resolve().parents[2] / "network_guard.py"


def _load_network_guard() -> ModuleType:
    """各匯入情境的 sys.path 不同：依路徑載入並登記 canonical 名稱，與 conftest 共用一份。"""
    loaded = sys.modules.get(_NETWORK_GUARD_MODULE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_NETWORK_GUARD_MODULE, _NETWORK_GUARD_PATH)
    if spec is None or spec.loader is None:
        message = f"Cannot load the network guard from {_NETWORK_GUARD_PATH}"
        raise SmokeSandboxError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NETWORK_GUARD_MODULE] = module
    spec.loader.exec_module(module)
    return module


network_guard = _load_network_guard()


def is_credential_environment_name(name: str) -> bool:
    """Treat provider keys and any credential-shaped environment name as secret."""
    normalized = name.upper()
    return any(
        normalized == marker or normalized.endswith(f"_{marker}") or f"_{marker}_" in normalized
        for marker in _CREDENTIAL_NAME_MARKERS
    )


def _validated_temp_root(path: Path) -> Path:
    root = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if root == temp_root or not root.is_relative_to(temp_root):
        message = f"Smoke sandbox must be a child of the OS temp directory: {root}"
        raise SmokeSandboxError(message)
    return root


def build_smoke_environment(
    root: Path,
    base_env: Mapping[str, str] | None = None,
    *,
    owner: str,
) -> dict[str, str]:
    """Return a write-root sandbox with credential-shaped env names scrubbed."""
    sandbox_root = _validated_temp_root(root)
    env = dict(os.environ if base_env is None else base_env)
    for key in tuple(env):
        if is_credential_environment_name(key):
            env.pop(key, None)

    config_dir = sandbox_root / "config"
    data_dir = sandbox_root / "data"
    cache_dir = sandbox_root / "cache"
    workspace_dir = sandbox_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            SMOKE_ROOT_ENV: str(sandbox_root),
            SMOKE_OWNER_ENV: owner,
            "MEMORY_APP_DATA_DIR": str(sandbox_root),
            "SANCTUARY_DATA_PATH": str(data_dir),
            "TOUKA_DATA_DIR": str(data_dir),
            "HEARTBEAT_CONFIG_FILE": str(sandbox_root / "heartbeat.json"),
            "TOUKA_CONFIG_DIR": str(config_dir),
            "TOUKA_ENV_FILE": str(sandbox_root / ".env"),
            "WORKSPACE_ROOT": str(workspace_dir),
            "HOST_WORKSPACE_ROOT": str(workspace_dir),
            "HF_HOME": str(cache_dir / "huggingface"),
            "SENTENCE_TRANSFORMERS_HOME": str(cache_dir / "sentence_transformers"),
            **_SANDBOX_FLAGS,
        }
    )
    return env


def _hydrate_config_examples(config_dir: Path) -> None:
    repo_config = Path(__file__).resolve().parents[3] / "ToukaProxy" / "config"
    for example in repo_config.glob("*.json.example"):
        shutil.copyfile(example, config_dir / example.name.removesuffix(".example"))

    repo_skills = repo_config / "skills"
    for relative_path in _TRACKED_SKILL_FIXTURES:
        destination = config_dir / "skills" / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo_skills / relative_path, destination)


def _remove_standalone_sandbox(path: Path) -> None:
    root = _validated_temp_root(path)
    for attempt in range(_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(root)
        except OSError:
            if attempt == _CLEANUP_ATTEMPTS - 1:
                raise
            time.sleep(_CLEANUP_RETRY_SECONDS)
        else:
            break
    if root.exists():
        message = f"Smoke sandbox cleanup left residue: {root}"
        raise SmokeSandboxError(message)


def _cleanup_standalone_at_exit(path: Path) -> None:
    """Make standalone cleanup failure observable as a non-zero process exit."""
    try:
        _remove_standalone_sandbox(path)
    except BaseException:
        traceback.print_exc(file=sys.__stderr__)
        sys.__stderr__.flush()
        os._exit(1)


def _fail_on_network_violations_at_exit() -> None:
    """被產品吞掉的外網違規也要讓 smoke 以非零結束，並列出被擋的目的地。"""
    violations = network_guard.drain_violations()
    if violations:
        sys.__stderr__.write(network_guard.format_violations(violations) + "\n")
        sys.__stderr__.flush()
        os._exit(1)


def run_cleanup_steps(*steps: Callable[[], None]) -> None:
    """Attempt every cleanup callback, then re-raise the first failure."""
    first_error: BaseException | None = None
    for step in steps:
        try:
            step()
        except BaseException as exc:
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise first_error


def enable_smoke_sandbox(name: str = "smoke", *, allow_external_network: bool = False) -> str:
    """建立暫存 sandbox 並注入隔離環境變數；回傳 sandbox 根目錄，程序結束時自動清除。

    種入確定性的 identity_map.json（DebugAgent→Debugger），並複製生產 permissions
    設定。P4 fallback 對 Debugger 刻意 fail-closed；正常 smoke 不應誤用異常降級模式。
    預設封鎖外部網路；``allow_external_network=True`` 只限已取得人類同意的人工 live script。
    """
    if not allow_external_network:
        network_guard.install()
        atexit.register(_fail_on_network_violations_at_exit)
    inherited_root = os.environ.get(SMOKE_ROOT_ENV)
    owner = os.environ.get(SMOKE_OWNER_ENV)
    standalone_owned = not inherited_root
    if inherited_root:
        root = _validated_temp_root(Path(inherited_root))
        if owner != _RUNNER_OWNER:
            message = "Inherited smoke root is missing the runner ownership marker"
            raise SmokeSandboxError(message)
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix=f"touka_smoke_{name}_")).resolve()
        owner = _STANDALONE_OWNER

    sanitized_env = build_smoke_environment(root, os.environ, owner=owner)
    for key in set(os.environ) - set(sanitized_env):
        os.environ.pop(key, None)
    os.environ.update(sanitized_env)
    if not allow_external_network:
        network_guard.clear_proxy_environment(os.environ)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.addHandler(logging.NullHandler())

    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (root / "data" / "soul").mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(
        "# standalone smoke: provider credentials intentionally absent\n",
        encoding="utf-8",
    )

    _hydrate_config_examples(config_dir)

    (config_dir / "identity_map.json").write_text(json.dumps(_SEED_IDENTITY_MAP), encoding="utf-8")
    if standalone_owned:
        atexit.register(_cleanup_standalone_at_exit, root)
    return str(root)

"""Python 子程序的環境（SSOT）：dispatch 與系統快照／還原腳本共用。"""
from __future__ import annotations

import os

from src.config import PROXY_ROOT, WORKSPACE_ROOT, settings


def child_python_env() -> dict[str, str]:
    """回傳環境複本，PYTHONPATH 依序為 PROXY_ROOT、ToukaMemory/src、原本的值（去重保序）.

    回傳的是完整環境（`settings.get_full_env()`，含金鑰），只能給 Touka 自己的子程序；
    不可給不可信子程序（代理人指令、執行器、launcher、relay、沙盒、第三方 MCP）。
    隔離邊界的禁用由 `tests/lint/test_isolation_boundary_no_full_env.py` 執法。
    """
    env = settings.get_full_env()
    python_paths = [str(PROXY_ROOT), str(WORKSPACE_ROOT / "ToukaMemory" / "src")]
    python_paths.extend(path for path in env.get("PYTHONPATH", "").split(os.pathsep) if path)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
    return env

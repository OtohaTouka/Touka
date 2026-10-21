"""P5d 整合：terminate_process_tree 能真正終止以新行程群組啟動的子進程（平台相依）。"""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from src.services.process_registry import terminate_process_tree


async def test_terminate_process_tree_kills_running_process():
    """以新群組啟動一個長眠子進程，terminate_process_tree 應讓它結束。"""
    args = [sys.executable, "-c", "import time; time.sleep(30)"]
    if sys.platform == "win32":
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kw = {"start_new_session": True}

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kw,
        )
    except FileNotFoundError:  # pragma: no cover - 環境缺直譯器
        pytest.skip("python interpreter not spawnable in this environment")

    assert proc.returncode is None
    terminate_process_tree(proc)
    await asyncio.wait_for(proc.wait(), timeout=15)
    assert proc.returncode is not None  # 已被終止

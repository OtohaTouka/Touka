"""ExecutionBroker——子程序啟動的單一入口（ADR-0239 §2，純重構）.

只依賴標準庫的葉節點模組，提供三個純轉傳函式：

- ``new_process_group_kwargs()``：新行程群組 kwargs（Windows ``CREATE_NEW_PROCESS_GROUP``／
  POSIX ``start_new_session=True``），於呼叫當下依平台判定；
- ``spawn_exec(*argv, new_process_group=False, **kwargs)``：非同步啟動入口；
- ``run_sync(argv, **kwargs)``：同步啟動入口。

設計約束（零行為變更）：
- **不注入任何預設值**（env／stdin／encoding／timeout／close_fds 皆由呼叫端決定）；各呼叫端
  的環境淨化、逾時、資源限制與終止方式維持原樣。
- **不檢視回傳物件**：子程序物件原樣交回呼叫端（登記、串流、終止仍歸呼叫端）。
- 一律經模組屬性呼叫 ``asyncio.create_subprocess_exec``／``subprocess.run``
  （不 ``from … import``），讓測試對全域的 patch 照常生效。
"""

import asyncio
import subprocess
import sys
from typing import Any


def new_process_group_kwargs() -> dict[str, Any]:
    """回傳以新行程群組啟動的 kwargs，讓 terminate_process_tree 能連坐殺孫進程。

    於每次呼叫時判定平台；POSIX 分支不讀取 ``subprocess.CREATE_NEW_PROCESS_GROUP``
    （該常數僅存在於 Windows）。
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def spawn_exec(
    *argv: Any, new_process_group: bool = False, **kwargs: Any
) -> asyncio.subprocess.Process:
    """非同步啟動子程序（``asyncio.create_subprocess_exec`` 的純轉傳）。

    Args:
        *argv: 位置參數原樣轉傳（程式與其引數）。
        new_process_group: True 時併入 ``new_process_group_kwargs()``。
        **kwargs: 原樣轉傳（cwd／env／stdout／stderr 等）。

    Returns:
        ``asyncio.create_subprocess_exec`` 的回傳物件，未經檢視或修改。
    """
    group_kwargs = new_process_group_kwargs() if new_process_group else {}
    return await asyncio.create_subprocess_exec(*argv, **kwargs, **group_kwargs)


def run_sync(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """同步執行子程序（``subprocess.run`` 的純轉傳）。

    Args:
        argv: 原樣轉傳的指令（呼叫端負責固定 argv、``shell=False``）。
        **kwargs: 原樣轉傳（capture_output／text／check／env／timeout 等）。

    Returns:
        ``subprocess.run`` 的回傳物件，未經檢視或修改。
    """
    return subprocess.run(argv, **kwargs)  # noqa: S603

"""執行中子進程登記表 (Process Registry).

以 session_id 追蹤該會話正在執行的 OS 子進程（asyncio subprocess），讓 `/stop`
端點能「真的」終止正在跑的 bash 指令（硬中斷），而非只設軟中斷旗標、等迴圈邊界。

bash 工具於執行期登記、結束時註銷；`/stop` 呼叫 ``terminate_session`` 強殺。
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

from src.utils.task_utils import spawn_logged

logger = logging.getLogger(__name__)

# 取消清理時等待被殺子程序回收的上限（秒）：逾時就放手，不讓清理無限卡住。
CANCEL_REAP_TIMEOUT_SECONDS = 5.0


def terminate_process_tree(proc: Any) -> None:
    """強制終止 proc 及其整個行程群組／樹（含 bash 衍生的孫進程），避免殘留孤兒 (P5d).

    proc 須以新行程群組啟動（POSIX: start_new_session；Windows: CREATE_NEW_PROCESS_GROUP）。
    POSIX 以 ``os.killpg`` 連坐殺整個群組；Windows best-effort 以 ``taskkill /F /T`` 殺進程樹。
    任一步失敗（或 proc 尚未啟動）則回退至 ``proc.kill()``（至少殺直接子進程）；全程吞例外不外拋。

    Args:
        proc (Any): asyncio subprocess 物件（需具 pid 與 kill()）。
    """
    pid = getattr(proc, "pid", None)
    if pid is None:
        with contextlib.suppress(Exception):
            proc.kill()
        return
    try:
        if sys.platform == "win32":
            completed = subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(pid)],  # noqa: S607
                capture_output=True,
                check=False,
            )
            if getattr(completed, "returncode", 0) != 0:
                proc.kill()
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # 已結束或無權限
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


async def reap_process_tree(proc: Any, kill: Callable[[Any], None]) -> None:
    """在執行緒裡殺行程樹（不卡事件迴圈），再有界等待 ``proc.wait()`` 回收（逾時分支用）。

    清理包成獨立 task 並以 ``asyncio.shield`` 等待：呼叫端被取消時 ``CancelledError`` 往上拋，
    殺行程與回收照樣做完；被殺的行程一直不結束時，等 ``CANCEL_REAP_TIMEOUT_SECONDS`` 就放手。
    ``kill`` 由呼叫端傳入自己模組 import 的 ``terminate_process_tree``，讓既有測試對各模組
    名稱的 patch 照常生效。

    Args:
        proc (Any): asyncio subprocess 物件（需具 wait()）。
        kill (Callable[[Any], None]): 殺行程樹的同步函式（會在執行緒裡呼叫）。
    """

    async def _kill_and_reap() -> None:
        try:
            await asyncio.to_thread(kill, proc)
        except Exception:  # 殺行程失敗不能蓋掉呼叫端的取消；照樣等待回收
            logger.exception("Failed to kill a process tree")
        with contextlib.suppress(ProcessLookupError, TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=CANCEL_REAP_TIMEOUT_SECONDS)

    await asyncio.shield(spawn_logged(_kill_and_reap(), name="reap-process-tree"))


async def reap_cancelled_process(proc: Any, kill: Callable[[Any], None]) -> None:
    """取消清理：同 ``reap_process_tree``，但呼叫端再被取消一次只會提早返回（取消已在處理中）。

    Args:
        proc (Any): asyncio subprocess 物件（需具 wait()）。
        kill (Callable[[Any], None]): 殺行程樹的同步函式（會在執行緒裡呼叫）。
    """
    with contextlib.suppress(asyncio.CancelledError):
        await reap_process_tree(proc, kill)


def terminate_processes(procs: Iterable[Any], session_id: str) -> int:
    """強制終止給定的子進程（含行程樹）；可在執行緒裡呼叫（``/stop`` 以 to_thread 呼叫）。

    Args:
        procs (Iterable[Any]): 先前登記、已自登記表取出的子進程。
        session_id (str): 會話識別碼（記錄用）。

    Returns:
        int: 實際送出終止訊號的子進程數量。
    """
    killed = 0
    for proc in procs:
        try:
            if getattr(proc, "returncode", None) is None:
                # P5d：殺整個行程群組／樹（含孫進程），而非只殺直接子進程
                terminate_process_tree(proc)
                killed += 1
        except ProcessLookupError:
            pass  # 已結束
        except Exception:
            logger.warning("Failed to kill a process for session %s", session_id)
    if killed:
        logger.info(
            "🛑 [ProcessRegistry] Terminated %d process(es) for session %s",
            killed,
            session_id,
        )
    return killed


class ProcessRegistry:
    """以會話為單位登記／終止執行中子進程的單例。

    Attributes:
        _session_processes (dict[str, set[Any]]): session_id → 執行中子進程集合。
    """

    def __init__(self) -> None:
        """初始化登記表。"""
        self._session_processes: dict[str, set[Any]] = {}

    def register(self, session_id: str | None, proc: Any) -> None:
        """登記一個執行中子進程到指定會話（session_id 為空則略過）。

        Args:
            session_id (str | None): 會話識別碼。
            proc (Any): asyncio subprocess 物件（需具 kill() 與 returncode）。
        """
        if not session_id or proc is None:
            return
        self._session_processes.setdefault(session_id, set()).add(proc)

    def unregister(self, session_id: str | None, proc: Any) -> None:
        """子進程結束後從會話登記中移除。

        Args:
            session_id (str | None): 會話識別碼。
            proc (Any): 先前登記的子進程物件。
        """
        if not session_id:
            return
        procs = self._session_processes.get(session_id)
        if not procs:
            return
        procs.discard(proc)
        if not procs:
            self._session_processes.pop(session_id, None)

    def pop_session(self, session_id: str) -> list[Any]:
        """取出並移除某會話登記中的子進程（在事件迴圈上呼叫；之後才到執行緒裡殺）。

        Args:
            session_id (str): 會話識別碼。

        Returns:
            list[Any]: 原本登記的子進程（沒有則為空串列）。
        """
        return list(self._session_processes.pop(session_id, None) or ())

    def terminate_session(self, session_id: str) -> int:
        """強制終止某會話所有登記中的子進程（同步；事件迴圈上請改用 pop_session＋執行緒）。

        Args:
            session_id (str): 會話識別碼。

        Returns:
            int: 實際送出終止訊號的子進程數量。
        """
        return terminate_processes(self.pop_session(session_id), session_id)

    def count(self, session_id: str) -> int:
        """回傳某會話目前登記中的子進程數量。"""
        return len(self._session_processes.get(session_id, ()))

    def reload_all(self) -> None:
        """清空所有登記（SSOT：供測試環境重置）。"""
        self._session_processes.clear()


# 全系統單例
process_registry = ProcessRegistry()

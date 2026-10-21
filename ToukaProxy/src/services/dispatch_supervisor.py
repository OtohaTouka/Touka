"""DispatchSupervisor — dispatch 子程序生命週期監督（ADR-0082，SRP）.

ToukaDispatch 併入 proxy 容器後，由本服務於 lifespan 啟動時 spawn
``python ToukaDispatch/server.py``（localhost `/mcp`），並以看門狗監督：
崩潰→封頂指數退避重生；關機→連坐殺整棵行程樹（含 ADR-0067 召喚的外部 AI，
為一致的監督語意——任務看板狀態落盤於 data/，不因此遺失）。

設計約束：
- 一律讀 ``settings`` 屬性（ENV SSOT；測試以 monkeypatch.setattr 切換）。
- 就緒探測失敗只警告、不擋 proxy 開機（fail-open 觀測，看門狗續管）。
- 不註冊 process_registry 的 session 表——避免 dispatch 成為可被 /stop
  端點殺掉的偽 session；由本服務自持 proc 參考。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import secrets
import signal
import sys
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from src.config import (
    WORKSPACE_ROOT,
    is_public_secret,
    persist_generated_secret,
    settings,
    usable_secret,
)
from src.protocols.mcp.http_client import MCP_HTTP_EXCEPTIONS, build_mcp_http_client
from src.services.execution_broker import spawn_exec
from src.services.process_registry import terminate_process_tree
from src.utils.child_env import child_python_env
from src.utils.task_utils import spawn_logged

logger = logging.getLogger(__name__)

_READY_TIMEOUT_SECONDS = 15.0
_READY_POLL_INTERVAL_SECONDS = 0.25
_GRACEFUL_STOP_TIMEOUT_SECONDS = 5.0
_FORCED_STOP_TIMEOUT_SECONDS = 5.0
_GROUP_QUIESCENCE_TIMEOUT_SECONDS = 5.0
_GROUP_QUIESCENCE_POLL_INTERVAL_SECONDS = 0.05
_BACKOFF_CAP_SECONDS = 30
_MODERN_PROTOCOL_VERSION = "2026-07-28"
_REQUIRED_DISPATCH_TOOLS = frozenset(
    {
        "dispatch_handshake",
        "dispatch_initialize_mission",
        "dispatch_update_task",
        "dispatch_get_board",
    }
)


def _signal_dispatch_process_group(proc: Any) -> None:
    """要求獨立 process group 進入 Uvicorn graceful lifespan shutdown。"""
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        return
    os.killpg(proc.pid, signal.SIGTERM)


def _dispatch_process_group_quiescence(proc: Any) -> bool | None:
    """Probe the independently spawned group; ``None`` means unprovable."""
    if sys.platform == "win32":
        return None
    group_id = getattr(proc, "pid", None)
    if type(group_id) is not int or group_id < 1:
        return None
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError as error:
        return error.errno == errno.ESRCH
    return False


async def _wait_dispatch_process_group_quiescent(proc: Any) -> bool:
    """Boundedly prove POSIX group-empty; unsupported hosts fail closed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _GROUP_QUIESCENCE_TIMEOUT_SECONDS
    while True:
        quiescent = _dispatch_process_group_quiescence(proc)
        if quiescent is None:
            return False
        if quiescent:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_GROUP_QUIESCENCE_POLL_INTERVAL_SECONDS)


def _force_dispatch_process_group(proc: Any) -> None:
    """Kill the retained POSIX PGID even after its leader has exited (blocking; run in a thread)."""
    if sys.platform == "win32":
        terminate_process_tree(proc)
        return
    group_id = getattr(proc, "pid", None)
    if type(group_id) is not int or group_id < 1:
        terminate_process_tree(proc)
        return
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        terminate_process_tree(proc)


async def _terminate_and_wait(proc: Any) -> None:
    """仍在跑才強制終止整棵行程樹（在執行緒裡跑：Windows 的 taskkill 會卡住），並有界等待退出。"""
    if proc.returncode is not None:
        return
    with contextlib.suppress(Exception):
        await asyncio.to_thread(terminate_process_tree, proc)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=_FORCED_STOP_TIMEOUT_SECONDS)


async def _stop_dispatch_process(proc: Any) -> bool:
    """Graceful 停止 dispatch；回傳 False 表示已強制終止或結果無法證明乾淨。"""
    if proc.returncode is not None:
        return False
    try:
        _signal_dispatch_process_group(proc)
        returncode = await asyncio.wait_for(
            proc.wait(),
            timeout=_GRACEFUL_STOP_TIMEOUT_SECONDS,
        )
    except Exception:
        await _terminate_and_wait(proc)
        return False
    if not (type(returncode) is int and returncode == 0 and proc.returncode == 0):
        await _terminate_and_wait(proc)
        return False
    try:
        group_quiescent = await _wait_dispatch_process_group_quiescent(proc)
    except Exception:
        group_quiescent = False
    if not group_quiescent:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_force_dispatch_process_group, proc)
    return group_quiescent


async def _probe_dispatch_mcp(port: int) -> bool:
    """真送 modern tools/list；TCP 可連但協定壞掉不得判成 ready。"""
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with asyncio.timeout(1.0):
            async with build_mcp_http_client() as http_client:
                target = streamable_http_client(
                    url,
                    http_client=http_client,
                    terminate_on_close=False,
                )
                async with Client(
                    target,
                    mode=_MODERN_PROTOCOL_VERSION,
                    cache=None,
                    read_timeout_seconds=1.0,
                ) as client:
                    tools = await client.list_tools()
    except (
        *MCP_HTTP_EXCEPTIONS,
        ExceptionGroup,
        MCPError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ):
        logger.debug(
            "[DispatchSupervisor] modern /mcp readiness probe 尚未通過",
            exc_info=True,
        )
        return False
    return {tool.name for tool in tools.tools} >= _REQUIRED_DISPATCH_TOOLS


class DispatchSupervisor:
    """spawn／監督／終止 dispatch 子程序的單一職責服務。

    Attributes:
        _proc: 目前的 asyncio subprocess（None＝未啟動或已停止）。
        _watchdog_task: 看門狗背景任務（spawn_logged 持有強參考）。
        _stopping: 關機旗標——看門狗據此停止重生。
        _restarts: 累計重生次數（/stats 觀測）。
        _status: disabled | running | crashed | failed | stopped。
    """

    def __init__(self) -> None:
        """初始化為未啟動狀態（status=disabled，待 start() 依設定決定）。"""
        self._proc: Any | None = None
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._process_started = False
        self._stopping = False
        self._restarts = 0
        self._status = "disabled"

    # ------------------------------------------------------------------
    # 路徑與子程序環境（dispatch 的執行契約由此單點注入）
    # ------------------------------------------------------------------

    def resolve_server_path(self) -> Path:
        """解析 dispatch server.py 路徑：settings 覆寫優先，否則 repo 根標準位置。"""
        if settings.DISPATCH_SERVER_PATH:
            return Path(settings.DISPATCH_SERVER_PATH)
        return WORKSPACE_ROOT / "ToukaDispatch" / "server.py"

    def build_child_env(self) -> dict[str, str]:
        """組裝 dispatch 子程序環境（bind host／port／token／回呼 URL／memory 路徑）。

        DISPATCH_TOKEN 解析（禁令 15，比照 HEARTBEAT_TOKEN 生成即持久化）：
          1. settings 非空且不是公開過的值 → 直接沿用（compose passthrough 或已持久化值）；
          2. 空 → 首次隨機生成並持久化至 .env（bootstrap 啟用時），再放進子環境。
        生成不會自動啟用派工——spawn 另由 DISPATCH_AUTOSPAWN 把關。公開過的值（含父環境
        繼承來的）一律不進子環境：寫不回時子程序不帶 token，派工拒絕所有請求。
        """
        env = child_python_env()
        env["DISPATCH_BIND_HOST"] = settings.DISPATCH_BIND_HOST
        env["DISPATCH_PORT"] = str(settings.DISPATCH_PORT)
        token = usable_secret("DISPATCH_TOKEN", settings.DISPATCH_TOKEN)
        if not token:
            candidate = secrets.token_urlsafe(32)
            if persist_generated_secret("DISPATCH_TOKEN", candidate):
                token = candidate
        if token:
            env["DISPATCH_TOKEN"] = token
        if is_public_secret("DISPATCH_TOKEN", env.get("DISPATCH_TOKEN", "")):
            del env["DISPATCH_TOKEN"]
        # ADR-0051 回呼：同容器 localhost——dispatch_callback 路由的來源檢查自然成立
        env["TOUKA_PROXY_URL"] = f"http://127.0.0.1:{settings.PORT}"
        env["MEMORY_APP_DATA_DIR"] = settings.get_app_data_dir()
        return env

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """依 settings.DISPATCH_AUTOSPAWN 啟動 dispatch；失敗不拋例外、不擋 proxy。

        Returns:
            bool: 是否實際啟動（disabled／找不到 server.py／spawn 失敗皆 False）。
        """
        if not settings.DISPATCH_AUTOSPAWN:
            self._status = "disabled"
            logger.info(
                "[DispatchSupervisor] DISPATCH_AUTOSPAWN=false → 不啟動"
                "（native 模組化／測試模式照舊）"
            )
            return False
        if self._proc is not None and self._proc.returncode is None:
            return True  # 已在跑（冪等）
        path = self.resolve_server_path()
        if not path.exists():
            logger.error("[DispatchSupervisor] 找不到 dispatch server：%s", path)
            self._status = "failed"
            return False
        self._stopping = False
        try:
            self._proc = await self._spawn()
        except OSError:
            logger.exception("[DispatchSupervisor] spawn dispatch 失敗")
            self._status = "failed"
            return False
        self._status = "running"
        self._process_started = True
        logger.info("🚀 [DispatchSupervisor] dispatch 已啟動 (pid=%s, %s)", self._proc.pid, path)
        self._watchdog_task = spawn_logged(self._watchdog(), name="dispatch_supervisor_watchdog")
        await self._wait_ready()
        return True

    async def stop(self) -> None:
        """先 bounded graceful shutdown；任何不確定性皆 hard-kill 並 fail-closed。"""
        self._stopping = True
        task = self._watchdog_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._watchdog_task = None
        proc = self._proc
        process_started = self._process_started
        if proc is None and not process_started:
            return
        # proc 為 None 但曾啟動＝看門狗已偵測到退出，視為異常停止。
        abnormal = True
        if proc is not None:
            abnormal = not await _stop_dispatch_process(proc)
        self._proc = None
        self._process_started = False
        if self._status != "disabled" or process_started:
            self._status = "failed" if abnormal else "stopped"
            logger.info("🛑 [DispatchSupervisor] 已停止")

    def status(self) -> dict[str, Any]:
        """回傳觀測快照（/stats fold 用）：status／pid／restarts。"""
        return {
            "status": self._status,
            "pid": getattr(self._proc, "pid", None),
            "restarts": self._restarts,
        }

    # ------------------------------------------------------------------
    # 內部：spawn／就緒探測／看門狗
    # ------------------------------------------------------------------

    async def _spawn(self) -> Any:
        """以新行程群組 spawn dispatch（stdout/stderr 繼承 proxy → docker logs 可見）。"""
        path = self.resolve_server_path()
        # 新行程群組：讓 terminate_process_tree 能連坐殺孫進程（同 terminal.py 的 P5d 模式；
        # 群組 kwargs 由 ExecutionBroker 單一入口提供，ADR-0239）
        return await spawn_exec(
            sys.executable,
            str(path),
            new_process_group=True,
            cwd=str(path.parent),
            env=self.build_child_env(),
        )

    @staticmethod
    def _backoff_delay(restart_count: int) -> int:
        """封頂指數退避：1,2,4,8,16 → 上限 30 秒（防重生風暴）。"""
        return min(2 ** max(restart_count - 1, 0), _BACKOFF_CAP_SECONDS)

    async def _wait_ready(self, timeout: float = _READY_TIMEOUT_SECONDS) -> None:
        """Modern `/mcp` tools/list 輪詢就緒；逾時僅警告。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        port = settings.DISPATCH_PORT
        while loop.time() < deadline:
            if await _probe_dispatch_mcp(port):
                logger.info(
                    "✅ [DispatchSupervisor] dispatch modern /mcp 就緒（127.0.0.1:%d）",
                    port,
                )
                return
            await asyncio.sleep(_READY_POLL_INTERVAL_SECONDS)
        logger.warning(
            "⚠️ [DispatchSupervisor] dispatch 未於 %.0fs 內就緒——看門狗將持續監督", timeout
        )

    async def _watchdog(self) -> None:
        """監視子程序退出；非關機情境下退避重生（respawn 失敗則下次退避再試）。"""
        while not self._stopping:
            exited_proc = self._proc
            if exited_proc is None:
                break
            await exited_proc.wait()
            if self._stopping:
                break
            self._proc = None
            self._restarts += 1
            self._status = "crashed"
            respawn_attempt = 0
            while not self._stopping and self._proc is None:
                respawn_attempt += 1
                delay = self._backoff_delay(respawn_attempt)
                logger.warning(
                    "💥 [DispatchSupervisor] dispatch 退出 (rc=%s)，%ds 後重生"
                    "（事故第 %d 次／嘗試第 %d 次）",
                    getattr(exited_proc, "returncode", None),
                    delay,
                    self._restarts,
                    respawn_attempt,
                )
                await asyncio.sleep(delay)
                if self._stopping:
                    break
                try:
                    replacement = await self._spawn()
                except OSError:
                    logger.exception("[DispatchSupervisor] respawn 失敗（下次退避再試）")
                    continue
                self._proc = replacement
                self._status = "running"
                logger.info("🔁 [DispatchSupervisor] dispatch 已重生 (pid=%s)", replacement.pid)
                await self._wait_ready()

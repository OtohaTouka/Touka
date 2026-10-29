"""Hook 的兩種執行方式（ADR-0292）.

command：
- 不經 shell：``command`` 與 ``args`` 原樣交給 ``execution_broker.spawn_exec``，
  ``|``、``;`` 只是文字。
- 事件內容只從標準輸入給；工作目錄是設定資料夾的 ``hooks/``。
- 子程序環境只有啟動白名單（``LaunchEnvironmentPolicy.launcher_base``），Touka 與廠商金鑰不帶。
- 新行程群組啟動，逾時或被取消時在執行緒裡用 ``terminate_process_tree`` 結束整棵樹；
  stdout／stderr 各留前 ``MAX_HOOK_OUTPUT_BYTES``，之後的照讀照丟（不讓子程序卡在寫滿的管線上）。

http：
- POST 事件內容（``application/json``）；不跟隨轉址、不讀環境的代理與 .netrc。
- 3xx、非 2xx、連不上、逾時都算失敗；``timeout`` 是整次請求（含讀回應）的上限。
- 回應讀到 ``MAX_HOOK_OUTPUT_BYTES`` 就停止讀取並關閉連線。只收沒有壓縮的回應（送
  ``Accept-Encoding: identity``，回應有壓縮就算失敗、不解開），上限算的就是收到的位元組：
  壓縮過的回應解開前看不出多大，幾百位元組可以解成幾百 MiB。

這裡只負責執行與收結果；結果代表什麼（擋下、補充說明）由 ``outcome`` 解讀。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import httpx

from src.protocols.mcp.environment import LaunchEnvironmentPolicy
from src.services.execution_broker import spawn_exec
from src.services.process_registry import (
    reap_cancelled_process,
    reap_process_tree,
    terminate_process_tree,
)
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.hooks import CommandHook, HttpHook

logger = logging.getLogger(__name__)

MAX_HOOK_OUTPUT_BYTES: Final[int] = 64 * 1024
_READ_CHUNK: Final[int] = 16 * 1024
# http 回應只收沒有壓縮的（``Content-Encoding`` 沒寫或是 identity）
_PLAIN_ENCODINGS: Final[frozenset[str]] = frozenset({"", "identity"})

RunStatus = Literal["ok", "timeout", "error", "rejected"]


@dataclass(frozen=True)
class RunResult:
    """一次 hook 執行的原始結果。"""

    status: RunStatus
    duration_ms: int
    exit_code: int | None = None
    http_status: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    detail: str | None = None  # 失敗原因（給設定頁看；不含輸出內容）


class _CappedReader:
    """讀到 EOF，只留前 ``cap`` 位元組。"""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._buffer = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        room = self._cap - len(self._buffer)
        if room > 0:
            self._buffer += chunk[:room]
        if len(chunk) > max(room, 0):
            self.truncated = True

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


async def _drain(stream: asyncio.StreamReader | None, reader: _CappedReader) -> None:
    if stream is None:
        return
    while chunk := await stream.read(_READ_CHUNK):
        reader.feed(chunk)


async def _feed_stdin(stdin: asyncio.StreamWriter | None, payload: bytes) -> None:
    if stdin is None:
        return
    try:
        stdin.write(payload)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # 程式不讀標準輸入就結束，不算失敗
    finally:
        with contextlib.suppress(Exception):
            stdin.close()


def _elapsed(started: float) -> int:
    return int(get_monotonic_ms() - started)


async def run_command(hook: CommandHook, payload: bytes, *, cwd: Path) -> RunResult:
    """執行 command hook；呼叫前路徑界線已檢查過。"""
    started = get_monotonic_ms()
    try:
        proc = await spawn_exec(
            hook.command,
            *hook.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=LaunchEnvironmentPolicy.launcher_base(),
            cwd=str(cwd),
            close_fds=True,
            new_process_group=True,
        )
    except OSError as exc:
        return RunResult(
            status="error", duration_ms=_elapsed(started), detail=type(exc).__name__
        )
    out, err = _CappedReader(MAX_HOOK_OUTPUT_BYTES), _CappedReader(MAX_HOOK_OUTPUT_BYTES)
    work = asyncio.gather(
        _feed_stdin(proc.stdin, payload),
        _drain(proc.stdout, out),
        _drain(proc.stderr, err),
        proc.wait(),
    )
    try:
        await asyncio.wait_for(work, timeout=hook.timeout)
    except TimeoutError:
        # 在執行緒裡結束（Windows 的 taskkill 是同步的，不能卡住其他請求），再有界等待回收
        await reap_process_tree(proc, terminate_process_tree)
        return RunResult(
            status="timeout",
            duration_ms=_elapsed(started),
            stdout=out.text(),
            stderr=err.text(),
            truncated=out.truncated or err.truncated,
            detail=f"timeout after {hook.timeout}s",
        )
    except BaseException:
        # 被取消或寫標準輸入時出錯：先結束程式再往上丟，不讓它不受逾時限制一直跑
        await reap_cancelled_process(proc, terminate_process_tree)
        raise
    return RunResult(
        status="ok",
        duration_ms=_elapsed(started),
        exit_code=proc.returncode,
        stdout=out.text(),
        stderr=err.text(),
        truncated=out.truncated or err.truncated,
    )


async def run_http(
    hook: HttpHook, payload: bytes, *, transport: httpx.AsyncBaseTransport | None = None
) -> RunResult:
    """執行 http hook；``transport`` 只供測試替換。"""
    started = get_monotonic_ms()
    headers = {**hook.headers, "Content-Type": "application/json", "Accept-Encoding": "identity"}
    body = _CappedReader(MAX_HOOK_OUTPUT_BYTES)
    try:
        async with (
            asyncio.timeout(hook.timeout),
            # trust_env=False：不讀 .netrc 與代理設定，hook 網址拿不到其他地方的帳密。
            httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=hook.timeout, transport=transport
            ) as client,
            client.stream("POST", hook.url, content=payload, headers=headers) as response,
        ):
            if response.headers.get("content-encoding", "").strip().lower() not in _PLAIN_ENCODINGS:
                return RunResult(
                    status="error",
                    duration_ms=_elapsed(started),
                    http_status=response.status_code,
                    detail="compressed response",
                )
            # 已確認沒有壓縮：讀到的就是收到的位元組，不會先解開一大塊
            async for chunk in response.aiter_bytes():
                body.feed(chunk)
                if body.truncated:
                    break
    except (TimeoutError, httpx.TimeoutException):
        return RunResult(
            status="timeout", duration_ms=_elapsed(started), detail=f"timeout after {hook.timeout}s"
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return RunResult(status="error", duration_ms=_elapsed(started), detail=type(exc).__name__)
    if not response.is_success:
        return RunResult(
            status="error",
            duration_ms=_elapsed(started),
            http_status=response.status_code,
            detail=f"HTTP {response.status_code}",
        )
    return RunResult(
        status="ok",
        duration_ms=_elapsed(started),
        http_status=response.status_code,
        stdout=body.text(),
        truncated=body.truncated,
    )

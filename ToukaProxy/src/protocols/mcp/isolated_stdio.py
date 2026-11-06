"""隔離路徑的 MCP stdio 傳輸（iso-c2 C2-3；U3e 差異設計 §3.1、§4.1）.

SDK 的 ``stdio_client`` 不能給 ``pass_fds``、``close_fds``、``preexec_fn``，被包住的伺服器（本機
Landlock 啟動器、執行器 relay）一律經這裡。形狀同 SDK 的 Transport：async context manager，交出
（讀端、寫端）兩條緩衝 0 的 anyio memory stream。

- 進入時才 ``runner.plan``（spawn 前才取 fd），經 ``spawn_exec`` 以新行程群組、``close_fds``、
  只傳 plan 的 fd 啟動；spawn 成功或失敗都 ``plan.release()``。
- stdout 一行一則 JSON-RPC，單行上限 ``MCP_STDIO_LINE_LIMIT``；壞行交給 session 一個只帶型別的
  例外（原文可能含環境值，不進 log 也不進 session）。
- stderr 一直讀（子行程不會卡在寫滿的 pipe）；記錄先遮罩 ``exec_env`` 的值與 ``sanitize``，再截斷，
  每個視窗有行數上限。
- 伺服器自己結束（stdout EOF）、stdin 寫不進去、單行超過上限，看 ``on_eof``（呼叫端一定要選）：
  ``raise`` 在 task group 裡拋 ``McpServerExited``／``McpStdoutLineTooLong``，anyio 會取消 host
  task——只有 host 是 ``McpConnectionOwner`` 的 task 時才對（owner 當失聯，``on_lost`` 一次）；
  其他呼叫端（演化驗證、創建試跑）一律 ``close``：只關讀端（同 SDK），等著的請求拿
  CONNECTION_CLOSED，呼叫端自己的 task 不被取消。
- 關閉：正常離開時關 stdin、等、群組 SIGTERM、等、SIGKILL（合計 ≤4 秒，期間 stdout 照常排空）；
  取消或例外時立刻 SIGKILL 群組。群組長還沒被回收才 killpg（pgid 不會被重用）。最後讀狀態 fd，
  有代碼才 ``report_failure``（只收代碼）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from src.services.execution_broker import spawn_exec
from src.services.resource_manager import resource_manager
from src.utils.mcp_secrets import escape_log_controls, redact_values, secret_parts, secret_values
from src.utils.time_utils import get_monotonic_ms

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from src.services.isolation.runners import ExecRequest, LaunchPlan, SandboxRunner

logger = logging.getLogger(__name__)

McpEofMode = Literal["raise", "close"]

MCP_STDIO_LINE_LIMIT = 16 * 1024 * 1024  # stdout 單行上限（spawn 給 StreamReader 的 limit）
MCP_STDIO_STDIN_GRACE_SECONDS = 1.5  # 關 stdin 之後等它自己結束
MCP_STDIO_TERM_GRACE_SECONDS = 1.5  # 群組 SIGTERM 之後再等
MCP_STDIO_REAP_SECONDS = 1.0  # SIGKILL 之後等事件迴圈看到它結束
MCP_STDIO_EXIT_POLL_SECONDS = 0.01
MCP_STDERR_LOG_LINES = 50  # 每台每個視窗最多記幾行 stderr
MCP_STDERR_LOG_WINDOW_SECONDS = 60.0
MCP_STDERR_LOG_LINE_BYTES = 2048  # 每行記錄的上限（遮罩之後才截）
MCP_STDERR_TAIL_BYTES = 4096  # 離開時交給呼叫端的最後一段（已遮罩）
MCP_STDERR_READ_BYTES = 65536  # stderr、排空 stdout 每次讀的量
SIGTERM = 15
SIGKILL = 9  # Windows 的 signal 模組沒有這個常數（單元測試在 Windows 也跑）
_SERVER_EXITED = "隔離的 MCP 伺服器結束了（stdout 關閉或 stdin 寫不進去）。"
_LINE_TOO_LONG = "隔離的 MCP 伺服器送出超過上限的一行。"
_LINE_INVALID = "MCP 伺服器送出一行不是 JSON-RPC 的輸出"


class McpServerExited(ConnectionError):
    """伺服器自己結束（stdout EOF）或 stdin 寫不進去."""


class McpStdoutLineTooLong(ConnectionError):
    """stdout 的一行超過 ``MCP_STDIO_LINE_LIMIT``."""


class McpStdoutLineInvalid(ValueError):
    """stdout 的一行不是合法的 JSON-RPC；只帶原例外的型別，不帶原文."""


def _killpg(pgid: int, sig: int) -> None:
    """``os.killpg``，呼叫時才取（Windows 沒有；匯入時不能碰）."""
    os.killpg(pgid, sig)


def _verbatim(text: str) -> str:
    return text


def _monotonic_seconds() -> float:
    return get_monotonic_ms() / 1000


def parse_line(raw: bytes) -> SessionMessage | Exception:
    """一行 stdout；壞的回例外值給 session（同 SDK），但不帶原文、不記 log（M3）."""
    try:
        message = types.jsonrpc_message_adapter.validate_json(raw, by_name=False)
    except ValueError as exc:  # pydantic 的 ValidationError（含 UTF-8 壞掉）
        return McpStdoutLineInvalid(f"{_LINE_INVALID}（{type(exc).__name__}）")
    return SessionMessage(message)


class StderrLog:
    """stderr 的記錄：自己切行，每行先遮罩（``exec_env`` 的值、``sanitize``）再截斷.

    值有多行時逐行也遮罩。一直不換行的部分最多留「行上限＋最長的值」：超過時整段丟掉、只計數，
    到下一個換行之前的也丟掉——不會截斷到一半的值。每個視窗最多記 ``MCP_STDERR_LOG_LINES`` 行，
    多的只計數，下一個視窗開始（或結束時）記一行台數。
    """

    def __init__(
        self, label: str, values: Iterable[str], sanitize: Callable[[str], str] | None
    ) -> None:
        self._values = tuple(values)
        secrets = {p.encode("utf-8", "surrogatepass") for p in secret_parts(self._values)}
        self._secrets = sorted(secrets, key=len, reverse=True)
        self._label = label
        self._sanitize = sanitize or _verbatim
        self._clock = _monotonic_seconds
        self._pending = bytearray()
        self._pending_limit = MCP_STDERR_LOG_LINE_BYTES + max(map(len, self._secrets), default=0)
        self._skipping = False
        self._window: float | None = None
        self._logged = 0
        self._dropped = 0
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        """收一塊 stderr."""
        *lines, rest = chunk.split(b"\n")
        for piece in lines:
            if self._skipping:
                self._skipping = False
            else:
                self._pending += piece
                self._line(bytes(self._pending))
            self._pending.clear()
        if self._skipping:
            return
        self._pending += rest
        if len(self._pending) > self._pending_limit:
            self._pending.clear()
            self._skipping = True
            self._dropped += 1

    def close(self) -> None:
        """EOF：沒換行的最後一段也算一行；補記沒記到的行數（可重複呼叫）."""
        if self._pending and not self._skipping:
            self._line(bytes(self._pending))
        self._pending.clear()
        self._skipping = False
        self._flush_dropped()

    def tail(self) -> str:
        """最後 ``MCP_STDERR_TAIL_BYTES``（已遮罩、已截斷的行）."""
        return bytes(self._tail).decode("utf-8", "ignore")

    def _line(self, raw: bytes) -> None:
        raw = redact_values(raw, self._values)
        text = escape_log_controls(self._sanitize(raw.decode("utf-8", "replace")))
        text = text.encode()[:MCP_STDERR_LOG_LINE_BYTES].decode("utf-8", "ignore")
        self._tail += text.encode() + b"\n"
        del self._tail[:-MCP_STDERR_TAIL_BYTES]
        now = self._clock()
        if self._window is None or now - self._window >= MCP_STDERR_LOG_WINDOW_SECONDS:
            self._flush_dropped()
            self._window, self._logged = now, 0
        if self._logged >= MCP_STDERR_LOG_LINES:
            self._dropped += 1
            return
        self._logged += 1
        logger.info("MCP %s stderr: %s", self._label, text)

    def _flush_dropped(self) -> None:
        if self._dropped:
            logger.info("MCP %s: %d stderr lines not logged", self._label, self._dropped)
            self._dropped = 0


class _Pipes:
    """一個伺服器行程的三條 pipe、兩條 memory stream 與收尾（只在 host task 的 task group 用）."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        on_eof: McpEofMode,
        killpg: Callable[[int, int], None],
        stderr_log: StderrLog,
    ) -> None:
        self._proc = proc
        self._pgid = proc.pid  # 新 session：群組長的 pid 就是 pgid
        self._on_eof = on_eof
        self._killpg = killpg
        self._stderr_log = stderr_log
        self.closing = False
        self.read_send, self.read_recv = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        self.write_send, self.write_recv = anyio.create_memory_object_stream[SessionMessage](0)

    async def stdout_loop(self) -> None:
        """一行一則轉給 session；session 走了只排空；結束、超限照 ``on_eof``."""
        async with self.read_send:
            ended = await self._forward()
        if ended is not None and not self.closing and self._on_eof == "raise":
            raise ended
        with contextlib.suppress(OSError):  # 排空：伺服器不會卡在寫滿的 stdout，看得到 stdin 關閉
            while await self._proc.stdout.read(MCP_STDERR_READ_BYTES):
                pass

    async def _forward(self) -> ConnectionError | None:
        stdout = self._proc.stdout
        while True:
            try:
                raw = await stdout.readuntil(b"\n")
            except (asyncio.IncompleteReadError, OSError):  # EOF；沒換行的殘段丟掉
                return McpServerExited(_SERVER_EXITED)
            except asyncio.LimitOverrunError:
                return McpStdoutLineTooLong(_LINE_TOO_LONG)
            try:
                await self.read_send.send(parse_line(raw))
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                return None  # session 已經離開（M1）：不拋，之後只排空

    async def stdin_loop(self) -> None:
        """把 session 送的一則一行寫進 stdin；寫端結束時不關 stdin（只有收尾才關）."""
        stdin = self._proc.stdin
        async with self.write_recv:
            async for message in self.write_recv:
                data = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                try:
                    stdin.write(data.encode("utf-8") + b"\n")
                    await stdin.drain()
                except OSError:  # BrokenPipeError、ConnectionResetError：伺服器不讀了
                    break
            else:
                return
        if self.closing:
            return
        if self._on_eof == "raise":
            raise McpServerExited(_SERVER_EXITED)
        await self.read_send.aclose()  # 同 SDK：session 看到連線結束，不會一直等回應

    async def stderr_loop(self) -> None:
        """一直讀到 EOF，交給記錄（記錄量有上限，讀不停）."""
        with contextlib.suppress(OSError):
            while chunk := await self._proc.stderr.read(MCP_STDERR_READ_BYTES):
                self._stderr_log.feed(chunk)
        self._stderr_log.close()

    def signal_group(self, sig: int) -> None:
        """群組長還沒被回收才 killpg（pgid 不會被重用）."""
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self._killpg(self._pgid, sig)

    async def graceful_stop(self) -> None:
        """關 stdin → 等 → SIGTERM → 等 → SIGKILL → 等；讀端先關，卡著的送出改成排空（M1）."""
        self.read_recv.close()
        self.write_send.close()
        self._close_stdin()
        if await self.wait_exit(MCP_STDIO_STDIN_GRACE_SECONDS):
            return
        self.signal_group(SIGTERM)
        if await self.wait_exit(MCP_STDIO_TERM_GRACE_SECONDS):
            return
        self.signal_group(SIGKILL)
        await self.wait_exit(MCP_STDIO_REAP_SECONDS)

    async def wait_exit(self, seconds: float) -> bool:
        """輪詢 returncode（``proc.wait()`` 要等 pipe EOF，孫行程拿著 pipe 時會卡住）."""
        with anyio.move_on_after(seconds):
            while self._proc.returncode is None:
                await anyio.sleep(MCP_STDIO_EXIT_POLL_SECONDS)
            return True
        return False

    def close(self) -> None:
        """關 stdin 與子行程的 transport：倖存的孫行程拿著 pipe 時也不漏 fd（同 SDK）."""
        self._close_stdin()
        transport = getattr(self._proc, "_transport", None)  # asyncio 沒有公開的關法
        close = getattr(transport, "close", None)
        if callable(close):
            with contextlib.suppress(PermissionError):
                close()

    def _close_stdin(self) -> None:
        with contextlib.suppress(OSError):
            self._proc.stdin.close()


def _report(runner: SandboxRunner, plan: LaunchPlan, report_failure: Callable[[str], None]) -> None:
    """狀態 fd 有啟動器或 relay 的代碼才回報（只收代碼；伺服器自己的輸出不看）."""
    failure = runner.failure(plan.read_status())
    if failure is not None:
        report_failure(failure.code)


@asynccontextmanager
async def isolated_stdio_client(
    runner: SandboxRunner,
    request: ExecRequest,
    *,
    label: str,
    report_failure: Callable[[str], None],
    on_eof: McpEofMode,
    sanitize: Callable[[str], str] | None = None,
    stderr_tail: Callable[[str], None] | None = None,
    spawn: Callable[..., Awaitable[asyncio.subprocess.Process]] = spawn_exec,
    killpg: Callable[[int, int], None] = _killpg,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """被包住的 stdio 伺服器（規則見模組說明）；``label`` 是記錄用的伺服器名.

    ``sanitize`` 在遮罩 ``exec_env`` 之後再套到 stderr 的每一行；``stderr_tail`` 離開時收到最後
    一段 stderr（已遮罩）。``spawn``、``killpg`` 只給測試注入。
    """
    plan = runner.plan(request)  # 拿不到就往外拋（連線失敗）
    try:
        # cwd 在代理人資料夾（區）裡時，spawn 當下經交接交出、最後一段不能是連結（ADR-0293 P6）
        with resource_manager.handoff_cwd(str(plan.cwd)) as cwd:
            proc = await spawn(
                *plan.argv,
                new_process_group=True,
                cwd=cwd,
                env=plan.env,
                pass_fds=plan.pass_fds,
                close_fds=True,
                preexec_fn=plan.preexec_fn,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MCP_STDIO_LINE_LIMIT,
            )
    except BaseException:
        plan.release()
        _report(runner, plan, report_failure)
        raise
    plan.release()
    stderr_log = StderrLog(label, secret_values(request.exec_env), sanitize)
    pipes = _Pipes(proc, on_eof=on_eof, killpg=killpg, stderr_log=stderr_log)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pipes.stdout_loop)
            tg.start_soon(pipes.stdin_loop)
            tg.start_soon(pipes.stderr_loop)
            try:
                yield pipes.read_recv, pipes.write_send
            except BaseException:  # 取消、例外、owner 期限到的硬關：任何 await 之前先殺群組
                pipes.closing = True
                pipes.signal_group(SIGKILL)
                raise
            else:  # 要求關閉、正常離開
                pipes.closing = True
                with anyio.CancelScope(shield=True):
                    await pipes.graceful_stop()
            finally:
                tg.cancel_scope.cancel()
    finally:
        pipes.closing = True
        pipes.signal_group(SIGKILL)
        with anyio.CancelScope(shield=True):
            await pipes.wait_exit(MCP_STDIO_REAP_SECONDS)
        pipes.close()
        stderr_log.close()
        if stderr_tail is not None:
            stderr_tail(stderr_log.tail())
        _report(runner, plan, report_failure)

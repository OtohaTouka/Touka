"""iso-c2 C2-2：compose 的 ``python -m src.services.isolation.executor_server`` 真的跑得起來.

容器裡的工作目錄是 ``ToukaProxy``、PYTHONPATH 來自映像的 ENV、``PYTHONDONTWRITEBYTECODE=1``。
這裡在 conftest 的沙盒裡，用子行程以同樣的方式實際啟動模組（工作目錄 ``ToukaProxy``、``-B``、
不另組 PYTHONPATH），確認命令列設定有錯時是「保持存活、不服務」（不退出，避免重啟循環）：

- 命令列寫錯（``--help``；伺服器沒有 help）→ log ``usage``；
- compose 的命令列原樣，只把容器路徑換成這次的暫存區、kernel 換成 live 的 ``sandbox_kernel``
  （唯讀）→ 解析成功、kernel 依路徑載入、伺服器走到啟動檢查，因為 ipc 不存在而 log ``private_dir``；
  暫存區裡什麼都沒有被建立（沒有 ipc、socket、權杖）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml
from src.services.execution_broker import spawn_exec

if TYPE_CHECKING:
    from asyncio.subprocess import Process

ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = ROOT / "ToukaProxy"
LIVE_KERNEL = PROXY_DIR / "src" / "sandbox_kernel"
MODULE = "src.services.isolation.executor_server"
NOT_SERVING = "Executor is not serving: "
ROOT_ACCESS = ("rw", "ro")
STARTUP_SECONDS = 60.0  # 慢的 CI 上 Python 啟動加匯入的上限；平常一秒內
ALIVE_GRACE_SECONDS = 1.0  # log 之後再等一下，確認它沒有退出


def _compose_command() -> list[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["touka-executor"]["command"]


def _host_args(command: list[str], scratch: Path) -> list[str]:
    """把命令列裡的容器路徑換成暫存區（kernel 換成 live 的）；其他參數原樣."""
    kernel = command[command.index("--kernel-dir") + 1]

    def host(path: str) -> str:
        return str(LIVE_KERNEL if path == kernel else scratch / PurePosixPath(path).name)

    args = []
    for arg in command[3:]:
        access, colon, path = arg.partition(":")
        if colon and access in ROOT_ACCESS:
            args.append(f"{access}:{host(path)}")
        else:
            args.append(host(arg) if PurePosixPath(arg).is_absolute() else arg)
    return args


async def _not_serving_line(stream: asyncio.StreamReader) -> str:
    """第一行 ``Executor is not serving: …``；子行程先結束（EOF）時回空字串."""
    while line := await stream.readline():
        text = line.decode("utf-8", "replace").strip()
        if text.startswith(NOT_SERVING):
            return text
    return ""


async def _run_module(*args: str) -> tuple[str, bool]:
    """啟動模組，等到它 log 為什麼不服務；回傳（那一行, log 之後是否仍存活），最後一定殺掉."""
    proc: Process = await spawn_exec(
        sys.executable, "-B", "-m", MODULE, *args,
        cwd=PROXY_DIR,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(_not_serving_line(proc.stderr), STARTUP_SECONDS)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), ALIVE_GRACE_SECONDS)
        return line, proc.returncode is None
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def test_compose_runs_this_module_from_the_proxy_directory():
    command = _compose_command()
    assert command[:3] == ["python", "-m", MODULE]
    assert (PROXY_DIR / Path(*MODULE.split("."))).with_suffix(".py").is_file()


async def test_bad_usage_logs_usage_and_stays_alive():
    line, alive = await _run_module("--help")
    assert line == NOT_SERVING + "usage"
    assert alive


async def test_compose_command_line_parses_and_stays_alive_without_serving(tmp_path):
    command = _compose_command()
    args = _host_args(command, tmp_path)
    assert str(LIVE_KERNEL) in args and "--require-foreign-peer" in args
    # 容器路徑一個都不留：在剛好有 /app、/opt 的 Linux 主機上也只碰這次的暫存區
    paths = {a for a in command[3:] if PurePosixPath(a.partition(":")[2] or a).is_absolute()}
    assert len(paths) == 6 and not paths & set(args)  # ipc、kernel、state、三個 root
    line, alive = await _run_module(*args)
    assert line == NOT_SERVING + "private_dir"
    assert alive
    assert list(tmp_path.iterdir()) == []

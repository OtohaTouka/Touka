"""P8-B Slice 1：單一 Proxy process 與 local-filesystem topology gate。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType


_TOPOLOGY_WORKER = r"""
import asyncio
import sys
from pathlib import Path

from src.protocols.mcp.oauth.workload.topology import (
    McpWorkloadTopologyBusyError,
    SingleProxyProcessGate,
)


async def run() -> None:
    await asyncio.to_thread(sys.stdin.readline)
    gate = SingleProxyProcessGate(lock_path=Path(sys.argv[1]), filesystem_kind="local")
    try:
        await gate.acquire()
    except McpWorkloadTopologyBusyError:
        print("BUSY", flush=True)
        return

    print("ACQUIRED", flush=True)
    try:
        command = await asyncio.to_thread(sys.stdin.readline)
        if command.strip() == "cancel":
            raise asyncio.CancelledError
    finally:
        await gate.aclose()


try:
    asyncio.run(run())
except asyncio.CancelledError:
    pass
"""


def _topology_module() -> ModuleType:
    module_name = "src.protocols.mcp.oauth.workload.topology"
    assert importlib.util.find_spec(module_name) is not None, "workload topology seam is missing"
    return importlib.import_module(module_name)


@pytest.mark.asyncio
async def test_unknown_or_remote_filesystem_fails_before_lock_acquisition(
    tmp_path: Path,
) -> None:
    topology = _topology_module()

    for filesystem_kind in ("unknown", "remote"):
        gate = topology.SingleProxyProcessGate(
            lock_path=tmp_path / f"{filesystem_kind}.lock",
            filesystem_kind=filesystem_kind,
        )
        with pytest.raises(topology.McpWorkloadTopologyError):
            await gate.acquire()
        assert not gate.acquired


@pytest.mark.asyncio
async def test_second_proxy_gate_fails_closed_until_owner_releases_lock(
    tmp_path: Path,
) -> None:
    topology = _topology_module()
    lock_path = tmp_path / "mcp_oauth_p8b_runtime.lock"
    first = topology.SingleProxyProcessGate(
        lock_path=lock_path,
        filesystem_kind="local",
    )
    second = topology.SingleProxyProcessGate(
        lock_path=lock_path,
        filesystem_kind="local",
    )

    await first.acquire()
    assert first.acquired
    assert first.boot_id != second.boot_id

    with pytest.raises(topology.McpWorkloadTopologyBusyError):
        await second.acquire()
    assert not second.acquired

    await first.aclose()
    await second.acquire()
    assert second.acquired
    await second.aclose()


async def _spawn_topology_worker(lock_path: Path) -> asyncio.subprocess.Process:
    proxy_root = Path(__file__).resolve().parents[2] / "ToukaProxy"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _TOPOLOGY_WORKER,
        str(lock_path),
        cwd=proxy_root,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _read_worker_result(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    raw_line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    if raw_line:
        return raw_line.decode("utf-8").strip()
    assert process.stderr is not None
    stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
    pytest.fail(f"topology subprocess exited without a result: {stderr}")


async def _cancel_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.write(b"cancel\n")
        await process.stdin.drain()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.terminate()
        await process.wait()


@pytest.mark.asyncio
async def test_independent_proxy_processes_compete_and_cancel_without_stale_lock(
    tmp_path: Path,
) -> None:
    """兩個真 subprocess 只能一個取得鎖；取消 owner 後不得留下 stale lock。"""
    lock_path = tmp_path / "mcp_oauth_p8b_subprocess.lock"
    first, second = await asyncio.gather(
        _spawn_topology_worker(lock_path),
        _spawn_topology_worker(lock_path),
    )
    processes = [first, second]
    try:
        for process in processes:
            assert process.stdin is not None
            process.stdin.write(b"start\n")
        await asyncio.gather(*(process.stdin.drain() for process in processes if process.stdin))

        results = await asyncio.gather(*(_read_worker_result(process) for process in processes))
        assert sorted(results) == ["ACQUIRED", "BUSY"]

        owner = processes[results.index("ACQUIRED")]
        contender = processes[results.index("BUSY")]
        assert await asyncio.wait_for(contender.wait(), timeout=10) == 0
        await _cancel_worker(owner)
        assert owner.returncode == 0

        successor = await _spawn_topology_worker(lock_path)
        processes.append(successor)
        assert successor.stdin is not None
        successor.stdin.write(b"start\n")
        await successor.stdin.drain()
        assert await _read_worker_result(successor) == "ACQUIRED"
        await _cancel_worker(successor)
        assert successor.returncode == 0
    finally:
        await asyncio.gather(*(_cancel_worker(process) for process in processes))

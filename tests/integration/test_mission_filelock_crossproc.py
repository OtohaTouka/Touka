"""批⑤-D E28（ADR-0049）：MissionLock 跨行程互斥——真 subprocess 驗 OS 層 file lock。

既有 test_mission_filelock.py 的併發測試把 AsyncFileLock 整個 mock 掉（in-process
asyncio.Lock 自己就序列化＝遮蔽）；file lock 改成 no-op 仍全綠（家族⑦）。本測以
**真跨行程**證明：另一行程持鎖時，MissionLock 的 acquire 必須被 OS 層擋下。

前例：test_process_group_kill（sys.executable -c 真子進程）。
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from filelock import Timeout
from memory_server.services import mission_lock
from memory_server.services.mission_lock import MissionLock

_AGENT = "XProcAgent"


async def test_mission_lock_blocked_by_other_process(tmp_path, monkeypatch):
    """他行程持有 .lock → MissionLock acquire 於有限逾時內拋 Timeout；他行程亡→可取得。

    變異 __aenter__ 的 file_lock.acquire 改 no-op（asyncio.Lock 仍在）→ 跨行程照樣
    取得鎖（互斥破壞）→ 紅。
    """
    mission_path = tmp_path / "current_mission.json"
    lock_file = str(mission_path) + ".lock"

    # 縮短 file lock 逾時（預設 30s；須在建構 MissionLock 前生效——conftest 已清快取）
    monkeypatch.setattr(mission_lock, "_FILELOCK_TIMEOUT", 1)

    # 子行程：同步 FileLock 持鎖 → 印 LOCKED → 長眠（被本測 kill）
    child_code = (
        "import time\n"
        "from filelock import FileLock\n"
        f"lock = FileLock({lock_file!r})\n"
        "lock.acquire(timeout=10)\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(30)\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:  # pragma: no cover - 環境缺直譯器
        pytest.skip("python interpreter not spawnable in this environment")

    try:
        # 等子行程真的持鎖（防 race：未持鎖前就嘗試 acquire＝假綠）
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
        assert b"LOCKED" in line, f"child failed to acquire: {line!r}"

        # 他行程持鎖中 → 本行程 MissionLock 必須被 OS 層擋下（Timeout 而非取得）
        with pytest.raises(Timeout):
            async with MissionLock(_AGENT, mission_path):
                pass  # pragma: no cover — 不應取得
    finally:
        proc.kill()
        await proc.wait()

    # 陽性對照：持鎖行程亡（OS 釋放）→ 同一 MissionLock 可正常取得（證紅是特異的）
    async with MissionLock(_AGENT, mission_path):
        pass

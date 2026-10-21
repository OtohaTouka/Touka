"""TDD: MissionLock 跨平台 FileLock 行為測試 (ADR-0049).

驗證：
  1. MissionLock 是合法的 async context manager
  2. asyncio.Lock 在進入時被取得、離開時被釋放
  3. FileLock 在 .json.lock 路徑運作
  4. FileLock 取得失敗時 asyncio.Lock 必須被釋放（exception safety）
  5. 同一進程內多個 coroutine 被串行化（不互相覆蓋）
  6. _reset_mission_lock_cache() 正確清空快取
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from filelock import Timeout
from memory_server.services.mission_lock import (
    MissionLock,
    _asyncio_locks,
    _file_locks,
    _reset_mission_lock_cache,
)


class TestMissionLock:
    """MissionLock 行為單元測試。"""

    def setup_method(self) -> None:
        """每個測試前清空全域 asyncio.Lock 快取，確保隔離。"""
        _reset_mission_lock_cache()

    @pytest.mark.asyncio
    async def test_lock_is_async_context_manager(self, tmp_path: Path) -> None:
        """MissionLock 應能作為 async context manager 正常進入與離開。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        entered = False
        async with lock:
            entered = True
        assert entered

    @pytest.mark.asyncio
    async def test_asyncio_lock_acquired_during_context(self, tmp_path: Path) -> None:
        """進入 context 期間，asyncio.Lock 應處於 locked 狀態。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        locked_inside = False
        async with lock:
            locked_inside = lock._asyncio_lock().locked()
        assert locked_inside

    @pytest.mark.asyncio
    async def test_asyncio_lock_released_after_exit(self, tmp_path: Path) -> None:
        """離開 context 後，asyncio.Lock 應被釋放（locked=False）。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        async with lock:
            pass
        assert not lock._asyncio_lock().locked()

    @pytest.mark.asyncio
    async def test_file_lock_file_created_with_lock_suffix(self, tmp_path: Path) -> None:
        """FileLock 使用的鎖定檔應在 mission_path + '.lock' 路徑。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        async with lock:
            # filelock 在取得後會建立 .lock 檔
            lock_file = Path(str(mission_path) + ".lock")
            assert lock_file.exists(), f"Expected .lock file at {lock_file}"

    @pytest.mark.asyncio
    async def test_asyncio_lock_released_if_filelock_raises(self, tmp_path: Path) -> None:
        """FileLock.acquire 拋出例外時，asyncio.Lock 必須被釋放（exception safety）。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        with patch.object(lock._file_lock, "acquire", AsyncMock(side_effect=Timeout(str(mission_path)))):
            with pytest.raises(Timeout):
                async with lock:
                    pass  # should not reach here

        # asyncio.Lock 必須已被釋放
        assert not lock._asyncio_lock().locked(), (
            "asyncio.Lock must be released when FileLock.acquire raises"
        )

    @pytest.mark.asyncio
    async def test_asyncio_lock_released_on_exception_inside_context(self, tmp_path: Path) -> None:
        """context 內部拋出例外時，兩個鎖都應被釋放。"""
        mission_path = tmp_path / "current_mission.json"
        lock = MissionLock("DispatchAgent", mission_path)

        with pytest.raises(RuntimeError, match="test error"):
            async with lock:
                raise RuntimeError("test error")

        assert not lock._asyncio_lock().locked()

    @pytest.mark.asyncio
    async def test_concurrent_coroutines_serialized(self, tmp_path: Path) -> None:
        """同一進程內，兩個 coroutine 對同一 agent_id 的 MissionLock 應被串行化。

        FileLock 使用 mock 以排除跨執行緒/OS 鎖定的干擾：
        本測試驗證 asyncio.Lock 的串行化行為，filelock 庫的正確性由其自身測試保證。
        """
        mission_path = tmp_path / "current_mission.json"
        results: list[str] = []
        AGENT_ID = "DispatchAgent"

        with patch("memory_server.services.mission_lock.AsyncFileLock") as MockAsyncFileLock:
            mock_fl = MagicMock()
            mock_fl.acquire = AsyncMock(return_value=None)
            mock_fl.release = AsyncMock(return_value=None)
            MockAsyncFileLock.return_value = mock_fl

            async def worker(name: str) -> None:
                lock = MissionLock(AGENT_ID, mission_path)
                async with lock:
                    results.append(f"{name}:enter")
                    await asyncio.sleep(0.01)
                    results.append(f"{name}:exit")

            await asyncio.gather(worker("A"), worker("B"))

        # 串行化後，A 必須完整進出才輪到 B（或反之）
        enter_a = results.index("A:enter")
        exit_a = results.index("A:exit")
        enter_b = results.index("B:enter")
        exit_b = results.index("B:exit")

        # A 完整執行後 B 才進入，或 B 完整執行後 A 才進入
        no_interleave = (exit_a < enter_b) or (exit_b < enter_a)
        assert no_interleave, f"asyncio.Lock did not serialize coroutines: {results}"

    def test_reset_lock_cache_clears_state(self, tmp_path: Path) -> None:
        """_reset_mission_lock_cache() 應清空全域 _asyncio_locks 字典。"""
        mission_path = tmp_path / "current_mission.json"
        # 先建立一個 lock 讓 cache 有資料
        lock = MissionLock("DispatchAgent", mission_path)
        lock._asyncio_lock()  # 觸發 cache 寫入

        assert "DispatchAgent" in _asyncio_locks

        _reset_mission_lock_cache()
        assert len(_asyncio_locks) == 0


# ── 批⑤-D 0049×4（filelock 家族 LOW 補測；E28 跨行程半在 integration 檔）──────────


@pytest.mark.asyncio
async def test_file_lock_acquired_inside_and_released_after_exit(tmp_path: Path) -> None:
    """file lock 在 context 內為持有、離開後必釋放（0049①：release 從無斷言）。

    變異 __aexit__ 去 file_lock.release() → 離開後 is_locked 仍 True → 紅。
    """
    lock = MissionLock("DispatchAgent", tmp_path / "current_mission.json")
    async with lock:
        assert lock._file_lock.is_locked
    assert not lock._file_lock.is_locked


def test_file_lock_cached_per_agent_identity(tmp_path: Path) -> None:
    """同 agent 的 MissionLock 實例共用同一 AsyncFileLock 物件（0049②：快取同一性）。

    快取被移除（每次新建物件）→ 兩實例各持獨立 fd、互斥語意變弱 → 紅。
    """
    p = tmp_path / "current_mission.json"
    a = MissionLock("AgentA", p)
    b = MissionLock("AgentA", p)
    c = MissionLock("AgentB", p)
    assert a._file_lock is b._file_lock
    assert a._file_lock is not c._file_lock


def test_reset_lock_cache_clears_file_locks(tmp_path: Path) -> None:
    """_reset 也必須清 _file_locks（0049③：既有測試只驗 _asyncio_locks 半邊）。

    變異 _reset 去 _file_locks.clear() → 舊 AsyncFileLock（含測試改過的 timeout）
    洩漏到下一測 → 紅。
    """
    MissionLock("DispatchAgent", tmp_path / "current_mission.json")
    assert "DispatchAgent" in _file_locks

    _reset_mission_lock_cache()
    assert len(_file_locks) == 0


def test_file_lock_timeout_is_finite_30s(tmp_path: Path) -> None:
    """AsyncFileLock 以有限逾時（30s）建構（0049④：持鎖行程崩潰不無限死等）。

    變異 _FILELOCK_TIMEOUT 改 -1（無限）→ 紅。
    """
    lock = MissionLock("DispatchAgent", tmp_path / "current_mission.json")
    assert lock._file_lock.timeout == 30

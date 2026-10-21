# ADR-0049: 跨平台 MissionManager FileLock（Cross-Platform Mission FileLock）

## 狀態（Status）

✅ 已接受（Accepted）—— 2026-05-26（依賴宣告載體修訂 by [ADR-0117](0117-uv-workspace-single-lock-migration.md)：filelock 依賴自 requirements.txt 遷 pyproject/單一 uv.lock；FileLock 機制本身不變）

---

## 背景與問題（Context）

`MissionManager` 透過 `current_mission.json` 管理代理人的任務看板，採用 read-modify-write 模式。先前僅使用 `asyncio.Lock` 進行進程內 coroutine 序列化，但存在跨進程競態風險：

- **ToukaProxy**（DispatchAgent 內部呼叫）：透過 MCP tool 直接寫入任務看板
- **ToukaDispatch**（外部 AI 透過 MCP 呼叫）：透過獨立進程的 FastMCP 伺服器寫入

兩個進程可能同時執行 read-modify-write 操作，導致後寫者覆蓋先寫者的更新（TOCTOU race condition）。

---

## 決策（Decision）

引入 `filelock` 套件，在現有 `asyncio.Lock` 之上加入跨進程的 `AsyncFileLock`，並將鎖邏輯提取至獨立 SRP 模組 `mission_lock.py`：

### 鎖取得順序（Acquisition Order）

1. **`asyncio.Lock`**（記憶體層）— 在同一 event loop 內序列化 coroutine，速度快
2. **`AsyncFileLock`**（OS 層）— filelock 原生 async API；內建 `run_in_executor` 機制，序列化跨進程存取，不 block event loop；`thread_local=False` 為預設，無需手工指定

### 跨平台策略

`filelock` 底層自動選擇平台原語：
- **Windows**：`msvcrt.locking`（byte-range locking）
- **Linux / macOS**：`fcntl.flock`

### 物件快取（Cache）

`asyncio.Lock` 與 `AsyncFileLock` 均以 `agent_id` 為 key 快取於模組級字典：

```python
_asyncio_locks: dict[str, asyncio.Lock] = {}
_file_locks: dict[str, AsyncFileLock] = {}
```

這確保同一 agent 的所有呼叫共用同一對鎖物件，避免 Windows 上多實例存取同一鎖檔案的相容性問題。

### Exception Safety

`AsyncFileLock.acquire()` 失敗時，`asyncio.Lock` 必須在例外傳播前釋放：

```python
async def __aenter__(self) -> MissionLock:
    await self._asyncio_lock().acquire()
    try:
        await self._file_lock.acquire()  # AsyncFileLock：原生 async，thread_local=False 為預設
    except Exception:
        self._asyncio_lock().release()
        raise
    return self
```

### FileLock 超時設定

`_FILELOCK_TIMEOUT = 30` 秒，防止進程崩潰中途持鎖導致死鎖。

### 為何使用 AsyncFileLock（而非 FileLock + thread_local=False）

filelock 3.24.3 提供原生 `AsyncFileLock`，其 `thread_local=False` 為**預設值**，`acquire()` / `release()` 均為 async，内部使用 `_run_internal_method → loop.run_in_executor` 機制，不需手工 `run_in_executor`。

**歷史背景（2026-05-26 排查記錄）**：

初版實作使用 `FileLock`（同步）+ `loop.run_in_executor(None, self._file_lock.acquire)`。此方案存在嚴重陷阱：

`FileLock` 預設 `thread_local=True`，將 `lock_file_fd` 存入 `ThreadLocalFileContext`（繼承自 `threading.local`）。  
當 `acquire()` 在 thread pool 執行緒（T1）執行，`release()` 在 event loop 主執行緒（T0）執行時，兩者讀取的是**不同**執行緒本機存儲，`release()` 讀取到 `lock_file_fd=None`，誤判鎖未被持有並**靜默返回**，OS 層 byte-range lock 永遠不被釋放，後續取得嘗試 timeout 後才暴露症狀。

中繼修復為 `FileLock(path, timeout=30, thread_local=False)`；最終重構為 `AsyncFileLock`：
- `thread_local=False` 是 `AsyncFileLock` 的**預設值**，不依賴人工記憶
- `acquire()` / `release()` 為原生 async，移除手工 `run_in_executor` 樣板
- 由 `lint_ssot.py` 的 `FileLockAsyncRule`（`FILELOCK_ASYNC_SSOT`）防止未來退化：服務層禁止直接 `from filelock import FileLock`

```python
# ❌ 舊實作（已移除）
from filelock import FileLock
_file_locks[id] = FileLock(path, timeout=30, thread_local=False)
await loop.run_in_executor(None, self._file_lock.acquire)

# ✅ 現行實作
from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT
_file_locks[id] = AsyncFileLock(path, timeout=30)  # thread_local=False 為預設
await self._file_lock.acquire()
```

---

## 影響範圍（Consequences）

### 正面影響

- 消除 ToukaProxy ↔ ToukaDispatch 跨進程 TOCTOU race condition
- 呼叫點（`async with self._get_lock(agent_id):`）完全不變，符合 OCP
- `MissionLock` 提取為 SRP 模組，可獨立測試

### 負面影響

- 新增外部依賴 `filelock>=3.24.3`（ToukaMemory/requirements.txt，需支援 `AsyncFileLock`）
- `AsyncFileLock.acquire()` 內部仍使用 `run_in_executor`，有微小延遲（與原方案相同，但由套件自動管理）
- Windows 上 `.lock` 檔案在測試清理時可能被安全掃描器短暫占用（`shutil.rmtree` 使用 `ignore_errors=True` 規避）

### 不動範圍

- `ToukaDispatch`、`ToukaHeartbeat`、`ToukaChat` 完全不受影響
- `MissionManager` 的 4 個公開方法呼叫點保持不變

---

## 測試覆蓋（Test Coverage）

新增 `tests/unit/test_mission_filelock.py`（8 個測試案例）：

| 測試 | 驗證內容 |
|------|----------|
| `test_lock_is_async_context_manager` | MissionLock 可作為 async context manager |
| `test_asyncio_lock_acquired_during_context` | 進入 context 期間 asyncio.Lock 處於鎖定狀態 |
| `test_asyncio_lock_released_after_exit` | 離開 context 後 asyncio.Lock 已釋放 |
| `test_file_lock_file_created_with_lock_suffix` | FileLock 在 `.json.lock` 路徑建立鎖定檔 |
| `test_asyncio_lock_released_if_filelock_raises` | AsyncFileLock 取得失敗時 asyncio.Lock 必須釋放（`AsyncMock(side_effect=Timeout)`） |
| `test_asyncio_lock_released_on_exception_inside_context` | context 內部例外時兩個鎖都被釋放 |
| `test_concurrent_coroutines_serialized` | 同進程內多 coroutine 被 asyncio.Lock 序列化（AsyncFileLock 以 `AsyncMock` mock） |
| `test_reset_lock_cache_clears_state` | `_reset_mission_lock_cache()` 清空 `_asyncio_locks` 字典 |

---

## FILELOCK_ASYNC_SSOT 守衛（Guard）

為防止未來回退，`scripts/lint_ssot.py` 新增 `FileLockAsyncRule`：

- **規則 ID**：`FILELOCK_ASYNC_SSOT`
- **掃描範圍**：服務層（`services/`、`routers/`、`memory_server/` 等路徑）
- **偵測條件**：`from filelock import FileLock`
- **豁免**：`mission_lock.py` L15（`from filelock import AsyncFileLock  # noqa: FILELOCK_ASYNC_SSOT`）為唯一定義點豁免；純 sync 工具腳本（`scripts/`）不受此規則約束
- **NOQA_REGISTRY.md**：記錄 1 筆豁免，總計 133 筆

任何人在服務層誤用 `FileLock`，`guard_all.py` 會自動攔截。

---

## 參考（References）

- [ADR-0044](0044-agent-dispatch-system.md) — AI Agent 工作分派中心系統
- [ADR-0046](0046-toukadispatch-dedicated-mcp-server.md) — ToukaDispatch 專用 MCP 伺服器
- `ToukaMemory/src/memory_server/services/mission_lock.py`
- `filelock` 文件：https://py-filelock.readthedocs.io/

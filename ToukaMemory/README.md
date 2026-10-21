# 🧠 ToukaMemory: Touka 的長期記憶系統（整合模式單一部署）

ToukaMemory 是 Touka 生態系的高級記憶系統（核心記憶儲存／檢索／歸檔＋個人硬碟 Personal Drive＋舊資料庫 Legacy Vault），以同行程 Python 函式庫整合於 ToukaProxy。

> **ADR-0116（2026-06-25）**：ToukaMemory 收斂為**整合模式單一部署**，**退役了獨立 MCP 模式**。本模組設計給 Touka 專案自家使用，不再作為獨立 MCP 伺服器對外（市面 AI 記憶專案已多；且原獨立模式的降級代理會關閉身分隔離與路徑保護，不宜外露）。

---

## 安裝入口

使用 [整合產品安裝指南](../README_QUICKSTART.md)；Memory 隨 Proxy 映像提供，沒有獨立 installer、Docker 服務或 MCP server。開發依賴由 root `uv sync --locked --all-packages` 佈建，版本與測試前提見 [CONTRIBUTING](../CONTRIBUTING.md)。不要重建已退役的 setup/run 入口，亦不要直接執行資料遷移／重嵌腳本當成安裝。

## 🛠️ 整合模式（由 ToukaProxy 同行程調用）

在 Touka 完整生態系中，本模組被 `ToukaProxy` 直接以原生 Python 模組掛載執行（`from memory_server...`，掛在 `PYTHONPATH`）。

- **效能**：不需額外啟動伺服器，直接透過 Python 內部調用。
- **安全**：`ToukaProxy` 自動處理身份注入與 per-agent vault 資源隔離、受保護路徑強制。
- **部署**：docker 單一映像（ADR-0082）跑 `uvicorn src.main:app`，Memory 在 proxy 容器內。

---

## 🔄 資源代理（依賴反轉接縫，ADR-0116 保留）

ToukaMemory 不直接耦合 ToukaProxy 內部；所有 I/O 透過工廠取得**注入的** `ResourceManager`（依賴反轉，便於解耦與獨立測試；邊界由 Guard #21 `.importlinter` 守護）：

```python
from memory_server.config_utils import get_resource_proxy

proxy = get_resource_proxy()  # 回傳 ToukaProxy 的真實 ResourceManager
proxy.make_dir(path)
proxy.read_file_text(path)
```

**運行時行為**：`get_resource_proxy()` 導入並回傳 `src.services.resource_manager.ResourceManager`（實作 `memory_server.protocols.IResourceProxy` 協定，享完整身份隔離與權限管理）。整合模式下此匯入必成功；**取不到即 fail-loud**——不再有 `_FallbackResourceProxy` 降級代理（其 `get_vault_path→None`／`is_protected_path→False` 等同關閉隔離與保護）。

### 協議契約（`IResourceProxy`，見 `protocols.py`）

| 方法 | 說明 |
|------|------|
| `make_dir(path)` | 建立目錄（含父目錄） |
| `read_file_text(path, encoding)` | 讀取文本檔案 |
| `write_file_text(path, content, encoding)` | 寫入文本檔案 |
| `append_file_text(path, content, encoding)` | 追加至檔案末尾 |
| `exists(path)` | 檢查路徑是否存在 |
| `remove_file(path, missing_ok)` | 刪除檔案 |
| `list_directory(path, max_depth)` | 列舉目錄內容 |
| `glob_search(path, pattern)` | Glob 模式搜尋 |
| `get_vault_path(vault, identity)` | 解析身份相關的 vault 存儲路徑 |
| `is_protected_path(path)` | 受保護路徑檢查（soul／legacy 等不可寫區） |

- **模組級快取 (`_RESOURCE_PROXY_CACHE`)**：避免重複導入開銷；測試可透過 `_reset_resource_proxy_cache()` 清除以支援 Mock。

---

## ⚙️ 環境配置 (.env)

本系統遵循 **SANCTUARY_DATA_PATH** (SSOT) 進行資料定址。

| 變數 | 說明 | 狀態 |
| :--- | :--- | :--- |
| `SANCTUARY_DATA_PATH` | 記憶資料與個人磁碟的存放根目錄 | **必填 (SSOT)** |
| `FORCE_AGENT_ID` | ⚠️ **已廢棄（C1 安全修復移除）**：此變數已從 `server.py` 永久移除。任務派發請改用 ToukaDispatch（Port 8083）。 | — |

---

## 🆔 身份識別與單一事實來源 (Identity SSOT)

為了確保身份一致性，本模組採用以下機制：
- **身份地圖同步**：自動讀取 `identity_map.json`。
- **ID 歸一化 (ID Unification)**：全系統統一使用簡潔 ID（如 `Alice`），自動剝離 `ToukaSoul` 前綴。
- **別名正規化**：傳入別名會自動轉換為正式 ID。
- **主權隔離**：所有操作必須透過 `ResourceManager` 並基於 `AgentIdentity` 物件，整合模式下強制強型別定址（ADR-0116 退役獨立 MCP 模式後，已無 MCP 模式 `str` agent_id 的豁免分支）。

---

## 🧩 主要原生工具 (Toolset v5.1)

### 1. 記憶管家 (`memory_tools`)
- **`action="save"`**: 語義化儲存，支援 `importance` 與 `emotions`。
- **`action="query"`**: 檢索對話與事實。
- **`action="random"`**: 隨機回想舊記憶。

### 1-A-0. 模組邊界合規（diag.py 修復）⭐（2026-05-18）
- `memory_server/tools/diag.py` 已修復禁令 6 違規：移除直接 import `src.services.resource_manager` 和 `src.utils.time_utils`，改用 `get_resource_proxy()` 和 `get_current_epoch()`（均來自 `memory_server.config_utils`）。
- **當時的雙模設計**中，修復前的 MCP 獨立模式會因 `src.*` 模組缺失而崩潰；修復後當時兩種模式均正常。ADR-0116 後獨立 MCP 模式已退役，現行部署僅保留 ToukaProxy 同行程整合模式。
- 現由 `scripts/guard_module_boundaries.py`（第 19 道 guard，ADR-0032）靜態防護。

### 1-B. FactualStore 系統診斷 API ⭐（2026-05-17 新增）
- **`FactualStore.query_all_facts(include_archived, limit)`**: 跨代理人查詢所有事實記憶（不過濾 `agent_id`）。
  - **用途**: 僅供系統監控層（如 ToukaProxy observability 路由）使用，不應用於業務邏輯。
  - **呼叫路徑**: `get_manager().get_factual_store().query_all_facts(limit=200)`（延遲匯入以維持模組邊界；ADR-0116 後僅供 ToukaProxy 同行程整合路徑使用）。
  - **對比**: `query_facts(identity, ...)` 按代理人過濾；`query_all_facts()` 全局管理員查詢。

### 2. 專有檔案庫 (`vault_tools`)
- **`vault="personal"`**: 個人私有文件。
- **`vault="legacy"`**: 舊版搬遷數據 (Legacy Store)。`LegacyStore.get_stats()` 的 `message_count` 自 2026-05-21 起改為**來回數**（user 訊息數），與全系統口徑一致（ADR-0036）。
- **`vault="soul"`**: 讀取靈魂腳本設定。

### 3. 專案工作箱 (`project_tools`)
- **`list` / `find` / `grep`**: 瀏覽與搜尋專案代碼。
- **`read` / `write` / `str_replace`**: 高性能原始碼編輯。

### 4. 自主任務看板工具 (`mission_tools`)
- **`action="init"`**: 初始化全新目標與子任務樹。支援擴展欄位 `verification_cmd` (驗證命令) 與 `boundary_rules` (法律邊界)。
- **`action="get"`**: 載入並獲取當前進行中的任務狀態快照 (Pydantic MissionState)。
- **`action="update"`**: 更新指定子任務狀態。支援透傳 `artifact_ref` (單一產出物)、`artifact_refs` (更動檔案列表)、`test_log` (測試輸出) 與 `blocker_detail` (阻礙說明)。
  - **FSM 狀態機轉換矩陣**: 狀態更新強制執行 FSM 轉換檢驗（例如 `TODO -> DOING -> DONE`，不允許 `TODO -> DONE` 等非法跳轉；`DONE` 為終端狀態不可被再次變更）。
  - **FAILED 狀態特權重置限制**: 進入 `FAILED` 狀態後，唯有具特權能力（具有 `can_supervise_dispatch` 特權標籤，或角色為 `Owner`/`Debugger`/`admin` 的 Touka Agent）才能將狀態重新重設為 `TODO`，外部 AI coding agent 無法自己重置。
  - **產出物欄位解耦**: 產物屬性解耦為獨立的單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str])，兩者獨立寫入，避免了物理覆蓋與踩踏，並皆經過嚴格的路徑安全與路徑穿越防禦。
- **特權代理標籤 (`can_supervise_dispatch`)** ⭐: 
  - 專門為 `DispatchAgent` 協同看板所引進。
  - 當 AgentIdentity 具有 `"can_supervise_dispatch"` 能力且目標為 `DispatchAgent` 時，系統會代理該身份以存取 `DispatchAgent` 的任務看板目錄，實現安全的跨目錄協作而不破壞路徑監獄。
  - **⚠️ 注意（C1 安全修復，ADR-0046；後經 ADR-0116 退役 MCP 模式）**：原 Standalone MCP 的自動自癒授權機制早於 ADR-0046 移除（不再有 `access_mission_board_tool`、不讀取 `FORCE_AGENT_ID`）；ADR-0116 進一步整個退役獨立 MCP 模式、刪除 `server.py`。外部 AI 工具的任務派發一律改用 **ToukaDispatch**（Port 8083，Bearer Token 認證）。
- **H1 安全修復（BLOCKED 必填 blocker_detail）** ⭐（2026-05-26）：任務轉換為 `BLOCKED` 狀態時，`blocker_detail` 欄位為必填。空值或缺失時 `MissionManager.update_task()` 以 `ValueError` 拒絕轉換。
- **H2 安全修復（賦值時路徑穿越防禦）** ⭐（2026-05-26）：`Task` Pydantic 模型啟用 `model_config = ConfigDict(validate_assignment=True)`，確保 `@field_validator` 在直接屬性賦值時亦觸發，防止繞過初始化驗證的賦值攻擊面。
- **ADR-0049 跨進程 FileLock 實作** ⭐（2026-05-26）：`mission_lock.py` 採用 `filelock.AsyncFileLock`（`thread_local=False` 為預設），結合 `asyncio.Lock` 雙重加鎖，消除 ToukaProxy ↔ ToukaDispatch 跨進程 TOCTOU race condition。服務層禁止直接 `from filelock import FileLock`（由 `FILELOCK_ASYNC_SSOT` 守衛靜態攔截）。

---

## 🧪 驗證與測試 (Verification)

> **資料完整性（ADR-0078）**：記憶與任務看板的 JSON 寫入經 `get_resource_proxy()` → `ResourceManager.write_file_text`，已採**原子寫入**（temp+fsync+`os.replace`）——寫到一半當機不再使 mission/記憶檔損毀；壞檔讀取（`JSONDecodeError`）會記明確 corrupt 告警而非靜默歸空。

本模組測試已併入全域測試架構。

```powershell
# 1. 執行全區域健康檢查 (推薦在根目錄執行)
pytest tests/

# 2. 執行 Memory 專屬單元測試
pytest tests/unit/memory/
```

---

## 🛡️ Memory 內容消毒 (H3 fix) ⭐

`save_memory()` 在持久化前**必須**通過 [`utils/content_guard.py`](src/memory_server/utils/content_guard.py) 的 `sanitize_memory_content()`：

- **長度上限**：`MAX_CONTENT_LENGTH = 8000` 字元，超長自動截斷。
- **Prompt Injection 偵測**：8 種模式覆蓋 `ignore previous instructions`、`[SYSTEM]` / `<|im_start|>` role override、`you are now X`、newline-injection 等。
- **政策**：保守偵測（不完全拒絕），記錄 WARNING 但允許保存，避免誤判但讓未來查詢可追溯風險。

詳見 [ADR-0033](../docs/architecture/adr/0033-infosec-hardening-c1-m6.md) 第 3 道守衛（結構化邊界校驗）。

---

## 🧬 嵌入模型生命週期（下載／切換／重嵌）⭐（ADR-0118，2026-07-02）

首次啟動精靈（Setup Wizard）新增嵌入模型管理，由 `ToukaProxy/src/services/embedding_service.py` 提供。**三個動作嚴格分離**（延伸 ADR-0070）：

1. **下載 (DOWNLOAD)**：把模型檔抓進 HF 快取（背景任務＋輪詢狀態；`hf_hub_download` 逐檔位元組進度；已在快取者透過 `snapshot_download(local_files_only=...)` 跳過）。**純累加**——不 `force_download`、不刪除任何既有模型；**完全不動向量與事實**。
2. **切換 (SWITCH)**：寫入 `EMBEDDING_MODEL` 並重置 `MemoryManager` 單例（**免重啟即時生效**）。`evaluate_switch` 會以 `FactualStore.query_all_facts` 偵測既有記憶，當**已有記憶且新模型不同**時，**要求使用者確認**（`force` 旗標）才放行。
3. **重嵌 (RE-EMBED)**：僅限離線維運（`reembed_memory.py`），**絕不自動觸發**，且**絕不動到 `core_memory.db`**——事實是無損的單一事實來源（SSOT），向量只是可重建的衍生索引。

> **切換模型的提醒**：切到不同嵌入模型後，舊向量與新模型的維度／語意不相容，需離線重跑 `reembed_memory.py` 由 `core_memory.db` 的事實重建向量索引；**期間事實資料（`core_memory.db`）從頭到尾不被觸碰**，重嵌失敗也可安全重試。

新增端點：`POST /v1/admin/setup/embedding/download`、`GET /v1/admin/setup/embedding/download-status`、`POST /v1/admin/setup/embedding/switch {model, force}`。

### HF 快取持久化（`HF_HOME` / `hf-cache` 卷）

- `docker-compose.yml` 新增頂層具名卷 **`hf-cache`**，掛載於 touka-proxy 的 `/app/hf_cache`；新增環境變數 **`HF_HOME=/app/hf_cache`**。
- **單一變數同時涵蓋兩條路徑**：精靈的模型下載，以及 `vector.py` 執行期 `SentenceTransformer` 載入——兩者都走 HF hub 快取。
- `touka-init` 掛載同一卷並將 `/app/hf_cache` chown 為 `1001:1001`（全新具名卷預設 root 擁有，appuser 需 chown 後才可寫入；延伸 ADR-0038 init-container-volume-permissions）。
- **持久性**：已下載模型可跨 `docker compose up --build` 保留；`docker compose down -v` 會清除。無需改動 Dockerfile。

---
*Created by Antigravity for the Harmony of Digital Memory.*

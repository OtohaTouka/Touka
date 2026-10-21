# ADR-0116：ToukaMemory 收斂為「整合模式單一部署」— 退役獨立 MCP 模式、保留依賴反轉接縫

- **狀態**：✅ 已接受（2026-06-25；擁有者核准 Option B，已實作並全綠驗證：guard 37/37、後端 pytest 2431 passed/2 skipped、smoke 24/24，整合路徑零回歸）
- **日期**：2026-06-25
- **影響範圍（若採納）**：ToukaMemory（`server.py`／`run_server.py`／`config_utils._FallbackResourceProxy` 退役、`get_resource_proxy()` 簡化、獨立安裝足跡）＋ 守衛/文件（Guard #21 `.importlinter`、ADR-0032、`AGENTS.md` 禁令 14、`docs/guides/deployment_guide.md`、`ToukaMemory/README.md`、`tests/unit/test_memory_resource_proxy.py`）
- **關鍵字**：ToukaMemory, MCP standalone mode, integration-only, dependency inversion, IResourceProxy, ResourceManager, Guard #21, ADR-0032, 禁令 14, deployment simplification, maintenance burden, scope reduction

## 背景（Context）

ToukaMemory（套件 `memory_server`）目前設計為**雙模式**運作：

- **模式 A（整合模式）**：被 ToukaProxy **以同行程函式庫**方式直接呼叫（`from memory_server...`，15+ 處）。這是**唯一的正式部署路徑**——docker 單一映像（ADR-0082）跑 `uvicorn src.main:app`，ToukaMemory 掛在 `PYTHONPATH` 於 proxy 容器內。**完整功能**（per-agent vault 身分隔離、受保護路徑強制）只在此模式生效。
- **模式 B（獨立 MCP 模式）**：把 `memory_server` 當成 FastMCP 伺服器（`run_server.py` → `memory_server/server.py`）跑成獨立進程，供 **Claude Desktop／Cursor 等外部 MCP 客戶端**接入（`ToukaMemory/README.md` 模式 B 的明示用途）。

模式 B 的存在驅動了一整套抽象與守衛：依賴反轉協定 `IResourceProxy`、工廠 `get_resource_proxy()`、純 `pathlib/os` 的 `_FallbackResourceProxy`、模組邊界守衛 Guard #21（`.importlinter` contract `toukamemory-independence`，即 AGENTS.md **禁令 14** / ADR-0032），確保 ToukaMemory 不硬依賴 ToukaProxy 內部模組。

**觸發本評估的兩件事**：①雙模式提高了 ToukaMemory 每次變更的維護稅（「兩個模式都要顧」）；②擁有者裁示——GitHub 上 AI 記憶專案眾多，**ToukaMemory 是設計給 Touka 自家用的，不需要再保留 MCP 模式給外部工具**。

### 已驗證的事實（2026-06-25 對抗式查證，附證據）

| 事實 | 結論 | 證據 |
|---|---|---|
| 正式環境只跑整合模式 | ✅ 確認 | `docker-compose.yml:31` 單一 `touka-proxy` 容器跑 `uvicorn src.main:app`；ToukaMemory 經 `PYTHONPATH`（`:47`）in-process。無獨立 memory 服務。 |
| MCP 註冊表無 memory 條目 | ✅ 確認 | `server_config.json:2-31` 僅 `grok_imagine`/`fetch`/`mcp-docgen`，無 memory。 |
| 無腳本/CI 啟動獨立 memory 服務 | ✅ 確認 | `run_server.py`/`run.ps1`/`setup.*` 為開發手動工具，未被任何 compose/CI 引用。 |
| 模式 B 為**降級**功能 | ✅ 確認 | `config_utils.py:130-132` `get_vault_path()`→`None`（無身分隔離）、`:146-148` `is_protected_path()`→`False`（**無路徑保護**）。 |
| **模式 B 有測試**（修正先前誤記） | ✅ 確認 | `tests/unit/test_memory_resource_proxy.py`（~123 行）測 `_FallbackResourceProxy` 與 `get_resource_proxy()` 工廠。故非「未測死碼」，而是**有測試但無人使用**。 |
| 工具表面為**薄包裝**（修正先前誇大） | ✅ 確認 | `server.py` 7 個 `@mcp.tool()` 皆呼叫**共用 SSOT** `memory_server.tools`；server.py 是註冊樣板（194 行），非重複邏輯。 |
| **Option B 可行**：ResourceManager 實作全部 `IResourceProxy` 方法 | ✅ 確認 | `resource_manager.py` 具全 10 法：`read_file_text:479`/`write_file_text:487`/`append_file_text:746`/`exists:533`/`make_dir:537`/`remove_file:543`/`list_directory:645`/`glob_search:687`/`is_protected_path:459`/`get_vault_path:122`。 |
| `get_resource_proxy()` 呼叫面 | ✅ 確認 | 18 處呼叫、跨 7 檔（config_utils/factual/legacy_sync/mission_manager/diag/personal_drive/vector）。 |

## 決策（Decision）

**採 Option B：把 ToukaMemory 收斂為「整合模式單一部署」，但保留 `IResourceProxy` 依賴反轉接縫。**

具體：
- **退役**獨立 MCP 部署裝置：`memory_server/server.py`（FastMCP 伺服器＋7 個 `@mcp.tool` 註冊）、`run_server.py`、`server.py` 內的 fallback `AgentIdentity`、獨立安裝足跡（`setup.sh/.ps1`、`run.ps1`、`.env.example` 的獨立執行說明）。
- **退役降級代理**：`_FallbackResourceProxy`（`config_utils.py:85-148`）。`get_resource_proxy()` 簡化為「取真實 `ResourceManager`，取不到即 **fail-loud**」——不再有靜默降級分支，故 `get_vault_path`/`is_protected_path` 永遠是有身分隔離與路徑保護的真實實作。
- **保留**（這是 Option B 與「全拆」Option C 的關鍵差異）：`IResourceProxy` 協定（`protocols.py`）＋ `get_resource_proxy()` 工廠 ＋ 18 處呼叫端**全部不動**。ToukaMemory 仍是乾淨的依賴反轉領域模組（注入 `ResourceManager`），維持鬆耦合與可獨立測試性。
- **Guard #21（模組邊界）保留、但重述其正當性**：原本理由是「維持 MCP 獨立性」；改為「維持依賴反轉與子系統解耦」。`.importlinter` contract 與既有 `config_utils` try-import 豁免可大致沿用（移除 `except` 降級分支後，import 成功為唯一合法路徑）。

## 考慮的選項（Options Considered）

### Option A：維持雙模式（現狀）
| 維度 | 評估 |
|---|---|
| 複雜度 | 高（每次變更「兩個模式都要顧」） |
| 維護成本 | 養 FastMCP 伺服器＋fallback 代理＋降級分支＋獨立安裝/文件＋邊界守衛 |
| 解耦/可測試 | 好 |

**Pros**：保留「對外開放記憶給第三方 MCP 客戶端」的未來選項。
**Cons**：為一個**沒人用、且功能降級（無身分隔離/路徑保護）**的模式長期付雙模式稅。

### Option B：整合 only，保留依賴反轉接縫 ⭐（本案決策）
| 維度 | 評估 |
|---|---|
| 複雜度 | 中低 |
| 維護成本 | 移除真正負擔（雙模式思考、降級路徑、FastMCP 表面、獨立安裝/文件） |
| 解耦/可測試 | **好（維持 `IResourceProxy` 注入）** |

**Pros**：去掉維護稅又不犧牲解耦；動到的全是**未在正式環境使用**的程式碼；`ResourceManager` 已實作全介面（落地零阻力）。
**Cons**：放棄「對外開放記憶」未來選項（但該模式本就降級、不安全外露）；仍是中型重構、需配回歸測試。

### Option C：整合 only，連抽象一起拆
| 維度 | 評估 |
|---|---|
| 複雜度 | 低（最少程式碼） |
| 維護成本 | 最低 |
| 解耦/可測試 | **變差**：ToukaMemory 直接 import `ResourceManager`，兩子系統緊耦合 |

**Pros**：行數最少、最直白。
**Cons**：失去 ADR-0032 的解耦紀律，未來想再模組化／抽出記憶子系統會很難；退役 Guard #21 後易隨時間累積硬耦合。**不建議**。

## 取捨分析（Trade-off Analysis）

核心取捨是「**簡化程度 vs 解耦保留**」。Option C 最簡但把記憶與代理層焊死；Option A 最有彈性但為死功能付稅。Option B 取中間：**只移除「跨進程/雙部署」這個真正昂貴的維度，保留「依賴反轉」這個便宜又有價值的設計**。由於 `IResourceProxy` 是純介面、`get_resource_proxy()` 是單一注入點、`ResourceManager` 已實作全介面，保留它幾乎零成本，卻換得解耦與可測試性。

唯一被放棄的能力是「把 Touka 記憶當獨立 MCP server 開放給外部 AI 宿主」。鑑於：①該模式現為**降級**（無身分隔離、無路徑保護），本就不宜外露；②擁有者明示**不需要**（ToukaMemory 為自家用、市面 AI 記憶專案已多）；③日後若真要，應重新設計**有隔離的正式版**，而非沿用今日的降級分支——此選項的喪失成本實質為零。

## 後果（Consequences）

**變容易**：
- ToukaMemory 每次新功能不再需要「兩個模式都要顧」，少一層心智負擔。
- 移除一條**降級且不安全**的執行路徑（`is_protected_path→False`／`get_vault_path→None`），縮小安全推理面。
- 部署敘事單一化（只剩整合模式），文件更誠實（不再宣傳一個沒人用的模式）。

**變難 / 放棄**：
- 失去「對外 MCP 開放記憶」的現成接縫——未來要做需重建（且應重建為有隔離的版本）。
- 此決策偏不可逆：再加回 MCP 模式是真實工作量。

**需重訪 / 連帶更新**：
- `ADR-0032`（模組邊界守衛）：標註為 Amended/Superseded-in-part（守衛保留、但正當性由「MCP 獨立」改為「解耦」）。
- `AGENTS.md` 禁令 14 描述、`docs/guides/deployment_guide.md`（移除 Standalone/Native 部署列）、`ToukaMemory/README.md`（刪模式 B 章節）。
- `tests/unit/test_memory_resource_proxy.py`：移除 `_FallbackResourceProxy` 專屬測試、保留/改寫 `get_resource_proxy()` 注入測試。

## 行動項目（Action Items — 已於 2026-06-25 全數完成）

1. [x] 退役 `memory_server/server.py`（FastMCP）＋ `run_server.py` ＋ fallback `AgentIdentity`（整檔刪除）；連帶刪 `run.ps1`/`setup.sh`/`setup.ps1`（純獨立安裝/啟動腳本）。
2. [x] 移除 `_FallbackResourceProxy`（連 `import shutil`）；`get_resource_proxy()` 改 fail-loud（無 except 降級），保留工廠與 `IResourceProxy`。
3. [x] 清整獨立安裝足跡＋ `.env.example` 改整合模式說明。
4. [x] Guard #21 `.importlinter` contract 保留、重述正當性（解耦而非 MCP 獨立）；4 條 `ignore_imports` 邊界仍為實際 import；**⚠️ 註解須 ASCII**（import-linter 以 Windows cp950 讀此檔，中文/em-dash 會 decode 失敗）。
5. [x] 改寫 `tests/unit/test_memory_resource_proxy.py`（去 fallback 測試、留「回傳真實 ResourceManager／fail-loud／快取」三測；`src` import 移頂層避開 LOCAL_IMPORT_BAN）＋移除 `test_mission_manager_bugfixes.py` 的 `TestC3RoleStringConsistency`（依賴已刪的 server.py）。
6. [x] 文件同步：`ADR-0032` 標 Amended、`AGENTS.md`（禁令 6/14＋整合模式原則）、`deployment_guide.md`、`ToukaMemory/README.md`、`SSOT_MASTER_MAP`、`NOQA_REGISTRY`（-3 ARG002／-2 IDENTITY_SSOT → 185）。
7. [x] 全關卡 `guard_all` **37/37**（含 Guard #21／#19／#28／#1）＋後端 pytest **2431 passed/2 skipped**＋smoke **24/24** 全綠；整合路徑零回歸。

> **驗證紀錄**：`reembed_memory.py` 於頂層即 `import src.models.identity`，本就需 ToukaProxy 在 path，故 fail-loud 的 `get_resource_proxy()` 在其情境正常。
>
> **對抗式複審（實作後）**：聚焦懸空消費者／整合健全性／安全。3 項 PASS（get_resource_proxy fail-loud 正確且 17-18 呼叫端傳播例外得當、ResourceManager 一律回真實隔離路徑、`.importlinter` ASCII 乾淨）。**修：先前漏掉的更多陳舊文件**——`deployment_guide.md`（方案 B 獨立執行段）、`docs/proxy/installation_guide.md`（方式 3 獨立安裝段）、`MEMORY_INTERFACE.md`（TL;DR＋模式段＋資源管理 API）、`docs/proxy/api_reference/*`（5 檔 store 模式說明）、`docs/memory/agent_instructions.md`、`docs/guides/testing_guide.md`、`ADR-0022`（標部分修訂）皆改為整合模式單一部署敘述。**延後（Med，非阻塞）**：`ToukaMemory/requirements.in` 的 `mcp`/`uvicorn` 隨 FastMCP server 刪除已成未使用直接相依——清理需改 `.in` 後跑 `lock_requirements.py` 重鎖（禁令 53／Guard #30），為獨立的相依衛生作業、另案處理（不影響功能；ToukaProxy 自身仍需 `mcp` 作 MCP 客戶端）。

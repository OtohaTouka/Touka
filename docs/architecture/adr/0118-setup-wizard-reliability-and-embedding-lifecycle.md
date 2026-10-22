# ADR-0118: 初次設定精靈可靠性強化與嵌入模型生命週期（First-Run Setup Wizard Reliability & Embedding Model Lifecycle）

## 狀態

✅ **已接受（實作完成、全綠——guard_all 37/37、後端 pytest 2485 passed/3 skipped、前端 vitest 934 passed）**。本 ADR **延伸／修訂 ADR-0053（初次設定精靈）**，並擴充 ADR-0052（環境感知 .env 載入）、ADR-0099（Provider Registry SSOT）、ADR-0070（中文向量記憶）、ADR-0038（Init 容器 volume 權限）。

> ⚠️ **部分被 [ADR-0151](0151-env-persistence-anchor-decouple.md) 取代（2026-07-12）**：本 ADR 的
> `TOUKA_ENV_FILE=/app/workspace/.env`（綁 `WORKSPACE_ROOT` 掛載）在使用者把 `WORKSPACE_ROOT`
> 指向 repo 以外時會寫進「幽靈 .env」。ADR-0151 改錨 compose 目錄（`./:/app/host_root` ＋
> `TOUKA_ENV_FILE=/app/host_root/.env`），與 `WORKSPACE_ROOT` 解耦。`resolve_env_file_path`
> SSOT／`apply_runtime_env` 即時生效等其餘決策沿用不變。

## 日期

2026-07-02

## 背景

維護者在測試環境 `docker compose up` 驗收時回報「引導精靈輸入金鑰後完全不能對話、金鑰沒寫進 .env」。對整個精靈相關功能做對抗式 code review，確認 **23 個真實問題**，收斂為三大根因（A/B/C）＋兩組其他問題（D 狀態偵測分裂、E 其他確認問題）：

- **A 寫錯檔／不持久**：`routers/config.py` 以裸相對 `".env"` 寫入 → docker 下落在容器暫存層 `/app/ToukaProxy/.env`（非 compose `env_file` 讀的宿主 .env、重建即蒸發）。
- **B 陳舊金鑰快照**：`main.py` 於 import 時以當時 `os.environ` 建好 Anthropic client、`factory.py` 以 `from src.config import GEMINI_API_KEY` 綁死 import 值 → 精靈補的金鑰在「免重啟」情況下到不了聊天路徑。
- **C 前端靜默吞失敗**：`fetchTouka` 對非 2xx 不 throw，`SetupWizard` 的 apply 不檢查 `res.ok`、catch 後仍無條件 `next()` → 金鑰打錯／寫檔失敗／401 全部無聲，使用者以為成功。
- **D／E**：狀態偵測三方分裂（重啟後有效卻顯示未設定）、provider 對照表漏 deepseek、CORS 精確比對擋掉 127.0.0.1／區網、500 洩漏例外字串等。

維護者另回報兩個延伸缺口：初次選了嵌入模型不會下載；想在精靈指定「容器外本機目錄」且 Touka 對其有完全 CRUD（類 Claude Desktop）。經查證 **Claude Desktop 是以原生 MCP 子程序 ＋ 允許目錄清單 ＋ 重啟** 存取本機檔案，並非容器動態掛載；而**運行中的容器無法動態掛載任意宿主路徑**（硬限制）——正確途徑是「宿主路徑寫入 .env → `docker compose up` bind-mount → 重啟生效」。

## 問題

1. 精靈寫入的金鑰不持久（重建即失）、且無法免重啟即時生效於聊天。
2. 前端把所有後端失敗靜默吞掉，使用者無從察覺。
3. 精靈只能設 `SANCTUARY_DATA_PATH`（且在容器內以 `is_dir` 驗證宿主路徑必失敗、回 500 醜英文）；`WORKSPACE_ROOT` 根本無從在精靈設定。
4. 選嵌入模型只寫 env、不下載、無進度；切換模型可能使既有向量索引失效卻無防呆。
5. 下載的模型在容器內為暫存，重建即失。
6. 狀態頁 provider 對照表硬編碼漏 deepseek；CORS 擋掉本機/區網存取；500 洩漏例外。

## 決策

以「**寫對檔案（SSOT）＋即時生效＋失敗必顯示**」為主軸，配合「**下載／切換／重新嵌入三動作嚴格分離**」與「**接受重啟模型的宿主目錄掛載**」，逐項修復：

### A — 環境檔寫入持久化（延伸 ADR-0052 / 禁令 38）
`routers/config.py` 讀寫 `.env` 一律經 `src.config.resolve_env_file_path()`（SSOT），移除裸相對 `".env"`。`resolve_env_file_path()` 新增 **`TOUKA_ENV_FILE` 顯式覆寫**（最高優先，凌駕整合/獨立自動偵測）。`docker-compose.yml` 設 `TOUKA_ENV_FILE=/app/workspace/.env`——即宿主根 `.env` 經 `WORKSPACE_ROOT` bind-mount 於容器內的掛載點，＝ compose `env_file` 讀的同一份檔 → 寫入持久、重建不失、下次啟動經 `env_file` 重新注入。`apply_runtime_env()` 同步 `os.environ`＋`settings`＋模組常數（免重啟）。

### B — Provider 金鑰即時解析（延伸 ADR-0099）
`dispatchers/factory.py`：Gemini 改 `registry.get_key_by_id("gemini")`（即時），Anthropic 以 `anthropic_client.with_options(api_key=registry.get_key_by_id("anthropic"))` 逐請求套用（共用 httpx 連線池、不重建）。新增 `registry.get_key_by_id(provider_id)` SSOT 便捷入口。OpenAI 相容供應商本就經 `get_provider_info`→`registry.get_key` 即時。

### C — 前端失敗必顯示（延伸 ADR-0053）
`Step1SystemHealth`／`Step3ApiKeys` 改為自帶 apply-fetch＋檢查 `res.ok`＋顯示錯誤＋僅成功才前進（對齊 Step2/Step5 既有模式）；`SetupWizard` 不再代理吞失敗。通則：`fetchTouka` 對 4xx/5xx 不 reject，呼叫端必檢 `res.ok`。

### bug② — 精靈可設宿主目錄（延伸 ADR-0053）
Step5 改雙欄位，`SANCTUARY_DATA_PATH`（資料）與 `WORKSPACE_ROOT`（工作區）皆為寫入 .env 供 compose 掛載的宿主路徑。`setup_service.apply()` 以 `_is_running_in_docker()`（`/.dockerenv`）感知驗證：容器模式跳過 `is_dir`（宿主路徑不可見、由掛載時把關），原生模式維持既有目錄檢查；路由 catch `ValueError`→`ToukaException(VALIDATION_ERROR)` 乾淨 422。新增 `SetupStatus.workspace_root_is_default`。掛載機制本就存在（`${SANCTUARY_DATA_PATH:-./data}:/app/data`、`${WORKSPACE_ROOT:-.}:/app/workspace`＋touka-init `chown` → 完全 CRUD），需 `docker compose up` 重啟套用。

### bug① — 嵌入模型生命週期（延伸 ADR-0070）
新增 `services/embedding_service.py`，**三動作嚴格分離**：
- **下載**＝背景任務逐檔 `hf_hub_download` 累積位元組進度（前端輪詢狀態）＋`snapshot_download(local_files_only)` cache-skip；**additive、絕不 `force_download`、絕不刪任何模型**；不碰向量/事實。
- **切換**＝寫 `EMBEDDING_MODEL`＋`MemoryManager.reset_singletons()`（免重啟）；`evaluate_switch` 偵測既有記憶（`FactualStore.query_all_facts`），既有記憶且換不同模型時**需使用者 force 確認**。
- **重新嵌入**＝離線維護（`reembed_memory.py`）唯一途徑，**絕不自動、絕不動 `core_memory.db`**（事實為無損 SSOT、向量為衍生索引）。

3 端點：`POST /v1/admin/setup/embedding/download`、`GET …/download-status`、`POST …/switch{model,force}`。

### HF 快取持久化（延伸 ADR-0038）
`docker-compose.yml` 新增 top-level named volume `hf-cache` 掛於 touka-proxy `/app/hf_cache`＋`HF_HOME=/app/hf_cache`（一個變數同時涵蓋精靈下載與 `vector.py` 執行期 `SentenceTransformer` 載入——皆走 HF hub 快取）；touka-init 掛同一 volume 並把 `/app/hf_cache` 併入 `chown -R 1001:1001`（新 named volume 預設 root 擁有、appuser 未 chown 前無法寫入——**強制**）。跨 `docker compose up --build` 持久化；`docker compose down -v` 才清除。無需改 Dockerfile。

### D#15 / E#17 / E#21
- **D#15**（延伸 ADR-0099）：`setup_service._get_configured_api_keys` 的 provider 清單改由 `registry.discoverable_specs()` 衍生（原硬編碼漏 deepseek→DeepSeek 永顯未設定），值仍讀 live `os.environ`，tavily 另納入。
- **E#17**（安全）：`main.py` CORS 改 `allow_origin_regex` 涵蓋 localhost／127.0.0.1／`[::1]`／私網（10/8、172.16–31、192.168/16）任意埠＋`vscode-webview://.*`，保留 `allow_origins`＝`ALLOWED_ORIGINS` 給公開網域。
- **E#21**（安全）：`routers/config.py` 寫檔失敗的 500 不再回傳 `error_output=str(e)`，改 `logger.exception` 只進 log。

### 已驗證免修 / 保留
- **D#13/20**（狀態三方分裂）已由 A 修掉（三源同步＋TOUKA_ENV_FILE 持久化）。
- **E#16/19**（Tavily 排除即時生效）非 bug——Tavily 為搜尋金鑰。
- **E#14**（改密碼登出自己，low、可重登恢復）維護者決定保留。

## 影響範圍（採納後改動的檔）

- 後端：`ToukaProxy/src/config.py`、`routers/config.py`、`routers/setup.py`、`services/setup_service.py`、`services/embedding_service.py`（新）、`services/dispatchers/factory.py`、`services/providers/registry.py`、`main.py`。
- 前端：`ToukaChat/src/components/setup/SetupWizard.tsx` 及 `steps/Step1/Step3/Step5`、`hooks/useSetupStatus.ts`、i18n（zh-TW/en）、`types/api.ts`（重生成）。
- 部署：`docker-compose.yml`（TOUKA_ENV_FILE、hf-cache volume＋HF_HOME、touka-init chown）。
- 契約：`openapi.json`（重生成，含新端點與 SetupStatus/SetupApplyRequest 欄位）。
- 守衛/文件：`docs/architecture/NOQA_REGISTRY.md`（ENV_SSOT_VIOLATION 等計數）、`SECURITY.md`、SSOT/介面/README/CHANGELOG。

## 設計決策 / 取捨

- **下載 vs 切換 vs 重新嵌入分離**：下載天生安全（僅新增快取），切換才有既有向量失效風險故需確認，重新嵌入破壞性故只限離線手動——杜絕「選模型即誤觸重嵌舊記憶」。事實（`core_memory.db`）永不被動。
- **接受重啟模型**：容器無法動態掛載任意宿主路徑；宿主目錄設定經 .env→`docker compose up` 掛載生效，與 Claude Desktop 需重啟同理。刻意不對使用者的專案資料夾 `chown -R`（破壞性），Windows Docker Desktop 掛載即可讀寫。
- **`with_options` 而非重建 client**：套用即時 Anthropic 金鑰又共用連線池，避免每請求重建 httpx pool。
- **狀態 provider 清單改 registry 衍生**：ADR-0099 registry 正為消除此類硬編碼漂移；值仍讀 os.environ 以反映真實生效並相容既有測試/重啟語義。
- **CORS 用 regex 而非放寬 `*`**：`allow_credentials=True` 下不能用 `*`；regex 精確涵蓋本機/私網/webview，公開網域仍白名單。
- **已知侷限**：standalone `ToukaProxy/docker-compose.yml`（無 init 容器）刻意不加 hf-cache/host-dir（無 chown 機制反而會壞，dev-only 不持久＝可接受漂移）；E#14 未修（維護者決定）。

## 後果

### 正面
- 精靈金鑰持久化（A）、免重啟即時對話（B）、失敗必顯示（C）、可設宿主資料/工作區目錄並完全 CRUD（bug②）、嵌入模型即時下載＋進度＋防呆切換（bug①）、下載模型跨重建持久（HF volume）、DeepSeek 狀態正確（D#15）、本機/區網可存取（E#17）、不洩漏例外（E#21）。
- 全程 TDD（先紅後綠）、SSOT（env 路徑、provider registry、事實/向量分層）、只用 DebugAgent 身分、未動任何 agent 的 `data/`。

### 負面 / 已知侷限
- 宿主目錄/快取變更需 `docker compose up` 重啟（Docker 固有）。
- standalone dev compose 不持久 HF 快取（記錄的可接受漂移）。
- `main.py` ModelJudge（eval 用）仍用啟動 client，非聊天路徑、暫不動。

## 相關 ADR

- [ADR-0053](0053-first-run-setup-wizard.md) — 初次設定精靈（**本 ADR 延伸／修訂其可靠性與範圍**）
- [ADR-0052](0052-environment-aware-env-loading-and-gemini-regex-filtering.md) — 環境感知 .env 載入（TOUKA_ENV_FILE 覆寫延伸）
- [ADR-0099](0099-provider-registry-ssot.md) — Provider Registry SSOT（get_key_by_id、狀態衍生延伸）
- [ADR-0070](0070-chinese-vector-memory-migration.md) — 中文向量記憶（嵌入模型下載/切換/重嵌延伸）
- [ADR-0038](0038-init-container-volume-permissions.md) — Init 容器 volume 權限（hf-cache chown 延伸）
- [ADR-0082](0082-dispatch-in-proxy-consolidation.md) — Dispatch 併入 Proxy 容器
- [ADR-0116](0116-toukamemory-integration-only.md) — ToukaMemory 整合模式單一部署

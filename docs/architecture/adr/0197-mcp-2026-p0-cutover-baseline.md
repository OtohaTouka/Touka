# ADR-0197：MCP 2026 遷移 P0 cutover 基線與回滾閘

> **現況**：本文提到的 v1 特性矩陣、cutover inventory、v1 rollback smoke 腳本與兩份 rollback manifest 共 5 個檔，已在 ADR-0238 的退役 commit（`4a0bf452`）刪除；`config/mcp_servers/*/*.py` 是執行時產生的路徑樣式，從來不在版控。

- **狀態**：Accepted；本機 previous-image live smoke 已通過，registry durability 由 owner 延後
- **日期**：2026-07-30
- **相關**：[ADR-0191](0191-mcp-hub-tier-a-shelf-and-version-pinning.md)、
  [ADR-0194](0194-mcp-remote-shelf-and-manual-remote-entry.md)、
  [MCP 2026 完整遷移計畫](../proposals/mcp-2026-07-28-protocol-migration.md)
- **盤點**：[MCP 2026 P0 Inventory](../mcp-2026-p0-inventory.md)

## 脈絡

Touka 的 Python workspace 仍鎖 `mcp 1.26.0`，client、Creation Engine 與自有 server
使用 v1 `ClientSession`／`FastMCP`／`initialize()`；ToukaIDE 使用 Node SDK v1 與
legacy HTTP+SSE。更重要的是，現行 manager 以 server name 共用 handle、無 request
identity 列 catalog，且永久快取 `tools/call` 完成結果。若直接升 SDK，不只 import
會壞，也會把跨 principal 汙染帶進新的 capability 與 OAuth 生命週期。

P0 的任務不是修這些 blocker，而是讓它們全部可見、可重現，並使 P2 在 blocker
未處置時機械失敗。既有 Creation 產物與 `data/` 受 owner 主權保護，掃描器只能讀取
與報告，不能自動改寫或遷移。

## 決策

### 1. v1 行為以既有行為測試＋版本化 fixture 雙重固定

`tests/fixtures/mcp_v1/characterization_matrix.json` 把 ADR-0191／0194 及相關安全行為
映射到真實 pytest nodeid：connect timeout、401、取消／雙重取消、teardown、startup
隔離、circuit breaker、enabled gate、cleanup、靜態 header 消毒、vault jail 與
per-tool permission。矩陣測試會驗證每個錨點檔案與函式仍存在。

stdio 與 Streamable HTTP 另保存 `mcp 1.26.0`／protocol `2025-11-25` 的 normalized
raw-wire transcript，並以 v1 SDK JSON-RPC model 驗證。這些 fixture 只聲稱固定 v1
基線，不假裝在單一 v1 lock 中測過 modern-v2 peer。

### 2. inventory 是唯讀工具；cutover 是顯式 fail-closed 模式

`scripts/mcp_v2_cutover_inventory.py` 掃描：

- Python dependency／lock、MCP import、FastMCP、ClientSession、initialize、
  camelCase model、legacy SSE、HTTP／monkeypatch surface；
- ToukaIDE SDK／lock／SSE endpoint／build 與缺少的 test script；
- Docker、CI、release 與所有含 MCP 合約的文件；
- runtime `server_config.json(.example)` command 與 `config/mcp_servers/*` 生成產物；
- private catalog、identity-less call、global handle 與 global tool-result cache 四類跨
  principal 紅燈。

掃描永遠剪除 `data/`、venv、node_modules 與生成型 build 目錄。正常 inventory 有
blocker 仍回 0，方便產報告；只有 `--check-cutover` 回 2 阻止 P2。它不進 `guard_all`，
因 P0 到 P1／P2 期間 blocker 本來就必須存在；P2 完成條件是這個顯式 gate 轉綠。

### 3. generated artifact 未處置即阻擋，不自動修

任何 `config/mcp_servers/*/*.py` 含 v1-only marker 都產生
`generated.v1_artifact` blocker。owner 必須手動修正、停用，或另行核准與驗證隔離
v1 runner；掃描器不修改檔案，也不進 `data/`。目前本機 registry 是空集合且無
generated artifact；部署或 named-volume config 仍須在 P2 前以 `--config-dir`
對目標環境重跑，不能用本機空集合推論所有部署都空。

### 4. rollback 必須是 immutable image，不接受 mutable tag

`scripts/mcp_v1_rollback_smoke.py` 只接受：

- proxy／heartbeat／chat 三個 `@sha256:` image reference；
- 40-hex source commit；
- 經 SHA-256 驗證的 v1 lock snapshot；
- 可解析且含 `mcpServers` 的 v1 config fixture。

live smoke 透過 `docker-compose.ghcr.yml` 加 P0 override 執行，但所有 bind mount
都改指向 caller 指定的 sandbox，使用獨立 compose project 與 named volumes，不會
觸碰 repo `data/`、現行 workspace 或 `.env`；override 亦使用 rollback 專屬固定
container name，避免繼承正式 compose 名稱而撞到現行容器。compose 插值改由 sandbox
內的獨立 env file 注入，不直接讀取全域環境。未提供 digest 的 tracked example
刻意驗證失敗；`latest`／semver tag 也一律拒絕。

owner 於 2026-07-30 明示暫不發布 GHCR，接受本機 content-addressed RepoDigest
作為本次遷移 rollback 基線。`rollback_manifest.local.json` 固定三個本機 digest；
通用 `rollback_manifest.example.json` 仍維持 fail-closed，避免把本機接受誤表述為
跨主機可恢復的 registry artifact。

## 結果

- P0 新增 15 個測試；現行 v1 runtime 程式與依賴未改。
- cutover inventory 全部 finding 都有 code、category、remediation 與 blocking
  分類，沒有 `unclassified`。
- 四個跨 principal 現況以紅燈固定，留待 P1 真正修復。
- 本機 P2 gate 目前非零是正確結果，不是測試失敗。
- previous-image live smoke 已以本機 immutable digest 通過 Proxy `/health`、
  Heartbeat `/status` 與 Chat `/`；專屬容器、network、volumes 均於 finally 清除，
  原 `touka` 三服務持續運行。
- 首次 live smoke 揭露 Heartbeat 啟動期 `RemoteDisconnected` 被誤判失敗；runner
  已改為對拒絕連線、提前關閉與暫時 HTTP error 作有限重試，並新增回歸測試。
- 終態 pytest 4039 passed／5 skipped；除既知 Guard #23 npm audit 外，其餘 43
  個 guards 皆通過。受限環境無法讓 `npx` 讀使用者 npm cache 時，16 個 TypeScript
  guards 另以本地 esbuild 等價執行並全部通過，未下載套件。

## 本機 rollback 邊界

P0 checklist 已完成，可進 P1。本次接受的是**本機** rollback：若刪除／prune 下列
images，manifest 仍會 fail-closed，但無法自行從 registry 復原。GHCR 發布前不得把
這份證據宣稱為跨主機災難復原能力；P2 前亦須確認三個 digest 仍存在。

- Proxy：`sha256:756e926734560c73681962a68397d455ff057247d306fc94f6ee15cf5501f64d`
- Heartbeat：`sha256:a7bb007bf73a1561f41eb21b0fa1111efbc5238ca90195e54b09e880e9b611e7`
- Chat：`sha256:a0cc4ad9ef8be66b93679ffc0aa97c85ff97f7dc45267a80ff5f8a035a93c5c8`

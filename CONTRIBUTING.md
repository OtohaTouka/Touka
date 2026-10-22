# Contributing to Touka

感謝你對 Touka 的興趣！本文件說明如何回報問題與提交貢獻。

---

## 🐛 回報 Bug

請使用 [GitHub Issues](https://github.com/OtohaTouka/Touka/issues) 回報問題。

回報時請盡量提供：
- 復現步驟
- 預期行為 vs 實際行為
- 系統環境（OS、Python/Node.js 版本、部署方式）
- 相關日誌（請先移除任何敏感資訊）

**安全弱點**：請勿在公開 Issue 中揭露。請用 GitHub 的私下漏洞回報：<https://github.com/OtohaTouka/Touka/security/advisories/new>，詳見 [SECURITY.md](SECURITY.md)。

---

## 💡 提交 Pull Request

### 環境設置（首次接手 / fresh clone）

**前置需求**：[uv](https://docs.astral.sh/uv/)、Node.js 24.21.0／npm 11.19.1、git。後端鎖定 **Python 3.12**（`.python-version`，uv 自動選用）。

```bash
# 1. 後端：一鍵佈建（uv workspace 單一 uv.lock：全部子專案依賴＋CPU 版 torch＋測試工具 dev group）
uv sync --locked --all-packages

# 2. 前端（lifecycle 關閉，顯式生成 artifact runtime）
cd ToukaChat
npm run install:controlled   # npm ci --ignore-scripts＋braces 回補與驗證（同 CI）
npm run gen:artifact-runtime
cd ../ToukaIDE
npm run install:controlled   # pytest 的 companion 測試要用它的 esbuild
cd ..

```

Windows PowerShell 啟用後驗證：

```powershell
.\.venv\Scripts\Activate.ps1
python -B scripts/guard_all.py
python -B -m pytest tests/
```

已具備相容開發環境的 Bash 使用者：

```bash
source .venv/bin/activate
python -B scripts/guard_all.py
python -B -m pytest tests/
```

兩者都必須讓 root venv 的 Scripts/bin 位於 PATH 最前；這些開發命令不是新增平台的產品安裝保證。

> **★ `uv sync` 必帶 `--all-packages`**：root 是虛擬協調者（無依賴），裸 `uv sync` 是 exact 同步 → 會**剝除全部成員依賴**（venv 歸零；數秒可用 `--all-packages` 復原，但誤剝後測試全紅）。`uv run <cmd>` 則安全（inexact，不剝除、可直接用）。（ADR-0117）
>
> **依賴變更（ADR-0117）**：改對應子專案 `pyproject.toml` 的 `dependencies`（開發工具改 root `[dependency-groups].dev`）→ 跑根目錄 `uv lock` → `uv sync --all-packages` → commit 重生的 `uv.lock`（**嚴禁手改**；禁令 53 / Guard #30 離線比對 pyproject↔uv.lock）。torch 已納入 uv.lock（CPU index，`[tool.uv.sources]`），無需另裝。
>
> **自癒的生成檔**：`ToukaProxy/config/errors.json`（後端錯誤碼 SSOT）與 `ToukaChat/src/generated/artifactRuntime.ts`（前端 artifact runtime）皆為 gitignore 生成檔，分別由 sandbox config bootstrap 與顯式 `npm run gen:artifact-runtime` 生成；`.npmrc` 關閉 lifecycle，不能假定 `npm ci` 會執行 postinstall。

既有 `ToukaProxy/setup.ps1`／`setup.sh` 是上述 uv 佈建的便利入口：從任何 CWD 都定位同一 repo，只更新開發 venv，依 `.python-version` 核對結果並透傳失敗碼；不建立 data/config/.env、不啟動 Docker。Windows 使用已允許執行本機腳本的 PowerShell；不可為此變更整機政策。兩個舊 `install_torch` 選單已退役，torch 由 CPU lock 供給，不再以裸 pip 或 GPU 選單覆寫。

原 `ToukaProxy/run_proxy.ps1` 僅供明確選擇 native 開發的使用者，使用 root venv、loopback、`--no-access-log`，埠占用直接失敗，不停止現有服務。啟動真正服務會經產品 bootstrap 寫入所配置資料；本批開發驗證只用 sandbox 假命令，沒有真 first-user 安裝測試。各子系統獨立安裝政策尚未擴充。

### PR 前必做

**CI 綠燈就能開 PR。** 下面每一項都標了誰跑：

- **「CI 會跑」**：GitHub Actions（`.github/workflows/native-tool-contracts.yml`）在 PR 上自動跑，全部要通過。想先在本機確認，就在**啟用的 root 3.12 venv** 內、用 Node 24.21.0／npm 11.19.1 照指令跑。本專案驗證優先原生 `python -B -m pytest`；如依 AGENTS 使用 `uv run`，仍須證明 interpreter、來源與 lock 正確，不混用全域 Python。
- **「維護者合併前補跑」**：CI 不跑，因為要特定環境或比較久。維護者會在合併前補跑，你能跑的話歡迎先跑。

```bash
# 1. 後端 pyflakes（CI 會跑）：四個產品子專案不能有未定義名稱、沒用到的匯入或變數，必須 0 錯誤
ruff check --select F ToukaProxy/src ToukaMemory/src ToukaHeartbeat ToukaDispatch

# 2. 後端完整 ruff 只減不增（CI 會跑）：每條規則的錯誤數必須和 tests/lint/ruff_baseline.json 完全相等，
#    CI 在第 5 項的 pytest 裡檢查（tests/lint/test_ruff_ratchet.py）。修掉舊錯誤後，重拍基準一起提交：
#    python -B scripts/lint_ratchet.py ruff --update
python -B scripts/lint_ratchet.py ruff

# 3. 前端 ESLint 只減不增（CI 會跑）：Guard #35 跑完整 ESLint，對照 scripts/eslint_baseline.json，
#    任一規則的 error 數比基準多就失敗（warning 不算）。要看逐條結果：cd ToukaChat && npm run lint
python -B scripts/guard_eslint_ratchet.py

# 4. 架構守衛（維護者合併前補跑）：guard_all 跑全部守衛，其中 lint_ssot、guard:api、guard:typecheck、
#    權限登記、ESLint 棘輪、原生工具合約這幾道 CI 也會跑。啟用 venv 後 .venv\Scripts 已在 PATH 最前，
#    guard_all 內部 shell 子命令才會走 3.12
python -B scripts/guard_all.py

# 5. 後端測試（CI 會跑）：新增測試檔時，在 tests/README.md 加一列（檔名放在反引號裡、不寫案例數），
#    tests/lint/test_tests_readme_listing.py 會檢查
python -B -m pytest tests/

# 6. 前端測試（CI 會跑）：npm run test 會先產生 artifact runtime 再跑 vitest
cd ToukaChat && npm run test && cd ..

# 7. ToukaIDE 測試、型別檢查、建置（CI 會跑）：照順序一個跑完再跑下一個，compile 與 package 會寫同一個 dist/
cd ToukaIDE && npm run install:controlled && npm test && npm run typecheck && npm run compile && cd ..

# 8. ToukaIDE 打包（維護者合併前補跑）：接在第 7 項之後跑
cd ToukaIDE && npm run package && cd ..
```

### 程式碼風格

- **Python**：`ruff` 自動格式化，行長 100 字元
- **TypeScript**：ESLint + Prettier，設定見 `ToukaChat/eslint.config.mjs`
- **禁令**：請勿繞過 `# noqa` 或 `// eslint-disable`，若有必要請在 PR 說明原因並登記於 `docs/architecture/NOQA_REGISTRY.md`

### 測試要求

- 新功能必須附帶測試（TDD 優先）
- 測試不得直接讀寫 `data/` 目錄（使用 `conftest.py` 的 sandbox fixture）
- 測試 fixture 請使用中性名稱（如 `"TestAgent"`, `"Alice"`, `"DebugAgent"`），不要使用真實 Agent ID
- 隔離的真核心測試只放 `tests/integration/test_isolation_linux.py`（marker `isolation_linux`，Windows 上整個模組跳過）；其他測試由 conftest 的隔離替身代勞，在 Docker 跑真核心的方法見 `tests/README.md`；執行器的端到端在 `tests/docker/isolation.compose.yml`（CI 的容器 job 在 PR 改到隔離相關路徑時自動跑，路徑清單在 workflow 裡；`isolation` 標籤可以強制跑）
- 隔離相關的結構測試會擋：隔離邊界（`sandbox_kernel/**`、`services/isolation/**`）用 `child_python_env`、`get_full_env`、`get_clean_env`；ResourceManager 新增碰檔案的公開方法卻沒有 `permission_repair` 或豁免裝飾器（同時更新 `tests/lint/test_resource_manager_permission_repair.py` 的清單）；Settings 新增 `*_URL` 或模型層新增 `route_url_attr` 卻沒在 `services/isolation/block_ports.py` 的上游清單或排除清單歸類

### Commit 訊息格式

```
feat: 簡短說明功能（50 字以內）
fix: 簡短說明修復
docs: 文件更新
test: 測試相關
refactor: 重構
```

---

## 📁 架構說明

詳見 `docs/architecture/` 目錄下的設計文件：

- `PROXY_INTERFACE.md` — ToukaProxy API 規範
- `CHAT_INTERFACE.md` — ToukaChat 前端規範
- `SSOT_MASTER_MAP.md` — 全系統單一事實來源地圖
- `NOQA_REGISTRY.md` — 靜態守衛豁免登記冊

---

## ⚠️ 注意事項

- **Agent 資料保護**：嚴禁自動化修改 `data/soul/` 和 `data/legacy/` 下的任何檔案
- **環境變數**：不得在代碼中硬編碼任何密鑰、路徑或 token，請使用 `.env`
- **本機部署假設**：修改認證或網路相關代碼時，請考慮 `CLOUD_MODE=true` 的雲端場景
- **隔離啟動器與執行器**：`ToukaProxy/src/sandbox_kernel/` 只能用標準函式庫，執行器伺服器（`services/isolation/executor_server.py`、`executor_session.py`）只准標準函式庫、`execution_broker` 與彼此；改了 kernel、執行器伺服器或 `execution_broker.py` 之後要重建映像（`docker compose up -d --build`），沒重建前代理人指令一律判 `kernel_outdated` 或 `executor_version_mismatch`（Auto／YOLO 要人核准）（ADR-0272）
- **不經 Docker 跑時的隔離**：原生 Windows 一律未隔離（`native_platform`）：Auto／YOLO 每條代理人指令都跳卡，心跳裡的指令、演化、驗證指令在所有模式都被拒，IDE 聊天面板不顯示審批卡、等 180 秒後被拒。原生 Linux 要自己在主機設 `net.ipv4.tcp_fastopen=0`（核心有 MPTCP 時再設 `net.mptcp.enabled=0`），否則一律 `none`、核准後只隔離檔案；原生 Linux 沒有執行器（只有本機 Landlock）；沒有映像裡的唯讀啟動器，所以沒有信任根；用 systemd-resolved 的主機在隔離內沒有 DNS（ADR-0272）

---

本專案由 **Otoha** 個人維護。歡迎貢獻，但請理解回應時間可能不穩定。謝謝！

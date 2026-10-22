# ADR-0071: 依賴鎖檔與環境一致性守衛（Dependency Lockfiles & Environment Parity Guard）

## 狀態

✅ 已接受（Phase 0 快速修正 + Phase 1 uv 鎖檔 + Phase 2 Guard #30 + Phase 3 可重現開發 venv（uv pip sync + Check 4） + Phase 4 基底映像統一 slim 已實作）

> 🗑️ **鎖檔機制已被 [ADR-0117](0117-uv-workspace-single-lock-migration.md) 取代（2026-07-02）**：本 ADR 的 `requirements.in` / `uv pip compile --universal --generate-hashes` / `# lock-input-sha256:` 戳記，以及 `scripts/lock_requirements.py` / `scripts/sync_dev_venv.py` 與 torch「動態兩階段偵測排除鎖檔」特例，均已移除，改為**單一 uv workspace**（根 `pyproject.toml` `[tool.uv.workspace]` + 一份 `uv.lock`；torch 經 `[tool.uv.sources]` 綁 `pytorch-cpu` index 直接進鎖檔）。Guard #16/#30 已重寫為結構性 `pyproject`↔`uv.lock` 比對（取代 `lock-input-sha256` 戳記制）。**Phase 4 基底映像統一 slim 部分仍有效。**

## 日期

2026-06-01

## 背景

Touka 的 Python 相依以 4 個分散的 `requirements.txt`（ToukaProxy / ToukaMemory / ToukaHeartbeat / ToukaDispatch）管理，開發以 `pip install -r` 裝進共用 `.venv`、部署以 Dockerfile `pip install -r` 裝進各容器。雖然 ADR-0033（P20）已將版本策略由 `>=` 收斂為 `~=`，但仍存在「開發環境 ↔ 部署環境裝到的版本對不上」的長期痛點。

## 問題

1. **沒有真正的 lockfile**：`uv.lock` 為空殼（root `pyproject.toml` 的 `dependencies=[]`），實際相依寫在 `requirements.txt`，且**完全未釘間接相依（transitive）**。`~=`/`>=` 範圍在「安裝當下」才解析 → 不同機器、不同時間重建的映像裝到不同版本，無法重現。
2. **混用釘版策略**：ToukaMemory / ToukaDispatch 仍用 `>=`（違反「依賴主權」`~=` 政策，P20 當時只遷移了 Proxy / Heartbeat）。
3. **陳舊釘版未隨現實更新**：例如 ToukaProxy 釘 `qdrant-client~=1.7.0`，但 ADR-0070 已將實際運行版本升至 `1.18.0`；ToukaHeartbeat 釘 `pydantic-settings~=2.7.0`，實際裝的是 `2.14.1`——正是「dev ≠ deploy」的活證據。
4. **守衛盲區**：既有 `guard_dependencies.py`（Guard #16）只驗「能否 import」與「Dockerfile 是否 COPY 到所有 `.py`」，**不驗版本一致**。
5. **Python 版本宣告漂移**：`ruff.toml` 的 `target-version = "py314"`，但 `.python-version` / `pyproject` / 所有 Dockerfile 皆為 3.12。

## 決策

採 **uv `pip compile` 鎖檔（pip-tools 模式）＋ 一道環境一致性守衛**，分三階段：

### Phase 0 — 快速修正
- `ruff.toml` `target-version` `py314 → py312`。
- ToukaDispatch：選擇性 `COPY server.py session_manager.py` → `COPY . .` ＋ 新增 `.dockerignore`（禁令 47，消除物理漂移）。
- ToukaMemory / ToukaDispatch 的 `>=` → `~=`（於 `.in` 落實）。

### Phase 1 — uv 鎖檔（`.in` → `.txt`）
- 每個子專案維持人類編輯的 `requirements.in`（抽象、`~=`，floor 對齊**實際安裝版本**以修正陳舊釘版），由 `scripts/lock_requirements.py` 以 `uv pip compile --universal --generate-hashes` 編譯成**完整釘版＋雜湊**的 `requirements.txt`。**開發與 Docker 都安裝這份釘死的 `.txt`** → 跨平台、跨時間可重現。
- `.in` 是唯一人改來源、`.txt` 是其決定性投影（與「程式碼 → `openapi.json`」同款 SSOT 關係）。Dockerfile / docker-compose **不變**（仍 `pip install -r requirements.txt`，只是內容變釘死）。
- **torch 特例**：`torch` 與其 CUDA 相依（`nvidia-*` / `triton`）以**動態兩階段偵測**排除於鎖檔（名稱含 CUDA 版本、隨 torch 變動，故動態偵測而非寫死清單）；CPU 版 torch 仍由 Dockerfile / install scripts 單獨安裝，避免把 5GB+ CUDA 套件鎖進 `requirements.txt`。
- `requirements.txt` 檔頭含 `# lock-input-sha256:` 戳記（= 對應 `.in` 內容雜湊），供守衛離線驗證。

### Phase 2 — Environment Parity Guard（Guard #30）
新增 `scripts/guard_environment_parity.py`（純讀檔、**無網路**、秒級，整合進 `guard_all.py`），三條 fail-closed 檢查：
1. **鎖檔新鮮度**：每個 `requirements.txt` 戳記 == 對應 `requirements.in` 雜湊 → 離線、決定性地擋掉「改了 `.in` 卻沒重跑 `lock_requirements.py`」（戳記化的「重生產物 + 守衛比對」，比照 `update_openapi.py` ＋ Guard #17，但無需連網）。
2. **Python 版本一致**：`.python-version` / `pyproject` `requires-python` / 各 `FROM python:` Dockerfile / `ruff target-version` 全對齊（以 `.python-version` 為基準）。
3. **釘版政策**：`requirements.in` 不得含 `>=`。

### Phase 3 — 可重現開發 venv ＋ venv-parity advisory（SAFE 變體）
共用開發 `.venv` 過去是歷次 `pip install` 累積、不保證對齊鎖檔（dev↔deploy 的「最後一哩」）。新增 `scripts/sync_dev_venv.py`：一鍵 `uv venv` → `uv pip sync`（四份鎖檔**聯集**）→ 單獨裝 CPU torch，把 `.venv` 佈建到與鎖檔逐一致。**安全預設**：拒絕對 live `.venv` 動手（須 `--provision-live`），`--venv-path` 可指向 throwaway 驗證、`--dry-run` 只印命令。

- **uvicorn 統一為 0.41**：聯集 sync 原會卡在 uvicorn 衝突（Proxy/Heartbeat 直接釘 0.41，Memory/Dispatch 經 `mcp` 間接帶到 0.48）。在 Memory/Dispatch 的 `.in` 明確釘 `uvicorn~=0.41.0` 後四鎖一致——順手消除 dev(0.41)↔Memory/Dispatch 容器(0.48) 的隱性落差。
- **Guard #30 Check 4（opt-in `--venv` / `--venv-path`）**：`uv pip freeze` 唯讀比對 venv 與四鎖釘版（只報「裝的≠鎖的」、不報 extras，故 torch 與 union 轉移依賴不誤報）。**純資訊：一律 exit 0、不進 `guard_all`**，故 30/30 與跨機確定性不受影響。`setup.{ps1,sh}` 改指向 `sync_dev_venv.py`（Proxy 共用 venv）/ `uv pip sync`（Memory 自有 venv），皆保留 pip fallback。
- **附帶修正鎖檔雜湊污染**：Phase 1 曾以 PyTorch CPU index ＋ `--index-strategy unsafe-best-match` 加速 torch 排除，副作用是部分共用套件（如 `markupsafe`）的雜湊被記成 PyTorch index 鏡像版、與 PyPI wheel 不符 → `uv pip sync` 與純 `pip install -r`（如 Proxy 容器，未曾建置）會 hash mismatch。改回**純 PyPI 兩階段**（pass 1 ~0.3 秒動態探 CUDA 名、pass 2 帶雜湊排除 torch+CUDA），雜湊全來自 PyPI 正規 wheel、與安裝端相符（`markupsafe` 雜湊數 1→13）。原以為「兩階段慢」實為 `stdin` 阻塞（已由 `stdin=subprocess.DEVNULL` 修正），非解析慢。

### Phase 4 — 基底映像統一（消除 musl 盲區）
原 `python:3.12-alpine`（musl libc）的 **ToukaHeartbeat / ToukaDispatch 統一改用 `python:3.12-slim`（glibc）**，與 ToukaProxy 一致：① alpine 用 musl、缺 manylinux wheel，C-extension 相依（如 `pydantic-core`）須源碼編譯，且開發端（glibc）無法證明其在 musl 可裝（musl 盲區）；slim 用 glibc + manylinux，直接裝預編譯 wheel。② 非 root 使用者建立改用 Debian `useradd -r -u 1001 -m appuser`（對齊 ToukaProxy 的 SSOT，取代 alpine BusyBox 的 `adduser -D`）。已實機 `docker build` 驗證兩服務於 Linux glibc 成功安裝雜湊鎖檔（**musl 盲區消除，非僅宣稱**）。`tests/lint/test_dockerfile_base_images.py` 靜態守衛三個 Python 服務皆 slim、非 alpine、非 root。

### 強制力
依維護者決策，Guard #30 **放進 `guard_all.py` 手動執行**（不加 pre-push hook / CI）。深度連網檢查 `python scripts/lock_requirements.py --check`（重編並比對釘版集合）為選用的人工/CI 驗證。

## 設計決策

### 為何戳記化而非每次重編比對
`update_openapi.py` 的重生是**純函式、離線**，故 Guard #17 可放心重生再比對。但 `uv pip compile` 需連 PyPI、且解析結果隨時間變動——若守衛每次重編，`guard_all.py`（唯一閘門、頻繁執行）會變得**慢且脆（網路依賴）**。改以 `.in` 內容雜湊戳記，守衛離線即可擋掉主要漂移（改了 `.in` 沒重鎖）；hand-edit `.txt` 的邊角情形由「請勿手改」檔頭聲明＋ `--check` 深度模式涵蓋。

### 為何 uv `pip compile` 而非 uv workspace
workspace（per-subproject `pyproject.toml` + 單一 `uv.lock` + `uv sync`）最正確，但要改寫每個 Dockerfile（`pip install`→`uv sync`）、並處理「無 Dockerfile、以掛載內嵌」的 ToukaMemory，blast radius 過大。`pip compile` 維持 `requirements.txt` 的名稱與角色不變，Dockerfile / compose 零改動，最小侵入。

### 為何 torch 不進鎖檔
torch 預設從 PyPI 解析會拉進 ~5GB CUDA（`nvidia-*`）。專案既有流程是「先單獨裝 CPU 版 torch（`--index-url .../cpu`），再裝 `-r requirements.txt`」。排除 torch 讓鎖檔保持精簡、Dockerfile 不需加 PyTorch index；代價是 torch 版本靠手動釘、不在鎖檔保證範圍內（已知、可接受的侷限）。

## 後果

### 正面
- 開發與部署裝同一份釘版＋雜湊相依，跨時間/跨機器可重現；供應鏈完整性（hashes）。
- 順手修正既有漂移：Proxy `qdrant-client` 1.7→1.18、Heartbeat `pydantic-settings` 2.7→2.14、Memory/Dispatch `>=`→`~=`、ruff `py314`→`py312`。
- 守衛離線、快、可單元測試（TDD），不增加 `guard_all.py` 的網路脆弱度。

### 負面 / 已知侷限
- **Recompile 稅**：加/升套件＝改 `.in` → 跑 `lock_requirements.py` → commit 重生的 `.txt`（忘了會被 Guard #30 擋）。
- **torch 永遠特例**：鎖檔對非 torch 相依完全可重現，torch 靠手動釘＋獨立 index。
- **~~musl 盲區~~ → 已於 Phase 4 消除**：ToukaHeartbeat / ToukaDispatch 已由 alpine 遷至 `python:3.12-slim`（glibc/manylinux），三個 Python 服務基底一致並經實機 `docker build` 驗證；不再有「開發端無法證明 musl wheel 可裝」的盲區。
- **手動強制**：守衛只在有人跑 `guard_all.py` 時生效（依決策；日後可低成本升級為 pre-push hook）。

## 相關 ADR

- [ADR-0033](0033-infosec-hardening-c1-m6.md) — InfoSec 加固（P20：`>=`→`~=` 版本策略起點）
- [ADR-0070](0070-chinese-vector-memory-migration.md) — 中文向量記憶遷移（qdrant-client 1.18 升級來源）
- [ADR-0016](0016-dependency-sovereignty-and-sandbox-verification.md) — 依賴主權與沙盒化驗證（Guard #16 `guard_dependencies.py`）
- [ADR-0013](0013-time-sovereignty-and-api-parity-guards.md) — 「重生產物 + 守衛比對」模式先例（OpenAPI Parity / Guard #17）
- [ADR-0117](0117-uv-workspace-single-lock-migration.md) — uv workspace 單一鎖檔遷移（**取代本 ADR 的 `requirements.in`／`lock_requirements.py` 鎖檔機制、`lock-input-sha256` 戳記與 torch 排除特例**）

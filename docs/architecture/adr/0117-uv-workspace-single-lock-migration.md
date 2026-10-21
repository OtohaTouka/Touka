# ADR-0117: 遷移至 uv workspace 單一鎖檔（Single-Lock uv Workspace Migration）

## 狀態

✅ **已接受；uv workspace單一鎖檔為現行依賴SSOT**。本 ADR **修訂 ADR-0071**——推翻其「為何 uv pip compile 而非 uv workspace」的當時決定（理由見〈背景〉）。原Phase 0–4遷移與requirements管線退役均已完成；後續完整測試／映像資格證據由各發行批另行封存。

> **2026-09-04 修訂（ADR-0222）**：workspace現有五個members（含後加的
> ToukaReleaseAuthority）。Guard #22由單一runtime export再強化為`runtime --no-dev`＋
> `build-test --all-groups`兩個frozen scope；repo外暫存、缺工具不安裝、parser／exit error
> fail-closed。該15-path foundation現納入P11-NEXT 21-path cumulative candidate；本批只重鎖
> ToukaChat npm manifest／lock，五份Python member `pyproject.toml`與`uv.lock`均不在write set。
> 此修訂不是當前`pip-audit`或Guard #22已執行／clean的證據，兩者仍為NOT RUN。

## 日期

2026-07-02

## 背景

- **現況（ADR-0071）**：Python 相依以 4 份 per-subproject `requirements.in` → `requirements.txt`（各自 `uv pip compile --universal --generate-hashes` **獨立編譯**）管理；開發共用 `.venv` 由 `sync_dev_venv.py` 以 `uv pip sync`（四鎖**聯集**）+ 單獨裝 CPU torch 佈建；Docker 各服務 `pip install -r requirements.txt`。root `pyproject.toml` 的 `dependencies=[]`、`uv.lock` 為空殼。
- **ADR-0071 當時刻意否決 uv workspace**，理由（原文）：「workspace 最正確，但要改寫每個 Dockerfile（`pip install`→`uv sync`）、並處理『無 Dockerfile、以掛載內嵌』的 ToukaMemory，blast radius 過大」。
- **自 ADR-0071（2026-06-01）以來的關鍵變化**：
  - **ADR-0082**（2026-06-12）：ToukaDispatch 併入 ToukaProxy 容器（單一映像 + proxy-spawn 監督）。
  - **ADR-0116**（2026-06-25）：ToukaMemory 收斂為整合模式單一部署、退役獨立 MCP／獨立容器。
  - ⇒ 部署面從「4 個獨立 Python 服務」縮成 **2 個映像**：整合 Proxy 映像（Proxy＋Memory＋Dispatch）＋ Heartbeat 映像。**ADR-0071 否決 workspace 的 blast-radius 理由已大幅弱化**（要改的 Dockerfile 從 4 降到 2；ToukaMemory 不再是獨立部署單位）。
- **觸發動機（擁有者）**：曾發生「各子專案／測試／Docker 裝到不同版本的套件，除錯困難」的漂移事故；並希望改用 uv 原生工作流（`uv run`／`uv sync`，體感更快、免手動 activate）。

## 問題（為何獨立鎖檔還不夠）

1. **獨立解析不保證跨子專案一致**：每份 `.in` 各自 compile，共用傳遞依賴（starlette／anyio／httpx／pydantic…）可能收斂到不同版本。目前實測一致，但那是**人工紀律／巧合**維持，非鎖版方法的構造保證。AGENTS.md 自舉的「starlette 0.52 vs 1.1」即此模型下的翻車紀錄。
2. **守衛盲區**：Guard #30 只驗「每份 `.in↔.txt` 新鮮度 + Python 版本一致 + `>=` 政策」，**不驗跨子專案版本一致** → 漂移可無聲進 CI。
3. **test ≠ deploy 風險**：dev/test venv 是四鎖聯集、部署各裝各的鎖檔；一旦分歧就是「測試綠、Docker 壞」。
4. **uv 原生工作流不可用**：`uv.lock` 空殼 → 裸 `uv run <cmd>` 會先 `uv sync` 把 venv 對齊到空鎖檔、**清空全部套件**（`uv sync --dry-run` 實測「Would uninstall 128 packages」）。擁有者想要的 uv 便利拿不到。

## 決策（提案）

採 **uv workspace ＋ 單一 `uv.lock`**：整個 repo 一次解析，**所有共用依賴依構造保證同一版本**（跨 dev／test／每個 Docker 映像位元相同）。

### 架構

- root `pyproject.toml` 宣告 `[tool.uv.workspace]`；現行members為`ToukaProxy`、`ToukaMemory`、`ToukaHeartbeat`、`ToukaDispatch`與`ToukaReleaseAuthority`。後者是dormant release-authority package，不會因此進入local product runtime。
- 每個子專案以自己的 `pyproject.toml`（`[project].dependencies`，人改抽象、維持 `~=`/`==`、**禁 `>=`**）取代 `requirements.in`。
- **單一 `uv.lock`**（`uv lock` 生成，含 hashes）為全 repo 唯一鎖檔 SSOT；workspace resolver 對整張圖一次求解，共用依賴不可能分歧。
- **torch 納入鎖檔（Option Y，Phase 0 已驗證）**：以 `[[tool.uv.index]] pytorch-cpu` ＋ `[tool.uv.sources] torch = { index = "pytorch-cpu" }` 讓 torch 解析為 CPU 版並**納入單一 `uv.lock`**（實測 `2.12.1+cpu`、鎖檔零 `nvidia-*`）。此取代 ADR-0071「torch 排除鎖檔、單獨裝」的特例——torch 成為可重現鎖定的一員（原侷限反被移除）。

### 佈建與部署

- **開發**：`uv sync`（取代 `sync_dev_venv.py` 的聯集 `uv pip sync`）。副產物：裸 `uv run <cmd>` 名正言順可用（解決問題 4）、`uv` 安裝速度不變。
- **Docker（精簡 image 的關鍵）**：workspace 支援 **per-member 安裝**——整合 Proxy 映像 `uv sync`（取 Proxy＋Memory＋Dispatch 需要的集合）；**Heartbeat 映像用 `uv sync --package toukaheartbeat` 只裝自身子集**，不被迫扛 torch/transformers/qdrant。這是 workspace 優於「option ③ 單一 union 鎖檔」之處（後者會讓 Heartbeat 映像膨脹）。

### 守衛

- **改寫 Guard #30**（`guard_environment_parity.py`）：原「每份 `.in↔.txt` 戳記新鮮度」→ 改為「`uv.lock` 與各 `pyproject.toml` 一致（`uv lock --locked`／離線戳記）」。**跨子專案一致性由單一 lock 構造保證**，不再需要跨檔版本比對。保留「Python 版本一致」與「禁 `>=`」檢查（移到 pyproject 掃描）。

## 遷移計畫（分階段、每階段可回退）

| Phase | 內容 | 風險 | 回退 |
|---|---|---|---|
| **0 — PoC** | 在 throwaway 分支建 workspace `pyproject`＋`uv lock`；比對生成的釘版集合 vs 現有四鎖（證明版本等價、無退版）。不動生產。 | 極低 | 丟棄分支 |
| **1 — pyproject 化** | 各子專案 `requirements.in` → `pyproject.toml [project.dependencies]`；`uv lock` 生成單一 `uv.lock`；throwaway venv `uv sync` 驗證可裝、pytest 全綠。 | 低 | 保留 `requirements.*` 不刪 |
| **2 — 佈建切換** | `sync_dev_venv.py` → `uv sync`（保留舊腳本一個 release 當 fallback）；文件（CONTRIBUTING/README/AGENTS）開發指令改 `uv sync`/`uv run`。 | 中 | 舊腳本仍在 |
| **3 — Docker 切換** | 2 個 Python Dockerfile：`pip install -r requirements.txt` → `uv sync`（Heartbeat 加 `--package`）；**實機 `docker build` 驗證** slim/glibc + hashes 可裝、映像大小回歸（Heartbeat 不得膨脹）。 | 中高 | Dockerfile 可暫時雙軌（保留舊 COPY+pip 路徑於註解） |
| **4 — 守衛/禁令切換** | 改寫 Guard #30；改寫 **禁令 53**（requirements.in/txt → pyproject/uv.lock）；退役 `lock_requirements.py`、刪除 `requirements.*`；更新相關 lint 測試（如 `test_dockerfile_base_images.py`）。 | 中 | Git revert 該 commit |
| **5 — 驗收** | `guard_all` 37/37＋pytest 全綠＋兩映像實機 build＋image 大小報告＋`uv run`/`uv sync` 冒煙。修訂 ADR-0071 標記、更新本 ADR 狀態為「已接受」。 | — | — |

### Phase 0 結果（2026-07-02 實測，零觸碰生產）

以 `uv pip compile --universal` 對 4 份 `requirements.in` 做**統一解析**（＝ workspace 單一 `uv.lock` 會產生的解析；同一 resolver、universal 模式）並與現行 4 鎖檔逐一比對（torch/CUDA 兩邊皆排除）：

- 統一解析 **零衝突**（compile exit 0）→ workspace 會乾淨鎖定。
- 現行 4 鎖檔目前**無內部衝突**（人工維持的巧合一致、無守衛保護——正是本 ADR 要用構造保證取代的脆弱點）。
- 統一 vs 現行聯集：**套件集合完全相同**（0 增 0 減）、**97 個版本相同、11 個升版、0 退版**。
- 那 11 個升版經證實為**時間漂移**（現行鎖檔切版後 PyPI 出的新補丁，如 anyio 4.14.0→4.14.1、click 8.4.1→8.4.2、huggingface-hub 1.20.1→1.21.0…）：**單獨**重編 ToukaProxy 也挑到相同新版 → **「統一」本身造成的版本差為 0**。

**(b) workspace 機制驗證（scratchpad 最小 workspace + `uv lock`，2026-07-02 實測）**：root `[tool.uv.workspace]`（4 member、`package = false` 虛擬成員）＋ `[[tool.uv.index]] pytorch-cpu` ＋ `[tool.uv.sources] torch = { index = "pytorch-cpu" }` → `uv lock` **成功、112 套件、1.3 秒**。torch 解析為 **`2.12.1+cpu`**（來源 `download.pytorch.org/whl/cpu`），**鎖檔內零 `nvidia-*`／CUDA 套件**（112 = 統一 compile 的 125 減掉 13 個 CUDA family）。
- ⇒ **torch 可直接納入單一 `uv.lock`（採 Option Y）**：由 `[tool.uv.sources]` 綁 CPU index，無需 CUDA、無需鎖檔外單獨裝。**這反而移除了 ADR-0071「torch 永遠是鎖檔外特例」的侷限**——遷移後 torch 也成為可重現鎖定的一員。
- ⚠️ 未做的 Phase 1 驗證：`uv sync` 實際裝進 throwaway venv（會下載 CPU torch/transformers）＋ pytest 全綠，留待 Phase 1。

**結論：版本層面的遷移風險 ≈ 0**（無衝突、同套件集、無退版）＋ **workspace 機制與 torch CPU 綁定實測可行**。剩餘風險為機械性——Docker 實機 build 驗證、守衛/禁令改寫——屬 Phase 1+，非解析風險。Phase 0 判定 **GO**。

### Phase 1 結果（2026-07-02 實測，零觸碰生產）

在 scratchpad 的最小 workspace（同 (b) 結構）做**實際安裝＋跑現有測試**的等價驗證：

- `uv sync --all-packages` → 真的安裝 **108 套件**進 throwaway venv：torch **`2.12.1+cpu`**、**零 CUDA**；fastapi／qdrant-client／sentence-transformers／transformers／mcp／discord／telegram 等重量 import **全通過**。
- 以**該 workspace venv 的 python** 跑 **live repo 的完整 `pytest tests/`**（確定性順序）：**2453 passed / 3 skipped / 0 failed**——與現行 `.venv` 基準**位元等價**。
- 版本 diff（workspace-locked vs 現行 4 鎖檔）＝ Phase 0(a) 那 11 個時間漂移升版、**0 退版、同套件集**。

⇒ **功能等價證實**：workspace 單一 `uv.lock`（含 torch CPU 納入 lock）裝出的環境，通過全部現有後端測試。全程 live `.venv`／`pyproject.toml`／`requirements.*` 未動、無新增 repo 檔（workspace 全在 scratchpad）。**剩餘**：Phase 2（佈建腳本切換）、Phase 3（Docker 實機 build，尤其 Heartbeat `--package` 精簡）、Phase 4（Guard #30／禁令 53 改寫）——皆機械性、可自動驗證。

### Phase 2 結果（2026-07-02 落地，repo 實際切換）

**workspace 正式進 repo、live venv 切換為 workspace 管理**：

- **新增 4 個成員 `pyproject.toml`**（依賴逐字取自各 `requirements.in`；Proxy/Memory 加 `torch~=2.12.1`）＋ **root `pyproject.toml`** 加 `[tool.uv.workspace]`／`[[tool.uv.index]] pytorch-cpu`（`explicit=true`）／`[tool.uv.sources] torch`／`[tool.uv] package=false`。
- **`[dependency-groups] dev`（政策變更）**：pytest／pytest-asyncio／pytest-randomly／pip-audit 納入鎖檔 dev group（`uv sync` 預設安裝、不進 Docker）——**退役**「測試工具刻意不入鎖檔」舊政策（單人＋AI 協作下鎖進 lock 更可重現；`tests/README.md` 同步改）。
- **真 `uv.lock` 生成**：134 套件（112 執行期＋dev group），torch `2.12.1+cpu`、零 `nvidia-*`，dev 工具版本與現用完全一致。
- **live `.venv` 切換**：`uv sync --all-packages` 僅置換 10 個補丁套件（130 持平）；**pytest 2453 passed／3 skipped（確定性順序）＋ guard_all 37/37 全綠**。
- **uv 行為實測（決定文件寫法）**：裸 `uv run`＝**inexact 同步、不剝除、安全**；裸 `uv sync`＝**exact 同步、剝空 venv（0 套件）**、`--all-packages` 數秒復原 → 文件規範「佈建一律 `uv sync --all-packages`」。
- **文件切換**：`CONTRIBUTING.md`／`README.md`（一鍵佈建）＋ `AGENTS.md` 直譯器主權（`uv run` 解禁、裸 `uv sync` 警告）＋ `tests/README.md`。`sync_dev_venv.py` 保留為 fallback（Phase 5 退役）。
- **⚠️ 過渡期雙軌**：Phase 3 前 Docker 仍裝 `requirements.txt`——依賴變更須同步「成員 `pyproject.toml`＋root `uv lock`」與「`requirements.in`＋`lock_requirements.py`」兩邊。Guard #30 現行檢查（`.in↔.txt` 戳記）不受影響、維持綠。

### Phase 3 結果（2026-07-02 落地，Docker 切換＋實機 build 驗證）

**兩個 Python 映像改由單一 `uv.lock` 佈建（`uv sync --locked --no-dev`）**：

- **ToukaProxy/Dockerfile**（root context 不變）：「pip 單獨裝 torch ＋ pip install -r」→ 先 COPY workspace 依賴描述檔（保 layer cache）→ `uv sync --locked --no-dev --all-packages`（整合映像＝全 workspace 依賴，免除「Proxy 依賴必為 Memory/Dispatch 超集」隱性不變量）；venv `/app/.venv` 入 PATH；`--mount=type=cache` 加速重建。
- **ToukaHeartbeat/Dockerfile**：改 **root context**（compose `context: .`＋`dockerfile:`；standalone compose `context: ..`）＋ **multi-stage**（uv 只在建置階段、runtime 無套件管理器）＋ `uv sync --locked --no-dev --package toukaheartbeat`（**精簡子集：無 torch/transformers，容器內 `import torch` 實測 ModuleNotFoundError**）。映像佈局刻意維持 `/app`＝子專案內容 → **compose 的 `/app/awaken.py`、`/app/config.json` 掛載零改動**。context 過濾改用 BuildKit **per-Dockerfile ignore**（`ToukaHeartbeat/Dockerfile.dockerignore` 白名單制；舊 `ToukaHeartbeat/.dockerignore` 已失效故移除）；根 `.dockerignore` 增 `!ToukaHeartbeat/pyproject.toml`（workspace 驗證需全成員 pyproject）。
- **映像大小（實機 build 比對基準）**：proxy **2.6GB**（基準 2.66GB ✅ 略小）；heartbeat **207MB**（基準 220MB ✅ 略小）。
  - ⚠️ **踩雷紀錄（重要）**：初版把依賴裝進 venv 後仍沿用結尾 `chown -R appuser /app`——**overlayfs 的 chown 會把整個 venv 重複進第二個 layer**（proxy 2.66→4.5GB、heartbeat 220→327MB）。修法＝venv 維持 root 擁有（appuser 只讀執行，與舊「系統 site-packages」等價）＋源碼 `COPY --chown` 一次到位＋`/app` 目錄本身非遞迴 chown（執行期可建新檔）。heartbeat 再以 multi-stage 拔掉 runtime 的 uv 二進位（~40MB）後才降到 207MB。
- **runtime 冒煙（容器內實測）**：兩映像 python/uvicorn 解析至 venv、以 `appuser` 執行、`/app` 可寫；proxy `torch 2.12.1+cpu`＋fastapi/mcp/qdrant 匯入 OK；heartbeat apscheduler/fastapi/httpx/pydantic-settings OK。
- **守衛/測試同步**：Guard #16 `check_dockerfile_parity` 擴充接受「`COPY [--chown] . .`」與「`COPY [--chown] <專案目錄>/ .`」兩個等價變體；`test_dockerfile_dispatch.py` 的 full-copy 斷言同步接受 `--chown`。Dockerfile 相關 lint/unit 測試 29/29、Guard #16 4/4、`docker compose config` 解析通過。
- **注**：`requirements.in/.txt` 與 `lock_requirements.py` 尚未退役（Phase 4）——Dockerfile 已不再使用它們，但守衛（#30）與 parity 測試仍讀取，雙軌維持至 Phase 4。實機 `docker compose up` 全鏈路驗收（需擁有者 `.env`/data volume）留待 Phase 5。

### Phase 4 結果（2026-07-02 落地，守衛/禁令切換＋requirements 全面退役）

**雙軌結束——依賴管理只剩 pyproject＋單一 `uv.lock` 一條軌**：

- **退役刪除（11 檔）**：4× `requirements.in`＋4× `requirements.txt`＋`scripts/lock_requirements.py`＋`scripts/sync_dev_venv.py`＋`tests/unit/test_sync_dev_venv.py`。
- **Guard #30 改寫**（`guard_environment_parity.py`）：鎖檔新鮮度由「`.in↔.txt` 雜湊戳記」→ **pyproject↔uv.lock 離線逐項比對**（stdlib `tomllib` 解析成員 `[project].dependencies`／root dev group vs 鎖檔記錄的 `requires-dist`／`requires-dev`，名稱＋規格集合相等；比戳記制更強——能精準指出漂移是哪個套件）。Python 版本檢查擴及成員 pyproject 與 multi-stage Dockerfile；`>=` 禁令改掃 pyproject。Check 4 advisory 改比對 venv freeze vs uv.lock 版本集（同名多版本如 torch/+cpu 任一即合；未裝不報——per-member 子集屬正常）。
- **Guard #16 改寫**（`guard_dependencies.py`）：沙盒佈建由「pip install requirements.txt＋torch 特例」→ **`uv sync --frozen --no-dev --package <member>`（`UV_PROJECT_ENVIRONMENT` 指向快取 venv）**；快取鍵＝sha256(uv.lock＋成員 pyproject)；torch 特例刪除（鎖檔 CPU index 供給）。匯入測試與 Dockerfile parity 檢查不變。
- **Guard #22 改寫**（`guard_python_cve.py`）：Phase 4當時由掃2份requirements.txt改為`uv export --frozen --all-packages --no-dev`。ADR-0222再修訂為現行五member的runtime與build/test雙scope，並移除repo內export與tool自動安裝。
- **測試同步**：`test_guard_environment_parity.py` 重寫（21 測：規格漂移/增刪依賴/dev group/multi-stage FROM/torch 多版本/advisory 不翻 exit）；`test_dispatch_deps_parity.py` 改新契約（proxy Dockerfile 必帶 `--all-packages`＋dispatch 依賴 ∈ uv.lock——ADR-0082 防漂移保證的結構化版本）；`test_sec_batch_delta.py` P20 改掃 pyproject（含 root dev group）。
- **禁令 53 改寫**（AGENTS.md，ADR-0071 amended by ADR-0117）：SSOT＝pyproject＋uv.lock；變更流程＝改 pyproject → `uv lock` → `uv sync --all-packages` → commit；嚴禁手改 uv.lock、嚴禁裸 `uv sync`。
- **配套**：`setup.ps1/.sh` 佈建改 `uv sync --all-packages`；`ToukaHeartbeat/README`、`tests/README`、CONTRIBUTING（移除過渡期雙軌註記）、成員 pyproject 檔頭同步。
- **誠實取捨**：Guard #30 新 Check 1 只驗「宣告 ↔ 鎖檔記錄」一致，不重新解析（離線、秒級）；「鎖檔記錄 ↔ 實際可解析」由 Docker build 的 `uv sync --locked` 與 Guard #16 的 `--frozen` 沙盒安裝把關（縱深防禦）。

## 影響範圍（採納後要改的檔）

- **禁令 53**（AGENTS.md §18）— 重寫依賴主權為 pyproject/uv.lock 模型。
- **Guard #30**（`scripts/guard_environment_parity.py`）— 重寫。
- **ADR-0071** — 修訂（其 workspace 否決被本 ADR 推翻）。
- `scripts/sync_dev_venv.py`、`scripts/lock_requirements.py` — 退役/改寫。
- 各子專案 `requirements.in`/`requirements.txt` → `pyproject.toml`（root ＋ 4 members）。
- 2 個 Python `Dockerfile`（Proxy、Heartbeat）＋ 可能的 `setup.{ps1,sh}`。
- 文件：`AGENTS.md`（直譯器主權段）、`CONTRIBUTING.md`、`README.md`。
- 測試：`tests/lint/test_dockerfile_base_images.py` 及依賴守衛相關測試。

## 設計決策 / 取捨

- **為何現在重議 workspace（vs ADR-0071 否決）**：① 整合模式（ADR-0082/0116）把部署面 4→2，blast radius 大幅縮小；② 擁有者的漂移痛點需要**構造保證**而非人工紀律；③ 順帶滿足 uv 原生工作流需求。
- **為何 workspace 優於替代方案**：
  - vs **option ①（共用 constraints ＋ 跨專案一致性守衛）**：constraints 只保證「列進去的」套件、需靠守衛補漏（雙保險）；workspace 對**整張圖**構造保證，且免維護 constraints 檔。
  - vs **option ③（單一 union 鎖檔）**：union 會讓分離的 Heartbeat 映像扛全套（torch/transformers）；workspace 可 per-member 精簡安裝。
- **已知風險 / 侷限**：① 一次性遷移工程量大（多檔、跨 Docker）；② Docker build 必須**實機**驗證（slim/glibc + hashes，開發端無法純靜態證明）；③ torch 仍是特例（`[tool.uv.sources]` 或鎖檔外）；④ 綁定 uv 作為必要建置工具（本專案已重度使用 uv，屬可接受）。
- **回退策略**：Phase 1 保留 `requirements.*` 不刪；Phase 3 Dockerfile 可雙軌；任一 Phase 失敗即 revert 該 commit，不影響前序。

## 後果

### 正面
- **漂移不可能發生**：單一 lock、一次解析，dev＝test＝每個映像位元相同（直接根治擁有者痛點）。
- uv 原生工作流可用（`uv run`/`uv sync`），移除「裸 `uv run` 會清空 venv」的地雷。
- 移除自製鎖檔管線（`lock_requirements.py`＋四份 `.in/.txt`＋聯集 sync）的維護面。

### 負面 / 已知侷限
- 一次性遷移成本與風險（尤其 Docker 實機驗證）。
- torch仍以`[tool.uv.sources]`綁定CPU index，但版本與artifact identity均在單一lock內，不是鎖檔外安裝特例。
- 對 uv 版本/行為的耦合加深。

## 相關 ADR

- [ADR-0071](0071-dependency-lockfiles-and-environment-parity.md) — 依賴鎖檔與環境一致性守衛（**本 ADR 修訂／推翻其 workspace 否決**）
- [ADR-0082](0082-dispatch-in-proxy-consolidation.md) — Dispatch 併入 Proxy 容器（縮小部署面）
- [ADR-0116](0116-toukamemory-integration-only.md) — ToukaMemory 整合模式單一部署（縮小部署面）
- [ADR-0033](0033-infosec-hardening-c1-m6.md) — `>=`→`~=` 版本策略起點
- [ADR-0016](0016-dependency-sovereignty-and-sandbox-verification.md) — 依賴主權與沙盒化驗證（Guard #16）
- [ADR-0068](0068-adr-integrity-guard.md) — ADR 完整性守衛（本檔編號主權依據）
- [ADR-0222](0222-dependency-audit-gate-hardening.md) — Guard #22雙scope、repo外暫存與fail-closed parser修訂

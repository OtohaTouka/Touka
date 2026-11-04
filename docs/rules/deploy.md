# 部署與建置規則

範圍：Dockerfile、compose、依賴、CI 與 `scripts/**`。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 禁令

*   **禁令 47（依賴與部署主權）**：必須驗證各 workspace 成員在單一 `uv.lock` 下的依賴（成員 `pyproject.toml` 漏列直接依賴時沙盒匯入即失敗）與 `Dockerfile` 的同步性——由 `guard_dependencies.py` 執行沙盒匯入測試與「Dockerfile 物理完整性檢查」，作為 Docker Build 前的**強制預檢**。Guard #16 在任何依賴快取工作前，必須先依 [ADR-0019](../architecture/adr/0019-secure-session-management-and-deployment-parity-guard.md) 掃描 repo-local `.pytest_cache`：只驗 metadata／開檔權限、不讀內容、不追蹤 symlink／junction／reparse、拒絕 hard link 與不確定狀態；只回報相對路徑，且不得進入任何 `data/`。顯式 pytest `--basetemp` 若等於 repo 或位於其下，或 Windows 路徑無法證明安全，必須在啟動時拒絕。嚴禁在 `Dockerfile` 手動白名單式 COPY 子模組，建議 `COPY . .` 搭配 `.dockerignore`。
*   **禁令 53（依賴鎖檔主權 / Dependency Lockfile SSOT）**：全系統 Python 相依以 **uv workspace 單一 `uv.lock`** 為唯一鎖檔 SSOT——各成員（ToukaProxy/Memory/Heartbeat/Dispatch）的抽象相依宣告於各自 `pyproject.toml` 的 `[project].dependencies`（人改、`~=`/`==`，**禁 `>=`**）、開發/測試工具宣告於 root `[dependency-groups].dev`。**嚴禁手改 `uv.lock`**。加/升套件**必須**：改對應 `pyproject.toml` → 跑根目錄 `uv lock` → `uv sync --all-packages` → commit 重生的 `uv.lock`。**開發與部署共用同一次解析**：dev venv＝`uv sync --all-packages`；Docker＝`uv sync --locked --no-dev`（proxy 明列四個產品 package／heartbeat `--package toukaheartbeat` 精簡子集）。**佈建嚴禁裸 `uv sync`**（exact 同步至空虛擬 root＝剝空 venv；必帶 `--all-packages`）。`torch` 由 root `[tool.uv.sources]` 綁 `pytorch-cpu` index **納入鎖檔**（CPU 版、零 `nvidia-*`；GPU 需求部署時另行覆寫）。Python 版本宣告（`.python-version`／root＋成員 `pyproject` `requires-python`／各 Dockerfile 的實際 Python APK pin／相容 `FROM python:`／`ruff target-version`）**必須**全對齊。由 `scripts/guard_environment_parity.py`（Guard #30：pyproject↔uv.lock 離線逐項比對＋版本對齊＋禁 `>=`）自動執法；Docker build 的 `uv sync --locked` 為第二道 fail-loud 防線。`guard_environment_parity.py --venv` 提供唯讀 venv-parity 諮詢（advisory、exit 0、**不進 guard_all**）。（ADR-0071 amended by ADR-0117：requirements.in/.txt 與 `lock_requirements.py`／`sync_dev_venv.py` 已退役）

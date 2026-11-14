# 本機開發環境

搬自根 `AGENTS.md` 的直譯器與 Windows 本機前置條件，沒有改寫；交付前要跑的指令仍在 `AGENTS.md`。

> ⚠️ **直譯器主權（Windows / Interpreter Sovereignty）**：裸 `python` 可能解析到系統安裝的其他版本（例如全域 Python 3.14），而非專案鎖定的 **3.12 venv**（`.venv`，由 uv 管理；`.python-version` / `pyproject.toml` / `uv.lock` 維持 `==3.12.*`；Python 服務與 Chat builder 使用 digest 釘版的 `python:3.12-slim-trixie` 基底，tagged FROM 供 Guard #30 核對 Python 3.12）。**所有驗證（pytest、guard_all、lint_ssot）必須在 3.12 venv 下執行**，否則是在錯的直譯器＋錯的套件版本（例如 starlette 0.52 vs 1.1）上測，綠燈不可信。正確調用任一即可：
> - **PowerShell 先 `.\.venv\Scripts\Activate.ps1` 再執行**（**推薦**；啟用後 `.venv\Scripts` 自動置於 PATH 最前，連 guard_all 的 shell 子命令也走 3.12）
> - `.venv\Scripts\python.exe <cmd>`（直接指定；但跑 guard_all 仍須另把 `.venv\Scripts` 置於 PATH 最前）
> - `uv run <cmd>`（ADR-0117 後可用：workspace 單一 uv.lock 已含全部依賴；uv run 為 inexact 同步、不剝除既有套件）
> - **★ 佈建/更新依賴一律 `uv sync --all-packages`**：root 為虛擬協調者（無依賴），**裸 `uv sync` 會剝除全部成員依賴**（exact 同步至空 root；數秒可用 --all-packages 復原，但誤剝後測試全紅）。（ADR-0117）
>
> 注意：`guard_all.py` 以 `shell=True` 跑子命令，若沒先把 `.venv\Scripts` 置於 `PATH` 最前面（啟用 venv 即自動滿足），內部的 `python` 仍可能落到系統的其他版本（例如全域 3.14）。

> 🪟 **Windows 本機跑後端測試的前置條件（pytest、guard_all）**：GitHub CI 已在 workflow 裡備好，這段只管本機。
> - **Node／npm 版本**：PATH 上的 `node`、`npm` 要等於 `ToukaChat/package.json` 的 `engines` 與 `ToukaIDE/package.json` 的 `devEngines`（目前 Node 24.22.3、npm 11.19.1）。系統是別的版本時不改系統 Node，只替這次指令切換，例如 `fnm exec --using 24.22.3 -- python -m pytest tests/`。版本不對時 `test_mcp_modern_companion` 會 ERROR，guard_all 的前置檢查直接擋下。
> - **前端依賴**：用上面的 Node 先在 `ToukaIDE` 跑一次 `npm run install:controlled`（companion 測試要用它的 esbuild，沒裝會 ERROR）；要跑 guard_all 時 `ToukaChat` 也要跑過一次，再跑 `npm run gen:artifact-runtime`（沒有產生檔時 Guard #27 型別檢查會失敗）。
> - **Git for Windows**：9 個安裝腳本測試用 Git for Windows 自帶的 `bin\bash.exe`（從 PATH 上 `git` 的位置推算，不用 WSL 的 `bash`）；沒裝時這 9 個以「Bash is unavailable on this host」跳過。
> - **核對結尾摘要**：pytest 結尾會列出失敗、錯誤與每個跳過的原因（`pytest.ini` 的 `-rfEs`）。
>   - 已知的本機失敗：Docker Desktop 下 `test_local_compose_bindings` 有 6 個 failed（找不到 compose 外掛），以同一個 commit 的 GitHub CI 為準（ADR-0250 E1）。
>   - 條件都對時只剩 21 個環境跳過：這台機器不能建 symlink 的 3、只在 POSIX 跑的 14（行程群組 1、檔案連結 12、大小寫有別的檔案系統 1；ADR-0293）、Windows 資料夾沒有可寫位元的 1、只在 Linux 跑的隔離測試模組 1（`tests/integration/test_isolation_linux.py`，整個模組算一個跳過；在 Docker 跑的方法見 `tests/README.md`）、要另外同意才跑的 Docker 部署與 ToukaIDE Marketplace 打包比對各 1。
>   - 比這多就查原因，不能當成通過；新增或移除這類環境失敗或跳過時同步改這裡。

# ADR-0076: 演化沙箱 OS 級資源限制（setrlimit 強制 + 跨平台降級）

**狀態 (Status):** ✅ 已接受（[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 修訂：隔離時 rlimit 由啟動器依 spec 套用，直接執行時仍由 preexec 套用；JailedWriter 照舊）
**日期 (Date):** 2026-06-06
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

Touka 的自主演化流水線會在沙箱子進程中執行**模型自行撰寫**的 MCP 工具碼（`EvolutionPathGuard.run_sandbox_command`）。目前資源限制只有兩道，皆**不足以對抗惡意/失控的演化碼**（對抗式安全複審 DA11–16 列為 Medium）：

1. **掛鐘逾時（唯一 OS 級限制）**：`subprocess.run(timeout=...)` 限制總執行時間——可靠，但只管時間。
2. **儲存配額（in-process monkeypatch，可繞）**：`_build_jail_code` 注入 `builtins.open → JailedWriter`，於 Python 層累計寫入位元組數。**致命缺陷**：監獄碼與模型自寫的 `server.py` 在**同一個子進程**執行，且模型碼在 monkeypatch *之後*執行，可輕易繞過——`os.write`/`os.open`（不經 `builtins.open`）、引用仍在的 `_original_open`、重新賦值 `builtins.open`、或 `subprocess`/`os.fork` 開子進程寫檔。
3. **記憶體配額（宣告但零強制）**：`SandboxLimits.max_memory_mb`（預設 256）只是 SSOT 欄位，**全系統無一處讀取或強制**。

全 ToukaProxy 程式碼**無任何** `setrlimit`/`RLIMIT`/cgroup/Job Object——即沒有任何 OS 核心級的資源邊界。一個被注入或惡意的演化工具可耗盡主機儲存/記憶體，沙箱形同虛設。

**約束：**
- 開發環境為 **Windows**（無 POSIX `resource` 模組）；生產部署多為 **Linux/Docker**。
- 沙箱子進程目前以**與 Touka 相同的使用者**執行（非專用 uid、非容器）。
- 不引入新的執行期相依（pywin32 等）；跨平台優雅降級、**不可在 Windows 上崩潰**。

## 方案比較 (Options Considered)

### 方案 A：POSIX `setrlimit`（preexec_fn）+ Windows best-effort 降級　★採用
子進程啟動時（fork 後、exec 前）以 `resource.setrlimit` 設定核心級硬限制：`RLIMIT_FSIZE`（單檔大小＝儲存配額）、`RLIMIT_CPU`（CPU 秒數）、`RLIMIT_AS`（虛擬位址空間≈記憶體）。Windows 無 `resource` 模組 → 回退既有（逾時＋JailedWriter）並記一次 info。

| 維度 | 評估 |
|---|---|
| 複雜度 | 低（stdlib `resource` + preexec_fn，無新相依）|
| 涵蓋 | 生產（Linux/Docker）真 OS 強制；Windows dev 維持現狀 |
| 維護 | 低 |

**優點**：`RLIMIT_FSIZE` 直接 OS 級封死儲存配額（**取代可繞的 JailedWriter**）；`RLIMIT_CPU` 補足掛鐘逾時；wire 起久未使用的 `max_memory_mb`；零新相依；JailedWriter 保留為縱深＋Windows fallback。
**缺點**：Windows 無 OS 級強制（dev 環境，可接受）；`RLIMIT_AS` 限制的是**虛擬位址空間**（非 RSS），過低可能讓 Python 子進程啟動失敗 → 需保守設定/可調。

### 方案 B：方案 A ＋ Windows Job Object（ctypes）
另以 ctypes 呼叫 Win32 `CreateJobObject`/`SetInformationJobObject` 設定記憶體/CPU 上限與 kill-on-close。
**優點**：真跨平台 OS 強制。**缺點**：Windows 專屬 C 互操作碼（複雜、難測、易碎）、dev-only 環境收益低。→ 列為**未來增強**。

### 方案 C：將演化沙箱容器化（cgroup）
每次驗證以獨立容器執行，套用 cgroup memory/cpu/pids 限制。
**優點**：最強隔離。**缺點**：架構大改（沙箱子進程→容器生命週期）、需 Docker-in-Docker、延遲高。→ 過度工程，列為長期選項。

## 決策方案 (Decision)

採 **方案 A**。於 `EvolutionPathGuard.run_sandbox_command` 加入由 `_build_rlimit_preexec` 產生的 `preexec_fn`，在 POSIX 子進程套用：

- `RLIMIT_FSIZE = max_storage_mb`：**取代可繞的 JailedWriter 成為儲存主防線**（核心以 `SIGXFSZ`/`EFBIG` 擋下超量寫入，`os.write`/子進程一樣撞牆）。
- `RLIMIT_CPU = execution_timeout_sec`：CPU 秒硬限，補足只管掛鐘的逾時。
- `RLIMIT_AS = max_memory_mb`：wire 起 inert 欄位（語意為虛擬位址空間，文件註明）。
- **不設 `RLIMIT_NPROC`**：Linux 上以**真實 uid** 計（含 Touka 自身進程），共用 uid 下會誤殺；待專用 uid/容器再加。

跨平台：`_build_rlimit_preexec` 先試 `import resource`，失敗（Windows）→ 回傳 `None`、記 info、`run_sandbox_command` 不設 `preexec_fn` → 既有行為。限額由 SSOT `SandboxLimits` 經 caller（`verify_mcp_locally`）傳入（沿用 DA14 clamp 後的有效值作 ceiling）。JailedWriter **保留**為縱深防禦與 Windows 唯一儲存防線。

## 後果與影響 (Consequences)

- **儲存配額變 OS 級不可繞**（Linux/Docker）：模型自寫碼以任何方式寫檔都撞 `RLIMIT_FSIZE`。
- **`max_memory_mb` 從死欄位變實際限制**（Linux/Docker，虛擬位址空間語意）。
- **CPU 秒硬限**補足掛鐘逾時。
- **Windows dev 維持現狀**（逾時＋JailedWriter），明確記錄「OS 級限額在此平台不強制」。
- **需重訪**：① Windows Job Object（方案 B）若 dev 也需強制；② 專用沙箱 uid 後可加 `RLIMIT_NPROC`；③ `RLIMIT_AS` 對某些合法工具可能過嚴 → 調高 `max_memory_mb` 或改容器 cgroup。
- **技術註記**：`preexec_fn` 在多執行緒進程 fork 有理論上的鎖死風險；本處 preexec 僅呼叫 `setrlimit`（不持鎖），屬 CPython 文件認可的標準安全用法。

## Action Items
1. [x] `run_sandbox_command` 加 `_build_rlimit_preexec`（FSIZE/CPU/AS；Windows→None）。
2. [x] `verify_mcp_locally` 傳入 SSOT `max_storage_mb`/`max_memory_mb`/timeout。
3. [x] TDD：POSIX 路徑（mock `resource` 驗 setrlimit 呼叫值）＋ Windows 路徑（preexec=None）＋ 不破壞既有沙箱測試。
4. [ ] （未來）Windows Job Object、專用 uid + `RLIMIT_NPROC`、容器 cgroup 評估。

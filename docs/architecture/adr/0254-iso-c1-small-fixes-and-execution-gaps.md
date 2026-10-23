# ADR-0254：隔離第一批（iso-c1）：小件修補與執行路徑既有缺口

> C2-3 現況補註：缺口 2 的檔案隔離已接上 stdio MCP；第三方穩定不可用與人同意的 direct 仍無檔案隔離，原生 Windows 也在此列。本文先前的「等第二批」描述該版範圍，現況見 SECURITY.md。 詳見 ADR-0275。

- 狀態：✅ 已接受（維護者 2026-10-15 核准隔離計畫第一批與各項做法；U1～U6 各單位已驗收，跨單位最終審查見文末驗證；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 補註：缺口 2〔第三方 MCP 讀得到 `.env` 檔〕寫的「要等第二批」指的是 MCP 伺服器的隔離〔第二批第三段 C2-3，另一份 ADR〕；ADR-0272 只隔離代理人指令，這個缺口在 C2-3 合併前仍在）
- 2026-10-16 由 [ADR-0271](0271-claude-5-5-thinking-by-purpose.md) 修訂（r55）：O20 的評分卡情境上限送 Claude 時，碰到 5.x（default_on 思考）照實際值補到 8,192（`_scenario_request` 不標 fields_set 的做法不變，其他家照舊不帶上限）
- 日期：2026-10-15
- 決策者：維護者
- 影響範圍：
  - 金鑰與擁有權（U1）：`services/setup_service.py`、`services/custom_model_service.py`、`utils/custom_io.py`、`routers/souls.py`
  - 執行路徑缺口（U2）：`tools/builtin/__init__.py`（拿掉一個自帶審批綁定）、`scripts/native_tool_contract_baseline.json`（Guard #45 基準）、`protocols/mcp/environment.py`、`services/creation/smoke.py`、`services/tool_executor.py`（修復名單與它的模組常數）、`tools/builtin/evolution.py`、`services/auto_reviewer.py`、`services/review/evidence.py`、`services/approval_flow.py`
  - 刪走不到的程式（U3）：刪 `routers/scheduler.py`、`services/scheduler_service.py`；`config.py`、`ToukaProxy/.env.example`、`docs/architecture/NOQA_REGISTRY.md`、`services/dispatch_supervisor.py`
  - CONTRIBUTING／CI（U4）：`CONTRIBUTING.md`、`.github/workflows/native-tool-contracts.yml`、`services/telemetry.py`（刪死碼）
  - 前端與 IDE（U5）：ToukaChat `ModelStudio.tsx`、`SoulStudio.tsx`、`lib/default_model.ts`、`settings/searchIndex.ts`、兩語 `translation.json`；ToukaIDE `InlineCompletionProvider.ts`；Proxy `services/dispatchers/utils.py`
  - 還原失敗提示（U6）：新 `models/system_restore.py`、`routers/system.py`、`scripts/endpoint_auth_admin_required.py`、`openapi.json`、ToukaChat `types/api.ts`、`lib/types.ts`、新 `hooks/useRestoreWatch.ts`、`EnvSettings.tsx`、兩語 `translation.json`
  - 文件（U7）：`SECURITY.md`、`PROXY_INTERFACE.md`、`CHAT_INTERFACE.md`、`SSOT_MASTER_MAP.md`、`system_architecture.md`、`HANDOVER.md`、`tests/README.md`、`docs/CHANGELOG.md`；`tool_executor.py` 的 ADR-0149 註解（只改註解）
- 修訂 [ADR-0149](0149-evolution-deploy-approval-wiring.md)：`evolution_implement_and_verify` 不再列入自帶審批，推翻它「無人在場時沙盒建置與驗證可以跑，不算安全退化」的取捨（缺口 1）；`request_mcp_creation` 的部署卡與 headless fail-closed 不變
- 修訂 [ADR-0229](0229-trusted-tool-execution-classification.md)：自帶批准工具從三個變兩個（`request_skill_install`、`request_mcp_creation`）
- 修訂 [ADR-0198](0198-mcp-sdk-neutral-boundary-and-principal-handle-isolation.md) §5：`LaunchEnvironmentPolicy.production` 不再組「完整必要環境」，改成白名單底；`clean` 改用同一個白名單、不再收 clean base factory（缺口 2）
- 更正 [ADR-0238](0238-retire-release-qualification-machinery.md) 決策 5「CI 直接執行 Ruff」：從 `4a0bf452` 起 workflow 其實沒有 Ruff 步驟（見下方「ADR-0238 的更正」）
- 修正 [ADR-0246](0246-model-layer-stage-2b-2c.md) 的已知限制：「互審驗證指令被審查員轉人時，那張卡不顯示審查理由」已修（H4）；「Auto 下 F5 的自帶審批工具會出兩張卡」的工具清單少了 `evolution_implement_and_verify`（它在 Auto 下仍是兩張卡，只是第一張由一般分類產生）
- 修正 [ADR-0250](0250-touka-leftovers.md) 的已知限制：建立靈魂不驗模型名（H9）、自訂模型只差大小寫改名照樣回 200（H12）、ModelStudio 下拉可能混入 human（H10）、Proxy 排程 CRUD 與 `ENABLE_SCHEDULER`（H7，B4 段「排程 CRUD 與 `routers/scheduler.py` 保留」不再成立）、`custom_model_service.py` 寫死 Gemini（H8）
- 修正 [ADR-0251](0251-dev-environment-hygiene.md) 的已知限制：「設定精靈的 `apply` 仍可寫入公開值」已修（H20）
- 相關：ADR-0067（Dispatch `_build_child_env` 白名單做法，本 ADR 參考但不跨模組匯入）、ADR-0137（互審驗證指令）、ADR-0148（自帶審批工具）、ADR-0241（不送取樣參數）。這些 ADR 的決策都不變。

## 背景

- 隔離系列（iso）＝模型層第 5 段「隔離執行環境」＋幾件小修。分兩批：第一批（本 ADR）做 `HANDOVER.md` §3 待排小項裡仍成立、快、風險低的，以及研究找到的三個執行路徑既有缺口；第二批 iso-c2（隔離執行環境，另一份 ADR）等維護者選完設計才開始。
- 三個既有缺口（審查時抽查屬實）：
  - 缺口 1：`evolution_implement_and_verify` 在註冊時被分類成「自帶審批」，Manual／CONFIRM 時 executor 不跳卡，工具自己只在最後「部署」才問人；前面的寫檔與建 venv 不問人就跑，無人在場也跑。後面的 `pip install`、本機驗證（import 代理人寫的 `server.py`）與部署原本同樣不問人，只是 H3 之前 venv 的 Python 路徑錯（相對路徑 `Scripts/python.exe`／`bin/python`，venv 實際在 `.venv` 底下），這幾步一律 FileNotFoundError，從沒真的跑起來（跨單位最終審查查出）。H3 修好路徑的同時，缺口 1 讓整個建置改走一般審批。Auto 下它屬 F5（只有人能核准），所以 Manual 反而比 Auto 鬆。
  - 缺口 2：已安裝的 MCP stdio 伺服器啟動時拿到 `os.environ.copy()`（`settings.get_full_env()`），含全部廠商金鑰與 `TOUKA_AUTH_SECRET`、`DISPATCH_TOKEN`、`HEARTBEAT_TOKEN`。
  - 缺口 3：終端自我修復名單寫死 `["bash","terminal__bash","execute_terminal"]`，沒有現在廣告的 `execute_command`，背景指令的結果經 `get_command_result` 也不在名單，失敗後的修復提示從不附上。
- 小件來自 ADR-0246、0250、0251 的已知限制與交班待排小項；A4 來自研究發現 CI 沒有跑 Ruff、CONTRIBUTING 的指令和 CI 對不上。

## 決策

### 金鑰與擁有權

- **H20 設定精靈拒收公開值**：`POST /v1/admin/setup/apply` 的 `dispatch_token`、`auth_secret` 在寫任何東西之前先過 `config.is_public_secret`（ADR-0251 的同一份對照表）；是公開值就丟 `ValueError`，路由既有的處理回 422 `VALIDATION_ERROR`（3001），`.env` 不寫（另一欄是合法值也不寫）。錯誤訊息只寫欄位名、不寫值。非公開值照舊寫入。沒有新增 noqa（用 `raise ValueError(msg)`，不照 `setup_service.py` 既有的 `# noqa: TRY003` 慣例）。
- **H12 自訂模型改名失敗要報錯**：`CustomModelService.rename_model` 不再回 `bool`，搬資料夾失敗時丟例外、`config.json` 不改：來源不見 `FileNotFoundError`、新 ID 已被占用 `FileExistsError`、其他（沙箱檢查不過、搬移出錯）`OSError`。`POST /ui/custom_models/{id}/rename`（含 `/v1`）既有的 except 分別回 404 `MODEL_NOT_FOUND`（1202）、409 `MODEL_ALREADY_EXISTS`（1403）、500 `SYSTEM_ERROR`（1000）；擁有權（`users.json` 的 owned agents）只在搬成功之後才搬。只差大小寫的改名（Windows 上新舊名是同一個資料夾）：`custom_io.move_custom_dir` 用 `samefile` 判斷後直接 `rename`，不當成撞名；新增 `custom_io.custom_path_exists`（逃出沙箱的名字一律當沒有）。改名成功時資料夾、`config.json` 的 id、擁有權照舊一起改（釘住）。
- **H9 建立靈魂先驗模型名**：`POST /v1/souls` 比照存檔（ADR-0250 A1）：`config.base_model` 先經 `normalize_model_choice`（`None`、空字串、只有空白當沒指定、存成 `null`；有值時去掉前後空白），正規化後的值寫回 `config.json`；有指定時在建任何資料夾之前呼叫 `is_selectable_brain`，不可選回 404 `MODEL_NOT_FOUND`，只差大小寫分不出時 `resolve_custom_route` 直接丟 409 `MODEL_NAME_AMBIGUOUS`（1220）；兩者都不建資料夾、不註冊、不加擁有權。可選或沒指定照舊建立（可選時設成主要模型）。檢查順序：人類 ID 撞名、靈魂已存在（409 `SOUL_ALREADY_EXISTS`）之後才驗模型。
- **H8**：`custom_model_service.py` 自訂模型清單的顯示用 `base_model` 拿掉寫死的 Gemini 預設，沒存就是 `None`；`test_no_hardcoded_model_ids.py` 的檢查清單加這個檔。

### 執行路徑缺口

- **缺口 1 演化建置先過一般審批卡**（計畫做法 (a)）：拿掉 `evolution_implement_and_verify` 在 `tools/builtin/__init__.py` `BUILTIN_EXECUTION_BINDINGS` 的 `SELF_APPROVING` 綁定，Guard #45 基準同步。工具回到一般分類：
  - Manual、有人在場：先跳一般審批卡（`tool_approval_request`），核准才建 venv、pip、驗證；拒絕就什麼都不跑。
  - Manual、無人在場：executor 既有路徑直接拒絕（`approval_required`），什麼都不跑。
  - `submit_for_deploy=True` 時部署前那張 `skill_creation_approval_request` 卡照舊（工具靠所有工具都拿得到的 `_interactive` kwarg 判斷）；無人在場時部署照舊 fail-closed。
  - Auto（F5 只有人能核准）：照舊兩張卡（executor 的「只有你能核准」卡＋部署卡），無人在場拒絕。YOLO：行為不變（executor 不跳卡、建置照跑，部署卡有人才問、無人 fail-closed）。
  - `request_skill_install`、`request_mcp_creation` 仍是自帶審批；`tool_executor.py` 的分類判斷不改、不依工具名特判（ADR-0229）。
- **缺口 2 MCP stdio 啟動環境改白名單底**：`protocols/mcp/environment.py` 新增模組常數 `LAUNCH_ENV_ALLOWLIST`（禁令 18）：語系與時區（`LANG`、`LANGUAGE`、`LC_ALL`、`LC_CTYPE`、`TZ`）、暫存目錄（`TMPDIR`、`TMP`）、Windows 系統目錄與 cmd（`COMSPEC`、`WINDIR`、`PROGRAMDATA`、`PROGRAMFILES`、`PROGRAMFILES(X86)`、`PROGRAMW6432`、`COMMONPROGRAMFILES`、`NUMBER_OF_PROCESSORS`、`OS`）、POSIX 快取與設定目錄（`XDG_CACHE_HOME`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME`）、uv／npm 的非機密設定（`UV_PYTHON_DOWNLOADS`、`UV_CACHE_DIR`、`UV_TOOL_DIR`、`UV_PYTHON_INSTALL_DIR`、`NPM_CONFIG_CACHE`、`NPM_CONFIG_PREFIX`）。
  - 正式（`LaunchEnvironmentPolicy.production`）＝父環境裡白名單內的變數＋條目自己的 `env`（解析哨兵；名稱含 TOKEN／KEY 的值照給）＋`TOOL_BASE_URL`。
  - 創建引擎試跑（`LaunchEnvironmentPolicy.clean`，`services/creation/smoke.py`）＝同一個白名單底＋spec 宣告的 `env`（不解析哨兵，SP7 不變）；`clean` 不再收 `clean_base_factory` 參數，`smoke.py` 不再用 `EvolutionPathGuard.get_clean_env`。避免候選伺服器拿到比正式伺服器寬的環境。
  - MCP SDK 的 `stdio_client` 啟動時本來就會合併 `get_default_environment()`（Windows：`APPDATA`、`PATH`、`PATHEXT`、`SYSTEMROOT`、`TEMP`、`USERPROFILE` 等；POSIX：`HOME`、`PATH`、`SHELL` 等），白名單只補 SDK 沒帶的；Windows 名稱比對不分大小寫。
  - 刻意不列：代理伺服器設定（`HTTP_PROXY`／`HTTPS_PROXY`，可能夾帶帳密）、uv／npm 的 index、registry 設定（可能夾帶 token）、`PYTHONPATH`、`HF_HOME`、Windows 的 `HOME`。伺服器需要的話寫進條目自己的 `env`。
  - 效果只擋「環境變數順手帶走」，擋不住會去讀 `.env` 檔的惡意套件：Docker 裡 MCP 子程序和 Touka 同容器同帳號，`.env` 掛在 `/app/host_root`、金鑰也在 PID 1 的環境裡；原生 Windows 讀得到 repo 根的 `.env`。要擋惡意套件得靠第二批的隔離執行環境。
- **缺口 3 終端自我修復名單**：名單改成 `tool_executor.py` 的模組常數 `TERMINAL_REPAIR_TOOLS`（`execute_command`、`get_command_result` 加上舊名 `bash`、`terminal__bash`、`execute_terminal`）。非 0 exit code 附修復提示並送 `repair` 進度事件，0 時重置次數。`get_command_result` 對同一個 execution_id 每次都回同一份含 `[EXIT_CODE]` 的結果，只計第一次：記住已計過的 (session, execution_id)，上限 `MAX_TRACKED_REPAIR_RESULTS = 1024`、超過淘汰最舊的（行程記憶體、不落地）。只附提示文字給模型，下一次執行仍照常過政策，不會自動執行任何東西。
- **H3 演化沙盒用 venv Python 的絕對路徑**：新 helper `evolution._venv_python(skill_id)` 回 `<工具沙盒根>/.venv/Scripts/python.exe`（POSIX `bin/python`），`.venv` 名稱是模組常數 `_VENV_DIR`、和建 venv 那步共用。pip 安裝、本機驗證與部署寫進 `server_config.json` 的 `command` 都用它。原本前兩者傳相對路徑（Windows 找執行檔不看子程序 cwd，而且 venv 在 `.venv` 底下，相對路徑本來就指錯）；部署的 `command` 少了 `.venv`，部署後的伺服器指向不存在的直譯器（研究原判為沒問題，實作時查出並補做）。所以修補前演化從沒真的執行過代理人的程式；H3 之後才第一次會真的跑（Manual／Auto 由缺口 1 先問人；YOLO 照定義會跑）。
- **H4 互審驗證指令的卡帶審查理由與「只有你能核准」**：`AutoReviewer.command_reviewer` 回 `ReviewOutcome`（判決＋理由）；`policy_command_gate` 在審查員轉人時把理由、在 `HUMAN_ONLY`（底線）時把 `human_only=True` 交給 approver；`services/review/evidence.py` 的 `CommandApprover` 改成 `typing.Protocol`，多兩個選填 keyword（`review_reason`、`human_only`），`approval_flow.CommandApprover` 放上卡，和 executor 的終端卡一致。底線指令照舊不送審查員，審查員的核准不能滿足底線（釘住）。

### 刪走不到的程式與 Windows 停止

- **H7**：刪掉沒有掛載、走不到的排程 CRUD（`routers/scheduler.py`、`services/scheduler_service.py`）與沒人讀的 `ENABLE_SCHEDULER`（`config.py` 的 Settings 欄位與模組常數、`ToukaProxy/.env.example` 的那一節）。Settings 維持 `extra="ignore"`，舊 `.env` 還留著這個鍵（任何值）時照常載入（釘住）。Proxy 照舊不做主動排程；真人排程走 Heartbeat 與 ADR-0230 的橋接。`NOQA_REGISTRY.md`：B008 233 → 228、TC001 62 → 61（「其他」列 326 → 325）。
- **H2**：`DispatchSupervisor` 停止 ToukaDispatch 時，`terminate_process_tree`（Windows 上經 `subprocess.run` 跑 taskkill、沒有逾時）與 Windows 每次停止都會走到的強制殺群組，都改在 `asyncio.to_thread` 裡跑，不再卡住事件迴圈；有界等待、殺行程樹語意與 POSIX 送的訊號不變，`process_registry` 與 `execution_broker` 不動。

### 前端與 IDE

- **H10**：ModelStudio 的基礎引擎下拉和 SoulStudio 用同一套過濾規則：`ToukaChat/src/lib/default_model.ts` 新增 `isSelectableBaseModel`（不列 `is_agent`、`owned_by` 是 human 或 touka 的項目），兩頁都用它。
- **H11**：IDE 行內補全不送 `temperature`（原本寫死 0.1，後端本來就不送，ADR-0241）。
- **N1**：設定頁搜尋拿掉已不存在的「取樣參數（溫度／Top P／Top K）」條目，兩語翻譯同步刪除。
- **O20**：Proxy 的 Anthropic 形狀入口（IDE／Chat 選 anthropic 協定時的 `/v1/messages`）接非 Claude 模型時，`convert_to_openai` 帶上呼叫端明確給的 `max_tokens`（看 `model_fields_set`）；沒給時 `AnthropicRequest` 的預設 8192 照舊不帶。
  - 內部呼叫端（子代理、評分卡）維持原行為（跨單位最終審查查出 O20 波及這兩條，已改回）：子代理不再明寫 `max_tokens=8192`，Claude 仍用 `AnthropicRequest` 的預設 8192、其他家不帶上限；評分卡的情境上限只給 Claude，`scorecard/runner.py` 的 `_scenario_request` 照設值，但用 `model_construct(_fields_set=…)` 不記成呼叫端明確給的欄位，轉成其他家時就不帶。評分卡的模型可能是靈魂，要到 SoulLayer 才換成主要模型，所以不在組請求時依模型名判斷家族。`context_snapshot_service.py` 的 `max_tokens=1024` 只跑 `pipeline.execute`（各中介層）、不送模型，不受影響。
  - L2 自訂模型設定裡的 `max_tokens`（`CustomModelLayer` 寫進請求）在 Anthropic 入口現在也會帶給非 Claude 的基礎模型，和 openai 協定本來的行為一致。

### CONTRIBUTING 與 CI（A4）

- CI `contracts` job 在 `uv sync` 之後跑鎖定版 ruff：`uv run --no-sync ruff check --no-cache --select F --config ruff.toml ToukaProxy/src ToukaMemory/src ToukaHeartbeat ToukaDispatch`（pyflakes：未定義名稱、沒用到的匯入或變數，必須 0）。為此先刪掉 `services/telemetry.py` 的死碼（`safe_agent_id`、`_get_safe_filename`、`import re`）。
- 新 `toukaide` job（ubuntu-24.04、Node 24.21.0、npm 11.19.1）：`ToukaIDE` 依序跑 `npm run install:controlled`、`npm test`、`npm run typecheck`、`npm run compile`。`package` 只在本機。
- `CONTRIBUTING.md`「PR 前必做」每一項標「CI 會跑」或「只在本機」，**完整 ruff 與 ESLint 不拿掉**：完整 ruff 目前還有上百個既有錯誤、沒有棘輪基準，要求「你改到的檔不比改之前多」；ESLint 走 Guard #35 對照 `scripts/eslint_baseline.json` 的棘輪；vitest 改 `npm run test`（先產生 artifact runtime）；guard_all 標只在本機並列出 CI 有跑的 5 道（#1、#2、#27、#32、#45）；加上 ToukaIDE 的 test、typecheck、compile（CI）與 package（本機）。
- `tests/lint/test_ci_pr_checks.py` 釘住：四個產品子專案 pyflakes 0 錯誤；CI 恰有一個鎖定版 ruff F 步驟涵蓋四個根目錄、沒有放寬旗標；`toukaide` job 在 devEngines 的 Node／npm 上照順序跑四個指令，不能被 `if` 或 `continue-on-error` 跳過；CONTRIBUTING 每項都有標示、保留完整 ruff 與 ESLint、標示和 CI 實際跑的對得上。

### 還原失敗提示（H1）

- 後端：`routers/system.py` 在記憶體記住最後一次還原的結果（`_last_restore`，模型 `models/system_restore.py` 的 `RestoreStatus`：`state` 為 `idle`／`running`／`failed`，`error_code` 只在失敗時有值）。還原被接受時記 `running`（蓋掉上一次的失敗），背景工作丟例外時記 `failed`＋`SYSTEM_BACKUP_FAILED`（1303）；成功時進程 `os._exit` 重啟，紀錄跟著消失，新進程回 `idle`。只記狀態與錯誤碼，不記路徑或例外原文（禁令 23）。
- 新端點 `GET /v1/system/restore/status`（與根路徑對等）：admin 限定（`Annotated[..., Depends(_require_admin)]`，`require_admin_authority` 的別名，登記進 Guard #24 的 admin 清單），唯讀，`response_model=RestoreStatus`；沒登入 401 `AUTH_INVALID_TOKEN`、非 admin 403 `AUTH_ADMIN_REQUIRED`。`openapi.json` 與 `api.ts` 重生。
- 前端：新領域 hook `useRestoreWatch`（經 `fetchTouka`）擁有計時器與查詢。還原送出後 15 秒第一次查：`failed` → `EnvSettings` 關掉「還原中」對話框、顯示常駐錯誤 toast（專用翻譯 `dashboard.env_settings.restore_failed`）、不重整、回到備份清單；`running` → 5 秒後再查、不重整；其他（成功後重啟回 `idle`、伺服器重啟中連不上、非 2xx）→ 照舊重整。`RestoreStatus` 在 `lib/types.ts` 從生成的合約取別名。

### 這次不做

H5 審查員準確率實測（要付費 API）、H6 YOLO 心跳無人在場用 MCP 工具（移到第二批）、H13 小寫模型名直送自訂端點、O11、O13（延到模型路由規劃）、H14 `update_docs.py` 實跑、H15 Guard #22 誤報、H18 強制結束留沙盒（照原條件再做）、H16 禁令 22 守衛掃到子專案 `.venv`（還沒發生）、H17 ADR-0250 其他限制（延到「靈魂模型三種模式」任務）、H19 Heartbeat 8082 在重建前接受公開值（不改程式）、H21（另案，已由 ADR-0252 做完）、CI 跑完整 guard_all（會連網、誤報會卡合併）。這些留在 `HANDOVER.md` §3。

## 單位驗收時的裁定

- H12：錯誤碼對應 404 `MODEL_NOT_FOUND`／409 `MODEL_ALREADY_EXISTS`／500 `SYSTEM_ERROR` 接受（沿用路由既有 except，沒有新增錯誤碼）。
- H9：只有空白的模型名照 ADR-0250 A1 當「沒指定」，第一次實作會回 404，補做改用 `normalize_model_choice` 並把正規化後的值寫回 `config.json`（`5b80cad5`）。
- H8：清單裡沒存 `base_model` 時回 `None` 接受。
- H3：部署寫進 `server_config.json` 的 `command` 少了 `.venv`（研究誤判為沒問題），在核准範圍內，補做（`d40f2ea1`）。
- O20：`test_no_sampling_on_wire.py` 原本就釘著「原生 Anthropic 入口不帶 `generationConfig`」這個錯（註解寫「另記」），翻轉是核准行為的直接結果；新斷言仍禁取樣參數、其他入口仍不帶 `generationConfig`。
- H7：`test_role_plane_characterization` 的 PD 數 16 → 15 原本沒點名，是刪掉排程閘道的直接結果，接受。
- H1：
  - 還原還在跑時每 5 秒再查、不重整，接受（照實顯示比強制重整好；代價是還原卡住時畫面停在「還原中」）。
  - 失敗提示用專用翻譯、不用 `errorMsg(SYSTEM_BACKUP_FAILED)`（「系統備份失敗」用在還原會誤導），接受；用的是 `t()`、不是硬編碼，前端錯誤守衛通過。API 回的錯誤碼語意不精確，新增 `SYSTEM_RESTORE_FAILED` 要動另一批同時在改的 `errors.json`，列待排。
  - 計畫清單外的 4 個檔（`models/system_restore.py`、`hooks/useRestoreWatch.ts`、`lib/types.ts` 的別名、Guard #24 admin 清單）都是工作說明直接要求的，接受。
- 缺口 2：`test_subprocess_hardening_security.py` SP7 那個測試的前置改動接受（拿掉 `clean_base_factory` 的直接結果，改用假的父環境，斷言意思不變）。
- H4：介面多兩個選填欄位後，4 個測試檔的假 approver／reviewer 改簽章接受（斷言沒動）。
- `tool_executor.py` 的 ADR-0149 註解：2026-10-15 決定由 U7 在本批改正（只改註解）。

## 期望翻轉（只限核准行為的直接結果）

| # | 測試或基準 | 原本 | 現在 | 依據 |
|---|---|---|---|---|
| 1 | `tests/unit/test_evolution_deploy_approval.py`（約 :171-174） | `evolution_implement_and_verify` 在自帶審批名單 | 不在；`request_mcp_creation` 仍在 | 核准的計畫、缺口 1 |
| 2 | `tests/unit/test_evolution_deploy_approval.py`（約 :177-212） | Manual／CONFIRM 下不跳卡、直接執行（含無人在場） | 有人在場先跳一般審批卡；無人在場 executor 拒絕、不執行 | 核准的計畫、缺口 1 |
| 3 | `tests/unit/test_tool_execution_classification.py`（約 :139-160、:293-339） | 分類清單含演化工具；Manual 無人在場時回應含工具的「interactive session」部署拒絕訊息 | 清單少一個；executor 先拒絕；有人在場先跳卡 | 核准的計畫、缺口 1 |
| 4 | `scripts/native_tool_contract_baseline.json`（Guard #45 基準） | 演化工具的 `SELF_APPROVING` 條目 | 刪掉；另加 `_venv_python` 的 consumer 條目（H3） | 核准的計畫 |
| 5 | `tests/unit/test_scheduler_engine_removed.py` | 只斷言引擎函式不在、CRUD 在沙盒照常 | 兩個檔不在、設定不在、範本不寫、舊 `.env` 帶這個鍵照常載入（4 → 7 cases） | 核准的計畫、H7 |
| 6 | `tests/unit/test_role_plane_characterization.py` | 含 `scheduler:_require_scheduler_role` 閘、PD 16 個 | 拿掉、PD 15 個（255 → 246 cases） | 核准的計畫、H7 |
| 7 | `tests/integration/test_server_lifecycle.py` | 啟動前設 `ENABLE_SCHEDULER=False` | 拿掉那一行 | H7 的直接結果 |
| 8 | `ToukaChat/.../settings/__tests__/searchIndex.test.ts` | 搜「溫度」命中「取樣參數」 | 搜不到；通用命中與排序改用其他條目 | 核准的計畫、N1 |
| 9 | `test_soul_admin_stepup.py`、`test_soul_crud_real_writes.py`、`test_soul_role_binding.py` | 用不可選的 `claude-test-brain` 建立靈魂 | fixture 把它種成清單裡可選的一般模型（只改前置，斷言不動） | 核准的計畫、H9 |
| 10 | `tests/unit/test_creation_engine.py`（約 :225-230） | patch `get_clean_env` 再斷言合併結果 | patch 父環境，斷言白名單底＋spec 宣告的 env | 核准的計畫、缺口 2 |
| 11 | `tests/unit/test_mcp_2026_p1_boundary.py`（約 :420-440） | 注入 `clean_base_factory` | 改用白名單常數；仍釘住「不繼承父環境的金鑰、只加 spec 宣告的 env、不解析哨兵（SP7）」 | R4-1 |
| 12 | `tests/unit/test_subprocess_hardening_security.py`（SP7，約 :556-561） | 傳 `clean_base_factory` | 改用假的父環境，斷言不變 | 驗收裁定（缺口 2） |
| 13 | `test_auto_review_execution_security.py`、`test_hard_floor_security.py`、`test_review_awaiting_human_security.py`、`test_subprocess_hardening_security.py` 的假 approver／reviewer | approver 只收 `cmd`；reviewer 回字串 | approver 多收卡片 keyword；reviewer 回 `ReviewOutcome`（斷言不動） | 驗收裁定（H4） |
| 14 | `tests/unit/test_no_sampling_on_wire.py`（約 :178-181） | 原生 Anthropic 入口的 Gemini wire 沒有 `generationConfig` | 只有 `{"maxOutputTokens": 100}`；其他入口仍沒有 | 驗收裁定（O20） |

## ADR-0238 的更正

ADR-0238 決策 5 寫「改為直接執行 Ruff、lint_ssot、原生工具契約守衛、權限註冊守衛、pytest 與前端測試」。實際上從 `4a0bf452`（ADR-0238 的實作 commit）起，`.github/workflows/native-tool-contracts.yml` 一直沒有 Ruff 步驟（`8b2e242e` 的 workflow 也沒有）。本批起 `contracts` job 跑鎖定版 ruff `--select F` 掃四個產品子專案，並新增 `toukaide` job；完整 ruff（全部規則）仍有既有錯誤、只在本機跑。ADR-0238 的狀態區加一句更正並指向本 ADR。

## 規則鬆緊

- 加嚴：缺口 1（Manual 下演化建置先問人、無人在場不跑）、缺口 2（MCP 子程序拿到的環境變少）、H20、H12（失敗不再假裝成功）、H9、A4（CI 多兩項檢查）。
- 不變：缺口 3（只多附提示文字）、H3、H4（卡上多資訊）、H7、H2、H8、H10、H11、N1、O20、H1（多一個 admin 唯讀端點）。
- 沒有放寬。CONTRIBUTING 的完整 ruff、ESLint 照舊在「PR 前必做」裡。

## 決策落點表

| 決策 | 檔案 | 函式／設定 | 測試 |
|---|---|---|---|
| H20 | `services/setup_service.py` | `_reject_public_secret`（`SetupService.apply` 兩欄） | `test_public_secret_values_security.py`（+27） |
| H12 | `services/custom_model_service.py`、`utils/custom_io.py`、`routers/custom_models.py`（不改，既有 except） | `rename_model`（丟例外）、`move_custom_dir`（`samefile`）、`custom_path_exists` | 新 `test_custom_model_rename_security.py`（5） |
| H9 | `routers/souls.py` | `create_soul`：`normalize_model_choice`＋`is_selectable_brain` | `test_soul_primary_model_choice.py`（+9）；三個 soul 測試加 fixture |
| H8 | `services/custom_model_service.py` | `CustomModelService.list_models` 的 `base_model` | `test_no_hardcoded_model_ids.py`（清單多一檔） |
| 缺口 1 | `tools/builtin/__init__.py`、`scripts/native_tool_contract_baseline.json` | `BUILTIN_EXECUTION_BINDINGS` 少一個 | 新 `test_evolution_manual_approval_security.py`（10）；翻轉 1～4 |
| 缺口 2 | `protocols/mcp/environment.py`、`services/creation/smoke.py` | `LAUNCH_ENV_ALLOWLIST`、`_allowlisted_parent_env`、`LaunchEnvironmentPolicy.production`／`clean` | 新 `test_mcp_launch_env_security.py`（9）；翻轉 10～12 |
| 缺口 3 | `services/tool_executor.py` | `TERMINAL_REPAIR_TOOLS`、`MAX_TRACKED_REPAIR_RESULTS`、`_first_repair_report` | 新 `test_terminal_self_repair.py`（8） |
| H3 | `tools/builtin/evolution.py` | `_VENV_DIR`、`_venv_python`（pip、`verify_mcp_locally`、`request_mcp_creation` 的 `command`） | 新 `test_evolution_venv_python.py`（6） |
| H4 | `services/auto_reviewer.py`、`services/review/evidence.py`、`services/approval_flow.py` | `command_reviewer` 回 `ReviewOutcome`；`CommandApprover` Protocol；`policy_command_gate` | 新 `test_review_card_reason_security.py`（9）；翻轉 13 |
| H7 | 刪 `routers/scheduler.py`、`services/scheduler_service.py`；`config.py`、`ToukaProxy/.env.example`、`NOQA_REGISTRY.md` | `ENABLE_SCHEDULER` | `test_scheduler_engine_removed.py`（7）；翻轉 5～7 |
| H2 | `services/dispatch_supervisor.py` | `_terminate_and_wait`、`_stop_dispatch_process` 經 `asyncio.to_thread` | `test_dispatch_supervisor.py`（+2） |
| H10 | `ToukaChat/src/lib/default_model.ts`、`ModelStudio.tsx`、`SoulStudio.tsx` | `isSelectableBaseModel` | `ModelStudio.test.tsx`（+1） |
| H11 | `ToukaIDE/src/providers/InlineCompletionProvider.ts` | 請求 body 拿掉 `temperature` | `inlineCompletion.test.ts`（+1） |
| N1 | `ToukaChat/src/components/settings/searchIndex.ts`、兩語 `translation.json` | 刪 `model_params` 條目 | `searchIndex.test.ts`（+1，翻轉 8） |
| O20 | `services/dispatchers/utils.py`；內部呼叫端 `tools/subagent_tools.py`、`services/scorecard/runner.py` | `convert_to_openai` 的 `max_tokens`；`_assemble_subagent_request` 不明寫上限、`_scenario_request` | `test_kf9_anthropic_entry.py`（+2）；翻轉 14；新 `test_internal_callers_max_tokens.py`（9） |
| A4 | `.github/workflows/native-tool-contracts.yml`、`CONTRIBUTING.md`、`services/telemetry.py` | `contracts` job 的 ruff F 步驟、`toukaide` job | 新 `tests/lint/test_ci_pr_checks.py`（6） |
| H1 | 新 `models/system_restore.py`、`routers/system.py`、`scripts/endpoint_auth_admin_required.py`；ToukaChat 新 `hooks/useRestoreWatch.ts`、`EnvSettings.tsx`、`lib/types.ts`、兩語 `translation.json`；`openapi.json`、`types/api.ts` | `RestoreStatus`、`_last_restore`、`_record_restore`、`get_restore_status`；`useRestoreWatch` | 新 `test_restore_status_security.py`（11）；`EnvSettings.test.tsx`（+4） |
| 文件 | `tool_executor.py` 註解、`SECURITY.md`、介面文件、`SSOT_MASTER_MAP.md`、`system_architecture.md`、`HANDOVER.md`、`tests/README.md`、`docs/CHANGELOG.md` | — | Guard #28、`test_documentation_contracts.py` |

## 考慮過、不採用的做法

- 缺口 1 改在 executor 依工具名特判（例如演化工具在 Manual 時強制跳卡）：分類的 SSOT 是註冊時的 binding（ADR-0229），依名稱特判會多一份清單。
- 缺口 2 沿用 `EvolutionPathGuard.get_clean_env`：它會把名稱含 KEY／TOKEN／PYTHON／NODE_ 的全拿掉（條目自己的 `NOTION_TOKEN` 這類也一起拿掉），還把 `PYTHONPATH` 設成 `.`；黑名單也漏得掉 `*_PASS`、`DATABASE_URL` 一類。改用白名單底，並參考 Dispatch `_build_child_env`（ADR-0067）的做法、不跨模組匯入。
- 缺口 2 白名單列 `HTTP_PROXY`／`HTTPS_PROXY`：可能夾帶帳密，要用的伺服器寫進自己的 `env`。
- H1 新增 `SYSTEM_RESTORE_FAILED`：要動另一批同時在改的 `errors.json` 與兩端產生檔，本批沿用 `SYSTEM_BACKUP_FAILED`，前端用專用翻譯，新錯誤碼列待排。
- H1 還原還在跑時照舊 15 秒就重整：會在還原中途重整，畫面以為成功。
- A4 CI 跑完整 guard_all：會連網跑 audit、誤報會卡合併，Guard #30 由維護者決定只手動跑（ADR-0071）。
- A4 CI 跑完整 ruff：還有上百個既有錯誤、沒有棘輪基準，現在加會整片紅；先只擋 F。

## 實作偏離

- H9：計畫只寫「不可選 404、模糊 409」；驗收時加了「只有空白當沒指定、正規化值寫回」（比照存檔），見「單位驗收時的裁定」。
- H3：計畫只列 pip 與本機驗證（約 :211、:265）；部署的 `command` 也補上（驗收時補做）。
- H1：計畫只寫「失敗就顯示錯誤、不自動重整」；多了「還在跑時每 5 秒再查」與專用翻譯（驗收時接受）。
- 缺口 3：去重用 (session, execution_id)、行程記憶體、上限 1024（計畫只要求「同一個 execution_id 只計一次」）。
- H12：只差大小寫的改名用 `samefile` 判斷（不比字串），`custom_io` 多一個 `custom_path_exists`。
- 研究更正（U4）：研究說留下 `_get_safe_filename` 會讓 vulture 紅，實際不會（信心度 60 < 80）；死碼照計畫刪掉，因為 ruff F 會抓到 `safe_agent_id`。
- 跨單位最終審查要求的修正：O20 波及子代理與評分卡兩條內部路徑（核准範圍只有 IDE／Chat 的 Anthropic 入口），改回原行為並以 `test_internal_callers_max_tokens.py` 釘住；缺口 1 的背景與回復說明照實改寫（H3 之前演化的後續步驟跑不起來）。

## 已知限制

- 缺口 2 只擋環境變數繼承；第三方 MCP 套件直接讀 `.env` 檔（Docker 的 `/app/host_root/.env`、PID 1 的環境；原生 Windows 的 repo 根 `.env`）仍讀得到，要等第二批的隔離執行環境。
- 缺口 2 之後，MCP 伺服器不再拿到 `PYTHONPATH`、`HF_HOME`、`HTTP_PROXY`／`HTTPS_PROXY`、Windows 的 `HOME`，以及沒寫進條目 `env` 的廠商金鑰；靠這些的伺服器會連不上並顯示錯誤，到 MCP 設定頁把需要的值寫進該伺服器的 `env` 即可。靠 `PYTHONPATH` 或公司內部憑證變數（`SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`）的 MCP 也一樣，要把變數寫進該條目自己的 `env`。更新後請到 MCP 設定頁看一次連線狀態。
- 缺口 1：Manual 下第一張是一般審批卡，很長的程式碼參數可能只預覽前段（`approval_preview_truncated`）；部署卡仍顯示全文。YOLO 無人在場時建置照跑（YOLO 的定義），部署照舊 fail-closed。
- 缺口 3 的去重紀錄只在行程記憶體，重啟歸零。
- H1：狀態只在記憶體，重啟後是 `idle`；還原卡住時畫面停在「還原中」並每 5 秒再查；錯誤碼用 `SYSTEM_BACKUP_FAILED`（語意不精確，待排）；`manage_backup.py` 還原找不到 zip 時印錯誤後 exit 0，會被當成成功（端點接受前已檢查，只剩時間差，待排）。
- O20 只修 Proxy 端；ToukaChat 與 ToukaIDE 的 Anthropic 協定自己寫死 `max_tokens: 4096`，選 anthropic 協定、接非 Claude 模型時會被截在 4096（Gemini 2.5 含思考 token）。預設協定是 openai，只影響改選 anthropic 的人（待排）。
- `pickDefaultModel` 沒排除 `owned_by` 是 human／touka 的項目（H10 只統一了下拉，待排）。
- `ErrorCode.SCHEDULE_NOT_FOUND`（1205）已沒有後端使用者；刪除要動 `errors.json` 與兩端產生檔（待排）。
- ESLint 棘輪基準過時：`scripts/eslint_baseline.json` 記 6 個 error、實際 0 個（待排收緊）。
- 完整 ruff 還有上百個既有錯誤、沒有棘輪基準，CI 只擋 F。
- CI 的新 ruff 步驟與 `toukaide` job 要等 PR 實跑才算確認。
- H20 只管精靈會寫的兩把金鑰（`DISPATCH_TOKEN`、`TOUKA_AUTH_SECRET`）；把 A 金鑰的公開值填在 B 金鑰仍不擋（ADR-0251 刻意）。
- `test_grandfather_batch1_auth.py` 仍打 `/scheduler/schedules`，接受 401／403／404（路由刪除後是 404），沒改。
- `docs/architecture/system_architecture.md` 的路由圖本來就和 `main.py` 對不上（圖上 24 個、`CORE_ROUTERS` 30 個＋`APP_ROUTERS` 2 個），本批只拿掉 scheduler 節點。

## 驗證

本機 Windows、Python 3.12 venv、fnm Node 24.21.0；各單位在自己的工作資料夾，基準 `8b2e242e`（ADR-0251 紀錄：8888 passed／7 skipped／6 failed，6 個是 E1 的 `test_local_compose_bindings`）。沒有打任何付費 API，也沒有啟動 Touka；自動化測試都在沙盒裡。

- U2（缺口 1、2、3、H3、H4，`d40f2ea1`）：紅燈與突變紀錄齊全；全量固定順序 8929 passed／7 skipped／6 failed；lint_ssot、#19、#28、#44、#45、#21、vulture 通過。
- U3（H7、H2，`1cf83eee`）：全量固定順序 8883 passed／7 skipped／6 failed（比基準少 5，已逐一對帳：role-plane 少 9、scheduler 測試 +3、supervisor +2、ratchet 少 1）；lint_ssot、#19、#28 等相關守衛與 vulture 通過。
- U5（H10、H11、N1、O20，`2b07888b`）：全量固定順序 8890 passed／7 skipped／6 failed；ToukaChat Vitest 265 檔 1626；IDE install、test、typecheck、compile、package 全過；lint_ssot、#19、#28、vulture 與前端守衛（含 #33 i18n）通過。
- U4（A4，`4acccf17`）：突變 18 個都紅；全量 8894 passed／7 skipped／6 failed；IDE 五指令、ToukaChat Vitest 1624、ESLint 棘輪、vulture＋knip 通過。
- U1（H20、H12、H9、H8，`5b80cad5`）：全量 8926 passed／7 skipped／6 failed；lint_ssot、#19、#28、#24、#21、#44、vulture、ruff 通過；沒有新增 noqa、錯誤碼、依賴。
- U6（H1，`02674bda`）：突變後端 5、前端 5 都紅；全量 8937 passed／7 skipped／6 failed；Vitest 1630、前端守衛全過、#24、#41、#19、#28 通過。
- 整合分支跨單位最終審查：通過（附註）；`54959d82` 的 pytest 固定順序與隨機順序都是 8984 passed／7 skipped／6 failed（＝8888 ＋ U2 41 − U3 5 ＋ U5 2 ＋ U4 6 ＋ U1 41 ＋ U6 11）。
- 修正後（`d77a4f51`，含 dev `22416643` 與最終審查要求的修正）：pytest 固定順序與隨機順序（seed 20261015）都是 9028 passed／7 skipped／6 failed（＝8984＋dev 的 35＋修正新增的 9），6 個 failed 都是 E1 的 `test_local_compose_bindings`，7 個跳過與基準相同；guard_all 43／43 PASS；自動 smoke 24／24；ToukaChat Vitest 272 檔 1657 個，typecheck、guard:api、guard:i18n 通過；ToukaIDE test、typecheck、compile、package 通過。
- 文件（U7，`56c65da4`，已 merge 整合分支 `03a23bda`；之後補 `6ff201c4`、`993a87bf` 兩處說明）：lint_ssot、Guard #19、Guard #28（252 份，0001..0252 連續）通過；`test_documentation_contracts.py`＋`test_tool_execution_classification.py` 40 passed、`test_guard_adr_integrity.py` 25 passed；`tool_executor.py` 只改註解，ruff 通過。建正式檔（已 merge dev `22416643`）後 Guard #28 為 254 份、0001..0254 連續。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。各項各自 commit，可獨立 revert 產品程式與測試；不改資料格式、不搬資料。缺口 1 撤回時本 ADR 的缺口 1 段標「已撤回」，ADR-0149、0229 的反向註記改回。缺口 1 與 H3 必須一起撤：只撤缺口 1、保留 H3 時，Manual 與無人在場會真的不問人就裝套件、跑代理人的程式（H3 之前這幾步因路徑錯誤跑不起來）。缺口 2 撤回時 MCP 伺服器回到繼承完整環境（含全部金鑰）。H7 撤回要一起還原 `NOQA_REGISTRY.md` 的計數。

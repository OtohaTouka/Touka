# ADR-0239：Authorization Gateway 與 ExecutionBroker（純重構）

- 狀態：✅ 已接受（維護者 2026-10-09 核准計畫；[ADR-0272](0272-iso-c2-agent-command-isolation-and-executor.md) 修訂：子程序仍只經 ExecutionBroker 啟動，runner 只改寫啟動參數，exec 類呼叫只准出現在 `sandbox_kernel/launcher.py`；執行器伺服器也經 broker 啟動啟動器，ratchet 不放寬）
- 日期：2026-10-09
- 決策者：維護者
- 影響範圍：`ToukaProxy/src/routers/*` 的角色檢查 helper、`services/authorization_gateway.py`（新增）、`services/execution_broker.py`（新增）、terminal／dispatch supervisor／review evidence／evolution／system backup／restore／`/mcp/scan` 的子程序啟動、`tests/lint/` ratchet 測試
- 相關：ADR-0141（角色平面分離）、ADR-0034（端點認證守衛）、ADR-0069（創建主權）、ADR-0076（演化沙箱資源限制）、ADR-0082（Dispatch 併入 Proxy 容器）、ADR-0103（Windows Proactor 反死鎖）

## 背景

外部設計稿提出「Security Kernel」：把安全檢查從「每個呼叫端記得呼叫 helper」改成「危險操作只能經過少數固定入口」。依 2026-10-09 的唯讀盤點，只有兩塊確實散落重複：

- **角色檢查**：`routers/` 內有 25 個私有 helper（如 `_require_mcp_role`、`_require_admin`），全部只是包一層 `AgentIdentity` 的判斷式（`is_privileged()`、`is_owner_or_admin()`、`is_admin_authority()`），歸納起來是 4 類相同行為加 1 個特例。
- **子程序啟動**：Windows／POSIX 新行程群組設定重複 2 處；同步／非同步啟動散在 7 個服務與 router。各處環境淨化（`get_clean_env` 黑名單、Dispatch 白名單、Dispatch supervisor 全量＋注入、MCP `LaunchEnvironmentPolicy`）是刻意不同的政策，不是複製。

維護者決定本輪是**純重構**：收斂重複程式碼，對外行為（HTTP 狀態碼、錯誤碼、優先序、子程序參數與環境）完全不變。

## 決策

### 1. Authorization Gateway

新增 `ToukaProxy/src/services/authorization_gateway.py`，提供 5 個共用函式，皆依賴 `get_verified_identity`、同步、不記錄日誌、通過時回傳原 identity：

| 函式 | 判斷 | 錯誤碼 |
| --- | --- | --- |
| `require_privileged` | `is_privileged()` | `AUTH_PERMISSION_DENIED` |
| `require_owner_or_admin` | `is_owner_or_admin()` | `AUTH_PERMISSION_DENIED` |
| `require_admin_authority` | `is_admin_authority()` | `AUTH_ADMIN_REQUIRED` |
| `require_authenticated` | 已登入 | `AUTH_PERMISSION_DENIED` |
| `require_admin_human` | 人類且 admin（原樣搬移，保留既有缺少 None 檢查的行為） | `AUTH_ADMIN_REQUIRED` |

各 router 保留原 helper 名稱，改為指向共用函式的別名（例如 `_require_mcp_role = require_privileged`）。因此端點簽名、Guard #24（依名稱子字串辨識認證／admin）、Guard #29（要求 `_require_creation_role` 字面）、測試的 dependency override 與 `openapi.json` 都不變。

不納入、原樣保留的檢查：

- 在函式內呼叫與掛在 `Depends` 的形式各自不變。前者遇到錯誤 body 回 422，後者回 403，現有測試鎖定此差異。
- 行為特殊的 inline 檢查不併入：debug `/tools` 拒絕時回 200 加空清單、context-snapshot、terminal approve／reject（CSRF 先行）、`group_settings`（`AUTH_OWNER_REQUIRED`）、`chat_upload`（`AUTH_UNAUTHORIZED_ACCESS`）、souls step-up、`tools_approval`、`resource_manager` 的人類 admin 判斷、`_require_mcp_data_access`（授權而非角色）。

### 2. ExecutionBroker

新增 `ToukaProxy/src/services/execution_broker.py`（只依賴標準庫），提供 3 個純轉傳函式：

- `new_process_group_kwargs()`：Windows 用 `CREATE_NEW_PROCESS_GROUP`，其餘用 `start_new_session=True`。
- `spawn_exec(*argv, new_process_group=False, **kwargs)`：非同步啟動的單一入口。
- `run_sync(argv, **kwargs)`：同步啟動的單一入口。

不注入任何預設值（env、stdin、encoding、timeout）。遷移 terminal、dispatch supervisor、review evidence、evolution sandbox、system backup、restore、`/mcp/scan` 的啟動呼叫；各自的環境、逾時、資源限制與終止方式不變。刪除無呼叫者的 `utils/terminal.py`。

刻意不遷移：

- ToukaDispatch 的外部 AI 召喚：獨立服務邊界，AGENTS.md 指定 `_build_child_env`。
- `process_registry.terminate_process_tree`：它本身就是終止機制。
- MCP SDK 的 stdio 啟動：由 SDK 負責群組、環境與終止。

### 3. Ratchet

以 `tests/lint/` 測試防止回退，不新增 guard 編號：

- `routers/` 內不得再定義私有角色檢查函式，只能是指向 gateway 的別名。
- `subprocess.run`／`subprocess.Popen`／`asyncio.create_subprocess_*` 只能出現在 broker 與明列的例外模組。

## 後果

- 新端點與新工具只要沿用 gateway／broker，就不必重寫角色判斷或行程群組分支；審查焦點收斂到兩個模組。
- 別名讓測試覆寫某個 helper 時，會在該測試期間影響所有共用同一函式的端點；`tests/conftest.py` 的 autouse fixture 會還原，無實際影響。
- 盤點同時發現數個需要**改變行為**才能修的安全缺口（例如 review evidence 以完整機密環境執行 agent 撰寫的指令、取消時遺留子程序、`/mcp/scan` 抓未釘版套件）。這些不屬本輪純重構，另開後續任務處理。

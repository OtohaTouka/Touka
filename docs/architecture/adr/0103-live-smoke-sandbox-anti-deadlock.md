# ADR-0103：Live smoke 反死鎖 — full-lifespan smoke 走 sandbox（保留 in-process TestClient）

- **狀態**：✅ 已接受
- **日期**：2026-06-21
- **影響範圍**：tests/smoke（lib/sandbox_env、4 個 full-lifespan smoke、run_all_smoke_tests）＋ lint_ssot 白名單
- **關鍵字**：smoke, TestClient, anyio BlockingPortal, ProactorEventLoop, teardown 死鎖, sandbox, 軌A, Windows, 逾時防呆

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> pre-import sandbox 從 4 支 full-lifespan smoke 擴至全部自動 smoke，並新增 runner-side per-child
> sandbox、manifest 分類與計量 API 人工閘；120 秒 timeout 仍只是最後一道安全網。

## 背景（Context）

4 個 standalone smoke（`smoke_router`、`smoke_dispatch_callback`、`smoke_ldh_handshake`、`smoke_creation_engine`）以 `with TestClient(app)` 觸發**完整 lifespan**。在 Windows 上，整套 `run_all_smoke_tests.py` 會永久卡住（曾被觀察為「跑 20 分鐘不動」）。

根因（已用 `faulthandler` 與回溯測試證實）：

- 卡點在 **anyio `BlockingPortal` × Windows `ProactorEventLoop`** 的關閉交握——主執行緒卡在 `starlette wait_shutdown`、portal 事件圈卡在 `windows_events._poll`，互等成死鎖。lifespan 關閉協程本身會跑完；卡的是框架層拆除。
- **觸發條件是「真實 config 的重量級啟動」**：真實 agent 讓 `start_bots()` 起背景迴圈、真實金鑰讓動態模型發現打數十次 API 並起 executor 執行緒；這些殘留任務/執行緒讓 Proactor 圈無法乾淨停止。**空 sandbox 啟動（0 bots／0 models）則 teardown 乾淨**——這也是為何 pytest 的 `auth_client`（同樣 `with TestClient(app)`、但在 conftest sandbox 下）從不卡。
- 此死鎖**自這些 smoke 誕生（2026-06-04）即潛伏**；專案無 Linux CI（Linux 用 SelectorEventLoop 不踩此坑），故只在本機 Windows 驗、又一再被誤判為「背景執行 flake／被 `| tail` 吞輸出」。

## 決策（Decision）

**將 full-lifespan standalone smoke 改為在 sandbox 下執行，並保留 in-process `TestClient`。**

- 新增共用 helper `tests/smoke/lib/sandbox_env.enable_smoke_sandbox()`：在 **import 任何 src 之前**注入 conftest 式 sandbox env（`TOUKA_CONFIG_DIR`=暫存、`TOUKA_BOOTSTRAP_ENABLED=false`、`ENABLE_DYNAMIC_MODELS=false`、`DISPATCH_AUTOSPAWN=false`、暫存 data dir），並種入最小且確定性的 `identity_map.json`（`DebugAgent→Debugger`，AGENTS.md §17 標準測試身分）。程序結束 `atexit` 自動清除暫存。
- 4 個 smoke 於頂部呼叫 `enable_smoke_sandbox(...)` 後再 import `src.*`、`TestClient`；其餘測試邏輯（含 `dependency_overrides`、`monkeypatch`）**完全不變**。
- `run_all_smoke_tests.py` 為每個 smoke 加 `subprocess.run(timeout=SMOKE_TEST_TIMEOUT_SECONDS=120)` 防呆——未來若有 smoke teardown 卡死，逾時即終止該測、標記失敗、繼續，不再卡死整套。
- `sandbox_env.py` 加入 `lint_ssot` 的 `ENV_WHITELIST`（合法 `os.environ` 存取，比照 `conftest.py`／`test_server_lifecycle.py`）。

### 被否決的方案

- **Live-server 子程序（在子程序起真 uvicorn、打真 HTTP）**：可行且保真度略高，但工程量大（子程序生命週期、跨平台 kill-tree、鑄 token、認證重寫），且**失去 `dependency_overrides` 與 `monkeypatch`**（`smoke_ldh` 的非信任來源測試、`smoke_router` 的角色注入都得重寫）。實測證明 sandbox 一招即解，故否決。
- **改用 SelectorEventLoop 繞過 Proactor**：不可行——Windows 上 MCP 的 stdio 子程序**必須**用 ProactorEventLoop。

## 後果（Consequences）

- ✅ 死鎖消除：4 個 smoke 皆乾淨 EXIT；整套 `run_all_smoke_tests.py` 正常跑完。
- ✅ 覆蓋完整保留：in-process TestClient 保留 `dependency_overrides`／`monkeypatch`；與 pytest conftest 同模式、跨平台（Linux 本就不受影響）。
- ⚠️ smoke 改在**空 sandbox**（空資料）執行，驗證的是結構正確性（端點不 500、握手、安全閘），而非真實資料內容——符合「軌A 系統正確性」定位。
- ✅ runner 逾時防呆：任何未來「忘記走 sandbox 的 full-lifespan smoke」不會再卡死整套，只會該測逾時失敗（訊息指向本 ADR）。

## 驗證

- 每個 migrated smoke 在 **3.12 venv**（[[feedback-python-venv]] 直譯器主權）下 EXIT=0 並功能通過。
- 整套 smoke 在 3.12 venv 下完整跑完、無 hang。
- 建基於 conftest 的 sandbox 機制（ADR-0006 測試主權／沙盒）。

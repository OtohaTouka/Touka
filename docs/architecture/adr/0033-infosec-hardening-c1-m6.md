# ADR-0033: InfoSec Hardening — C1 ~ N12 統一安全加固

**狀態**: ✅ 已接受（持續迭代）
**日期**: 2026-05-19（最後更新: 2026-05-20）
**關鍵字**: 資訊安全, 硬編碼密鑰, CSRF, ReDoS, Prompt Injection, RBAC, 沙箱逃逸, TOCTOU, SSRF, IDOR, SQL

---

## 背景與問題

2026-05 進行系統性安全審計，發現 **21 項弱點**橫跨四大子系統（ToukaProxy / ToukaHeartbeat / ToukaMemory / ToukaChat），分布為：

- **9 Critical (C1–C9)**：可在不知任何憑證下取得管理員權限或執行任意指令
- **6 High (H1–H6)**：Token 安全、輸入消毒、沙箱強化
- **6 Medium (M1–M6)**：CSRF、資源耗盡、ReDoS、反序列化驗證

審計顯示這些弱點都源自相同的**安全反模式**：
1. 公開已知的硬編碼預設密鑰（C1、C2、H5）
2. 信任未驗證的輸入作為身份來源（C3、C7、M5）
3. 字串比對代替結構化校驗（C4）
4. 缺少統一的安全守衛層（C8、C9、M1、M2、M3、M4）
5. 沙箱黑名單不完整（C6、H4）
6. 內容無消毒即進入持久層（H3）

---

## 決策：建立「五道安全守衛層」

所有後續安全相關修改必須對齊以下五道守衛：

### 1. 密鑰主權 (Secret Sovereignty)

**規則**：
- 系統不得包含任何「公開已知」的硬編碼密鑰預設值。
- 若密鑰未透過環境變數設定，啟動時必須：
  - 短壽命密鑰（JWT）→ 用 `secrets.token_hex(32)` 隨機生成（重啟後 token 失效可接受）
  - 持久密鑰（Heartbeat token、admin password）→ 隨機生成並持久化至設定檔
- 必須記錄 `WARNING` 告知操作者正在使用臨時密鑰。

**SSOT**：[`ToukaProxy/src/core/security.py`](../../../ToukaProxy/src/core/security.py) `AuthSecretManager`

### 2. 身份主權 (Identity Sovereignty)

**規則**：
- 未驗證的客戶端輸入（HTTP Header、SNS 事件、Cookie）**永遠不可**作為 `agent_id` 的事實來源。
- 認證失敗時，`agent_id` 必須固定為 `"Guest"`，`role` 必須固定為 `"Guest"`。
- 跨子系統信任邊界（如 SNS 平台 webhook）必須有 `is_verified` 標記。

**SSOT**：[`ToukaProxy/src/routers/proxy.py`](../../../ToukaProxy/src/routers/proxy.py) `_build_guest_context()`、`_apply_identity_to_context()`

### 3. 結構化邊界校驗 (Structured Boundary Validation)

**規則**：
- 路徑安全檢查**禁用** `str.startswith()`，必須用 `Path.is_relative_to()`。
- Shell 指令分類**必須**偵測鏈式 metachar（`; | && $( \`` 等），即使白名單通過也要升級為 CONFIRM。
- 動態生成程式碼（如 evolution jail）**必須**用 `json.dumps()` 轉義所有外部輸入。
- 反序列化來自磁碟的設定檔**必須**透過 Pydantic `model_validate()` 並含 `field_validator`。

**SSOT**：[`ToukaProxy/src/routers/editor.py`](../../../ToukaProxy/src/routers/editor.py)、[`ToukaProxy/src/tools/terminal.py`](../../../ToukaProxy/src/tools/terminal.py) `_classify_command_strategy()`、[`ToukaProxy/src/tools/builtin/evolution.py`](../../../ToukaProxy/src/tools/builtin/evolution.py) `_build_jail_code()`

### 4. 統一安全守衛層 (Unified Security Guards)

**規則**：
- 跨切面安全邏輯必須抽出純函式至 `src/utils/`，可獨立單元測試。
- 端點層只負責呼叫，不重複實作驗證邏輯。
- 守衛包含：
  - **CSRF**：`csrf_guard.verify_origin()`
  - **Rate Limiting**：`rate_limiter.RateLimiter`
  - **Webhook auth**：`csrf_guard.verify_origin()` + 業務層 `_warn_if_unverified()`
  - **Resource bound**：常數上限 (`MAX_PENDING_APPROVALS`、`MAX_SNS_CONTENT_LENGTH`)

**SSOT**：[`ToukaProxy/src/utils/`](../../../ToukaProxy/src/utils/) 目錄下純函式模組

### 5. 沙箱白名單優先 (Sandbox Whitelist-First)

**規則**：
- 演化沙箱的環境變數過濾**不得**依賴單純黑名單。
- 必須採「擴展黑名單模式比對 + 顯式移除已知危險變數」雙保險。
- Python 解譯器控制變數（`PYTHON*`）、動態連結（`LD_*`、`DYLD_*`）必須無條件移除。

**SSOT**：[`ToukaProxy/src/services/evolution_security.py`](../../../ToukaProxy/src/services/evolution_security.py) `get_clean_env()`

---

## 對齊三大開發原則

| 原則 | 落實方式 |
|------|---------|
| **TDD** | 每個 CVE 編號對應一個 `test_*_security.py`，Red→Green→Refactor；目前共新增 100+ 安全測試 |
| **SOLID-SRP** | 安全邏輯獨立成 `csrf_guard.py` / `rate_limiter.py` / `content_guard.py` / `authToken.ts` / `authMiddleware.ts` 等單一職責模組 |
| **SSOT** | 所有安全常數（`MAX_PENDING_APPROVALS`、`MAX_SNS_CONTENT_LENGTH`、`SECURE_COOKIES` 等）透過 `src/config.py` 或模組級常數集中管理 |

---

## 驗證機制

1. **`scripts/guard_all.py`** — **22/22 守衛全綠**（含 lint_ssot、NOQA registry、依賴守衛、模組邊界、pip-audit、npm audit、Endpoint Auth Dependency Guard）。
2. **`pytest tests/`** — **555+ 測試全綠**，含所有 C1–N12 對應的安全測試與 Guard #22 批次測試。
3. **二次審計（2026-05-20）** — 完成系統性二次安全審計，補修批次一至三（N1–N11）+ N12，共 33 項弱點全部關閉。
4. **Guard #22 架構強制（2026-05-20）** — 建立 Endpoint Auth Dependency Guard（ADR-0034），AST 掃描確保所有非豁免端點均帶有認證 Depends；52 個 grandfather 技術債端點全部補修完畢。
5. **SOLID-SRP Heartbeat（2026-05-20）** — `verify_heartbeat_token()` 獨立為 FastAPI Depends，所有 JWT 驗證邏輯離開 `awaken_agent` body，11 個 TDD 測試全綠。

## 補修批次紀錄（二次審計）

| 批次 | 弱點 | 類型 | 修復摘要 |
|------|------|------|---------|
| 批次一 | H5-残留 | High | 補修 ToukaHeartbeat/config.py + heartbeat.py 兩處殘留硬編碼 token |
| 批次一 | N1/N2 | Critical | `/ui/connections` GET/POST/status 加入角色驗證；status 消毒錯誤訊息 |
| 批次一 | N3 | Critical | `POST /mcp/hub/install` 加入 Owner/admin 驗證 |
| 批次二 | N4 | High | `auth_service.py` 加入 `threading.Lock`，抽出 `_atomic_users_write()` 原語 |
| 批次二 | N5 | High | `terminal.py` 加入 `asyncio.Lock` 保護 approval dict |
| 批次二 | N6 | High | `fetch_npm_downloads()` 加入 npm package name 嚴格正則驗證防 SSRF |
| 批次三 | N7/N9 | Medium | `manager.py` 加入 `_sanitize_connection_error()` 防 API key 洩漏 |
| 批次三 | N8 | Medium | `fetcher.py` 改 `follow_redirects=False` 防 SSRF 重定向鏈 |
| 批次三 | N10 | Medium | `GET /ui/projects/{id}/overview` 加入 Owner/admin 驗證防 IDOR |
| 批次三 | N11 | Medium | `user_service.py` 改用 `_atomic_users_write()` 修復 TOCTOU |
| 批次四 | N12 | Low | `async_db.py` 動態 SQL placeholder 改為固定 `IN (?, ?)` 字面量 |
3. **每次新增 noqa 必須更新 `NOQA_REGISTRY.md`** —  `guard_noqa_registry.py` 強制檢查。

---

## 一致性紀錄

`SECURITY.md` 維護完整修復清單與通報管道。每項弱點修復同時更新：
1. `SECURITY.md` Known Resolved Vulnerabilities 表
2. 對應 `*_INTERFACE.md`（若涉及 API 行為變化）
3. `SSOT_MASTER_MAP.md`（若新增 SSOT 元件）
4. `AGENTS.md` §18 開發禁令（若衍生新禁令）

# ADR-0047: ToukaDispatch Session Token Handshake（Vuln 1/2 修復）

**狀態**：✅ 已接受  
**日期**：2026-05-26  
**影響範圍**：ToukaDispatch  
**關鍵字**：Session Token, Handshake, Identity Spoofing, Path Traversal, DISPATCH_TOKEN, DispatchAgent

---

## 背景

### 問題描述

`ToukaDispatch/server.py` 的三個操作工具（`dispatch_initialize_mission`、`dispatch_update_task`、`dispatch_get_board`）接受呼叫方自行宣稱的 `agent_id` 參數，並直接信任該值作為身分依據。

**Vuln 1 — Agent Identity Spoofing（身分偽冒）**：  
任何持有有效 `DISPATCH_TOKEN` 的呼叫方，可透過宣稱 `agent_id="DebugAgent"` 取得 `can_supervise_dispatch` 能力，越權存取或重置 DispatchAgent 的任務看板。

**Vuln 2 — Path Traversal via agent_id（路徑穿越）**：  
`agent_id` 未驗證即進入路徑拼接（`Path(...) / "personal_drive" / agent_id / "missions"`），攻擊者可利用 `../` 構造越出沙盒邊界，存取其他 Agent 的資料。

### 根本原因

DISPATCH_TOKEN 的設計只驗證「你知道這個密鑰」，不綁定特定身分。工具設計允許呼叫方自行聲明任意身分，形成不可信輸入直接影響授權決策的安全反模式。

---

## 決策

### 方案選擇

| 方案 | 說明 | 採用？ |
|------|------|--------|
| A. 固定 DispatchAgent + Session Token | handshake 換取短命 token，身分永遠固定為 DispatchAgent | ✅ 採用 |
| B. A2A 協議預認證 | 要求外部 AI 先以 A2A 協議建立雙向信任 | ❌ 複雜度過高，外部 CLI 工具不具備 HTTP Server 能力 |
| C. allowlist 身分映射 | 將 DISPATCH_TOKEN 對應固定 agent_id | ❌ 需維護映射表，且無法審計呼叫方資訊 |

### 採用架構

```
外部 AI (Claude Code / Aider)
    │
    │  dispatch_handshake(dispatch_token, caller_name)
    ▼
ToukaDispatch MCP Server
    │  驗證 DISPATCH_TOKEN
    │  建立 SessionInfo { agent_id: "DispatchAgent", caller_info: {...} }
    │  回傳 session_token (secrets.token_urlsafe(32))
    ▼
外部 AI 使用 session_token 呼叫操作工具
    │
    │  dispatch_initialize_mission(session_token, goal, tasks)
    │  dispatch_update_task(session_token, task_id, status, ...)
    │  dispatch_get_board(session_token)
    ▼
SessionManager.validate(session_token)
    │  確認 token 存在且未過期
    │  身分永遠固定為 DispatchAgent（不使用呼叫方宣稱值）
    ▼
MissionManager 操作 DispatchAgent 的任務看板
```

---

## 實作細節

### 新增：`ToukaDispatch/session_manager.py`（SRP）

- `SESSION_TTL_SECONDS = 3600`（1 小時）
- `FIXED_DISPATCH_AGENT_ID = "DispatchAgent"`（模組級常數）
- `SessionInfo` dataclass：`session_token`、`agent_id`（永遠是 `FIXED_DISPATCH_AGENT_ID`）、`caller_info`、`created_at`、`expires_at`
- `SessionManager.create_session(caller_info)` → `secrets.token_urlsafe(32)`
- `SessionManager.validate(session_token)` → `SessionInfo | None`（過期自動清除）
- 時間使用 `memory_server.config_utils.get_current_epoch()`（TIME_SSOT）

### 修改：`ToukaDispatch/server.py`

**移除**：
- `_build_identity(agent_id)` 函式與 `resolve_agent_identity` import
- `IDENTITY_MAP_PATH` 環境變數讀取（移除 1 個 `ENV_SSOT_VIOLATION` + 1 個 `PATH_LITERAL_SSOT`）
- 所有工具的 `agent_id`、`target_agent_id` 參數

**新增**：
- `_session_mgr = SessionManager()`（模組級單例）
- `_verify_dispatch_token(token)` → 使用 `secrets.compare_digest`，空環境變數直接拒絕
- `_get_dispatch_identity()` → 固定回傳 `_DispatchIdentity(agent_id="DispatchAgent")`（`# noqa: IDENTITY_SSOT`）
- `dispatch_handshake(dispatch_token, caller_name, caller_version="")` 工具

---

## NOQA 影響

| 規則 | 變化 | 原因 |
|------|------|------|
| `ENV_SSOT_VIOLATION` | 8 → 7 | 移除 `IDENTITY_MAP_PATH` 環境變數讀取 |
| `PATH_LITERAL_SSOT` | 1 → 0 | 移除預設 identity_map.json 路徑常數 |
| `IDENTITY_SSOT` | 24 → 24 | `_DispatchIdentity` 仍保留 1 個 noqa（獨立容器協約） |
| 全系統合計 | 134 → 132 | |

---

## 安全性分析

### 修復後的威脅模型

| 攻擊向量 | 修復前 | 修復後 |
|---------|--------|--------|
| 宣稱 `agent_id="DebugAgent"` 取得高權限 | ❌ 可行 | ✅ 不可能（工具無 agent_id 參數） |
| 路徑穿越（`../` in agent_id） | ❌ 可行 | ✅ 不可能（agent_id 已移除） |
| session token 重放 | N/A | 🟡 TTL=1小時，可接受 |
| DISPATCH_TOKEN 洩漏 | 影響全系統 | 影響範圍：session token 短命，舊 session 不可用 |

### 身分固定原則（Identity Fixation）

無論 `dispatch_handshake` 的 `caller_name` 宣稱為何（DebugAgent、Alice、admin），所有 session 的 `agent_id` 永遠固定為 `"DispatchAgent"`。`caller_name` 僅作為審計追蹤資訊（`caller_info`），不影響任何授權決策。

---

## 測試覆蓋

| 測試檔案 | 案例數 | 涵蓋範圍 |
|---------|--------|---------|
| `test_dispatch_session.py` | 12 | SessionManager 生命週期、Handshake、session token 驗證 |
| `test_dispatch_server_tools.py` | 9 | 靜態確認（無 agent_id）、操作工具功能、身分固定驗證 |

---

## 後果

**正面**：
- 徹底消除 Vuln 1（身分偽冒）與 Vuln 2（路徑穿越）
- 增加審計追蹤（`caller_info` 記錄呼叫方名稱與版本）
- `IDENTITY_MAP_PATH` 環境變數需求移除，部署簡化

**負面**：
- 外部 AI 需多一步 handshake 呼叫（設計上可接受，明確換取安全保證）
- Session 不跨進程持久化（單容器部署足夠）

---

## 參考

- [ADR-0044](0044-agent-dispatch-system.md) — AI Agent 工作分派系統初始設計
- [ADR-0046](0046-toukadispatch-dedicated-mcp-server.md) — ToukaDispatch 獨立 MCP 伺服器
- [SECURITY.md D1/D2](../../../SECURITY.md) — 弱點登記

# 🚀 ToukaDispatch — 專用 AI Agent 任務派發 MCP 伺服器

**發行**：使用與 Proxy 相同的映像版本，實際 source／Release 見[根安裝指南](../README_QUICKSTART.md)；1.5.0（2026-07-30）為先前元件標記。
**Port**：8083（綁定預設 `127.0.0.1`，可由 `DISPATCH_BIND_HOST` 升開）  
**協議**：MCP SDK v2；modern-only Streamable HTTP `/mcp`；保留 modern request-scoped SSE，舊 HTTP+SSE `/sse` 回 404
**ADR**：[ADR-0046](../docs/architecture/adr/0046-toukadispatch-dedicated-mcp-server.md)、[ADR-0047](../docs/architecture/adr/0047-dispatch-session-token-handshake.md)、[ADR-0050](../docs/architecture/adr/0050-dispatch-handshake-reinstate-token-param.md)、[ADR-0064](../docs/architecture/adr/0064-toukadispatch-mission-token.md)、[ADR-0065](../docs/architecture/adr/0065-touka-dispatch-skills.md)、[ADR-0067](../docs/architecture/adr/0067-toukadispatch-auto-trigger-cac.md)、[ADR-0082](../docs/architecture/adr/0082-dispatch-in-proxy-consolidation.md)、[ADR-0201](../docs/architecture/adr/0201-toukadispatch-streamable-http-and-request-auth.md)、[ADR-0212](../docs/architecture/adr/0212-mcp-runtime-rollout-producers-and-artifacts.md)

---

## 概述

ToukaDispatch 是 Touka 系統的**專用任務派發 MCP 伺服器**，為外部 AI 工具（Claude Code、Aider 等）提供統一的任務看板存取介面。

與早期設計（任務派發內嵌於 `ToukaMemory/server.py`）不同，ToukaDispatch 以**責任單一原則（SRP）**獨立運行，且不含任何自動特權注入邏輯。所有工具呼叫的身份均固定為 `DispatchAgent`，透過 Handshake + Session Token 機制建立安全的短命會話（ADR-0047）。

**部署形態（ADR-0082）**：Docker 模式下不再有獨立容器——dispatch 隨 **proxy 單一映像**出貨，由 ToukaProxy 的 `DispatchSupervisor` 於啟動時 spawn 為 **localhost 子程序**並監督（崩潰退避重生、關機連坐終止）；8083 由 `touka-proxy` 服務對外發布。本目錄仍是 dispatch 源碼的 SSOT。

**現行 transport 契約（ADR-0201／ADR-0221）**：

| Endpoint | 用途 | 狀態 |
|---|---|---|
| `/mcp` | MCP 2026 Streamable HTTP | 主路徑；所有新整合使用 |
| `/sse`＋`/sse/messages/` | legacy HTTP+SSE | 已退役，回 404；不提供 legacy fallback |
| `/health` | readiness＋匿名 transport telemetry | 不含 token、caller name、mission ID |

---

## 安全設計

### 認證流程：Handshake → Session Token（ADR-0047 + ADR-0050 + ADR-0064）

ToukaDispatch 支援兩種認證路徑：

```
【內部路徑】Touka Agent 呼叫 dispatch_handshake(DISPATCH_TOKEN, caller_name)
    │  server 以 secrets.compare_digest 驗證 DISPATCH_TOKEN
    │  建立 internal SessionInfo（只存 token digest＋授權分面）
    │  回傳 session_token (TTL=3600s)
    ▼
Touka Agent 呼叫 dispatch_initialize_mission → 取得 Mission ID + Mission Token

【外部路徑】外部 AI 呼叫 dispatch_handshake(mission_token, caller_name)
    │  鎖內原子驗證＋消耗 mission_token → 解析 mission/generation
    │  建立受限 SessionInfo { caller_type: external, bound mission/generation }
    │  回傳 session_token (TTL=3600s)
    ▼
外部 AI 使用 session_token 呼叫 dispatch_get_board / dispatch_update_task
    │  每次 request 重驗 expiry/transport/audience/method/generation
    │  board 完整性檢查：bound_mission_id 必須與當前 active mission 相符
    │  dispatch_initialize_mission 呼叫 → 立即拒絕（外部 session 不得建立任務）
    ▼
MissionManager 操作 DispatchAgent 的任務看板
```

規則：
- `DISPATCH_TOKEN` 為空 → 拒絕所有請求（無降級模式）
- `DISPATCH_TOKEN`（主金鑰）**永遠不傳遞給外部 AI**
- 外部 AI 只接收一次性 `mission_token`；首次成功 handshake 即消耗
- application session 固定綁 modern `/mcp` transport；舊 legacy session 不可採納或跨用
- MCP session、connection 與 caller 自述 `clientInfo` 不參與授權

### 已修復安全弱點

| 編號 | 等級 | 描述 |
|------|------|------|
| C1 | Critical | 移除 `FORCE_AGENT_ID` 自動特權注入 |
| C2 | Critical | 移除行內 `FallbackIdentity`，改用模組級 `_PathIdentity` |
| C3 | Critical | 消除 `role="base"` 小寫不一致問題 |
| H1 | High | BLOCKED 狀態必填 `blocker_detail` |
| H2 | High | `Task` 模型啟用 `validate_assignment=True` |
| Vuln 1 | High | 移除工具 `agent_id` 參數，消除身份偽冒攻擊面（ADR-0047） |
| Vuln 2 | High | `agent_id` 不再進入路徑拼接，消除路徑穿越攻擊面（ADR-0047） |
| A1 | Design | Mission Token 隔離：DISPATCH_TOKEN 不離開 Touka 邊界；外部 AI 僅接收一次性 mission_token（ADR-0064） |
| A2 | Design | 外部 session 不得呼叫 `dispatch_initialize_mission`，防止外部 AI 劫持或建立任務板（ADR-0064） |
| D5 | High | bearer store 改存 digest，逐 request constant-time 驗證並綁 transport/audience/method（ADR-0201） |
| D6 | High | mission credential 原子消耗；rotation 原子撤銷舊 generation 與 external session（ADR-0201） |

---

## MCP 工具

### `dispatch_handshake`

交換 token 換取短命 session token。**必須先呼叫，後續操作皆需 session_token。**

支援雙路徑認證（ADR-0064）：
- 傳入 `DISPATCH_TOKEN` → 內部 session（可建立 mission）
- 傳入 `mission_token` → 外部 session（只能存取指定 mission，不可建立 mission）

| 參數 | 型別 | 說明 |
|------|------|------|
| `dispatch_token` | str | `DISPATCH_TOKEN`（內部）或 `mission_token`（外部 AI 專用） |
| `caller_name` | str | 呼叫方名稱（審計追蹤用） |
| `caller_version` | str \| None | 版本字串（選填，審計追蹤用） |

### `dispatch_initialize_mission`

初始化 DispatchAgent 任務看板。**僅限內部 session 呼叫；外部 session 呼叫將被拒絕。**

回應包含 `Mission ID` 與 `Mission Token`（供 Touka 轉交給外部 AI 使用），以及（若 `auto_invoke`）召喚狀態（🚀 已召喚 / ⚠️ 跳過或失敗）。

| 參數 | 型別 | 說明 |
|------|------|------|
| `session_token` | str | 由 dispatch_handshake 取得的 session token（必須為內部 session） |
| `goal` | str | 任務目標描述 |
| `tasks` | list[dict] \| None | 初始任務列表 |
| `auto_invoke` | str \| None | **ADR-0067**：executor 鍵（如 `"claude-code"`）。設定時 ToukaDispatch 依 `config/dispatch_hooks.json` 白名單自動 subprocess 召喚外部 AI，只透過環境變數傳一次性 mission_token（不需手寫 bash） |

#### 自動召喚（ADR-0067，dispatch_hooks.json）

`auto_invoke` 讓 ToukaDispatch 在 mission 建立後 fire-and-forget 召喚外部 AI：
- **白名單**：`config/dispatch_hooks.json` 的 `on_mission_created.<executor>.command`（list，shell=False，零指令注入）。新增外部 AI 只改設定、不改程式碼。
- **env 隔離（whitelist）**：只保留 PATH 等執行必需 env + 三個 DISPATCH_*；全域 `DISPATCH_TOKEN` 主金鑰**絕不外洩**，外部 AI 只收到一次性 mission_token。
- **fail-closed**：缺檔/壞檔/executor 不在白名單 → 不召喚（mission 仍建立，回 ⚠️）。
- **安全**：`dispatch_hooks.json` 須限 Owner 寫（防竄改注入 command）。

### `dispatch_update_task`

更新任務狀態。

| 參數 | 型別 | 說明 |
|------|------|------|
| `session_token` | str | 由 dispatch_handshake 取得的 session token |
| `task_id` | str | 任務 ID |
| `status` | str | 新狀態（TODO/DOING/DONE/FAILED/BLOCKED） |
| `artifact_ref` | str \| None | 單一產出物路徑 |
| `artifact_refs` | list[str] \| None | 多產出物路徑列表 |
| `test_log` | str \| None | 測試結果日誌 |
| `blocker_detail` | str \| None | **status=BLOCKED 時必填** |

### `dispatch_get_board`

取得任務看板狀態。

| 參數 | 型別 | 說明 |
|------|------|------|
| `session_token` | str | 由 dispatch_handshake 取得的 session token |

---

## 外部 AI 協作模式（ADR-0064 + ADR-0065）

### Touka → 外部 AI 完整流程

```
Touka Agent（已啟用 touka-dispatch-coordinator skill）
    │
    ├─ 1. dispatch_initialize_mission(session, goal, tasks)
    │       → 回傳 Mission ID + Mission Token
    │
    ├─ 2. bash 呼叫外部 AI（環境變數傳遞，不放 prompt 明文）
    │       DISPATCH_TOKEN=<Mission Token>
    │       DISPATCH_URL=http://localhost:8083/mcp
    │       DISPATCH_BOARD=<Mission ID>
    │       claude -p "<外部AI使用指南>"
    │
    ├─ 3. 外部 AI 連接 ToukaDispatch：
    │       dispatch_handshake(mission_token) → 外部 session（受限）
    │       dispatch_get_board() → 查看任務清單
    │       dispatch_update_task(..., "DOING") → 執行
    │       dispatch_update_task(..., "DONE", artifact_ref=...) → 完成
    │
    └─ 4. Touka 等待 callback 或主動查詢 dispatch_get_board()
```

### 安全邊界

| Token | 流向 | 限制 |
|-------|------|------|
| `DISPATCH_TOKEN`（全域主金鑰） | Touka 進程內部 | **絕不傳遞給外部 AI** |
| `mission_token`（一次性） | Touka → 外部 AI（環境變數） | 首次成功 handshake 即消耗；只能建立指定 mission/generation 的 external session |
| `session_token`（短命） | MCP tool 參數 | 每次 request 重驗；固定 transport/method/audience，程序重啟失效 |

外部 AI 使用指南：[`docs/external/touka_dispatch_guide.md`](../docs/external/touka_dispatch_guide.md)

---

## 任務狀態機 (FSM)

```
TODO → DOING → DONE (終端)
TODO → DOING → FAILED
TODO → BLOCKED
BLOCKED → TODO
FAILED → TODO  ← 僅特權身份可執行
```

非法轉換（如 `TODO → DONE`）將被拒絕並回傳錯誤。

---

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `DISPATCH_TOKEN` | 認證 Token（**必填，不可空**） | — |
| `DISPATCH_PORT` | 監聽 Port | `8083` |
| `DISPATCH_BIND_HOST` | 綁定位址（ADR-0082：預設 loopback、需對外時顯式升開 `0.0.0.0`） | `127.0.0.1` |
| `TOUKA_PROXY_URL` | ToukaProxy 回呼 URL（ADR-0051） | `http://localhost:8000` |

---

## 安裝與啟動

產品安裝依 [根指南](../README_QUICKSTART.md)，Dispatch 由 Proxy 映像內的 DispatchSupervisor 啟動；它不是另一個 user installer 或需手動建立的容器。本機 profile 的 8083 只發布在 loopback。

原 `server.py` 是 source 開發入口，依 [CONTRIBUTING](../CONTRIBUTING.md) 在 root venv 安裝全部 workspace packages。需要隔離測試時用既有 mock/sandbox；人工 native 開發才從 `ToukaDispatch` 目錄，以 root venv 的 Python 執行 `server.py`，使用正確環境設定提供非空 `DISPATCH_TOKEN`。不要把真 token 寫進命令列、提示或共享日誌。這不代表獨立部署 profile 已重新資格，也不增加分開安裝承諾。

---

## 接入設定

新 MCP client 直接把完整 modern endpoint 設為 remote URL：

```json
"mcpServers": {
  "touka-dispatch": {
    "url": "http://localhost:8083/mcp"
  }
}
```

現行 client 只使用 modern `/mcp`，包含 request-scoped SSE；舊 `/sse` 與 `/sse/messages/` 已退役並回 404，不能作為安裝後的 fallback。`DISPATCH_URL` 必須是完整 modern endpoint；mission 認證與每次 request 的 token 驗證仍必須成立。

---

## 測試

```bash
# 單元測試
python -B -m pytest tests/unit/test_dispatch_server_tools.py -v     # 10 項
python -B -m pytest tests/unit/test_dispatch_session.py -v          # 12 項
python -B -m pytest tests/unit/test_dispatch_mission_token.py -v    # 9 項（ADR-0064）
python -B -m pytest tests/unit/test_mcp_2026_p4a_dispatch.py -v      # ADR-0201 request-auth
python -B -m pytest tests/unit/test_mission_manager_bugfixes.py -v  # 9 項

# 整合測試
python -B -m pytest tests/integration/test_mcp_dispatch_tools.py -v # 3 項
python -B -m pytest tests/integration/test_dispatch_mcp_transport.py -v

# 全體守衛
python scripts/guard_all.py  # 目前 43 guards
```

# ADR-0067: ToukaDispatch 自動觸發機制（Configuration as Code）

## 狀態

✅ 已接受（時點性內容修訂：dispatch_hooks.json 的 pass_env 欄位為死 config 鍵——server.py 從不讀取、硬編 _CHILD_ENV_WHITELIST 無條件注入該三者（值恰相符故行為一致）；auto_invoke 自動觸發機制本身不變）

## 日期

2026-05-30

## 背景

ADR-0065 設計了 `touka-dispatch-coordinator` skill，其 Step 2 要求 Touka Agent（LLM）手動執行 bash 指令召喚外部 AI：

```bash
DISPATCH_TOKEN=<token> DISPATCH_URL=... DISPATCH_BOARD=<id> \
  claude -p "你是被 Touka 召喚的外部AI。請依下列步驟執行：
  1. 呼叫 dispatch_handshake(...)
  2. 呼叫 dispatch_get_board(...)
  3. 執行任務...
  （省略數百字操作指南）"
```

此設計存在兩個問題：
1. **指令冗餘**：操作指南必須每次嵌入 bash prompt，因為外部 AI 沒有預先安裝的 skill
2. **職責錯誤**：LLM 浪費推理預算在完全機械性的 subprocess 組裝上

ADR-0066 確立了 Anthropic 標準 SKILL.md + `.mcp.json` plugin 格式。在此基礎上，外部 AI 可以預先安裝 Touka skill plugin，同時獲得操作指南（SKILL.md）與 MCP 連線設定（`.mcp.json`），根本消除了在 bash prompt 嵌入指南的必要性。

此時，bash 指令的職責縮減為只傳遞兩個動態值：
```bash
DISPATCH_TOKEN=<mission_token> DISPATCH_BOARD=<mission_id> claude -p ""
```

這是純機械性操作，不需要 LLM 介入。

## 問題

1. 在 mission 建立後，誰負責觸發外部 AI 進程？
2. 如何在不修改程式碼的情況下支援新的外部 AI 工具？
3. 觸發機制應放在哪一層才符合安全邊界？

## 候選方案

### 方案 A：ToukaDispatch 硬編碼 Executor

在 `.env` 中設定 `DISPATCH_EXECUTOR_CLAUDE_CODE=claude`，程式碼查表執行。

**否決原因**：新增支援的外部 AI 工具需要修改程式碼，違反 Open/Closed 原則。

### 方案 B：ToukaProxy Listener（事件驅動）

ToukaDispatch 發出 `mission.created` event，ToukaProxy 收到後執行 subprocess。

**否決原因**：在 `dispatch_initialize_mission()` 通過授權驗證後，subprocess 的觸發是已授權動作的直接後果，不是新的授權決策點。將執行職責搬到 ToukaProxy 並未增加任何安全檢查，只是加了一條服務間通訊路徑。此外，外部 AI 僅與 ToukaDispatch 通訊本身就是安全設計——增加 ToukaProxy 作為中介反而擴大了外部 AI 間接接觸的系統邊界。

### 方案 C：`dispatch_hooks.json`（Configuration as Code）✅ 採用

在 ToukaDispatch config 目錄放置 `dispatch_hooks.json`：

```json
{
  "on_mission_created": {
    "claude-code": {
      "command": ["claude", "-p", ""],
      "pass_env": ["DISPATCH_TOKEN", "DISPATCH_BOARD", "DISPATCH_URL"]
    },
    "aider": {
      "command": ["aider", "--message", ""],
      "pass_env": ["DISPATCH_TOKEN", "DISPATCH_BOARD", "DISPATCH_URL"]
    }
  }
}
```

## 決策

**採用方案 C**。`dispatch_initialize_mission()` 加入選填參數 `auto_invoke`，指定要使用的 executor 鍵值。ToukaDispatch 查 `dispatch_hooks.json` 白名單，以 env var 方式傳遞 token，啟動外部 AI 進程。

### 新的完整呼叫流程

```
Touka Agent（LLM）
  ↓ 規劃任務
  呼叫 dispatch_initialize_mission(session_token, goal, tasks, auto_invoke="claude-code")
      ↓ 驗證 session_token（RBAC 已確認）
      ↓ 建立 mission，生成 mission_token
ToukaDispatch（自動觸發）
  ↓ 查 dispatch_hooks.json["on_mission_created"]["claude-code"]
  subprocess.Popen(
      ["claude", "-p", ""],
      env={DISPATCH_TOKEN: mission_token,
           DISPATCH_BOARD: mission_id,
           DISPATCH_URL: ...}
  )
      ↓ 外部 AI 啟動
外部 Claude Code
  ↓ 偵測到 DISPATCH_TOKEN + DISPATCH_BOARD env var
  ↓ touka-dispatch SKILL.md 自動觸發（ADR-0066 + ADR-0065）
  ↓ 透過 .mcp.json 已掛載的 MCP 執行 handshake / get_board / update_task
  ↓ 任務完成，callback
      ↓
Touka Agent 收到通知，向使用者回報
```

### 授權鏈分析

```
使用者請求
  → ToukaProxy RBAC（role 驗證）
      → session_token 核發
          → dispatch_initialize_mission() 驗證 session_token  ← 授權完成點
              → mission 建立
                  → subprocess 觸發  ← 已授權動作的後果，非新授權決策
```

授權在 `dispatch_initialize_mission()` 驗證 session_token 時已完成。後續的 subprocess 觸發是確定性操作，不需要額外授權機制。

### 安全設計

| 風險 | 緩解措施 |
|------|---------|
| 任意指令注入 | `command` 從 JSON 白名單讀取，不做字串拼接 |
| Token 洩漏（ps 可見） | Token 只透過 env var 傳遞，不作為命令列參數 |
| 未授權觸發 | 需通過 `dispatch_initialize_mission()` 的 session_token 驗證 |
| 外部 AI 越界 | mission_token 一次性、範圍限單一 mission；外部 AI 只能呼叫 ToukaDispatch MCP |

### `touka-dispatch-coordinator` Skill 的變化（更新 ADR-0065 Step 2）

**ADR-0065 原設計（Step 2）：**
```
LLM 組裝 bash 指令，嵌入完整操作指南（數百字），手動執行
```

**本 ADR 更新後（Step 2）：**
```
LLM 呼叫 dispatch_initialize_mission(..., auto_invoke="claude-code")
ToukaDispatch 自動觸發，不需要 LLM 進一步操作
```

Skill 的 `system_prompt` 可大幅簡化——Step 2 的 bash 指令組裝與外部 AI 操作指南完全移除。

### `dispatch_hooks.json` 放置位置

```
ToukaDispatch/config/dispatch_hooks.json
```

與 `session_config.json` 等設定檔同層，由 `resource_manager` 管理，符合 ADR-0004 物理路徑禁令。

### `auto_invoke` 不傳時的行為

`auto_invoke` 為選填。不傳時，`dispatch_initialize_mission()` 只建立 mission 並回傳 token，不觸發任何 subprocess。這保持對現有流程的向下相容性（使用者仍可手動執行 bash，或由其他機制觸發）。

## 後果

### 正面

- 新增支援的外部 AI 工具只需修改 `dispatch_hooks.json`，不動程式碼
- LLM 職責純化：只負責規劃任務與回報結果，機械性 subprocess 下沉至設定層
- 外部 AI 的 bash prompt 從數百字操作指南縮減為空字串（skill 自動提供指引）
- 外部 AI 的攻擊面維持最小：只能透過 mission_token 呼叫 ToukaDispatch MCP

### 負面

- ToukaDispatch 需要加入 subprocess 管理能力（process lifecycle、timeout、錯誤回報）
- `dispatch_hooks.json` 需要妥善保護（config 寫入權限限 Owner/admin）
- 外部 AI 進程的 stdout/stderr 需要決策是否捕捉（目前設計：fire-and-forget，依賴 callback）

## 相關 ADR

- [ADR-0064](0064-toukadispatch-mission-token.md) — Mission Token 安全隔離（mission_token 的產生機制）
- [ADR-0065](0065-touka-dispatch-skills.md) — ToukaDispatch 協調員 Skill（本 ADR 更新其 Step 2）
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — Skill 標準遷移（`.mcp.json` 讓外部 AI 自動掛載 MCP 的基礎）

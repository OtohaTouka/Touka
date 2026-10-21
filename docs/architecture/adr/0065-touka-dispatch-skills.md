# ADR-0065: ToukaDispatch 協調員 Skill 與外部 AI 使用指南

## 狀態

✅ 已接受（決策 Skill 1「touka-dispatch-coordinator」內部技能已於 2026-07-22 退役，見下方 ⚠️ banner；Skill 2 外部 AI 使用指南與安全守則仍有效）

## 日期

2026-05-30

> ⚠️ **部分內容已演進**：本 ADR 的 skill 儲存格式（`skills.json`）已由 [ADR-0066](0066-skill-system-anthropic-standard-migration.md) 遷移至 Anthropic 標準目錄式 `SKILL.md` + `.mcp.json`；Step 2 的「LLM 手動 bash 召喚外部 AI」已由 [ADR-0067](0067-toukadispatch-auto-trigger-cac.md) 的 `auto_invoke`（`dispatch_hooks.json` 白名單自動召喚）取代。本文保留為歷史決策紀錄，當前實作請以 ADR-0066 / ADR-0067 為準。

> 🗑️ **內部 coordinator skill 已退役（2026-07-22，Phase 2 候選缺口 G-1）**：Skill 1「touka-dispatch-coordinator」的前提——「Touka 內部 Agent 呼叫 `dispatch_*` MCP 工具召喚外部 AI」——在整合模式下**架構上不可達**：proxy 的 MCP client 為 stdio-only（[ADR-0116](0116-toukamemory-integration-only.md)），達不到 ToukaDispatch 的 SSE，`dispatch_*` 工具永不掛載於內部（僅存在於 `ToukaDispatch/server.py` 供外部 AI）。內部 agent 若照該 SKILL.md 操作＝呼叫不存在的工具（dead-on-arrival）。故該內部 skill 目錄（`ToukaProxy/config/skills/touka-dispatch-coordinator/`）已刪除，內部多步編排改由 in-process builtin（`dispatch_subagent`／`mission_tools`，[ADR-0134](0134-in-process-subagent-dispatch.md)~[ADR-0138](0138-parallel-fanout-and-resumable-delivery.md)）承接——Owner 恆常即有、毋須此 skill。**Skill 2（外部 AI 使用指南 `docs/external/skills/touka-dispatch/SKILL.md`）與 DISPATCH_TOKEN 安全守則不受影響、仍有效**（外部 AI 確實透過 SSE 使用 `dispatch_*` 工具）。防回歸守衛：`tests/unit/test_skill_mcp_declarations.py`（內部技能不得宣告 proxy 無法掛載的 MCP 伺服器）。

## 背景

ToukaDispatch（ADR-0046）提供了完整的任務協調 MCP 層，ADR-0064 解決了 Token 安全隔離問題。但仍缺少：

1. 讓 Touka 內部 Agent 知道「如何操作 ToukaDispatch 並召喚外部 AI」的 skill
2. 讓外部 AI 知道「如何連接 ToukaDispatch 並執行任務」的使用指南

若沒有這兩個組件，使用者需要手動協調整個分派流程，且難以保持安全守則（DISPATCH_TOKEN 不能洩漏）。

## 問題

1. Touka Agent 沒有內建的 ToukaDispatch 操作指南
2. 外部 AI 工具（Claude Code 等）沒有標準化的 ToukaDispatch 接入指南
3. 安全守則（DISPATCH_TOKEN 隔離）需要在兩個系統中一致執行

## 決策

### Skill 1：`touka-dispatch-coordinator`（Touka 內部 Skill）

**位置**：`ToukaProxy/config/skills.json`

此 skill 透過 `system_prompt` 注入完整的任務分派作業程序，教導 Touka Agent：

**Step 1**：呼叫 `dispatch_initialize_mission(session_token, goal, tasks)` 取得：
- `Mission ID`：任務識別碼
- `Mission Token`：外部 AI 專用一次性 token

**Step 2**：用環境變數傳遞連線資訊，透過 bash 呼叫外部 AI：
```bash
DISPATCH_TOKEN=<Mission Token> DISPATCH_URL=http://localhost:8083 DISPATCH_BOARD=<Mission ID> claude -p "<外部AI指南>"
```

**Step 3**：等待 ToukaProxy callback（最多 300 秒）或主動查詢 `dispatch_get_board()`。

**Step 4**：整理任務結果並回報使用者。

Skill 同時聲明 `mcp_servers: ["touka-dispatch"]`，確保 Agent 取得 ToukaDispatch MCP 工具存取權。

安全守則內嵌於 system_prompt：
- `DISPATCH_TOKEN`（全域主金鑰）**絕不傳遞給外部 AI**
- 外部 AI 只能收到 `Mission Token`（一次性）
- Token 一律透過環境變數傳遞，不放進 prompt 明文

### Skill 2：外部 AI 使用指南（獨立文件）

**位置**：`docs/external/touka_dispatch_guide.md`

提供給外部 AI 工具的獨立使用指南，說明：

1. **環境變數接收**：
   - `DISPATCH_TOKEN`：一次性 mission_token（此處命名為 DISPATCH_TOKEN 以維持介面一致性）
   - `DISPATCH_URL`：ToukaDispatch 服務位址
   - `DISPATCH_BOARD`：被指派的 mission board ID

2. **四個必要步驟**：authenticate → view tasks → execute → finish

3. **任務狀態更新格式**：`artifact_ref`, `artifact_refs`, `blocker_detail`

4. **安全限制**：
   - session TTL 3600 秒
   - 不能呼叫 `dispatch_initialize_mission`
   - token 範圍只限被指派的 mission

## 設計決策

### 為何使用 bash 而非 MCP Server？

外部 AI 工具（如 Claude Code CLI）透過 bash 呼叫提供了：
- **環境隔離**：每次任務獨立進程
- **標準化介面**：任何支援 `-p` flag 的 AI CLI 均可使用
- **安全邊界**：Token 透過環境變數而非命令列參數傳遞（避免 `ps` 可見）

### 為何外部 AI 指南是獨立文件而非 Touka 內部 skill？

- 外部 AI 沒有 Touka skill 機制，需要 prompt 形式的指南
- 指南可以直接嵌入 `claude -p` 的 prompt，不需要額外安裝
- 指南保持獨立，各家 AI 工具可以使用同一份文件

## 後果

### 正面

- 使用者只需指揮 Touka Agent，無需了解外部 AI 的連接細節
- Touka 自動處理 mission_token 生成、傳遞、等待與結果整理
- 外部 AI 使用指南獨立於 Touka 系統，可直接作為 prompt inject
- 安全守則（DISPATCH_TOKEN 隔離）由 skill 系統強制執行

### 負面

- Skill system_prompt 較長（含完整操作步驟與安全守則）
- 外部 AI 需要支援環境變數注入與 MCP SSE transport

## 相關 ADR

- [ADR-0064](0064-toukadispatch-mission-token.md) — Mission Token 安全機制（先決條件）
- [ADR-0046](0046-toukadispatch-dedicated-mcp-server.md) — ToukaDispatch MCP 伺服器
- [ADR-0051](0051-dispatch-callback-channel.md) — Dispatch 雙向回呼通道（Step 3 等待機制）

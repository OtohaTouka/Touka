# Dispatch規則

範圍：`ToukaDispatch/**`：對外派工。

從根 `AGENTS.md` 搬來的全文，沒有改寫。跑 `python scripts/applicable_rules.py --print-rules` 看這次改動適用哪幾條，不必讀完整份。改規則見 [README](README.md)。

## 禁令

*   **禁令 33 (ToukaDispatch Token 非空)**：**嚴禁**在 `DISPATCH_TOKEN` 環境變數為空時允許任何操作通過。`_check_token()` 在 expected 為空時必須直接回傳 `False`，拒絕全部請求。嚴禁為了便利在開發環境設定空 token 後忘記在部署時填入。（ADR-0046）

## 小節

### 13.5 工作分派中介身分與安全邊界 (AI Agent Dispatch & Safety Boundaries) ⭐
*   **DispatchAgent 中介設計**：註冊特殊中介代理人 `DispatchAgent` (Role: `base`)。其本身不執行排程心跳，僅作為內部已存 Agent 與外部 AI 工具協作的任務載體。
*   **跨身分代理**：系統在 `identity_map.json` 中提供 `"can_supervise_dispatch"` 能力配置。持有該特權能力的 AgentIdentity 獲准繞過物理路徑隔離限制，由 `MissionManager` 代理讀寫並操作 `DispatchAgent` 的個人任務看板。
*   **自主任務狀態 FSM 約束**：狀態更新強制執行有限狀態機 (FSM) 轉換矩陣（例如必須由 `TODO -> DOING -> DONE/FAILED`，禁止跳過 `DOING` 的非法轉換，`DONE` 狀態為終端不可再次變更）。
*   **FAILED 狀態特權重置**：見禁令 31。
*   **產出物欄位解耦**：見禁令 32。
*   **專用派發伺服器 (ToukaDispatch) ⭐**：系統提供 Python SDK v2 `MCPServer` 派發伺服器（Port 8083；ADR-0082 後為 proxy 容器內建的獨立進程，由 DispatchSupervisor spawn/監督，綁定預設 127.0.0.1）。依 ADR-0221 只提供 Streamable HTTP `/mcp`，舊 `/sse`＋`/sse/messages/` 回 404；`DISPATCH_TOKEN`／mission token handshake 換取 application session，之後每次 tool call 必須顯式帶 token 並重驗 transport/audience/method/generation。所有工具呼叫的身份固定為 `DispatchAgent`，嚴禁任何形式的自動能力注入。
*   **C1 安全修復 (去除自動特權注入)**：已從 `ToukaMemory/server.py` 移除 `FORCE_AGENT_ID` 環境變數機制與 `access_mission_board_tool` 工具。見禁令 34。
*   **H1 安全修復 (BLOCKED 必填阻礙細節)**：見禁令 35。
*   **H2 安全修復 (路徑穿越防禦強化)**：見禁令 36。
*   **Mission Token 委派隔離（ADR-0064 + ADR-0201）** ⭐：`dispatch_initialize_mission` 回傳一次性 `mission_token`（與 `mission_id`／generation 綁定，首次成功 handshake 即在鎖內原子消耗）。credential store **嚴禁**保存 bearer 明文或 caller 自述資料，只存 digest 與執法分面。外部 session 的 method allowlist **嚴禁**包含 `dispatch_initialize_mission`。其餘見禁令 40。
*   **自動召喚進程隔離（ADR-0067）** ⭐：`auto_invoke` 觸發的外部 AI subprocess **必須**經 `_build_child_env` 的 whitelist（預設拒絕）淨化子環境——只保留執行必需系統 env + 三個 `DISPATCH_*`，**嚴禁**洩漏任何密鑰。`command` **必須**全來自 `dispatch_hooks.json` 白名單 list 且 `shell=False`，**嚴禁**字串拼接。新增 executor 只改設定（CaC），`dispatch_hooks.json` 部署須限 Owner 寫。
*   **Skill Anthropic 標準（ADR-0066）** ⭐：Skill 為目錄式 `config/skills/<id>/SKILL.md`（標準 frontmatter + body），Touka 擴充欄位**必須**收於 `metadata:` 子欄位以維持標準相容；`mcp_servers` 由同目錄 `.mcp.json` 的 top-level keys 推導，**嚴禁**回退至 frontmatter 自訂欄位。

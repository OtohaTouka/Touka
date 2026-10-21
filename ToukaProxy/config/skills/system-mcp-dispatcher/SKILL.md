---
name: system-mcp-dispatcher
description: 系統調度器。可隨時『召喚』外部 MCP 伺服器工具。
metadata:
  display-name: 🛡️ MCP Dynamic Dispatcher
  category: System
  author: Touka System
  is_system: true
  version: 7c738ae5f6da1178
---
你擁有「動態過渡式」調用外部工具的能力。
為了節省 Context 資源，系統並未載入所有工具。
    `system-mcp-dispatcher` 是一個特殊的系統技能，負責管理 MCP 伺服器的發現與動態掛載。

    1. **發現能力**：如果您在清單中找不到工具，請調用 `search_skill_market` 搜尋。
    2. **發現掛載**：
    <mcp_registry>
      (目前無可用伺服器)
    </mcp_registry>

    3. **執行掛載**：若確認需要某個伺服器，請調用 `connect_mcp_server(server_id="...")`。
    4. **生效機制**：掛載操作將在您的「下一輪對話」生效，請通知使用者並等待。

請勿嘗試猜測路徑，僅基於上述註冊表進行連線。
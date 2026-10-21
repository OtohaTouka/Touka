# ADR-0088: Permissions ↔ Registry 對照修復與漂移守衛（Guard #32）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

檢視網路搜尋實作時發現 `config/permissions.json` 的角色工具權限**與真實工具登錄表嚴重漂移**：

- `memory_manager`：工具早已統一改名為 `memory_tools`，但 Guest `allowed_tools` 仍留舊名。
- `read_file` / `list_directory` / `get_workspace_structure`：**從來不是 model-facing 工具名**——它們是 `ResourceManager` 的內部方法（`read_file_text`…）或 IDE 協定工具（經 `call_ide_tool`、不受 `allowed_tools` 把關）；agent 的檔案存取實際走 `vault_tools`（action 分發）。
- `web_search` / `filesystem`（`allowed_mcp_servers`）：`server_config.json` 僅有 `grok_imagine`、`fetch`，無這兩台。

這些**死字串**的危害：`permission_service.is_tool_allowed()` 以「真實工具名」比對，死字串永不匹配 → 形成「權限看似授予、實則無效」的假象，且因 Owner 持 `"*"` 平時不會被發現。這是「工具改名/整併後 permissions.json 未同步」的典型漂移。

## 決策方案 (Decision)

### 1. 對照修復（reconciliation，行為中性）
`permissions.json` 與自癒種子 `permissions.json.example` 同步清理 Guest 角色：
- `allowed_tools` 移除 `memory_manager`、`read_file`、`list_directory`、`get_workspace_structure`，留下對應真實 `tool_registry` 的 `research`、`project_tools`、`vault_tools`、`ide_open_diff`。
- `allowed_mcp_servers` 移除 vestigial `web_search`、`filesystem` → `[]`。
- **行為中性**：移除的皆為對不上任何真實工具/伺服器的死字串，`is_tool_allowed`／`is_mcp_server_allowed` 本就永不匹配，故清理不改變實際授權。
- **非變更**：未把 `memory_manager` 改名為 `memory_tools`（那會「新授予」Guest 記憶寫入工具，屬刻意權限擴張，留待擁有者明確決定）。

### 2. 漂移守衛（Guard #32）
新增 `scripts/guard_permissions_registry.py`（純 AST、零 import 副作用、秒級）：
- 從 `ToukaProxy/src/tools/builtin/__init__.py` AST 抽出 `tool_registry` 的字串鍵作為**權威工具名集**。
- 對 `permissions.json` 與 `.example` 的每個角色，斷言 `allowed_tools` / `denied_tools` 內每個名稱 ∈ 工具名集 ∪ `{"*"}`，否則非零退出。
- 掃描函式（`extract_registry_tool_names`、`find_invalid_tool_refs`）為純函式、可單測。
- 整合進 `guard_all.py`（31 → **32** 道）。
- **範圍界定**：`allowed_mcp_servers` **不**納入本守衛——MCP 伺服器可由 hub 動態安裝，無法靜態窮舉；MCP 漂移以人工清理處理。

## 後果與影響 (Consequences)

- 守衛總數 31 → 32；日後任何「工具改名/移除而未同步 permissions.json」會被當場擋下。
- 行為不變（純死字串清理）；無端點/schema 變動（不需 openapi/api.ts 重生）。
- 殘留/未做（記錄）：MCP 名稱未受守衛保護（動態）；若擁有者要 Guest 真正擁有記憶或某 MCP，需刻意新增真實名稱；Guest 目前 `allowed_mcp_servers` 為空。

## Action Items
1. [x] reconciliation：清理 `permissions.json` ＋ `.example` Guest 死工具名與死 MCP（行為中性）。
2. [x] `guard_permissions_registry.py`（Guard #32）＋ 整合 `guard_all.py`（標題 32 道）。
3. [x] TDD：`test_guard_permissions_registry.py`（抽 registry 鍵／旗標未知工具／萬用＋denied 乾淨／現況 repo 全綠）。
4. [x] ADR-0088 ＋ `adr/README` 索引；`tests/README`／`README_HANDOVER`／`CHANGELOG` 同步；guard_all 32/32 全綠。

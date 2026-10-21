# ADR-0079: 第一方開源文件 MCP（mcp-docgen）整併與 curated Hub 條目（survive-sync）

> **現況**：輸出夾共用改成 per-agent vault jail（ADR-0115 反轉）。

## 狀態

✅ 已接受（2026-06-09 實作：mcp-docgen 已發佈 PyPI 0.1.0；Touka MCP Hub 收錄 curated 條目、`document-generator` 薄技能、sync 保留 curated、install 解析輸出夾哨兵）

> 🗑️ **「輸出夾 v1 共用（非 per-agent 隔離）」已被 [ADR-0115](0115-vault-jailed-mcp.md)（vault-jailed MCP）反轉**：現 `mcp_hub.json` curated 條目帶 `vault_jail_arg="base_dir"`、`tool_executor` 每次派發呼叫 `mcp_vault_jail.apply()` 把 `base_dir`（輸入+輸出根）覆寫為呼叫 agent 自己的 vault＝**per-agent 監獄，非共用 temp**。（另：外部 mcp-docgen 已由 0.1.0/三工具進展至 v0.6/17 工具含 PDF/HTML；哨兵解析已抽至 `mcp_sentinels.py` SSOT＋加第 2 哨兵 `@TOUKA_APPDATA@`。）curated 整併／survive-sync／`document-generator` 薄技能核心仍有效。

## 日期

2026-06-09

## 背景

Anthropic 的 docx/pptx/xlsx/pdf 文件技能是專有授權（source-available、禁 retain/copy/distribute），Touka 不可收錄（見 [ADR-0074](0074-skill-catalog-curated-real-skill-install.md) 的授權護欄）。為補上「產生 Office 文件」能力，另建一個**獨立、MIT 授權、Markdown 驅動**的 MCP 伺服器 `mcp-docgen`（包 python-docx／openpyxl／XlsxWriter／python-pptx，clean-room 自寫，已發佈 PyPI 0.1.0），任何 MCP 客戶端皆可 `uvx mcp-docgen` 復用。本 ADR 記錄把它接進 Touka 的整併決策。

## 問題

1. **技能不執行程式碼**：Touka 的 `SKILL.md` 是純指令、不跑程式（`get_skill_resource` 唯讀）；要實際產檔得靠「會執行的」MCP 伺服器或內建工具。因目標是「讓任何人復用」，選獨立 MCP（內建工具只服務 Touka 自身）。
2. **Hub sync 覆寫**：`MCPHubService.sync()` 以 official+smithery 整個覆寫 `mcp_hub.json`，會抹掉手加的第一方條目。
3. **技能 `.mcp.json` 僅宣告**：`.mcp.json` 只貢獻 server 名稱（→ `ensure_connected(name)`），真正啟動設定在 `server_config.json`，由 Hub `install()` 寫入。故 Hub 條目 `name` 與技能 `.mcp.json` key 必須對齊（皆 `mcp-docgen`）。
4. **輸出檔可取性**：MCP 子程序 `env` 為靜態（非 per-agent），且預設 `./out` 在容器內不可取。

## 決策

**以「curated（第一方策展）Hub 條目 ＋ `document-generator` 薄技能」整併，並讓 curated 條目 survive sync。**

- **Hub 條目（curated）**：`config/mcp_hub.json` ＋ tracked 種子 `config/mcp_hub.json.example` 各加一筆 `id=curated-mcp-docgen`、`source=curated`、`is_official=false`（誠實標示：Touka 策展、**非 Anthropic 官方**，延續 [ADR-0072](0072-skill-hub-curated-manifest-and-pinned-sync.md) 誠信原則）、`category=File`、recipe `uvx mcp-docgen`、`env.MCP_DOCGEN_OUTPUT_DIR=@TOUKA_OUTPUT@`。`bootstrap.py` ESSENTIAL_CONFIGS 加 `mcp_hub.json`（缺檔自癒）。
- **sync 保留 curated**：`sync()` 先判斷外部兩源皆空 → `no_update`（保留現有檔）；否則讀現有 `source=="curated"` 條目，與 official/smithery 依 `name` 去重合併（curated 優先）→ 第一方條目永不被外部覆寫。
- **install 解析輸出夾**：`install()` 合併 curated 條目的預設 `env`，並把哨兵 `@TOUKA_OUTPUT@` 解析為 Touka 受管共用輸出夾（`resource_manager.get_temp_path("docgen_output")` 絕對路徑）→ 安裝即帶上可取的 `MCP_DOCGEN_OUTPUT_DIR`。
- **薄技能**：`config/skills/document-generator/`（`SKILL.md` `is_system=false`、`category=Productivity`；`.mcp.json` 宣告 `mcp-docgen`）教 agent 三工具與 Markdown 慣例。`.mcp.json` 的 value 帶 `uvx` recipe，亦供外部 Claude Code plugin 安裝（[ADR-0066](0066-skill-system-anthropic-standard-migration.md) 雙用）。

## 後果

### 正面
- Touka 取得 Office 文件產出能力，全程開源乾淨授權；`mcp-docgen` 對外可復用（不綁 Touka）；curated 條目耐 sync；誠實標示不誤導。
- 無 OpenAPI 變動（`install()` 讀原始 JSON，`MCPHubServerItem` 不加 `env` 欄位，extra 欄位被 response_model 忽略不外洩）。

### 取捨／限制
- 輸出夾為 v1 **共用**（非 per-agent 隔離）；per-agent 隔離需動 `connect_mcp_server` 動態注入 `env`，列未來增強。
- 技能需先在 MCP Hub 安裝 `mcp-docgen` 才能實際呼叫工具（未裝時工具不出現）。

### Clean-room
- `mcp-docgen` 實作只依 MCP 公開規格＋各開源庫公開文件，**未讀任何 Anthropic 技能/MCP 實作碼**。

## 相關 ADR

- [ADR-0074](0074-skill-catalog-curated-real-skill-install.md)（Skill Catalog 授權護欄）—— 排除 source-available 文件類技能，是本 MCP 自建路線的前提。
- [ADR-0072](0072-skill-hub-curated-manifest-and-pinned-sync.md)（Skill Hub 誠信修正）—— curated 條目 `is_official=false` 的誠實標示沿用其原則。
- [ADR-0033](0033-infosec-hardening-c1-m6.md)（InfoSec：命令注入防護）—— Hub install 命令過 `is_command_safe`；mcp-docgen 輸出 jail。
- [ADR-0069](0069-touka-native-creation-engine.md)（Creation Engine / register_server）—— 同屬 MCP `server_config` 寫入體系。
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md)（Skill 標準 / `.mcp.json` 雙用）—— 技能 `.mcp.json` 既宣告又供外部 plugin 安裝。
- [ADR-0115](0115-vault-jailed-mcp.md)（vault-jailed MCP）—— **反轉本 ADR「輸出夾共用」：docgen 輸出改 per-agent vault jail**。

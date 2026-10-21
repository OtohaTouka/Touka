# ADR-0066: Skill 系統遷移至 Anthropic 標準 SKILL.md 格式

## 狀態

✅ 已接受

> 🔧 **兩處決策描述與現行碼不符（無對應修訂 ADR，屬碼-doc 漂移）**：①**frontmatter `is_system`/`author`/`category` 由 ADR 示的「扁平頂層鍵」改為巢狀 `metadata:` 子鍵**（`skill_parser.build_frontmatter`；因頂層未知欄位過不了 Anthropic `quick_validate` 的 `ALLOWED_PROPERTIES`，與 ADR「標準工具會忽略未知頂層欄位」的相容性論證相反）；②**Phase P3「auto-trigger（description 匹配引擎）」與 `/skill-name` user-invoked 型未落地**——實際出貨為 **Pull 模式**（`render_skill_registry` 無條件列全部非系統 skill＋agent 顯式呼叫 `activate_skill`；`argument-hint` 後端/前端零讀取）。SKILL.md SSOT／`.mcp.json`／三層 Progressive Disclosure 核心遷移仍有效。

## 日期

2026-05-30

## 背景

Touka 原有 Skill 系統以單一 `skills.json` 平面 JSON 儲存所有技能，格式為 Touka 專有設計：

```json
{
  "skills": {
    "skill-id": {
      "id": "...", "name": "...", "system_prompt": "...",
      "mcp_servers": ["..."], "is_system": true, ...
    }
  }
}
```

Anthropic 於 2025 年發布 [Agent Skills 開放標準](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)，採用目錄式 `skill-name/SKILL.md` 格式，適用於 Claude.ai、Claude Code、Developer Platform 等所有平台。

Touka 的長期目標是成為所有外部 AI Agent 的指揮層。若 Touka 內部 Skill 格式與業界標準不相容，外部 AI 無法直接使用 Touka 的 skill 套件，也無法利用標準工具鏈（evals、packaging、description optimization）驗證和分發技能。

## 問題

1. `skills.json` 平面格式與 Anthropic 標準不相容，外部 AI 無法安裝 Touka 的 skill
2. `mcp_servers` 欄位是 Touka 自訂擴充，不在標準規格內，無法被標準工具鏈處理
3. 現有格式無 Progressive Disclosure（system_prompt 整包注入，context 效率差）
4. 無 Bundled Resources 支援（scripts/, references/, assets/）
5. 無 `.skill` 打包與分發機制

## 決策

**採用方案一：完全遷移**（而非橋接模式）。

Touka 的 Skill 系統全面遷移至 Anthropic 標準目錄格式，Touka 原生的特殊需求以**相容性擴充**（額外 frontmatter 欄位）而非格式修改的方式實現。

### 新目錄結構

```
config/skills/
└── skill-name/
    ├── SKILL.md          # 必填，標準格式
    ├── .mcp.json         # 選填，此 skill 需要的 MCP 連線設定
    ├── scripts/          # 選填，可執行腳本
    ├── references/       # 選填，參考文件
    └── assets/           # 選填，靜態資源
```

### 標準 Frontmatter 欄位（完整支援）

```yaml
---
name: skill-id            # 必填，唯一識別碼（等同目錄名稱）
description: "..."         # 必填，觸發條件，model-invoked 靠此自動觸發
version: 1.0.0            # 選填，語意版本（取代 updated_at）
argument-hint: <arg>      # 選填，User-invoked slash command 的參數提示
allowed-tools: [Read]     # 選填，預核准工具白名單（對應 ABAC 管控）
model: haiku              # 選填，覆寫執行模型
---
```

### Touka 擴充欄位（向後相容，標準工具忽略）

```yaml
---
# Touka 擴充，不影響標準相容性
is_system: true           # 系統保護 skill，不可由使用者刪除
author: "Touka System"    # 作者標記
category: "System"        # UI 分類顯示
---
```

### `.mcp.json` 取代 `mcp_servers` 欄位

**原設計（非標準）：**
```yaml
mcp_servers: ["touka-dispatch"]   # frontmatter 自訂欄位
```

**新設計（標準相容）：**
```json
// config/skills/touka-dispatch-coordinator/.mcp.json
{
  "touka-dispatch": {
    "type": "sse",
    "url": "${DISPATCH_URL}/sse"
  }
}
```

此設計帶來雙重效益：
- **Touka 內部**：SkillService 啟動 skill 時讀取 `.mcp.json` 自動掛載 MCP
- **外部 AI**：安裝 skill plugin 時，`.mcp.json` 自動在 Claude Code 等客戶端註冊 MCP

同一個檔案，兩個場景皆無需額外配置。

### 兩種 Skill 類型（由 Frontmatter 區分）

| 類型 | 識別方式 | 觸發方式 |
|------|---------|---------|
| **Model-invoked** | 只有 `name` + `description` | description 匹配自動觸發 |
| **User-invoked** | 含 `argument-hint` 或 `allowed-tools` | 使用者輸入 `/skill-name` |

### Progressive Disclosure（三層載入）

| 層級 | 內容 | 載入時機 |
|------|------|---------|
| 1 — Metadata | frontmatter（name + description） | 常駐 context |
| 2 — Body | SKILL.md 本文 | skill 觸發時 |
| 3 — Resources | scripts/, references/, assets/ | 按需讀取 |

### 遷移計畫（四個 Phase）

| Phase | 內容 | 工程量 |
|-------|------|--------|
| P1 | 格式遷移：SkillService 改讀目錄 + YAML parse，遷移現有 2 個 skill | 3 天 |
| P2 | Progressive Disclosure：三層載入 + Bundled Resources 支援 | 2 天 |
| P3 | Auto-trigger：description 匹配引擎（MVP：關鍵詞；完整版：LLM 評分） | 4 天 |
| P4 | 打包分發：`.skill` 格式 + `POST /skills/install` | 1 天 |

## 設計決策

### 為何選擇完全遷移而非橋接模式？

Touka 目標成為所有 AI Agent 的指揮層，需要與業界 skill 生態系完全相容。橋接模式（保留 `skills.json` + 支援 SKILL.md 匯入）會維持兩套格式，增加長期維護負擔，且外部工具只能部分整合。完全遷移一次解決根本問題。

### 為何 `.mcp.json` 優於 `mcp_servers` frontmatter 欄位？

Anthropic 標準 plugin 格式本身就包含 `.mcp.json`（plugin-level MCP config）。將 MCP 設定放入 `.mcp.json` 符合標準，且讓同一份設定同時服務於 Touka 內部自動掛載與外部 AI 客戶端自動安裝，不需要任何客製化橋接邏輯。

### Touka 擴充欄位的相容策略

標準工具（skill-creator、packaging scripts）在解析 frontmatter 時忽略未知欄位。Touka 擴充欄位（`is_system`, `author`, `category`）不破壞任何標準工具的行為，可安全共存。

## 後果

### 正面

- Touka 的 skill 可直接打包為 `.skill` 分發給外部 AI 安裝
- 外部 AI 安裝 Touka skill plugin 即自動取得 MCP 連線設定，零額外配置
- 可使用 skill-creator 的 evals 工具鏈驗證 skill 效果
- description optimization script（`run_loop.py`）可直接用於 Touka skill
- Progressive Disclosure 減少不必要的 context 佔用

### 負面

- SkillService 需要大幅改寫（讀取目錄 + YAML parse）
- `sync_system_dispatcher()` 需改為寫 SKILL.md 格式
- 現有 2 個 skill 需手動遷移至目錄格式

## 相關 ADR

- [ADR-0063](0063-skill-hub-external-sync.md) — Skill Hub 外部同步（前驅設計）
- [ADR-0065](0065-touka-dispatch-skills.md) — ToukaDispatch 協調員 Skill（本 ADR 更新其 skill 格式部分）
- [ADR-0067](0067-toukadispatch-auto-trigger-cac.md) — ToukaDispatch 自動觸發機制（依賴本 ADR 的 `.mcp.json` 設計）

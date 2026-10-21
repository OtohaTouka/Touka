# ADR-0077: 退役 persona Skill Hub，技能探索整併至真技能目錄（Retire Skill Hub, Consolidate into Catalog）

## 狀態

✅ 已接受（2026-06-07 實作：persona Skill Hub 子系統全移除、真技能目錄分頁更名「Skill Hub」、全套守衛/測試綠）

## 日期

2026-06-07

## 背景

Touka 的「技能」一度有三套並行概念：

1. **Skill Hub（ADR-0063 → ADR-0072）**：6 筆內建 persona 預設（`system_prompt` ＋ `required_tools` 字串清單），本質是**佔位用的提示詞範本**（`author` 全為「Touka 內建範本」、`mcp_servers` 全空、`rating` 為示範值）。
2. **真 Skill（ADR-0066）**：目錄式 `SKILL.md`，可含 `scripts/`（可執行）/`references/`/`.mcp.json`，由 `SkillService` 以三層 Progressive Disclosure 載入。
3. **Skill Catalog（ADR-0074）**：從 GitHub 釘 40-hex commit、逐檔 SHA-256 驗證安裝的**真 `.skill`**（初始策展：mcp-builder / skill-creator / webapp-testing / gh-address-comments）。

## 問題

1. **persona Hub 是真 Skill 的退化子集**：它只能注入指令，無法帶腳本／資源／MCP；即使是「純指令」場景，真 Skill 也更省 context（漸進揭露 vs persona 的全量恆注入）。
2. **架構冗餘**：設定頁同時有「探索中心（Hub）」與「真技能目錄（Catalog）」兩個探索中心，概念重疊、易混淆。
3. **6 筆人格實為佔位且零依賴**：查證確認——全 repo grep 僅出現在種子檔；無任何 agent/soul 引用；無機制把 persona 注入 agent（前端 `installHubSkill` 僅「一鍵把 `system_prompt` 轉成一個陽春 skill」，且無 agent 預先綁這六個）。soul 的 `system_prompt` 只讀 `soul.md`，與 Hub 無關。

## 決策

**退役整個 persona Skill Hub 子系統，由真技能目錄（ADR-0074）承接「技能探索中心」角色，前端分頁更名為「Skill Hub」。**

- **後端移除**：刪 `services/skill_hub_service.py`；`routers/skills.py` 拔 `GET /v1/skills/hub`＋`POST /v1/skills/hub/sync` 端點、`SkillHubModel`、`get_skill_hub_path`；`config.py` 拔 `SKILL_HUB_MANIFEST_*` 常數與 `get_skill_hub_manifest_url()`；`bootstrap.py` 從 `ESSENTIAL_CONFIGS` 拔 `skill_hub.json`；刪種子 `config/skill_hub.json.example`。OpenAPI（根 `openapi.json` ＋ 前端 `api.ts`）重生。
- **前端移除＋更名**：刪 `SkillHubView`/`SkillHubItem`；`useSkillSettings` 拔 `skillHubItems`/`fetchSkillHub`/`installHubSkill`/`syncHub`；`SettingsView` 拔串接；`SkillsTab` 拔「探索中心」分頁、**「真技能目錄」分頁更名「Skill Hub」**；`types.ts` 拔 `HubSkill`。
- **測試**：刪 `test_skill_hub_service.py`/`test_skill_hub_seed_integrity.py`/`test_hub_skill_type_safety.py`/`smoke_skill_hub.py`；清 `test_bootstrap`/`SkillsTab.test` 的 hub 引用（冒煙 18→17、pytest −20、vitest −3）。
- **文件**：ADR-0063/0072 標「已取代（Superseded by 本 ADR）」並**保留檔案**（維持 ADR 守衛的編號連續性與引用完整）；`SSOT_MASTER_MAP` 拔 Skill Hub Sync 列；`SECURITY`（A14–A16）/`AGENTS`（禁令 54）標退役；NOQA registry `B008` 134→132（移除兩個 hub 端點的 `# noqa: B008`）。

## 後果

- **正面**：單一、更強、更省 context 的技能探索中心（真 Skill 是 persona 超集）；消除三套概念中最弱、最冗餘的一套；死碼守衛（#20 vulture+knip）證實零孤兒殘留。
- **相容（零 agent 影響）**：無任何 agent/soul 引用那六個 persona，刪除不破壞任何既有 agent 資料。
- **功能取捨**：失去「一鍵把 persona 預設轉成陽春 skill」的低摩擦入口；改由「自建技能」或安裝真技能目錄項目達成（品質更高、來源可審）。
- **ADR 守衛**：採「標記 superseded、保留檔案」而非刪除，維持編號連續（0001..0077）與引用不斷鏈（ADR-0068）；本 ADR 為 0063/0072 的唯一 superseder。
- **驗證**：guard_all **31/31**、pytest **1433**、vitest **314**、冒煙 **17/17** 全綠。

## 相關 ADR

- **ADR-0063**（SkillHubService 外部技能來源同步）、**ADR-0072**（Skill Hub 策展清單與釘版同步）——本 ADR 取代之（persona Hub 全退役）。
- **ADR-0074**（真技能策展目錄）——承接「技能探索中心」角色（前端分頁＝「Skill Hub」）。
- **ADR-0066**（Skill 系統 Anthropic 標準）——真 Skill 為 persona 的超集，是整併的技術前提。
- **ADR-0068**（ADR 完整性守衛）——退役採「標記 superseded、保留檔案」以維持連續性與引用完整。

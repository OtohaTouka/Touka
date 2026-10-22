# ADR-0119: 原生工具介面重設計——失敗可見性、Schema 衛生與統一檔案動詞（Unified Tool Interface Redesign）

## 狀態

✅ **已接受（Tier 1–3 全部實作完成、全綠——guard_all 37/37、後端 pytest 2589 passed/4 skipped；擁有者尚未 commit）**。本 ADR 收斂提案 `docs/architecture/proposals/native-tool-interface-redesign.md`（設計 2026-07-03 與擁有者定案；量測數據與逐層落地紀錄見提案 §11），並延伸 ADR-0100（工具 schema canonical）、ADR-0102（技能↔MCP 接線）、禁令 59（工具失敗可見性）。

Tier 2 的「Anthropic 原樣保留」一句已由 ADR-0266 修訂：送給 Claude 時只剝根層組合子（Anthropic 不收根層 `oneOf`／`anyOf`／`allOf`）；條件必填的正本與 Gemini 剝除不變。

Tier 3 第 6 點「雙軌遷移」與「後果」的「舊名呼叫經 shim 完全相容」已由 ADR-0269 修訂為有條件相容：模型迴圈裡叫舊名時，它的後繼正式工具要全部列在這一輪的清單上才放行，否則回 `not_advertised`；shim 本身、舊名的角色權限與 `/debug/execute` 不變。

## 日期

2026-07-04

## 背景

維護者回報 AI agent 常「調用工具失敗或幻覺」。實地稽核 `ToukaProxy/src/tools/**` 與 middleware/handler，對照 Anthropic《Writing tools for agents》、OpenAI function-calling 與 MCP tools 準則，定位三類病灶：**A 失敗不可見**（Gate 1 靜默過濾、幻覺偵測器漏 MCP）、**B 錯誤無結構混語言**（模型無法自我修正）、**C schema 衛生**（action-enum god-tools、`vault_tools` 無 schema 永不被廣告、隱藏必填參、動詞不一致）。select-only eval 基準（DebugAgent、真實模型）證實：兩腦（deepseek-v4-flash／grok-4.3）選對率皆僅 80%。

設計錨定 pi coding agent 的啟示：**裸動詞（read/write/edit…）在模型的 RL 訓練分布內**，自製 action-enum 方言不在——弱模型受害最深。

## 決策

三層漸進（每層獨立可回退；量化驗收由 `tests/tool_eval/` select-only eval 把關）：

### Tier 1 — 失敗可見（2026-07-03）
結構化錯誤 SSOT（`services/tool_errors.py`，`error_class` 首鍵、附 `suggestion`）；executor 全錯誤路徑分類（「查無」`resource_not_found` 與「被拒」`permission_denied` 明確分流）；ToolLayer 對政策 DENY 注入 `<tool_availability>` 可讀說明（不再無聲）；幻覺偵測器涵蓋 MCP；policy 未載入 fallback CONFIRM→**DENY**（fail-safe）。（禁令 59）

### Tier 2 — Schema 衛生（2026-07-04）
補 `VAULT_TOOLS_SCHEMA`；identity/project_id **描述層透明化**（不進 required——executor 注入在 `**args` 之後永遠蓋過模型值，宣告 required 只會誘導編造）；重疊工具描述互指「何時不要用」；mission_tools 壓平（`task_id`/`dependency_task_ids`＋橋接雙收）；條件必填 oneOf（action 判別式）；**Gemini 出場剝除 `oneOf`/`anyOf`/`allOf`/`const`**（其 OpenAPI 子集不支援、不剝會 400；Anthropic 原樣保留）。

### Tier 3 — 統一檔案動詞＋scope（2026-07-04，本 ADR 主體）
1. **9 顆裸動詞工具**（`src/tools/file_verbs.py`）：`read / list / find / grep / write / edit / insert / undo / delete`。原則一句話：**參數形狀由動詞決定、權限由 scope 決定**——每顆 schema 固定形狀＋top-level required（不再需要 oneOf），必填 `scope` enum、無預設、每次調用自包含（嚴禁狀態機式範圍切換）。
2. **scope 語義**：`project`（當前專案；project_id 依會話自動注入）→ 委派 `project_tools` 引擎（路徑沙盒＋審計事件不變）；`personal`/`legacy`/`soul` → 委派 `vault_tools` 橋（身分錨定＋soul 唯讀硬化不變）。**零邏輯重複**：動詞是既有引擎的薄轉接層，不複製任何沙盒／審計邏輯。
3. **soul 讀統一、寫獨立**：read-family scope 含 `soul`；write-family 的 scope enum **根本沒有 soul**（非法狀態不可表示），碰到即回 `permission_denied`＋指路；寫入走專屬 **`edit_soul`**（只收 write/str_replace/insert/undo_edit/delete，委派 `update_my_soul` 沿用靈魂保護與審計；read 動作回可讀錯誤指向 `read(scope=soul)`）。
4. **動詞語義整併**：`append`/`create` 併入 `write(mode=overwrite/append/create_only)`（create_only 以讀探測拒絕覆寫）；`view` 併入 `read(view_range)`（映射引擎 offset/limit）；`str_replace` 正名 `edit`；`insert`/`undo` 動詞側實作（undo＝行程內快照、深度 1、零 `.bak` 檔案污染、跨 agent／專案以錨點隔離）。`grep` 不廣告引擎做不到的 `path` 參數。
5. **`sns_control` → `send_message`**：丟掉單值假 action enum。
6. **雙軌遷移**：新工具廣告；舊 god-tools（`project_tools`/`vault_tools`/`update_my_soul`/`sns_control`）**保持可執行（shim）但不再廣告**（`LEGACY_SHIM_SCHEMAS` 保留形狀文件；smoke `SCHEMA_EXEMPT` 同步）。廣告面 21→28 顆、每顆憑名字可選對。ToolLayer 的 project_tools 條件注入退役（動詞常駐，未選專案時由執行層回結構化可讀錯誤）。〔2026-10-15 修訂，見 ADR-0269：模型迴圈裡舊名要它的後繼正式工具全部列在這一輪的清單上才放行，否則回 `not_advertised`。〕
7. **權限**：Guest 授予 9 動詞、`edit_soul` 入 denied（鏡像 `update_my_soul`）；`CRITICAL_TOOLS` 升為模組常數並納入新名（空參即拒）。registry 一律**字面鍵**（Guard #32 以 AST 靜態抽取）。

### 刻意遞延（範圍外，另案啟動）
`bash` → `execute_command`（非同步審批）、演化管線 4→2 段、per-identity 動態 scope-enum 收窄（現無受益角色；Gate 2 已硬防）、未選專案時動態剝除 `project` enum 值。

## 後果

- **量測（select-only eval、8 案例、ANY-OF 語義；各輪快照皆保留於 `tests/tool_eval/baselines/`）**：
  - **grok-4.3**：pre-Tier-2 80%（選錯 20%）→ Tier 2 後 **100%（選錯 0%）** → Tier 3 後 **100%（選錯 0%）**——含 `read(scope=soul)` 統一讀、`write(scope=personal)` 完整參數。
  - **deepseek-v4-flash**：pre-Tier-2 80%（選錯 16.7%）→ Tier 2 後 83.3%（選錯 14.3%）→ Tier 3 後 **83.3%（選錯 5.0%，n=3 取樣）**；單樣本（n=1）曾見 66.7%（一次探索性 read 取代 write 的抖動），三取樣後動詞家族行為全對（`read(scope=soul)`／`read(scope=project)`／`write(scope=personal, content, mode)` 皆正確）。
  - 幻覺率／無效參數率**全程 0%**、政策拒絕 100%。
  - **殘留**：`mission-initialize`（稀疏無目標 prompt）為 deepseek 三輪一貫的唯一慢性失敗（跑去 ask_user／探索）——與檔案動詞無關，屬 mission_tools 意圖訊號問題，列為後續觀察指標。
- 舊名呼叫（技能、錄製案例、外部 prompt）經 shim 完全相容；軟下架待 eval 長期綠燈後另議。〔2026-10-15 修訂，見 ADR-0269：改為有條件相容，模型迴圈裡後繼正式工具全部列在這一輪的清單上才放行；代理只開了部分新工具時，用舊名會被擋。〕
- 新增動詞若與 MCP 工具撞名：MCP 已有 `server__` 前綴、結構上不撞；builtin 撞名由 ToolLayer 去重警告（Tier 1）把關。

## 相關

- 提案全文：`docs/architecture/proposals/native-tool-interface-redesign.md`（§11 含逐層驗證數據）
- [ADR-0100](0100-tool-schema-canonical-openai.md) — 工具 schema canonical（出場轉換單點；Tier 2 的 Gemini 剝除落於此路徑）
- [ADR-0102](0102-skill-mcp-wiring-guards.md) — 技能↔MCP 接線一致性
- 禁令 59（工具失敗可見性）、禁令 15/16（密鑰／身分主權——identity 注入不可由模型提供）

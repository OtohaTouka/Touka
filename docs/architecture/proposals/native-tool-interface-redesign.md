# 提案：原生工具介面重設計（讓 AI agent 少調用失敗／幻覺）

> 狀態：**Tier 1–3 全部落地（2026-07-04）、已收斂為 [ADR-0119](../adr/0119-unified-tool-interface-redesign.md)。** 方向 2026-07-03 與使用者定案；三輪 eval 量測見 §11（Tier 3 後：grok 100%／deepseek 83.3%·選錯 5%）。本文件保留為設計依據與逐層落地紀錄。
> 已決策：①檔案家族統一為 **9 顆裸動詞工具＋scope 必填 enum**（§6）；②soul **讀統一、寫獨立**（保留專屬 `edit_soul`）；③eval 以**手動腳本、先「只選不執行」**接 DebugAgent 實測（§8）。
> 收斂：本提案已於 2026-07-04 收斂為 [ADR-0119](../adr/0119-unified-tool-interface-redesign.md)（原生工具介面重設計）。
> 約束：**由人類 commit**；只用 **DebugAgent** 身分驗證、不動其他 agent 的資料；**嚴禁自動修改 `data/`**。
> 佐證：本提案基於一次對 `ToukaProxy/src/tools/**` 與 middleware/handler 的實地稽核（file:line 見下），以及 Anthropic《Writing tools for agents》、OpenAI function-calling、MCP tools spec 的公認準則。

---

## 1. 目的

使用者回報：AI agent 常「調用工具失敗或幻覺」。稽核確認**問題不在工具數量**（~22 顆 builtin，遠低於「破壞性懸崖」的 100+），而在**三類品質缺陷**。本提案依 **ROI（效益／改動風險）** 分三層，讓你能先做「高效益、低破壞」的部分，結構性重構留到後面、且可分階段雙軌上線。

---

## 2. 稽核摘要（依影響力排序）

### 🔴 A. 失敗不可見 —— 這是**幻覺的頭號成因**（最高優先）

| 病灶 | 證據 | 為何害幻覺 |
|---|---|---|
| **Gate 1 靜默過濾**：`ToolLayer` 因 policy/role DENY 直接把工具從 advertised 清單移除，不記錄、不告知模型 | `middleware/layers/tool.py:88–91`（`if decision.effect=="DENY": continue`） | 模型永遠不知道某工具「存在但被權限擋掉」，於是**幻覺去呼叫它** |
| **兩個 Gate 不對稱**：Gate 2 執行層會回 `❌ 政策攔截`，Gate 1 卻完全無聲 | `tool_executor.py:170–186` vs `tool.py:88–91` | 同一件事（權限不足）有時有訊息、有時沒有，模型無法建立穩定預期 |
| **幻覺偵測器漏掉 MCP 工具** | `claude_handler.py:325–329` 只比對 `tool_registry / memory_tool_registry / plugin_registry`，**沒呼叫** `protocol_manager.get_all_tools()` | 被 Gate 1 濾掉的 MCP 工具名不在靜態 registry → 偵測器對它「假陰性」 |
| **desired_tools／ask_user 也靜默移除** | `tool.py:79–80`、`tool.py:172–182` | 模型收不到任何解釋 |

### 🟠 B. 錯誤訊息無結構、混語言 —— 模型**無法自我修正**

| 病灶 | 證據 |
|---|---|
| 「找不到工具」與「工具存在但被拒」都回同一句 `Error: Tool 'X' not registered.` | `tool_executor.py:283,285` |
| 錯誤字串中英夾雜 + emoji：`❌ 執行攔截`（中）/ `Error:`（英）/ `✅`（成功） | `terminal.py:155,186`、`editor.py:188`、`mission_tools.py:64,91` |
| 例外被截斷成 200 字 `Internal Execution Failure`，結構資訊丟失 | `tool_executor.py:328–338` |
| policy 載入失敗時 fallback 成 `CONFIRM`（把設定錯誤偽裝成正常審批） | `permission_service.py:493–498` |

### 🟡 C. Schema 衛生 —— 工具「長得像但語意不同」，模型選錯

| 病灶 | 證據 |
|---|---|
| **action-enum 巨獸**：`update_my_soul`（7 動作）、`project_tools`/`vault_tools`（各 8 動作） | `schemas.py`、`project_tools.py:130–144` |
| **人造窄化**：`sns_control` 只有 1 個 action（`send_message`），enum 形同虛設 | `schemas.py:99–100` |
| **`vault_tools` 根本沒有 schema** → 模型只能從 docstring 猜參數 | `vault_tools.py`（無 `VAULT_TOOLS_SCHEMA`） |
| **隱藏必填參**：`identity`、`project_id` 由 `kwargs.get()` 注入，schema 的 `required` 只有 `action` | `project_tools.py:50–51,154`、`mission_tools.py:52,259` |
| **動詞不一致**：`str_replace` vs `replace`、`artifact_ref` vs `artifact_refs` | 跨檔 |
| **重疊工具無邊界說明**：`project_tools` / `vault_tools` / `text_editor` 都能改檔，schema 沒說何時用哪個 | `schemas.py` 描述僅 1–2 句、無「when NOT to use」 |
| **深層巢狀**：`mission_tools.tasks`（3 層）、`scaffold_mcp.tools`（2 層） | `mission_tools.py:204–223` |
| **名稱唯一性無保證**：builtin↔MCP 撞名時 last-write-wins 靜默去重 | `tool.py:207–216`、`registry.py:81–97` |
| **RBAC 門檻沒寫進描述**：`create_skill`/`create_mcp`/`scaffold_mcp` 只有執行時才知非管理員會失敗 | `creation.py:22–29` |

---

## 3. 設計原則（本提案依據）

1. **繞著「意圖」整併，不繞著實作** —— 工具名 = 意圖；避免 god-tool，也避免碎成 list→get→process。
2. **讓失敗可見且可據以修正** —— 廣告出去的工具集 == 可呼叫的工具集；拒絕要「可讀、分類、附建議」。
3. **schema 自我說明** —— 名稱／描述含「何時用、何時不要用」；必填參數要出現在 `required`。
4. **參數極簡、預設合理、enum 節制、巢狀 ≤1 層。**
5. **一次只改一件事、可回退、非破壞優先。**
6. **動詞走訓練分布（pi 的啟示）** —— `read/write/edit/bash` 是模型在 RL 訓練中看過海量範例的「母語」；自製方言（action enum 巨獸）強迫模型現學。參考：[pi coding agent](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)（4 顆核心工具、系統提示＋工具定義 <1000 tokens、能力靠 bash+CLI+README 漸進揭露）。
7. **無狀態** —— 每次調用自包含；**嚴禁**「先切換範圍、後續動詞作用其上」的隱藏狀態設計（模型會忘記所在範圍→跨範圍誤寫）。

---

## 4. 重設計（分三層，可獨立採納）

### ✅ Tier 1 — 讓失敗可見（**最高 ROI、非破壞、建議先做**）

> 這一層**不改任何工具的參數或名稱**，只改「錯誤與過濾的呈現」。預期對「幻覺／調用失敗」的改善最大。

1. **統一錯誤分類**（見 §5 表）：把 `tool_executor.py` 的錯誤全部改成結構化 JSON：`{error_class, message, suggestion, ...}`。五類：`validation_error / permission_denied / resource_not_found / timeout_exceeded / upstream_error`。
2. **Gate 1 不再靜默**：`ToolLayer` 對 DENY 的工具，改為在 context 累積 `denied_tools`（含 reason）。二選一策略（建議 **b**）：
   - (a) 仍不 advertise，但當模型嘗試呼叫時，Gate 2 回 `permission_denied`（含 reason + 建議）而非 `not registered`；
   - (b) 對「存在但被拒」的工具，仍不放進可呼叫清單，但在 system context 附一則簡短「你目前無權使用 X（原因：…）」讓模型不要幻覺、也能向使用者解釋。
3. **修幻覺偵測器**：`claude_handler.py` 的 `all_known_tools` 加入 `protocol_manager.get_all_tools()`（涵蓋 MCP），並把 `permission_denied` 視為「合法的被過濾訊號」而非幻覺。
4. **`not registered` 拆義**：真的不存在 → `resource_not_found`；被 Gate 1 濾掉 → `permission_denied`。
5. **policy 載入失敗改 fail-safe**：`permission_service.py:493–498` 的 fallback 由 `CONFIRM` 改 `DENY`（設定錯不應偽裝成正常審批）。
6. **名稱撞名要出聲**：`registry.find_server_by_tool_name` 撞名時記 warning；MCP 一律 `server__tool` 前綴確保唯一。

### ✅ Tier 2 — Schema 衛生（**非破壞、加法為主**）——**已於 2026-07-04 落地**

> 只加欄位／描述，不改行為；可與 Tier 1 併行。（實作細節與量測見 §11。）

1. **補 `vault_tools` schema**（含 `vault` enum `personal/legacy/soul`、動作 enum、`soul` 為唯讀的說明）。
2. **`identity`、`project_id` 僅在描述說明由系統注入，不宣告成 schema `required`**（ADR-0119 實際落地決策；此處原 required 敘述已更正）。2026-09-15 公開輸入完整性與執行 grant 補強見 [ADR-0228](../adr/0228-native-tool-public-input-contracts.md)，不是重新啟動 Tier 3。
3. **每顆工具描述補「何時用 / 何時不要用」**（尤其 `project_tools` vs `vault_tools` vs `text_editor` 的邊界；`research` vs 本機檔案）。
4. **RBAC 門檻寫進描述**（`create_skill`/`create_mcp`/`scaffold_mcp` 標「需 Owner/Admin」）。
5. **巢狀壓平**：`mission_tools.tasks` 由「巢狀陣列」改為扁平 `{task_id, dependency_task_ids: [str]}`；原擬合併 `artifact_ref`/`artifact_refs`。**2026-09-15 現況更正**：兩者是獨立欄位，保留各自語意與驗證，只有 plural 對模型廣告；見 [ADR-0228](../adr/0228-native-tool-public-input-contracts.md)。
6. **條件必填用 JSON Schema `oneOf`/discriminator**（例：`mission_update` 依 `status` 決定 `artifact_ref`/`blocker_detail` 必填），把「執行時才檢查」提前到 schema。
7. **統一動詞詞彙**（見 §6），先在描述層對齊，暫不改工具名。

### 🔧 Tier 3 — 統一動詞＋scope 整併（**2026-07-03 已確認方向；分階段、雙軌上線**）

> ⚠️ 這一層會動工具名，屬破壞性，**建議 eval（見 §8）先錄基準、綠燈後再做**，並以「舊工具當 shim 轉呼新工具」雙軌過渡。

**核心設計（採使用者提案）：檔案家族統一成裸動詞工具，`scope` 是每次調用的必填 enum 參數。**

> 原則一句話：**參數形狀由「動詞」決定，權限由「scope」決定。**
> god-tool 的病根是「參數隨 action 變」（模型猜不到哪個 action 配哪些參數）；一動詞一工具後，每顆工具的 schema 固定，`scope` 永遠不改變參數形狀——它是「好的 enum」。
> 加碼理由（§3 原則 6）：裸動詞在模型的 RL 訓練分布內，弱模型（DeepSeek 等）獲益最大。

- **scope enum（必填、無預設）**：`project / personal / legacy / soul`。必填＋無預設＝強迫模型每次顯式選擇範圍；跨 scope 錯誤回可教學訊息（例：「scope=personal 找不到 src/main.py，該路徑存在於 scope=project」→ 引導自我修正）。
- **嚴禁狀態機式範圍切換**（§3 原則 7）：不做「先調工具切 scope、之後動詞作用其上」——隱藏狀態會讓模型忘記所在範圍、發生跨範圍誤寫，事後也無法從單一調用判讀行為。
- **依身分收窄 enum**：Base 角色收到的 schema 中 `scope` 根本沒有 `soul` 這個值（「非法狀態不可表示」），配合 Tier 1 可讀拒絕形成雙層防護；政策層授權對象改為 `(動詞, scope)` 配對。
- **Soul 寫入獨立（已決策：讀統一、寫獨立）**：讀取類動詞（`read/list/find/grep`）可用 `scope=soul`；**寫入保留專屬 `edit_soul`**（維持靈魂保護的儀式感、獨立審計與 HITL 確認）；`config` 寫入同樣走專屬路徑。
- **project_id**：僅 `scope=project` 時有意義；維持 session 自動注入，但在 schema 描述中明文說明。
- 家族之外照原計畫：`sns_control` → `send_message`（丟掉假 enum）；`bash` → `execute_command`＋**非同步審批**（`approval_pending` 讓模型可繼續而非卡死）；演化管線 4 步 → 2 段（狀態機驗序）；`agent_control`／`mission_tools`／`memory_tools` **維持不拆**（動作少、語意清，用 Tier 2 修描述即可）。

---

## 5. 錯誤分類表（Tier 1 核心）

| error_class | 何時 | 回給模型的形狀（範例） |
|---|---|---|
| `validation_error` | 缺必填參／enum 非法／schema 違反 | `{"error_class":"validation_error","message":"'goal' is required for mission initialize","param":"goal","expected":"str"}` |
| `permission_denied` | Gate 2 policy DENY（RBAC／auth_level／capability） | `{"error_class":"permission_denied","message":"role Base cannot use modify_codebase in read-only session","reason":"read_only_session","suggestion":"escalate auth_level or use read_codebase"}` |
| `resource_not_found` | 檔案／agent／task／MCP server 不存在 | `{"error_class":"resource_not_found","message":"file 'src/x.py' not found","resource_type":"file"}` |
| `timeout_exceeded` | 執行／沙盒／MCP 逾時 | `{"error_class":"timeout_exceeded","message":"command exceeded 30s","timeout_ms":30000}` |
| `upstream_error` | MCP／ToukaMemory／外部 API 5xx/網路錯誤 | `{"error_class":"upstream_error","message":"memory server 500","source":"ToukaMemory","retry_after_ms":5000}` |

原則：**英文、可解析、附 `suggestion`**；`message` 不得洩漏設定內部細節（延續 E#21 精神）。

---

## 6. 統一動詞詞彙（已確認：9 動詞完整版）

| 工具 | 參數（固定形狀） | 吸收自 |
|---|---|---|
| `read` | scope, path, view_range? | project_tools.read、text_editor.view、soul 讀 |
| `list` | scope, path?, depth? | 各家 list |
| `find` | scope, pattern | 檔名搜尋（各家 find） |
| `grep` | scope, query, path? | 內容搜尋（各家 grep） |
| `write` | scope, path, content, mode=`overwrite/append/create_only` | write＋append＋create（mode 不改變參數形狀，屬好 enum） |
| `edit` | scope, path, old_str, new_str | str_replace（project/vault/editor 三家統一為 `edit`） |
| `insert` | scope, path, line, text | text_editor.insert |
| `undo` | scope, path | text_editor.undo_edit |
| `delete` | scope, path | 各家 delete |

規則：跨工具**同義即同名**；`str_replace`/`replace` 併為 `edit`；`view` 併入 `read(view_range)`；`append`/`create` 併入 `write(mode)`。Tier 2 階段先在描述層對齊，Tier 3 落地成實際工具名。

---

## 7. 整併對照表（Tier 3，2026-07-03 定案版）

| 現況 | 整併後 | 動機 | 破壞性 |
|---|---|---|---|
| `project_tools`（8 動作）＋`vault_tools`（8）＋`text_editor`（5）＋`update_my_soul` 的檔案類動作 | **9 顆統一動詞工具＋scope enum**（§6） | 消滅檔案家族歧義；動詞回到訓練分布 | 中（雙軌可緩） |
| `update_my_soul` 寫入路徑 | 專屬 **`edit_soul`**（讀取走統一 `read(scope=soul)`） | 已決策「讀統一、寫獨立」：儀式感＋獨立審計＋HITL | 低 |
| `sns_control`（假 enum） | `send_message` | 動詞+名詞 | 低 |
| `bash`（同步 HITL） | `execute_command`（非同步審批） | 不再卡住模型 | 中 |
| 4 步演化管線 | `evolution_define` + `evolution_implement_and_verify`（狀態機驗序） | 強制順序 | 中 |
| `agent_control` / `mission_tools` / `memory_tools` | **維持**（Tier 2 修描述即可） | 動作少、語意清、意圖不同 | — |

工具數學：移除 4 顆巨獸（內藏 ~28 個 action 組合）→ 9 顆裸動詞＋`edit_soul`，廣告總數約 27（安全區內），每顆**憑名字即可選對**。
> `ide_open_diff` / `research` / `ask_user` / `connect_mcp_server` / skill 系列：**維持不變**，只做 Tier 2 描述補強。MCP 工具已有 `server__` 前綴，結構上不會與裸動詞撞名。

---

## 8. 驗收方式：工具調用 eval（見 (b)）

本提案的每一層都應由 **`tests/tool_eval/` 骨架**量化驗收：
`tool_selection_accuracy / wrong_tool_rate / invalid_param_rate / hallucinated_tool_rate / tool_error_rate / policy_denial_accuracy`。
做法：先錄一組基準（改動前），每層改動後重跑、比較數字下降/上升，**用數據而非感覺**決定是否上線。

**DebugAgent 實測計畫（已決策：手動腳本、先「只選不執行」）**：
1. **第一版 select-only**：以 DebugAgent 身分走 ToolLayer→handler 真實呼叫模型，**只收集 tool_use 決策、不執行工具**（零風險碰 `data/`），即可量測選對率／幻覺率／參數合法率。
2. **第二版沙盒執行**：唯讀動詞真執行；破壞性動詞僅在 scratch scope 執行。
3. **RECORD→replay**：實測結果存成錄製檔，自動成為離線回歸案例（套件自我成長）。
4. **節奏**：手動執行的腳本（不進 CI、不自動排程）；Tier 1 動工前先錄基準。
5. **加值**：同一套案例可跨腦比較（Claude vs Gemini vs DeepSeek 逐案例表現），服務多腦切換架構。
6. **多回合模式（2026-07-04 落地）**：`--turns N` 修正單回合計分對多步規劃的低估——
   非目標呼叫後餵入**模擬工具結果**（案例 `simulated_results` 腳本優先、通用後備；
   工具仍**永不執行**），達標／無呼叫／回合上限即停；`metrics.score_episode` 引入
   **探索中性**（read-family 非目標呼叫不記 wrong、但 selection 仍須在回合內達標）
   與 `turns_to_success` 指標；多回合模式強制 samples=1、檔名 `-mtN` 後綴不覆蓋
   單回合快照；新增 `explore-then-write` 離線 replay 案例鎖住語義（9 案例）。
   實測（turns=3）：**deepseek 85.7%（歷史新高；「先 list 再 write」第 2 回合達標
   被正確計分）、grok 100%（avg_turns 1.00＝直行派）**；慢性殘留仍僅
   mission-initialize（ask_user 後放棄）。

---

## 9. 遷移策略（非破壞優先）

- **Tier 1 / Tier 2 可直接做**（加法、改錯誤呈現），風險低。
- **Tier 3 雙軌**：新工具與舊工具並存一段時間；舊工具實作改為 **shim → 轉呼新工具**（零重複邏輯）；模型因描述更清會自然偏好新工具；待 eval 綠燈且無 skill 依賴舊名，再軟下架。
- **每一步都跑** `ruff` + `lint_ssot` + `guard_all`（37）+ `pytest`（2485）+ `vitest`。
- **人類 commit**；**DebugAgent-only**；**不碰 `data/`**。

---

## 10. 風險與注意

- **IO_PRISON**：新工具實作若落在含 `services`/`tools` 字樣的路徑，`lint_ssot` 禁止 `Path.write_text/open` 等——沿用既有工具檔（已豁免或用 dotenv/資源層）模式。
- **多提供商**：schema 品質要對 Claude/Gemini/DeepSeek 都成立；**弱模型最吃 god-tool 的虧**，Tier 3 對它們幫助最大。
- **雙軌期名稱撞名**：新舊工具＋MCP 必須保證唯一（Tier 1 第 6 點）。
- **DebugAgent 例外**：新驗證/錯誤處理不可擋住 Debugger 角色的測試路徑。

---

## 11. 決策紀錄與殘餘開放項

**實作進度（2026-07-04 二次更新——Tier 3 落地、收斂 ADR-0119）**：
- ✅ **Tier 3 已完成並驗證**（TDD 31 紅→綠；`tests/unit/test_file_verbs.py`）：
  ①新模組 `src/tools/file_verbs.py`＝9 顆裸動詞（固定形狀＋top-level required，
  **不再需要 oneOf**）＋scope enum（read-family 含 soul、write-family 靜態排除 soul）；
  引擎零重複（thin adapter over project_tools／vault_tools，沙盒／審計／身分錨定全沿用）；
  `view`→`read(view_range→offset/limit)`、`append/create`→`write(mode)`（create_only 以
  讀探測拒覆寫）、`str_replace`→`edit`；insert/undo 動詞側實作（undo＝行程內快照深度 1、
  零 .bak 污染、錨點隔離）；grep 不廣告引擎做不到的 path。②`edit_soul`（只收寫入動作、
  委派 update_my_soul；read 動作回可讀錯誤指路統一 read）。③`sns_control`→`send_message`。
  ④**雙軌＝shim 可執行、不廣告**（LEGACY_SHIM_SCHEMAS 形狀文件；廣告 21→28）；ToolLayer
  project_tools 條件注入退役。⑤Guest 授予 9 動詞、edit_soul 入 denied；CRITICAL_TOOLS 升
  模組常數納新名。**踩雷**：Guard #32 以 AST 抽 registry「字面鍵」——`**spread` 註冊不可見
  → registry 必須逐鍵字面列出。驗證：ruff/lint_ssot 全清、guard_all 37/37、pytest 2589。
- ✅ **Tier 3 後 eval（第三輪）**：grok-4.3 **100%（選錯 0%）** 穩定；deepseek-v4-flash
  n=1 曾見 66.7%（一次探索性 read 取代 write 的單樣本抖動）→ **n=3 取樣 83.3%（選錯
  5.0%，20 呼叫僅 1 錯）**；兩腦的統一動詞行為全對（`read(scope=soul)`／
  `write(scope=personal, content, mode)`）。**唯一慢性殘留＝mission-initialize**（deepseek
  三輪一貫跑去 ask_user／探索；與檔案動詞無關，屬 mission_tools 意圖訊號問題，續觀察）。
- 📌 **刻意遞延（另案啟動）**：~~`bash`→`execute_command`（非同步審批）~~ ✅ 已於
  2026-07-04 落地為 [ADR-0120](../adr/0120-async-command-approval.md)（approval_pending
  即回＋get_command_result＋審批端點/SSE/前端零改動）；~~演化管線 4→2~~ ✅ 同日落地為
  [ADR-0121](../adr/0121-evolution-pipeline-merge.md)（evolution_define＋
  evolution_implement_and_verify、狀態機驗序、submit_for_deploy 綁定綠色驗證；廣告
  29→27；eval：grok 100%／deepseek 83.3%·n=3 持平最佳水準）。其餘仍遞延：per-identity
  動態 scope 收窄（現無受益角色）、未選專案時動態剝除 project enum 值、多回合 eval
  （select-only 單回合計分會把「先 list 後寫」的合理多步規劃記為選錯——ADR-0120 量測
  時確認的結構限制）。
- ✅ **eval 基準已錄**（select-only、DebugAgent、真實 API）：deepseek-v4-flash 與
  grok-4.3 各一份；Tier 2 改動前的原始快照保存於
  `tests/tool_eval/baselines/*-select_only.pre-tier2.json`（兩腦皆：選對 80%）。
- ✅ **Tier 2 已完成並驗證**（TDD 21 紅→綠；`tests/unit/test_tool_schema_hygiene.py`）：
  ①補 `VAULT_TOOLS_SCHEMA`（vault enum＋soul 唯讀說明＋動詞與 project_tools 同義同名）
  並入 `builtin_tools_schema`（Gate 1 起會廣告；smoke `SCHEMA_EXEMPT` 移除 vault_tools）；
  ②**identity/project_id 採「描述層透明化」而非 schema required**——刻意偏離本文件
  第 2 點字面：executor 注入在 `**args` 之後（模型值永遠被蓋掉），宣告 required 只會
  誘導模型編造身分值；與 §4 Tier 3 對 project_id 的處理（「維持自動注入、描述明文說明」）
  一致；③重疊工具描述補「何時用／何時不要用」（project/vault/soul/research/bash 互相
  指路）；④RBAC 標記回歸鎖（create_* 已有「限 Owner/admin」）；⑤mission_tools 壓平：
  schema 廣告 `task_id`/`dependency_task_ids`（items 加 required），橋接層
  `_normalize_task_keys` 雙收正規化（引擎鍵 id/dependencies 不動、舊鍵向後相容）；
  schema 只留 `artifact_refs`（runtime 仍收 artifact_ref）；⑥條件必填 oneOf
  （action 判別式；檔案動詞家族共用 `FILE_ACTION_CONDITIONALS` SSOT）；
  ⑦**Gemini 出場剝除 oneOf/anyOf/allOf/const**（其 OpenAPI 子集不支援、不剝會 400；
  Anthropic/OpenAI 相容供應商原樣收到）。驗證：ruff/lint_ssot 全清、guard_all 37/37、
  pytest 2557/4 skipped。
- ✅ **Tier 2 後重測（同兩腦）**：eval 案例同步演化——`expected_tools` 語義改
  **ANY-OF**（合法替代路徑；soul 讀統一後 update_my_soul 與 vault_tools(soul) 皆合法）、
  `notes.txt` 案例消歧義（vault 上架後「無脈絡檔名」對人類也是真歧義→明示 in the
  current project）、新增 `write-personal-note` 反向邊界案例（8 案例）。結果：
  **grok-4.3 選對 80%→100%（選錯 20%→0%）**；**deepseek-v4-flash 80%→83.3%
  （選錯 16.7%→14.3%）**，且兩腦 write 都補上了 content（**oneOf 條件必填被遵循**，
  invalid_param 0%）、邊界雙向全對。deepseek 唯一殘留失敗＝mission-initialize
  （稀疏 prompt 下改前也錯：ask_user）——弱模型固有困難，留給 Tier 3。
- 📌 舊快照（未演化前案例）上兩腦曾同降至 60%：歸因拆解＝Grok 用了**設計上合法**的
  vault_tools(soul) 讀取被舊案例懲罰＋notes.txt 真歧義，非 schema 品質退步；
  原始數據保留於 `.pre-tier2.json` 供查核。

**實作進度（2026-07-03）**：
- ✅ **Tier 1 已完成並驗證**（TDD red→green）：`tool_errors.py`（結構化錯誤 SSOT，5+1 類）；
  executor 全錯誤路徑結構化（政策拒絕/未知工具/身分缺失/參數缺失/例外映射，
  「找不到」與「被拒」明確分流）；ToolLayer 記錄 `denied_tools` ＋ 注入
  `<tool_availability>` 可讀說明（採策略 (b)；因 pipeline 順序 ContextLayer 先於
  ToolLayer，說明由 ToolLayer 自行注入）；幻覺偵測器涵蓋 MCP 工具；policy 未載入
  fallback CONFIRM→**DENY**（fail-safe）；`_inject_tools` 撞名警告。
  驗證：ruff/lint_ssot 全綠、pytest 2513/2513（Docker 健康測試除外——容器未啟動的
  環境性失敗）、guard_all 37/37。
- ✅ **eval select-only LIVE runner 已就緒**（`tests/tool_eval/live_select_only.py` ＋
  `scripts/record_tool_eval_baseline.py`；dry-run 驗證通過：DebugAgent:Owner 廣告
  20 工具、Base 0 工具）。⚠️ **基準尚未錄**：`.env` 目前只有佔位金鑰（`sk-new`），
  待放入真實金鑰後跑 `python scripts/record_tool_eval_baseline.py --model <id>`。
  Tier 1 不動 schema，此刻錄的基準對 Tier 2/3 仍有效。
- 📌 附帶發現（未修，另行處理）：pipeline 中 ContextLayer 先於 ToolLayer 執行，
  故 ToolLayer 設定的 `mission_awareness` 疑似從未被 ContextLayer 渲染（潛在既有
  bug，與本提案無關）。

**已決策（2026-07-03 與使用者確認）**：
1. ✅ **Tier 3 採「統一動詞＋scope」**（使用者提案，取代本提案初稿的 `read_codebase`/`read_vault` 命名法）——9 動詞完整版（含 `insert`/`undo`；`append` 併入 `write(mode)`）。
2. ✅ **soul/config 讀統一、寫獨立**：讀取類動詞開放 `scope=soul`；寫入保留專屬 `edit_soul`（儀式感＋審計＋HITL）。
3. ✅ **eval 接 DebugAgent 實測**：手動腳本、第一版「只選不執行」（見 §8）。

**殘餘開放項（採預設，除非使用者反對）**：
1. Tier 1 的 Gate 1 策略：預設採 **(b)**——對「存在但被拒」的工具，在 context 給模型一句可讀說明（見 §4 Tier 1 第 2 點）。
2. Tier 3 過渡方式：預設採**舊工具 shim 雙軌**（舊名轉呼新工具，eval 綠燈後軟下架），非一次切換。

> 進度：eval 錄基準 ✅ → Tier 1 ✅ → Tier 2 ✅ → Tier 3 雙軌 ✅（[ADR-0119](../adr/0119-unified-tool-interface-redesign.md)）→ bash 非同步審批 ✅（[ADR-0120](../adr/0120-async-command-approval.md)）→ 演化管線 4→2 ✅（[ADR-0121](../adr/0121-evolution-pipeline-merge.md)）→ 多回合 eval ✅（2026-07-04，§8）。**本提案全部落地。** 剩餘＝舊 shim 軟下架時機（另案）。

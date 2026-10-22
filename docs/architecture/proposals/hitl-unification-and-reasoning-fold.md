# HITL 全面修復 ＋ 思考摺疊（HITL Unification & Reasoning Fold）

> **狀態**：**提案全數收官 🏁**——H 系列（H1–H4）2026-07-11（[ADR-0143](../adr/0143-hitl-unification.md)）；T 系列（T1–T3 思考分頻＋摺疊）2026-07-12（[ADR-0152](../adr/0152-reasoning-channel-and-fold.md)；T1-capture 實測定格式＝grok-4.5 走獨立 reasoning_content 欄位、Live E2E SPLIT OK）。
> **來源**：靈魂 Sumi（grok-4.5）上出現的三個症狀與診斷（2026-07-11；診斷回報後經維護者核准才開始修復）。
> **範圍**：H 系列＝HITL 四級權限對終端/原生/skill/MCP 全家族真實生效；T 系列＝思考內容分頻並在前端摺疊顯示。
> **驗收終態（H4）**：後端 pytest 3332＋／前端 vitest 996／guard_all 41/41；Manual 全鏈路可用（審批卡在對話裡）、agent 不可自批、headless 設定時定權。批次細節見 ADR-0143 各節。

## 1. 背景（診斷結論，全部已核實）

使用者在 UI 把 HITL 選成 Manual，靈魂 Sumi（Owner、grok-4.5、enabled_tools ~35 項）調用工具時**完全未經詢問**：非終端工具直接執行、終端工具直接失敗；且模型思考過程直接顯示在對話裡。三個獨立缺陷：

1. **CONFIRM fail-open（主因）**：`tool_executor.py:250-251` — Manual 下 `safety.py:78-80` 對一切回 `CONFIRM`，但參數無 `command` 的工具僅 log「suggested CONFIRM but executed anyway」後照常執行。全系統只有 `bash/execute_command` 真的走審批；memory、檔案動詞、send_message、dispatch_subagent、skill/MCP 家族在 Manual 下零審批。
2. **終端審批無處投遞**：審批通道為 `/terminal/stream` SSE（等「ToukaUI」訂閱者），ToukaChat 從未訂閱、無 `bash_approval_request` handler（`connections.ts:434` 白名單僅 skill_install/ask_user）→ `_is_touka_ui_available()` 恆 False → `terminal.py:513-524` 自動 `permission_denied/ui_unavailable`，全程不問人。
3. **auth_level 從未持久化**：前端把 `auth_level` 放進 session JSON 送 `POST /v1/ui/sessions/{id}`，但 `ui_sessions.py` `SessionModel` 無此欄位＝靜默丟棄；DB `user_sessions.auth_level` 永遠停在預設 `'Manual'`（`async_db.py` upsert 本就支援該參數、只是路由沒傳）；工具閘門讀 DB（`tool.py:65-74`）→ AuthSwitcher 近乎裝飾品。`permissions.json` 的 `default_auth_level` 為死設定（後端執行期零讀取），但 ToukaChat RbacMatrix 設定頁有一顆編輯它的無作用旋鈕。

思考漏出（T 系列動機）：grok-4.5 推理預設 high 且**不可關閉**；Touka openai 相容路徑（`openai_handler.py:181` 起）只讀 `message.content`（逐字透傳）＋`message.tool_calls`，全路徑零 reasoning 欄位處理——`reasoning_content` 被丟棄、`content` 內思考零剝離；前端 `MessageItem.tsx:214` 純 ReactMarkdown 直印。claude_handler 則主動把 thinking 包 `<thinking>` 塞進 content（`claude_handler.py:219-222`）。

引擎本身（`safety.py` 四級決策矩陣＋黑名單先行＋ADR-0089 唯讀豁免）**語意正確、予以保留**；本案修的是其輸入（分類、持久化）與輸出（CONFIRM 的處置、審批通道）。

## 2. 目標

1. HITL 全面生效：終端指令、Touka 原生工具、skill、MCP 都依四級 HITL 權限設定運作。
2. 思考內容保留可見，但預設摺疊在思考區塊，使用者需要時再展開。

## 3. 決策表（SSOT）

工具風險分類（`auth_policies.json` 新結構 `tool_risk`）：

| 風險類 | 定義 | 例子 |
|---|---|---|
| `read_only` | 零副作用 | research、read、list、find、grep、memory search/query、get_command_result |
| `safe` | 有副作用但低風險、範圍內 | write/edit/insert/undo（scope 內）、memory save、vault/project 寫入 |
| `dangerous` | 高風險/外向/花錢/不可逆 | execute_command（非白名單指令）、delete、edit_soul、send_message、dispatch_subagent(_group)、skill/MCP/evolution 家族、agent_control、sns_control |

等級 × 分類（黑名單恆 DENY 凌駕一切）：

| | read_only | safe | dangerous |
|---|---|---|---|
| **ReadOnly** | ALLOW | DENY | DENY |
| **Manual** | ALLOW | CONFIRM | CONFIRM |
| **SafeAuto** | ALLOW | ALLOW | CONFIRM |
| **FullYOLO** | ALLOW | ALLOW | ALLOW |

- 終端指令沿用 regex 白/黑名單：白名單命中＝`safe` 類、黑名單＝DENY、其餘＝`dangerous`。**刻意變更**：ReadOnly 下白名單指令由舊 ALLOW 改為 DENY（`pytest`/`npm test` 會執行程式碼，稱不上唯讀；測試對釘）。
- 聚合多動詞工具（memory_tools、vault_tools、project_tools、mission_tools、agent_control…）支援按 action 細分（如 memory search=read_only、save=safe）。**廣告時（schema_injection）取其最低風險類**（模型能看到、能用其唯讀動詞）；**執行時取實際 action 的類**，action 缺失/未知→取該工具最高風險類（fail-safe）。
- MCP 與 runtime 未知工具：預設 `dangerous`（fail-safe）。
- 原生工具分類 parity 棘輪：每個廣告中的原生工具必須有分類，新工具未分類＝CI 紅（Guard #32 擴充＋單元測試）。
- `read_only_tools` 欄位（ADR-0089）由 `tool_risk` 取代（語意保留：read_only 類＝全等級 ALLOW 免 HITL）；Gate-1 廣告過濾與 Gate-2 執行共用同一 `evaluate()`，故 ReadOnly 下不可用工具自動不廣告。

## 4. 已核准決策

1. **風險分類初表**：照上表三類（實作附完整逐工具清單）。（依建議）
2. **SafeAuto 自動放行集**：保守版——scope 內檔案寫入＋memory save 自動；send_message/dispatch/edit_soul 一律審批。（依建議）
3. **無人 UI 的 session（heartbeat/派工）**：（維護者修改後的版本）**在設定時就事先決定 auth_level，整個 session 照該權限跑**。CONFIRM 於無人情境的解算仍為 fail-closed 拒絕——事先選 FullYOLO＝全自動、Manual＝實質唯讀，決定權在設定當下的人。
4. **fail-closed 在 H1 就翻**：H2/H3 落地前 Manual 會嚴格（非唯讀全拒）；持久化同批修好，過渡期可切 SafeAuto。（依建議）
5. **`default_auth_level` 拆除**：後端模型/config/.example＋ToukaChat RbacMatrix 無作用旋鈕/types/i18n/測試一併拆（比接線乾淨，少一句謊）。（依建議）

## 5. 批次計畫

- **H1 — 語意層**：`tool_risk` 分類 SSOT＋`safety.py` 按分類求值＋`tool_executor.py` CONFIRM fail-closed（結構化 `permission_denied`、防退化錨「未獲決定前工具函式零呼叫」）＋`SessionModel` 補 `auth_level` 持久化＋heartbeat/派工 auth_level 事先決定機制＋拆 `default_auth_level`＋Guard #32 擴充＋全矩陣測試。
- **H2 — 統一審批 broker（後端）**：泛化 `request_approval`（asyncio.Event＋逾時自動拒）為 approval_service；審批請求走聊天 SSE（`tool_approval_request` 事件，skill_install 前例）；通用 `POST /v1/tools/approve|reject/{exec_id}`（人類＋session 所有權閘）；四家族 CONFIRM 收斂同一 broker；`/terminal/approve` 留相容別名。
- **H3 — ToukaChat 審批 UI**：`connections.ts` 白名單＋內嵌審批卡＋ToolStepTracker pending 態＋並行多筆＋AuthSwitcher 端到端＋useEvolution 遷移＋i18n。
- **H4 — HITL 收尾文件**：`<validation_protocol>` Manual 句對齊新現實、四級 desc 文案、SECURITY.md、ADR（取下一號）、NOQA/HANDOVER。
- **T1 — 思考分頻（後端）**：`split_reasoning`（`<think>`/`<thinking>` 剝離＋`reasoning_content` 欄位讀取）→ SSE `delta.reasoning_content`（additive）；claude_handler thinking 改走同頻道；dispatcher 可恢復緩衝保留；先抓一次 grok-4.5 真實封包定格式。
- **T2 — 前端摺疊 UI**：累積 `reasoning_content` → `msg.thinking`（隨 session 持久化）；MessageItem 摺疊區塊（預設收合、點開展開、串流中「思考中…」）；i18n；vitest。
- **T3 — 思考收尾文件**：ADR（取下一號）＋HANDOVER。

每批綠燈（pytest＋vitest＋guard_all）後由維護者 commit。

## 6. 範圍外

- 「不要用工具」自然語言硬開關（per-conversation 工具總閘）。
- `enabled_tools` 空清單 falsy 陷阱（`tool.py:199`）——行為翻轉需遷移決策，另案。
- 每靈魂 `reasoning_effort`、Gemini `includeThoughts`。
- 模型類型→角色硬綁定（維護者暫緩）。

## 7. 驗收

- 四級 × 三類 × 四家族（原生/memory/MCP/終端）矩陣測試全綠；CONFIRM 防退化錨。
- AuthSwitcher 切換 → 持久化 → 下一次工具調用行為改變（端到端）。
- heartbeat/派工 session 依設定時 auth_level 運作。
- Manual 下每個 safe/dangerous 工具調用出審批卡，批准執行、拒絕/逾時結構化拒絕（H2/H3）。
- 思考預設摺疊、點開可見、重開對話仍在（T2）。
- guard_all 41 道全綠、openapi/api.ts 同步、i18n key-sync、NOQA 台帳一致。

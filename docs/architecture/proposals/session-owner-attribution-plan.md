# Session 明確人類歸屬（owner_user_id）實作計畫

> **狀態：✅ 已實作（2026-07-05）——收斂為 [ADR-0132](../adr/0132-session-owner-attribution.md)。** 維護者決定：Q1 聯集／Q2 手動 backfill／Q3 不收緊（皆照 §8 推薦）。本文保留為計畫原文供查核。
> 比照 [ADR-0107](../adr/0107-user-self-service-data-clearing.md) 對 projects 的做法（`owner_user_id` 欄位＋明確擁有權優先＋legacy 反推 fallback），為 [ADR-0131](../adr/0131-auth-renewal-and-ldh-containment.md) 明列的「授權地基長期正解」。

---

## 1. 動機與現況缺口

**資料模型事實**（ADR-0107 已確立）：Touka 的對話以 `agent_id` 為鍵，人類經 `User.owned_agents` 間接擁有 agents。但 UI 對話常見的是**裸模型對話**（`agent_id` = `"gemini-2.5-flash"` 等模型 id）——模型 id 不是任何人的 agent，導致：

1. **normal 角色使用者看不到自己的對話**：`_can_access_session` 走 `is_owner`（[resource_manager.py:306-314](../../../ToukaProxy/src/services/resource_manager.py)）→ 模型 id ∉ `owned_agents` → `list_sessions` 過濾掉、`save_session` 綁 agent_id 時 403。ADR-0131 批1③ 以「升 admin」繞過（單人系統務實解），但**多用戶下無隔離可言**：要嘛看不到自己的（normal）、要嘛看到全部人的（admin）。
2. **`agent_id` 語意被迫兼職「歸屬」**：session 屬於哪個 agent（顯示分組用）與 session 屬於哪個人（授權用）是兩件事，現在擠在同一欄。
3. **附帶既有 bug——human id 污染 `agent_id`**：`_upsert_session_unlocked` 的 `identity` 參數語意在兩類呼叫面不一致（見 §3 盤點）：route／內部服務傳「session 所屬 agent」，唯獨 [`dispatcher._persist_assistant_turn`](../../../ToukaProxy/src/services/dispatcher.py) 傳「請求呼叫者」——UI 聊天下呼叫者是 human，`final_agent_id = agent_id or existing` 會把 **human UUID 寫進 `agent_id`**（`data/custom/<uuid>/` 目錄即此污染的實證；靠「前端 onDone 後寫富版」蓋回才不常顯形）。

## 2. 目標與非目標

**目標**
- `user_sessions` 增 `owner_user_id`：human 建立的 session 記錄明確人類擁有者。
- 授權：owner 本人（不論角色）可見/可管自己的 session——normal 使用者的裸模型對話自此可見、可持久化；多用戶互相隔離；admin/Debugger 全域不變。
- 附帶根治 `agent_id` 的 human-UUID 污染（`identity` 參數語意收斂）。

**非目標（明確不做）**
- 不動記憶/產出/feedback 的 agent-keyed 模型（維持 ADR-0107 認定）。
- 不做帳號刪除連動、不擴 ADR-0110 GDPR 匯出範圍（現行按 `owned_agents`；owner-scoped sessions 匯出列為後續選項）。
- 不動無痕（incognito 永不落盤，無 owner 可言）。
- `SessionModel`（request body）**不加欄位**——owner 一律 server-side attribution，client 不可指定（防偽造）。

## 3. 寫入面盤點（`upsert_session` 全呼叫面）

所有寫入 funnel 至 `_upsert_session_unlocked`（含 `append_message_to_session` 委派）——attribution 可**單點收口**。

| 呼叫面 | `identity` 現傳 | 語意 | 計畫動作 |
|---|---|---|---|
| [`ui_sessions.save_session`](../../../ToukaProxy/src/routers/ui_sessions.py) | `session.agent_id`（body） | 目標 agent | 不變；**另傳 `owner_user_id`**（`caller.is_human` 時 = caller.agent_id） |
| [`dispatcher._persist_assistant_turn`](../../../ToukaProxy/src/services/dispatcher.py) | **請求呼叫者**（human/agent） | ⚠️ 混用 | **修正**：human 呼叫者→`identity=None`（不再污染 agent_id）＋傳 `owner_user_id`；agent 呼叫者→維持現行 |
| `routers/heartbeat.py`（5 upsert＋2 append） | agent 身分 | 所屬 agent | 不變（owner 不傳＝不觸碰） |
| `services/sliding_window_service.py` | agent 身分 | 所屬 agent | 不變 |
| `services/sns_gateway.py` | agent 身分 | 所屬 agent | 不變 |
| `tools/builtin/system.py`（2） | 執行 agent | 所屬 agent | 不變 |
| `async_db.py` internal wrappers | 透傳 | — | 簽名透傳 `owner_user_id` |

## 4. 核心設計

### D1 — Schema 與遷移（沿用既有 pattern）
`user_sessions` 增 `owner_user_id TEXT`：CREATE TABLE 定義＋`PRAGMA table_info` → `ALTER TABLE ADD COLUMN` 冪等遷移（[async_db.py:727-759](../../../ToukaProxy/src/utils/async_db.py) 既有慣例，與 projects 完全同構）。既有 rows 為 NULL＝legacy，行為不變。

### D2 — Attribution 單點收口（`_upsert_session_unlocked`）
新增 kwarg `owner_user_id: str | None = None`，語意：
- **INSERT（新 session）**：寫入傳入值（None 亦寫 NULL）。
- **UPDATE（既有）**：**永不改**——`owner_user_id` 不進 `ON CONFLICT UPDATE` set（比照 ADR-0107「preserved, not in the UPDATE set」，杜絕後寫者蓋走歸屬）。
- `identity` 參數語意**固定為「session 所屬 agent」**；docstring 明文化，並修正唯一違例（D3）。

### D3 — 根治 agent_id 污染（`_persist_assistant_turn`）
```
identity.is_human → upsert(identity=None, owner_user_id=identity.agent_id)   # human 是 owner，不是 agent
非 human（agent）→ upsert(identity=identity)                                  # 現行為，零變
```
效果：human UI 聊天的伺服器端持久化不再把 human UUID／（ADR-0131 前的）DebugAgent 寫進 `agent_id`；`final_agent_id = None or existing` = 保留前端所設模型/agent id。

### D4 — 讀取授權：owner 通道採「聯集」（additive，零縮窄）
[`_can_access_session`](../../../ToukaProxy/src/routers/ui_sessions.py) 改為三段：
```
1. admin/Debugger → True                                   （不變）
2. session.owner_user_id 存在且 caller.is_human 且相等 → True （新通道）
3. 其餘 → 現行邏輯原封不動（agent_id None→False；is_owner agent-derived）
```
- 刻意選**聯集**而非 ADR-0107 的排他式 owner 優先：不縮窄任何既有權限（heartbeat/agent 對自身 session 的既有通道、`owned_agents` 共享場景全不受影響），既有 DA3 IDOR 測試**全數照舊全綠**＝遷移零回歸風險。隔離目標仍達成：他人 normal 既非 owner 也非 agent-owner → 不可見。
- 述詞簽名需從 `(caller, session_agent_id)` 擴為可見 owner——**改收整個 session dict**（或加參數），呼叫面 8 處同步（`list_sessions`／`get_session`／`save_session`×2／`stop`／`stream`／`streams/active`／`delete`；`_can_access_project` 的 session-derived fallback 傳 agent_id 處維持舊介面或包薄轉接）。

### D5 — 指派檢查放行「非 agent 的模型 id」（normal 可持久化的最後一關）
`save_session` 的第二檢查（`new_agent_id` 須 `_can_access_session` 通過）語意是「不得把 session 掛到別人的 agent 名下」——但裸模型 id 不是 agent，normal 使用者標 `"gemini-2.5-flash"` 仍 403。修正：
```
new_agent_id ∈ agent 名冊（agent_service.get_all_agents 的 id 集）→ 維持現行 is_owner 防護
new_agent_id ∉ 名冊（裸模型 id）→ 視為「模型標籤」放行
```
- **已知取捨**：惡意者可預先把 session 標成「未來才會建立的 agent 名」搶位（該 agent 建立後 session 顯示在其分組）。緩解為選配 hardening：僅放行 `model_manager` 註冊表已知的模型 id（fail-closed）；預設先不做（單人/小群體部署、攻擊收益趨近零，避免動態模型清單抖動誤傷正常儲存）。**待擁有者裁決**（§8-Q3）。

### D6 — 出口消毒
`list_sessions`／`get_session` 回傳前 `pop("owner_user_id")`（比照 projects 的 internal-authz-field 慣例，[ui_sessions.py:457](../../../ToukaProxy/src/routers/ui_sessions.py)）。回應皆為 `dict`（無 Pydantic response model）→ **openapi.json 預期零 diff、`api.ts` 免重生**（實作時仍以 `test_api_schema_sync` 驗證）。

## 5. 分階段實作（TDD，每階段先紅後綠）

| Phase | 內容 | 主要測試（RED 先行） |
|---|---|---|
| **0** | RED 測試全套落位 | 見 §6 清單 |
| **1** | D1 schema＋遷移；D2 `_upsert_session_unlocked` 介面（kwarg＋INSERT-only） | 遷移冪等；INSERT 記 owner；UPDATE 不改 owner |
| **2** | D3 呼叫面接線（route 傳 owner；dispatcher 語意修正；wrappers 透傳） | human 建立→owner 記錄；agent upsert→不觸碰；**human 持久化不再污染 agent_id（回歸鎖）** |
| **3** | D4 `_can_access_session` owner 聯集＋呼叫面簽名；D5 指派檢查放行模型 id | normal owner 可見/可寫自己的裸模型 session；他人 normal 不可見；admin/Debugger 不變；NULL legacy 行為逐位不變（既有 DA3 測試零改動全綠）；真 agent id 指派仍受 is_owner 防護 |
| **4** | D6 出口消毒；全量驗證；文件（§7） | list/get 不洩 `owner_user_id`；openapi 零 diff |
| **5**（選配） | 一次性 backfill 運維腳本 `scripts/backfill_session_owner.py`（NULL→指定 user id；dry-run 預設、`--apply` 落盤、僅 Owner 手動執行；**不進 runtime、不進 guard_all**） | 腳本單元測試（dry-run 不寫、apply 冪等） |

依賴序：1→2→3→4 嚴格串行；5 獨立。預估 diff 面：後端 4 檔（async_db／ui_sessions／dispatcher＋測試）＋文件；前端**零改動**。

## 6. TDD 測試清單（新檔 `tests/unit/test_session_owner_attribution.py`＋擴充既有）

1. 遷移：新欄位存在、重複 init 冪等（比照 projects 遷移測試）。
2. attribution：human 呼叫 `save_session` 建立 → `owner_user_id` = user id；agent 身分 upsert 既有 session → owner 不變；human 再更新 → owner 不變（UPDATE 不含）。
3. **污染回歸鎖**：`_persist_assistant_turn`（human identity）持久化後 `agent_id` 維持原模型 id、不為 human UUID。
4. 授權矩陣（參數化）：owner-normal 可見可寫｜非 owner-normal 不可見（list 過濾＋get 403＋save 403）｜admin/Debugger 全通｜NULL-owner legacy 全部維持現行（含 agent-derived is_owner 通道）。
5. 指派：normal 標裸模型 id 放行並落盤；normal 指派他人真 agent id 仍 403。
6. 出口：list/get 回應無 `owner_user_id` 鍵。
7. 整合冒煙：normal 使用者「建立→聊天→重整→list 仍可見」端到端（正是 ADR-0131 調查中 `ui_sessions.db` 長期空白的反面驗證）。

## 7. 文件義務（任務後協議 c）

- 新 ADR（配下一個連續號）建檔＋`adr/README.md` 索引；修訂 ADR-0107（Related 註記 sessions 同構延伸）與 ADR-0131（「另案」項回填連結）。
- `PROXY_INTERFACE.md` 認證/會話章節：owner 通道語意。
- `SSOT_MASTER_MAP` §資料層：`owner_user_id` 歸屬欄位登記。
- `tests/README.md` 新測試檔；`HANDOVER.md` 📌 條目＋現況數字。
- `openapi.json`：預期零 diff（D6）；若 Phase 4 驗出 diff 則照規重生＋`api.ts` 同步。
- SECURITY.md：**不進** Known Resolved（此為架構加固非弱點修復）；於新 ADR 記錄授權模型變更。

## 8. 待維護者決定（審核時請一併回覆）

- **Q1 owner 通道語意**：聯集（推薦，零回歸）vs 排他 owner 優先（ADR-0107 同構、隔離更嚴，但需重驗 heartbeat/共享 agent 場景與既有 DA3 測試面）？
- **Q2 legacy NULL 的處置**：僅靠 Phase 5 手動 backfill（推薦，運維動作可控）vs runtime「首個 human 更新者自動認領」（NULL→值單向 claim；現行 NULL session 僅 admin/Debugger 可寫，claim 實質只會發生在 admin 身上，收益低且把公共 session 綁死，**不推薦**）？
- **Q3 D5 hardening**：模型 id 放行是否收緊為「僅 registry 已知模型」（fail-closed；代價是動態模型清單抖動可能誤傷正常儲存）？預設不收緊。

## 9. 風險與回滾

- **風險最小化設計**：聯集通道＋UPDATE 不觸碰＋NULL legacy 原行為 → 對既有資料與單人 admin 部署**行為零變**；新行為只對「有 owner 的新 session × normal 呼叫者」生效。
- **回滾**：D4/D5 為純述詞邏輯（revert 即回）；D1 欄位留存無害（SQLite 加欄不可逆但 NULL 欄零影響）；D3 若出意外（某路徑依賴 human-id-as-agent_id 的隱性行為），revert dispatcher 兩行即回現狀——Phase 2 的污染回歸鎖會在實作期先揪出此類依賴。
- **明確不影響**：無痕（不落盤）、heartbeat/自主脈衝（agent 身分路徑零變）、ADR-0113 per-agent reset（agent-keyed 清理不看 owner）、串流緩衝/續傳（授權仍走 `_can_access_session`，自動繼承新通道）。

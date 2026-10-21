# ADR-0132: Session 明確人類歸屬（owner_user_id）——授權地基補全（Session Owner Attribution）

## 狀態

> ADR-0276 修訂對話寫入邊界：`owner_user_id` 仍只在 INSERT 記錄；人類身分透傳供交易內授權與 CAS，不能代替執行靈魂。換綁只限授權人類 UI 專用入口，非空換綁撤銷建立標記。

✅ **已接受（實作與驗證 2026-07-05；擁有者核准計畫後動工——Q1 聯集／Q2 手動 backfill／Q3 不收緊皆照推薦）**。實作 [ADR-0131](0131-auth-renewal-and-ldh-containment.md) 明列的「授權地基長期正解」；計畫全文見 [proposals/session-owner-attribution-plan.md](../proposals/session-owner-attribution-plan.md)。比照 [ADR-0107](0107-user-self-service-data-clearing.md) 對 projects 的做法。

## 日期

2026-07-05

## 背景

對話以 `agent_id` 為鍵、人類經 `owned_agents` 間接擁有 agents（ADR-0107 確立），但 UI 常見的**裸模型對話**（`agent_id="gemini-2.5-flash"`）不屬於任何人的 agent：

1. normal 角色看不到/存不了自己的對話（`is_owner` 對模型 id 恆 False → list 過濾光、save 403 且被 syncSession 靜默吞）——ADR-0131 以「升 admin」暫解，多用戶下無隔離。
2. `agent_id` 被迫兼職「歸屬」語意。
3. **附帶既有 bug**：`_persist_assistant_turn` 把「請求呼叫者」當 `upsert_session(identity=…)` 傳（8 個寫入面唯一違例）——human 呼叫時 `final_agent_id = agent_id or existing` 把 **human UUID 污染進 `agent_id`**（`data/custom/<uuid>/` 目錄為實證）。

## 決策

1. **Schema（D1）**：`user_sessions` 增 `owner_user_id TEXT`；CREATE＋`PRAGMA table_info`→`ALTER TABLE` 冪等遷移（與 projects 完全同構）。
2. **Attribution 單點收口（D2）**：[`_upsert_session_unlocked`](../../../ToukaProxy/src/utils/async_db.py) 增 kwarg `owner_user_id`——**僅 INSERT 寫入、刻意不進 `ON CONFLICT UPDATE` set**（後寫者永不得改動歸屬；Q2 裁決＝不做 runtime claim）。`identity` 參數語意明文固定為「session 所屬 agent」。
3. **污染根治（D3）**：[`_persist_assistant_turn`](../../../ToukaProxy/src/services/dispatcher.py)——human 呼叫者 → `identity=None`（agent_id 保留既有）＋`owner_user_id=呼叫者 id`；agent 呼叫者（heartbeat 型）維持現行為。[`save_session`](../../../ToukaProxy/src/routers/ui_sessions.py) 於 `identity.is_human` 時傳 owner（server-side attribution，`SessionModel` **不加欄位**、client 不可指定）。
4. **授權聯集通道（D4，Q1 裁決）**：`_can_access_session(caller, agent_id, owner_user_id=None)` 三段——admin/Debugger 不變 → **owner 本人（human）放行（新）** → 其餘走既有 agent-derived `is_owner`。**聯集 additive、零縮窄**：既有 DA3 IDOR 測試零改動全綠；NULL owner＝legacy 行為逐位不變。8 個 route 呼叫面傳入 owner。
5. **指派檢查放行裸模型 id（D5，Q3 裁決＝不收緊）**：`save_session` 的「不得掛到別人 agent 名下」防護改為**僅對真 agent 執行**（新 helper `_is_registered_agent`，名冊＝`agent_service.get_all_agents()`；**名冊不可用 fail-closed 當作 agent**）；裸模型 id 為顯示標籤、一律放行。已知取捨（低風險，接受）：可預標「未來 agent 名」搶位；hardening（僅 registry 已知模型）列為選配未做。
6. **出口消毒（D6）**：`list_sessions`／`get_session` 回傳前 `pop("owner_user_id")`（internal authz field，比照 projects）——前端合約零變、`openapi.json` 零 diff、ToukaChat 零改動。
7. **Backfill 運維腳本（Phase 5，Q2 裁決）**：[`scripts/backfill_session_owner.py`](../../../scripts/backfill_session_owner.py)——**預設 dry-run**、`--apply` 才落盤、只填 NULL（絕不覆蓋既有歸屬）、重跑冪等；僅 Owner 手動執行、不進 runtime/guard_all。單人部署以此把 legacy NULL rows 一次性歸屬主帳號。

## 後果

- **normal 使用者的裸模型對話自此可見、可持久化**；多用戶互相隔離（他人既非 owner 也非 agent-owner → 不可見）；admin/Debugger 全域不變。
- **agent_id 不再被 human UUID 污染**（回歸鎖：`test_persist_assistant_turn_human_does_not_pollute_agent_id`）。
- **零破壞遷移**：聯集通道＋UPDATE 不觸碰＋NULL legacy 原行為 → 對既有資料與單人 admin 部署行為零變；heartbeat／sliding window／SNS／工具等 agent 身分寫入路徑零改動。
- **假設/邊界**：owner 通道要求 `caller.is_human`（agent 不得冒用 human id 走此通道）；`_persist_assistant_turn` 對既有 session 恆為 UPDATE → owner 實際由 `save_session` 建立時記錄（dispatcher 傳 owner 為 no-op 防禦性完整）；無痕不落盤、與本 ADR 無交集。
- **驗收**：新測 [`test_session_owner_attribution.py`](../../../tests/unit/test_session_owner_attribution.py) **12**（遷移冪等／INSERT-only／不 claim／污染鎖×2／聯集矩陣／名冊 fail-closed／route 放行＋維持防護／出口消毒／backfill dry-run+冪等）；後端 pytest **2729 passed/4 skipped**（+12）；guard_all **38/38**；ruff＋lint_ssot 清；NOQA `LOCAL_IMPORT_BAN` 14→16（腳本 script-only 延遲載入）；openapi 零 diff（`test_api_schema_sync` 綠）；前端零改動（vitest 971 基準不變）。
- **剩餘（另案）**：GDPR 匯出（ADR-0110）擴及 owner-scoped sessions；自助清空（ADR-0107）連動 owner sessions；D5 hardening（僅 registry 已知模型）若多用戶公開部署再啟。

## 相關

- [ADR-0107](0107-user-self-service-data-clearing.md)：projects `owner_user_id` 先例（本 ADR 為 sessions 同構延伸）
- [ADR-0131](0131-auth-renewal-and-ldh-containment.md)：認證挾持修復（本 ADR 補其「授權地基」另案）
- [ADR-0081](0081-resumable-stream-cursor-buffer.md)：`_persist_assistant_turn` 伺服器端持久化（本 ADR 修正其 identity 語意）
- 計畫：[proposals/session-owner-attribution-plan.md](../proposals/session-owner-attribution-plan.md)

# ADR-0084: AI 回覆細粒度回饋收集（讚/倒讚＋原因，隔離儲存）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-13
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

公開發佈前的 Microsoft HAX（Guidelines for Human-AI Interaction）走查列出三個 ❌ 缺口，
其中 **G15（鼓勵細粒度回饋）** 是唯一需後端者：使用者對 AI 回覆不滿時只能「重新產生」，
這些品質訊號完全沒被收集。本 ADR 為每則 AI 回覆加上**讚/倒讚＋倒讚選填原因**，原因欄同時
兼作 **G6（回報有害/偏見內容）** 管道，並提供**擁有者檢視頁**讓收集到的回饋看得見——否則對
非技術擁有者而言，資料只進 SQLite 等於沒收。屬 HAX 修復第二批（H2）。

## 決策方案 (Decision)

### 核心安全邊界（不可違反）
回饋存入一個**全新、完全隔離的 SQLite（`feedback.db`，與 `ui_sessions.db` 同層但獨立檔）**。
**永不寫入** agent soul（`data/soul/`）、legacy（`data/legacy/`）或記憶系統（FactualStore／
向量庫）。回饋**僅供人工檢視，不自動回灌** agent 學習——避免自我優化迴圈、杜絕牴觸
AGENT_SOUL_PROTECTION 禁令。`FeedbackStore` 只開 `feedback.db`，物理上無從觸及受保護區。

### 後端
- **`src/services/feedback_store.py`**：`FeedbackStore`（靜態方法，仿 `SessionService` 範式）。
  路徑經 `resource_manager.get_base_path()` 動態解析（禁硬編碼）；表 `message_feedback`
  唯一鍵 `(session_id, message_id, agent_id)`；`feedback_type ∈ {up, down}`，`none` 代表撤回（刪列）；
  `reason` ≤500；以 per-session `asyncio.Lock` 串行化寫入。`CREATE TABLE IF NOT EXISTS` 自癒。
- **`src/routers/feedback.py`**（prefix `/feedback`，自動 `/v1/feedback/*`＋`/feedback/*`）：
  | 方法 | 路徑 | 授權 | 用途 |
  |---|---|---|---|
  | POST | `/feedback/submit` | 任何已驗證身分 | upsert/撤回回饋 |
  | GET | `/feedback/session/{session_id}` | 任何已驗證身分 | 當前身分於該 session 的回饋對映（還原按鈕狀態） |
  | GET | `/feedback/recent` | **僅 Owner/admin** | 檢視頁：最近回饋清單 |
  - 不新增錯誤碼：upsert＋寬鬆驗證＋FastAPI 預設 422（`feedback_type` 用 `Literal` 驗證），零 Guard #18 churn。

### 前端
- **`hooks/useFeedback.ts`**：`{messageId:{type,reason}}` 狀態；`submit()` 樂觀更新＋`fetchTouka` POST，
  失敗回滾＋toast；掛載依 `sessionId` 拉 `/feedback/session/{id}` 還原。
- **`components/chat/FeedbackReasonDialog.tsx`**：Radix Dialog（重用 `ui/dialog.tsx`）＋`<textarea maxLength={500}>`。
- **`MessageItem.tsx`**：assistant 訊息操作列（重生後、刪除前）加讚/倒讚鈕（`ThumbsUp`/`ThumbsDown`，
  active 著色、aria-label、≥24px）；倒讚開原因彈窗、讚即時送、再點同鍵＝撤回。`ChatArea.tsx` 持有
  `useFeedback`＋彈窗狀態並下傳（沿用既有 prop 模式）。
- **`components/sidebar/monitoring/FeedbackLog.tsx`**：唯讀列出 `/feedback/recent`（👍/👎＋原因＋時間），
  掛進 `SystemMonitoringView` 的「回饋紀錄」節。

### HAX 對映
- **G15**：讚/倒讚＋原因＝細粒度回饋管道（原本只有「重新產生」一條粗粒度訊號）。
- **G6**：倒讚原因欄＝有害/偏見內容回報管道（前端層此前完全缺）。

## 後果與影響 (Consequences)

- 新增隔離 `feedback.db`；新增三端點（皆有 auth 依賴，過 Guard #24）；`openapi.json`＋`api.ts` 重生（Guard #17）；
  `NOQA_REGISTRY.md` B008 計數隨新 `Depends` 上修（Guard #19）。
- 回饋為**寫入即收集**；v1 僅擁有者檢視頁消費，**不影響** agent 行為、記憶或 soul。
- 殘留（記錄不做）：跨使用者彙總統計、回饋匯出、回饋驅動的自動改善（後者為安全禁區）。

## Action Items
1. [x] `FeedbackStore`（隔離 feedback.db）＋ TDD（upsert／撤回／依 agent 過濾／隔離斷言）。
2. [x] `feedback.py` 三端點＋ owner-gate；整合測試（auth、403、原因、撤回）。
3. [x] 前端 `useFeedback`／`FeedbackReasonDialog`／MessageItem 讚倒讚／ChatArea 串接。
4. [x] `FeedbackLog` 檢視頁節點。
5. [x] 契約同步（openapi/api.ts）＋ NOQA 登記＋ `hax_basics` 守衛擴充＋全量驗證。

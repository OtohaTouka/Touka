# ADR-0081: 游標續傳可恢復串流（生成與連線解耦 ＋ 伺服器端串流緩衝）

**狀態**: ✅ 已接受（身分語意修訂 by [ADR-0132](0132-session-owner-attribution.md)：_persist_assistant_turn 人類呼叫者改 upsert identity=None＋owner_user_id（修 human UUID 污染 agent_id）；游標續傳串流機制本身不變）
**日期**: 2026-06-11
**影響範圍**: ToukaProxy（dispatcher/串流緩衝/UI 端點）＋ ToukaChat（resume 接回）
**關鍵字**: StreamBufferService, resumable stream, 游標續傳, starting_after, producer 解耦, 重整恢復

---

## 背景與問題

ADR-0080 達成「同分頁切走背景續跑」，但**重整/關閉分頁會中斷進行中生成**：
1. 後端生成綁定 HTTP 連線——client 斷線 → uvicorn 取消 generator → 生成死亡。
2. 前端進行中狀態（登錄表）只存在記憶體，重整即滅。
3. user 訊息只在完成時隨整樹持久化，重整連問題本身都會消失。

研究比較（ChatGPT／Claude／Gemini）顯示完整解法是 **ChatGPT／Vercel resumable-stream 藍圖**：生成與連線解耦＋伺服器端 per-stream 緩衝＋游標續傳（對應 OpenAI background mode 的 `sequence_number`/`starting_after`）。使用者已確認走完整版：TTL 10 分鐘、重整後接回全部進行中對話。

## 決策

### 後端：StreamBufferService（進行中互動串流 SSOT）＋ producer 解耦

- **`services/stream_buffer_service.py`**（新，SRP）：`Record<session_id, StreamEntry>`——原樣 SSE 行緩衝（seq=index）、狀態機（active→done/error/cancelled）、`asyncio.Condition` 通知、producer task 句柄。API：`create`（同 session active 先取消舊串流＝activeStreamId 競態守則）/`append`/`finish`/`cancel`/`subscribe(starting_after)`（重播→跟流→終態；30s `: heartbeat` 保活，照 terminal.py 範本）/`list_active`/`get_state`。常數（禁令 18）：`STREAM_BUFFER_TTL_SECONDS=600`、`STREAM_BUFFER_MAX_CHUNKS=50000`（超限終結 error）、`STREAM_ACTIVE_MAX_SECONDS=3600`（孤兒保險）；lazy 清掃；時間走 time_utils（禁令 43）。
- **dispatcher 解耦**：串流分支若 `context["resumable_stream"] is True` 且有 session_id → `spawn_logged` 啟動 producer（`_pump_stream_to_buffer`）抽乾 handler generator 寫入緩衝；HTTP 回應改為 `subscribe(starting_after=-1)` 訂閱者。**client 斷線只取消訂閱者，生成續跑**。producer 正常完成時以 `_extract_delta_text`（純函式）累積的 `full_content` 走 heartbeat 同款範式伺服器端持久化（`TreeMessageManager.append_node` + `upsert_session(identity=...)`，內建 per-session 鎖），**先持久化再標記終態**。
- **gating（防雙重持久化）**：`resumable_stream` 旗標**只在 proxy.py 兩個 UI 對話端點**（/chat/completions、/messages 的已驗證身分分支）設定。heartbeat 脈衝同走 dispatch_request 但不設旗標 → 原路徑、其自有持久化零影響；無 session_id 的外部 API 呼叫 → 原路徑；guest（無身分）→ 原路徑。
- **端點**（ui_sessions.py，`_require_authenticated` + `_can_access_session` IDOR 守衛，Guard #24）：
  - `GET /v1/ui/sessions/{id}/stream?starting_after=N`：SSE 重播＋跟流；session 不存在或非擁有者 fail-closed 拒絕；無緩衝回 `STREAM_NOT_FOUND`（errors.json id 1212，禁令 44）。
  - `GET /v1/ui/streams/active`：逐條 ownership 過濾的進行中清單。
  - `POST /sessions/{id}/stop` 擴充：原 rhythm interrupt + process kill 之外，加 `stream_buffer_service.cancel`（producer 連坐取消＋訂閱者喚醒）。
- 附帶修復：dispatcher 既有潛在 bug——guest 串流結束時 `record_working_brain(None)` AttributeError 多吐一個 error chunk，補 `if identity` 守衛。

### 前端：user 回合先行持久化 ＋ 重整接回

- **`startStream`（StreamRegistry）**：先以 `buildUserTurnTree` 建「只含 user 回合」的樹並 `syncSession` 持久化（重整後 user 訊息不再消失；resume 重建與後端 append 都以 currentId=user 為基底/掛點），再 `appendAssistantNode` 建登錄表 entry 開串流。
- **`resumeChatStream`（connections.ts）**：走 fetchTouka（GET、自動 Authorization、FETCH_SSOT 合規）讀 resume 端點；解析與 fetchChatStream 共用抽出的 `dispatchSseLine`（解析 SSOT，即時與重播語意一致）。
- **`resumeActiveStreams`（StreamRegistry）**：sessions 載入後一次性 `GET /streams/active` → 對每條（≤`MAX_CONCURRENT_STREAMS`）以已持久化的 user 回合樹＋`appendAssistantNode` 重建 entry → `resumeChatStream(sid, -1)` 從頭重播重建 aiText/steps/progress → 之後與一般串流無異（側欄 spinner 隨之恢復；ADR-0080 衍生顯示全沿用）。
- **持久化所有權順序**：producer 先存純文字版 → 前端（連線中或已接回）onDone 再以富樹（含 steps）整棵覆寫 → 最終以前端富版為準；前端從未接回 → 後端純文字版即最終。

## 對齊開發原則

| 原則 | 落實 |
|------|------|
| **TDD** | 後端 `test_stream_buffer_service.py`(11)＋`test_dispatcher_resumable.py`(4)＋`test_stream_resume_endpoints.py`(5)；前端 stream_tree 增 4、`connections.resume.test.ts`(3)、`StreamRegistry.resume.test.tsx`(3)；全 Red→Green |
| **SSOT** | 進行中串流＝StreamBufferService；SSE 解析＝dispatchSseLine；user 回合樹＝buildUserTurnTree；持久化沿用 session_service/TreeMessageManager；錯誤碼走 errors.json |
| **SOLID** | SRP：緩衝/泵/端點/前端接回各一職；OCP：原路徑零變（gating 加法）；DIP：producer 經 spawn_logged、回調注入 |

## 後果

### 正面
- **重整/換分頁後可接回**進行中生成（全部進行中對話，含背景多條），TTL 內已完成串流也可重播。
- user 訊息送出即持久化（重整不再丟問題）。
- 後端生成不再受前端連線生死影響——真正的伺服器端背景生成。

### 已知限制（誠實揭露）
- 緩衝 in-memory：**server 重啟**＝進行中生成與緩衝俱滅（生成本就隨進程死亡，非倒退）；完成後保留 10 分鐘，過期靠已持久化歷史（結果不丟，只是不重播）。
- 前端從未接回的完成串流：持久化為純文字（無 steps 富資訊）。
- 直連供應商串流（瀏覽器直打 ollama/openai）與 DebugAgent 模擬：無後端經手，不在恢復範圍（維持 ADR-0080 行為）。

## 相關 ADR

- [ADR-0080](0080-background-session-streaming.md) — 前端 per-session 登錄表（本 ADR 的前端基座）
- [ADR-0078](0078-resilience-and-failure-observability.md) — spawn_logged／upsert per-session 鎖／失效可見化（本 ADR 重用）
- [ADR-0036](0036-unified-round-count-ssot.md) — round 計算 SSOT（不受影響）
- [ADR-0034](0034-endpoint-auth-dependency-guard.md) — 端點認證守衛（新端點合規）
- [ADR-0014](0014-error-code-sovereignty.md) — 錯誤碼主權（STREAM_NOT_FOUND）

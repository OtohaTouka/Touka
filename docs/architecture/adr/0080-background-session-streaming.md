# ADR-0080: 聊天串流背景續跑（Per-Session Stream Registry 與顯示衍生化）

**狀態**: ✅ 已接受（已知限制已解除 by [ADR-0081](0081-resumable-stream-cursor-buffer.md)：文中「中途重整→串流中斷、無 server-side checkpoint」限制已由 StreamBufferService 游標續傳＋前端 resumeActiveStreams 補上；背景續跑機制本身不變）
**日期**: 2026-06-11
**影響範圍**: ToukaChat（前端串流架構）
**關鍵字**: StreamRegistry, per-session, 背景續跑, 顯示衍生, SSOT, 防污染, useChatStream

---

## 背景與問題

ToukaChat 的聊天串流原本綁定「當前顯示的對話樹」（ChatProvider 單一 `history` state）與三個全域單一假設：
1. `useChatStream` 用單一 `abortControllerRef` —— 只能追蹤一條串流。
2. 串流回調（onChunk/onEvent/onDone）寫進 ChatProvider 的單一 `history` —— 切走後 `history` 變成別的對話，chunk 會污染。
3. `isSending`/`isWaiting` 是全域單一 bool —— 無法表達「A 在背景跑、B 沒在跑」。

2026-06-11 review 修復 #1 為了防污染，採「切換對話即 `abortActiveStream()`」——但這使「送出後切走，生成即中斷」，使用者要求改為**切走後背景續跑、切回看到結果**。

探索證實此功能**純前端可完成**：後端串流只依 `session_id`/`request_id` 運作、與「前端顯示哪個對話」無關、無 client 斷線偵測、ReAct 多輪在同一 HTTP 串流內跑完，`/v1/ui/sessions/{id}/stop` 已 per-session。故只要前端不中止 fetch、持續消費 chunk 至 per-session 緩衝即可。

## 決策

### 進行中串流登錄表（per-session SSOT）＋ 顯示衍生化

**SSOT 邊界劃分**：
- **進行中串流 SSOT** = 新增 `StreamRegistryProvider`（`Record<sessionId, StreamState>`，各自 `AbortController` + 累積樹 + status）。
- **已完成/已持久化對話 SSOT** = `sessions` list（不變）。
- **顯示 `history`** = 衍生不另存：`activeStream?.tree ?? staticHistory`。
- **`isSending`/`isWaiting`** = 衍生自 `activeStream.status`。
- **`streamingSessionIds`** = 衍生供側欄顯示生成指示。

串流回調以函數式 setState 更新「對應 sid」的 entry，與「前端顯示哪個對話」完全無關 → 切走後背景續跑、切回從登錄表衍生顯示。**防污染由 per-session 隔離達成**（chunk 只寫進自己 sid 的樹，碰不到別的對話），取代 review #1 的 abort-based 防污染。

### 對外合約不變（OCP）

ChatProvider 仍暴露 `history`/`isSending`/`isWaiting`/`handleSend`/`handleStopGeneration`，只是改為**衍生值** → ChatArea、MessageItem、RightSidebar 等消費端零改動。新增 `streamingSessionIds` 為純加法（側欄 spinner）。

### SOLID

- **SRP**：`StreamRegistry` 專責進行中串流生命週期＋完成寫回；`useChatStream` 瘦身為「發送前準備＋委派 `startStream`」的薄層；ChatProvider 專責「把顯示綁到 active session」；樹變更邏輯抽為純函式 `stream_tree.ts`（`buildStreamNodes`/`applyChunkToTree`/`applyEventToTree`），複用既有 SSOT `reduceProgress`。
- **OCP**：對外 context 合約不變，消費端零改。
- **DIP**：UI 依賴 ChatProvider 暴露的衍生值/查詢；`setPendingInstall` 等 ChatProvider 專屬副作用以回調參數注入 registry（不反向依賴）。

### 元件

| 元件 | 位置 | 職責 |
|------|------|------|
| `StreamRegistryProvider` | `lib/providers/StreamRegistryProvider.tsx` | per-session 串流 SSOT；`startStream`/`stopStream`/`getStream`/`streamingSessionIds`；並發上限 `MAX_CONCURRENT_STREAMS=5` |
| `stream_tree.ts` | `lib/stream_tree.ts` | 樹變更純函式（防污染：目標節點不存在即原樹返回） |
| `useChatStream`（改寫） | `hooks/useChatStream.ts` | 發送前準備＋委派 `startStream`；`handleStopGeneration` 委派 `stopStream(activeSessionId)` |
| ChatProvider（改寫） | `lib/providers/ChatProvider.tsx` | `history`/`isSending` 衍生；切換/新建不再中止串流；artifact 即時更新（active-only）＋完成回填 `staticHistory` |
| 側欄指示 | `SessionItem`/`LeftSidebar`/`ProjectSidebarView` | 進行中（含背景）對話顯示 `Loader2` spinner |

Provider 巢狀：`SessionListProvider → StreamRegistryProvider → ChatProvider`（registry 用 `useSessionList` 寫回、`useDebug` 接 sim handlers）。

### artifact 協調（需謹慎的整合點）

即時 artifact（`processStreamChunk`）只對 **active session** 生效（ChatProvider 監 `activeStream.aiText`），背景串流不打擾面板；active 串流完成（entry 消失）時 `finalizeStream` 開面板並把已寫回 `sessions` 的完成樹回填 `staticHistory`，使顯示無縫接續；切回完成的背景對話時由 `handleSwitchSession` 重新 `migrate`。

## 對齊開發原則

| 原則 | 落實 |
|------|------|
| **TDD** | `stream_tree.test.ts`（10）＋`StreamRegistry.test.tsx`（7：建 entry/per-session 隔離/上限 5/stop/onDone 寫回/onError toast）＋`useChatStream.test.ts`（4：委派）＋`ChatProvider.backgroundStream.test.tsx`（2：端到端背景續跑＋雙串流隔離），全 Red→Green |
| **SSOT** | 進行中＝registry、完成＝sessions、顯示＝衍生；round 計算、fetch、time、reduceProgress 全走既有 SSOT |
| **SOLID** | 如上 |

## 後果

### 正面
- 切走後背景續跑、切回看到累積/完成結果；側欄一眼看出哪些對話在背景生成。
- 防污染從「abort」升級為「per-session 隔離」——更正確（不犧牲生成）。
- 對外合約不變，消費端零改。
- 純前端、零後端改動、零碰 agent 資料（持久化沿用 `syncSession` → user_sessions DB）。

### 已知限制
- **中途重整/關閉分頁 → 進行中生成中斷**（後端會跑完但前端 fetch 斷線收不到；互動串流無 server-side checkpoint，與 heartbeat `interrupted_runs` 機制不同、不可直接複用）。屬 v1 已知邊界，未來增強需 server-side 持久化互動串流的進行中狀態。
- **並發上限 5**：超過時 `startStream` 回 `false`，呼叫端 toast 提示先停其一。
- **完成不自動搶開 artifact 面板**（僅 active 完成時開）：背景對話完成不打擾當前焦點。

## 相關 ADR

- [ADR-0040](0040-eventsource-token-query-guard.md) — Fetch + ReadableStream SSE 串流（本功能消費的串流通道）
- [ADR-0042](0042-legacy-history-compatibility.md) — `migrateFlatToTree` 樹結構（顯示衍生與完成回填使用）
- [ADR-0036](0036-unified-round-count-ssot.md) — round 計算 SSOT（不動）
- [ADR-0043](0043-react-hooks-stability-decoupling.md) — UI/資料層 Hook 解耦（artifact 副作用上移精神）
- [ADR-0078](0078-resilience-and-failure-observability.md) — 失效可見化（registry onError 沿用 `NETWORK_CONNECTION_ERROR` toast）

# ADR-0300：左側邊欄的工作狀態與動畫，清單自己更新

- 狀態：✅ 已實作。一頁計畫由維護者核准，計畫經多輪審查。
- 日期：2026-11-06
- 決策者：維護者
- 相關：ADR-0078（失效可觀察）、ADR-0080（背景續跑）、ADR-0081（可恢復串流）、ADR-0086（無痕）、ADR-0231（對話保存可靠性）、ADR-0295（實際驗證）。
- 影響範圍：`ToukaProxy/src/routers/ui_sessions.py`、`src/models/session_activity.py`（新）、`src/services/activity_registry.py`、`src/services/stream_buffer_service.py`、`src/utils/activity_visibility.py`（新）、`src/routers/heartbeat.py`、`src/routers/stats.py`、`ToukaChat/src/lib/providers/SessionActivityProvider.tsx`（新）、`SessionListProvider.tsx`、`src/lib/session_activity.ts`（新）、`src/hooks/useActivityPoll.ts`（新）、`useSessionDiscovery.ts`（新）、`useUnseenSessions.ts`（新）、`usePendingRequestQueue.ts`（新）、`useReducedMotionPreference.ts`（新）、`src/components/sidebar/left/SessionStatusMark.tsx`（新）、`SessionItem.tsx`、`SessionTree.tsx`、`src/components/chat/SessionUpdatedNotice.tsx`（新）、`src/app/globals.css`。

## 背景

維護者要左側邊欄像 Claude、ChatGPT 桌面版那樣看得出哪段對話在工作：桌面版對話名稱明暗交替，手機版名稱前的小點明暗交替，黑白的呼吸燈。

改版前有三個缺口：

- 只有本分頁的串流看得到；心跳跑到第幾輪、另一個裝置的串流都看不到。
- 心跳新開的對話要重新整理才出現。
- 兩段對話同時問使用者時，後到的彈窗蓋掉先到的。

最直覺的做法是定時呼叫 `refreshSessions()`，但那條路的整份清單會把寫到一半的內容換掉，而且舊回應會蓋新內容，所以這個改版**完全不呼叫它**。

## 決定

### 後端

1. 新端點 `GET /v1/ui/activity?since=<毫秒>`，只看記憶體、不碰對話資料庫，輸出白名單：
   - `streams`：呼叫者自己開的串流（別人的不回，管理員也一樣；無痕不回；舊路徑沒有 `started` 的不回）。
   - `background`：看得到的靈魂正在跑的心跳，帶 `current`、`total`。
   - `finished`：`since` 之後結束的心跳，只有編號與時間。
   - `cursor`：這位呼叫者的下一個 `since`，別人的心跳結束時回應逐字不變，看不出次數。沒帶 `since` 時回目前時間、`finished` 為空。
2. `activity_registry` 多記最近 200 筆心跳結束紀錄（`MAX_FINISHED_ACTIVITY`），時間保證嚴格遞增。紀錄放在 `heartbeat.py` 內層 `finally` 的最後：前端看到就會下載，放太早會下載到剪裁前的內容。第一輪之前就停的也要記，對話可能已經寫進去了。
3. 誰看得到哪個靈魂的活動抽成 `utils/activity_visibility.py`，和 `/v1/stats` 共用。

### 前端

4. 全域一份 `SessionActivityProvider`，每 8 秒查一次，只在分頁顯示中查，回到分頁馬上查，同時只跑一個請求。IDE 不查。
5. 新對話用 `discoverSession(id)` 下載單筆（帶版本），不重抓整份清單。結果分「已加入、已知道、不存在、暫時失敗、永久拒絕」；暫時失敗留在佇列下次再試，永久拒絕這次掛載內不再試，每次查詢最多下載 5 筆。下載回來時確認編號仍不在清單、身分沒變才加入，不走會拿掉版本的 `setSessions`，所以下一則存得進去。
6. 已知的對話只在 Provider 記活動時間，不動對話內容、不動存檔狀態。排序與列上的日期取存檔時間和活動時間的較大值。
7. 本分頁的串流以本地登記為準，結束時呼吸燈馬上停；本地已結束的編號，要結束之後才發出的查詢結果才採用，舊查詢不會讓它多亮一下或留假小點。
8. 顯示優先序：等您處理（不動的圖示）＞ 工作中（呼吸）＞ 有新內容（小點）。小點只記「工作結束時您沒在看」，存瀏覽器、鍵名含使用者 ID、只存編號；無痕只放記憶體。
9. `pendingAsk`、`pendingInstall` 改排隊：對外仍是目前那一筆，答完跳下一筆，串流結束的那筆移除，換帳號清空。彈窗用請求編號當 key，選項不帶上一筆的。`ask_user` 的答案憑證改成一律隨機：原本用工具呼叫編號，有些模型是流水號，兩段對話同時問會撞在一起。
10. 開著的對話被別處寫入（心跳跑完、另一處串流結束），不再工作時在對話區顯示「有新內容，點一下更新」。存檔已失敗時不顯示，點了也不會更新，維持存檔失敗的畫面。
11. 動畫在 `globals.css` 定義 `touka-breathe`（只有明暗，2.4 秒一次，不快於 2 秒）。減少動態要同時看 App 設定與系統設定：App 設定優先，「跟隨系統」才看系統；停掉動畫時改用文字表示狀態。無障礙名稱一律帶狀態，不用每列 live region。
12. 查詢失敗不轉換狀態；連續 3 次失敗清掉心跳活動、不留小點，記 `logSSOTError`，不跳錯誤視窗（後端降級已有 `DegradedBanner`）。本地串流不受影響。

## 取捨

- **輪詢而不是推播**：8 秒一次的請求很小、只看記憶體，全系統不必多一條長連線。代價是心跳進度最多晚 8 秒。
- **別處的串流結束也算「有新內容」**：不比較 `updatedAt`，因為前端存檔的值是客戶端時間，也不用追蹤自己的存檔。
- **小點跟著瀏覽器走，不跟著帳號走**：換裝置不同步，夠用，也不必多一張表。

## 已知限制

- 提問與技能安裝的彈窗重新整理後回不來（既有限制），所以重新整理後這段不亮「等您處理」；核准卡與外掛工具的請求在串流樹裡，仍會亮。
- 清單讀完到第一次查詢之間結束的心跳不留小點。
- 後端重啟後結束紀錄清空，這段時間結束的心跳看不到。
- 換對話時既有的整份刷新不動，它的舊回應風險維持原樣。

## 驗證

- `tests/proxy/test_ui_activity_security.py`、`tests/unit/test_heartbeat_finished_record.py`。
- `ToukaChat/src/lib/providers/__tests__/SessionActivityProvider.test.tsx`、`SessionListProvider.discover.test.tsx`、`src/hooks/__tests__/usePendingRequestQueue.test.ts` 等。
- 實際驗證 `ToukaChat/e2e-real/session-activity.spec.ts`、`session-attention.spec.ts`（真前端、真後端、假模型）。動畫靠瀏覽器的 `getAnimations()` 驗，截圖會把動畫停掉。

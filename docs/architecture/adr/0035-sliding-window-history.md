# ADR-0035: Sliding Window History — 滑動視窗對話紀錄

**狀態**: ✅ 已接受（[ADR-0092](0092-context-window-token-sliding-window.md) 修訂：觸發量測 則數→token%、PostHeartbeat-only→雙觸發、ACID 2 步→3 步；核心 Decision 已由 0092 承接，`keep_rounds`／`upper_limit_rounds`／`compute_split` 已 deprecated 無生產讀者）
**日期**: 2026-05-21
**關鍵字**: 滑動視窗, 家對話, PostHeartbeat, ACID, Legacy 歸檔

---

## 背景與問題

對話長度無限增長，導致：
1. serialize/transmit 成本線性上升（每次 syncSession 傳輸整棵訊息樹）
2. 長對話可能超出 LLM context window
3. Agent 在心跳自主執行時 context 不穩定

需要一個機制，在不打斷 Agent 任務執行的前提下，自動維護對話長度在合理範圍。

---

## 決策

### 核心設計

**觸發時機**：PostHeartbeat — 所有 pulse 跑完後，在 `_process_heartbeat_loop` 的 `finally` 區塊觸發。

**作用範圍**：僅對**家對話（home session）**生效。子對話（`session_mode="independent"`）完全排除。

**計數單位**：來回數（round = user+assistant pair = 1），與全系統統一口徑對齊（ADR-0036）。

**ACID 安全順序**（防止資料遺失）：
```
1. 計算 split → (legacy_msgs, kept_msgs) [純記憶體]
2. resource_manager.write_json(legacy_path) ← Step A
   ↑ 失敗 → abort，session 不變，下次重試
3. session_service.upsert_session(kept_msgs) ← Step B
   ↑ 失敗 → legacy 已存，session 不變（安全）
```

### 元件

| 元件 | 位置 | 職責 |
|------|------|------|
| `SlidingWindowConfig` | `ToukaProxy/src/models/sliding_window.py` | PolicyAsCode 設定模型（enabled, keep_rounds, upper_limit_rounds） |
| `SlidingWindowService` | `ToukaProxy/src/services/sliding_window_service.py` | 純邏輯服務：count、should_trim、compute_split、check_and_trim |
| `sliding_window.json` | `ToukaProxy/config/sliding_window.json` | SSOT 設定物理來源（預設 disabled） |
| PostHeartbeat hook | `ToukaProxy/src/routers/heartbeat.py` | `_process_heartbeat_loop` finally 區塊呼叫 |
| `GET/POST /v1/system/sliding_window` | `ToukaProxy/src/routers/system.py` | 前端讀寫設定（GET: 已認證；POST: admin only） |
| `SlidingWindowTab` | `ToukaChat/src/components/settings/tabs/` | 設定 UI |

### 預設值

```json
{ "enabled": false, "keep_rounds": 100, "upper_limit_rounds": 150 }
```

觸發條件：`round_count >= upper_limit_rounds`，剪後保留 `keep_rounds` 個來回。

---

## 對齊三大開發原則

| 原則 | 落實方式 |
|------|---------|
| **TDD** | 18 個單元測試（`test_sliding_window_service.py`）+ 6 個整合測試（`test_heartbeat_sliding_window.py`），全部 RED→GREEN |
| **SOLID-SRP** | `SlidingWindowService` 僅負責視窗邏輯；PostHeartbeat hook 僅負責呼叫；各函式單一職責 |
| **SSOT** | `sliding_window.json` 為唯一設定來源；`count_rounds_from_linear` 為唯一計算函式（ADR-0036）；路徑透過 `resource_manager` 解析 |

---

## 影響與後果

- ✅ 心跳執行中 context 不被打斷（PostHeartbeat 設計保障）
- ✅ 子對話完全隔離，不受影響
- ✅ ACID-safe：最壞情況為「legacy 存成功但 session 未剪」，下次 heartbeat 重試
- ✅ 功能預設關閉（`enabled: false`），不影響現有行為
- ⚠️ Legacy 重複歸檔（若 ACID Step B 失敗後重試）：重複 JSON 檔案無害，帶時間戳檔名
- ⚠️ 子對話不支援滑動視窗：子對話無固定 context 基準，未來可依需求延伸

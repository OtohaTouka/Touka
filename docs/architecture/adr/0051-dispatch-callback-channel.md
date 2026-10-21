# ADR-0051: ToukaDispatch 雙向回呼通道（Bidirectional Dispatch Callback Channel）

**狀態**：✅ 已接受  
**日期**：2026-05-27  
**影響範圍**：ToukaDispatch、ToukaProxy

> 🔧 **取捨表「server 重啟 → pending token 失效」已被 [ADR-0138](0138-parallel-fanout-and-resumable-delivery.md) 補耐久層緩解**：`mission_tools.py` 的 `await_dispatch_result` 現先讀耐久 JSON（`read_result`，wait 前）、TIMEOUT/UNKNOWN 後再讀一次——已算完的結果可跨 proxy 重啟存活。in-memory `asyncio.Event` 核心回呼機制（`DispatchCallbackStore`）本身仍有效。

---

## 背景

系統 Agent（Alice 等）可透過 `mission_tools(target_agent_id="DispatchAgent")` 將任務分派至
DispatchAgent 看板，再由外部 AI 透過 ToukaDispatch MCP 執行。

**問題**：外部 AI 完成後，Alice 需要在同一個 ReAct loop 內取得結果。既有的心跳輪詢（heartbeat）
屬於「更上層的活動」，一次心跳含多個 ReAct loop，不適合在 ReAct loop 內等待任務回報結果
（需等到下次心跳才能知曉）。

架構邊界（嚴格）：
- 外部 AI 只能呼叫既有 ToukaDispatch MCP 工具（不變、不增）
- 外部 AI 不能直接呼叫 Alice 或 ToukaProxy
- 回呼通道由 ToukaDispatch server 透明觸發，外部 AI 無感

---

## 方案評估

### 方案 A（心跳輪詢）
讓 Alice 等到下次心跳觸發再讀取結果。
- ✅ 零新基礎建設
- ❌ 延遲不可接受：一次心跳含多個 ReAct loop，任務回報必須在同一 ReAct loop 內完成

### 方案 B（asyncio.Event 雙向回呼 — **採用**）
```
Alice ──(mission_tools notify_on_complete=True)──→ ToukaProxy：生成 callback_token，注冊 asyncio.Event
Alice ──(mission_tools await_dispatch_result)──→ ToukaProxy：掛起（asyncio.Event.wait）

                   外部 AI（獨立 session）
                     │ dispatch_update_task(status="DONE")  ← 行為不變
                     │
              ToukaDispatch server 內部：
                讀 MissionState.metadata["dispatch_callbacks"][task_id]
                → POST ToukaProxy /v1/internal/dispatch/callback
                     { X-Callback-Token, task_id, status, result_summary }

ToukaProxy：asyncio.Event.set() → await_dispatch_result 返回結果
Alice 繼續下一步
```
- ✅ 外部 AI 完全無感（MCP 工具不變）
- ✅ 在同一 ReAct loop 內同步取得結果
- ✅ 每個 task 獨立 token，支援同 mission 多任務並行等待
- ⚠️ in-memory：server 重啟後 pending token 失效（TTL 300s 內可接受）

---

## 決策

採用**方案 B**。

---

## 認證方案（Option A）

`callback_token` 本身即為 server-to-server 憑證：

1. ToukaProxy 在 `initialize_mission(notify_on_complete=True)` 時生成 token（`secrets.token_urlsafe(32)`）
2. Token 存入 `MissionState.metadata["dispatch_callbacks"] = {task_id: token}`
3. Token 回傳給 Alice（含在 mission_tools 回傳訊息中）
4. ToukaDispatch 任務完成後，從 metadata 取出 token，POST 至 ToukaProxy `/v1/internal/dispatch/callback`（Header: `X-Callback-Token`）
5. ToukaProxy 驗證 token 存在於 callback store → 交付結果 → asyncio.Event.set()

**雙層防護**（Guard #24 合規）：
- IP 來源限制：僅接受 `127.0.0.1 / ::1 / localhost`
- Token 存在驗證：token 必須在 callback store 中已注冊

---

## 實作細節

### 新增檔案

| 檔案 | 說明 |
|------|------|
| `ToukaProxy/src/services/dispatch_callback_store.py` | asyncio.Event Singleton store（SRP） |
| `ToukaProxy/src/routers/dispatch_callback.py` | `/v1/internal/dispatch/callback` 端點 |

### 修改檔案

| 檔案 | 變動 |
|------|------|
| `ToukaProxy/src/tools/mission_tools.py` | 新增 `notify_on_complete` 參數、`await_dispatch_result` action |
| `ToukaProxy/src/main.py` | 註冊 `dispatch_callback_router`（不在 CORE_ROUTERS，避免雙路徑） |
| `ToukaDispatch/server.py` | 新增 `_PROXY_URL`、`_notify_touka_proxy()`、回呼觸發邏輯 |
| `ToukaDispatch/requirements.txt` | 新增 `httpx>=0.27.0` |

---

## 取捨確認

| 面向 | 現況 |
|------|------|
| 回呼認證 | Option A：callback_token 本身即憑證，無額外 secret |
| 內部端點防護 | localhost 來源驗證 + token 驗證雙層（Guard #24 合規） |
| 外部 AI 行為 | ✅ 完全不變，無感知回呼機制 |
| server 重啟 | asyncio.Event 為 in-memory，重啟後 pending token 失效（TTL 300s 內可接受） |
| 多任務並行 | 每個 task_id 獨立 token，支援同一 mission 多任務並行等待 |
| 回呼失敗 | 靜默失敗（`BLE001 noqa`）：不影響外部 AI 的主要任務回報 |

---

## NOQA 新增

參見 [NOQA_REGISTRY.md](../NOQA_REGISTRY.md)（ADR-0051 2026-05-27 條目）。

總計 +8 處架構型豁免：+1 ENV_SSOT_VIOLATION、+2 BLE001、+2 B008、+1 TC002、+2 PLW0603。

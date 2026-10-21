# 🧠 記憶系統回傳範例文檔 (Memory Payload Example)

本文件展示了 Agent 在執行 `memory_tools(action="query", ...)` 時，系統實際回傳的結構化字串範例。這能幫助開發者理解 Agent 是如何感知其過往記憶的元數據。

> **注意（ADR-0116）**：以下範例適用於 ToukaProxy 同行程的**整合模式單一部署**。`get_resource_proxy()` 必須取得 Proxy 注入的真實 `ResourceManager`；缺失時 fail-loud，不存在獨立 MCP 或資源代理降級模式。

---

## 範例 1：包含豐富屬性的搜尋結果

當 Agent 執行：
`memory_tools(action="query", query="專案", emotions=["happy"], importance_min="4")`

### 系統回傳 (Agent 視角)：
```text
=== Retrieved Memories ===
1. (2024-04-13) [標籤:核心開發, 進度 | 重要度:5 | 情感:happy, proud]
   內容：「今天成功完成了系統重構的分發層，所有的測試都通過了，感覺這幾個月的努力非常值得。」

2. (2024-04-12) [標籤:專案管理 | 重要度:4 | 情感:happy]
   內容：「與使用者確認了下一階段的記憶系統增強計畫，對方非常滿意目前的設計提案。」
```

---

## 範例 2：特定時間範圍的精確檢索

當 Agent 執行：
`memory_tools(action="query", date_start="2024-03-01", date_end="2024-03-05")`

### 系統回傳 (Agent 視角)：
```text
=== Retrieved Memories ===
1. (2024-03-04) [標籤:日常 | 重要度:normal | 情感:none]
   內容：「更換了伺服器的冷卻風扇，運作聲音小了很多。」

2. (2024-03-02) [標籤:bug | 重要度:3 | 情感:sad]
   內容：「發現資料庫連線在連線數過高時會發生溢位，需要重新檢核 Socket 釋放邏輯。」
```

---

## 範例 3：查無資料時的降級回傳

當 Agent 執行：
`memory_tools(action="query", query="不存在的消息", tags=["不存在的標籤"])`

### 系統回傳 (Agent 視角)：
```text
No relevant memories found for search criteria.
```

---

## 🛠️ 技術細節說明

1. **標籤結構**：`[標籤:xxx | 重要度:xxx | 情感:xxx]` 區塊被設計成易於 LLM 解析的格式，這能大幅提升 Agent 判斷記憶優先權的能力。
2. **時間顯示**：統一採用 `(YYYY-MM-DD)` 格式，讓 Agent 具備明確的時間維度感。
3. **重要度過濾**：支援 `importance_min` (數字門檻) 與 `importance` (精確匹配)。
4. **情感匹配**：支援多情感共同篩選。

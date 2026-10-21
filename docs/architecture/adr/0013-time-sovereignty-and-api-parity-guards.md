# ADR 0013: 時間主權與 API 一致性守衛 (Time Sovereignty and API Parity Guards)

## 狀態 (Status)
已接受 (Accepted)

## 背景 (Context)
隨著 Touka 系統變得日益複雜，維持前端 (ToukaChat) 與後端 (ToukaProxy) 之間的一致性變得極具挑戰性。具體問題包括：
1.  **時間不一致**：使用原生的 `Date` 或 `datetime` API 導致時區偏移和計算錯誤。
2.  **API 合約違規**：前端 API 呼叫經常偏離後端 FastAPI 的路由定義（例如：缺失 `/v1` 前綴、不正確的尾部斜線）。
3.  **授權碎片化**：心跳執行權限過去透過過時的白名單端點管理，而非整合進中央集權的身分能力系統。

## 決策 (Decisions)
1.  **大一統時間主權 (Unified Time Sovereignty)**：
    - **後端**：透過 `src.utils.time_utils` 強制執行，並利用靜態分析 (`lint_ssot.py`) 封鎖原生 `datetime` 調用。
    - **前端**：透過 `src/lib/time_utils.ts` 與新的靜態檢查腳本 (`guard_time.ts`) 強制執行。所有前端邏輯必須使用 `getSystemEpoch()`。
2.  **API 一致性守衛 (API Parity Guard)**：
    - 實作 `guard_frontend.ts`，自動比對所有前端 `fetchTouka` 呼叫與後端導出的 `openapi.json`。
    - 所有核心 API 呼叫強制使用 `/v1` 前綴，以確保路由一致性。
3.  **基於能力的身分授權 (Capability-based Authorization)**：
    - 廢除 `/v1/heartbeat/whitelist` 端點。
    - 將心跳喚醒權限整合至 `identity_map.json` 中，作為 `heartbeat_awakening` 能力標籤。
    - 更新 `HeartbeatWhitelist` UI，改為透過 `/v1/identity` 端點管理這些能力。

## 後果 (Consequences)
- **正面 (Positive)**：
    - API 合約達成 100% 同步。
    - 消除所有與時區相關的錯誤 (Bugs)。
    - 代理人能力的安全性達成集中化管理。
- **負面 (Negative)**：
    - 開發者必須記得使用工具函式，而非原生 API。
    - 新增 API 端點在進行前端開發前，必須先執行 Schema 同步。

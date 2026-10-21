# ADR 0020: 靈魂模型復原與 ID 統一化 (Soul Restoration & ID Unification)

## 狀態
已通過 (Accepted)

## 背景
在之前的演進中，靈魂系列模型（Soul-A, Soul-B, Soul-C）因 ID 不一致（`ToukaSoulA` vs 物理目錄 `SoulA`）且缺乏保護機制，導致被 `ModelManager` 的自動清理（Pruning）機制誤判為離線並移除。此外，前綴 ID 增加了系統複雜性，不符合 SSOT（Single Source of Truth）的簡潔原則。

## 決策
1.  **ID 簡潔化**：全面移除 `ToukaSoul` 前綴，統一使用簡潔 ID 作為正式識別符。這使得 ID 直接對應物理路徑（`data/soul/{id}`）。
2.  **動態配置合併**：更新 `AgentService` 與 `ModelManager`，在發現物理代理人後，自動從 `models.json` 載入漂亮的顯示名稱與開關設定。
3.  **強制保護機制 (Soul Protection)**：在清理邏輯中實施「硬執法」，只要物理目錄存在，靈魂代理人即受保護，不會被 Pruning 移除。
4.  **兼容性映射 (Legacy Mapping)**：在 API 路由層支援舊版前綴 ID 的自動糾偏，確保舊有對話流與書籤不中斷。

## 後果
-   **優點**：架構更加簡潔，ID 與物理資源強耦合，消除了命名空間衝突與遺失風險。
-   **優點**：使用者自定義的靈魂名稱（如「靈魂-A」、「靈魂-C」）能正確持久化。
-   **缺點**：需要對現有路由進行攔截糾偏，增加了微小的處理開銷。
-   **驗證**：已透過 `tests/integration/test_soul_restoration.py` 驗證。

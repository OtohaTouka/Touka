# ADR 0009: 動態模型發現與管理 (Dynamic Model Management)

## 狀態
已接受 (Accepted)

## 背景
隨著模型供應商 (Google, OpenAI, xAI) 頻繁更新模型清單，手動維護 `models.json` 變得不可持續。同時，使用者可能透過 `connections.json` 自定義多個 OpenAI 相容的後端來源。系統需要一種自動化、具備健康感知的模型管理機制。

## 決策
實施委派式模型管理架構：
1. **單一調度中心 (Manager)**：`ModelManager` 負責管理快取與持久化，但不直接實現抓取邏輯。
2. **邏輯下沉 (Logic Sinking)**：
    - 將 HTTP 抓取邏輯收攏至專屬的 `fetcher.py`。
    - 將健康檢查與驗證邏輯收攏至 `health.py`。
3. **主權保護合併 (Sovereignty Merge)**：
    - 動態抓取到的新模型預設為 `enabled: True` 與 `visibility: visible`。
    - 既有模型的屬性受持久化層保護。合併時僅更新 `last_seen` 時間戳記，嚴禁覆蓋使用者已調整過的啟用狀態或可見性。
4. **健康檢查閉環**：定期執行健康檢查，僅對成功的連線更新 `last_seen`，作為後續「清理離線模型」的 SSOT 基準。

## 後果
- **正向**：消除了手動維護模型清單的負擔；增加了系統對多供應商環境的適應力；符合 SOLID 的職責分離。
- **負向**：啟動時的併發請求可能輕微增加網路開銷。

# 舊歸檔庫 (LegacyStore)

## 🔍 概述

`LegacyStore` 負責從舊版 JSON 格式對話紀錄遷移至 SQLite 資料庫，並支援高效檢索與備份。所有遺產資料被視為不可變的歷史紀錄。

### 整合模式單一部署資源契約

LegacyStore 的檔案掃描與遷移操作透過 `get_resource_proxy()` 進行：
- **真實資源管理器**：只回傳 ToukaProxy 注入的 `ResourceManager`，保留完整身分隔離與路徑保護。
- **fail-loud**：取不到 `ResourceManager` 即拋錯；ADR-0116 已退役獨立 MCP 模式與 `_FallbackResourceProxy`。

所有實作均遵循同一個整合契約，不進行模式切換或自動 I/O fallback。

---

## 📖 詳細 API

::: memory_server.services.legacy_store.LegacyStore

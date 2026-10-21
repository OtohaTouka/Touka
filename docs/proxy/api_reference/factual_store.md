# 事實存儲 (FactualStore)

## 🔍 概述

`FactualStore` 負責管理無損事實記憶的持久化與檢索。所有事實資料使用 SQLite 儲存，支援標籤、重要度、情感等多維度查詢。

### 整合模式單一部署資源契約

FactualStore 內部的所有 I/O 操作（資料庫檔案建立、遷移）透過 `get_resource_proxy()` 進行：
- **真實資源管理器**：只回傳 ToukaProxy 注入的 `ResourceManager`，保留完整身分隔離與路徑保護。
- **fail-loud**：取不到 `ResourceManager` 即拋錯；ADR-0116 已退役獨立 MCP 模式與 `_FallbackResourceProxy`。

所有實作均遵循同一個整合契約，不進行模式切換或自動 I/O fallback。

---

## 📖 詳細 API

::: memory_server.factual.FactualStore

# 語義向量 (VectorStore)

## 🔍 概述

`VectorStore` 負責將語義記憶（對話片段、事實精華）轉換為向量並存儲在 Qdrant 向量資料庫中。支援語義相似度檢索，為 Agent 提供「感受相似經驗」的能力。

### 整合模式單一部署資源契約

VectorStore 的所有持久化操作透過 `get_resource_proxy()` 進行：
- **真實資源管理器**：只回傳 ToukaProxy 注入的 `ResourceManager`，保留完整身分隔離與路徑保護。
- **fail-loud**：取不到 `ResourceManager` 即拋錯；ADR-0116 已退役獨立 MCP 模式與 `_FallbackResourceProxy`。

所有實作均遵循同一個整合契約，不進行模式切換或自動 I/O fallback。

---

## 📖 詳細 API

::: memory_server.vector.VectorStore

# 記憶管理器 (MemoryManager)

## 🔍 概述

`MemoryManager` 是 ToukaMemory 系統的核心調度者，負責協調長期記憶、事實庫、向量存儲與任務看板的生命週期。所有記憶操作均透過此管理器進行，確保資料一致性與隔離。

### 整合模式單一部署資源契約

**重要**：MemoryManager 內部的所有 I/O 操作統一透過 `get_resource_proxy()` 工廠函數進行：
- **真實資源管理器**：只回傳 ToukaProxy 注入的 `ResourceManager`，保留完整身分隔離與權限控制。
- **fail-loud**：取不到 `ResourceManager` 即拋錯；ADR-0116 已退役獨立 MCP 模式與 `_FallbackResourceProxy`。

呼叫端不選擇運作模式，所有實作均遵循同一個整合契約。

---

## 📖 詳細 API

::: memory_server.services.manager.MemoryManager

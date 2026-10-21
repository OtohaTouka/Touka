# ADR-0030: SQLITE3_IMPORT_BAN — sqlite3 直接調用禁令

**狀態**: ✅ 已接受（時點性宣稱過時：文中 observability.py「架構違規仍存在、待開工單」的現在式宣稱已不成立——該處已重構走整合層、noqa 清零；SQLITE3_IMPORT_BAN 規則本身不變）  
**日期**: 2026-05-17  
**關鍵字**: sqlite3, async_db, 資料庫主權, 並發安全, SSOT, LaC

---

## 背景與問題

`ToukaProxy/src/utils/async_db.py` 是 ToukaProxy 的非同步資料庫 SSOT，提供：
- 非同步佇列（基於 `aiosqlite`）
- 物理鎖定（防止並發寫入競態）
- 身分感知存取（AgentIdentity 追蹤）

直接 `import sqlite3` 繞過此層，會導致：
1. **並發寫入競態**：同步 sqlite3 與非同步 aiosqlite 同時操作同一資料庫
2. **身分追蹤失效**：直接存取不經過 `AgentIdentity` 授權核驗
3. **鎖定破壞**：物理鎖定保護機制被繞過

---

## 決策

在 `scripts/lint_ssot.py` 中新增 `Sqlite3ImportRule`（Rule ID: `SQLITE3_IMPORT_BAN`）。

### 白名單（3 個授權文件）

| 文件 | 模組 | 理由 |
|---|---|---|
| `async_db.py` | ToukaProxy | ToukaProxy 的非同步 DB SSOT；唯一授權的 sqlite3 代理入口 |
| `factual.py` | ToukaMemory | ToukaMemory 自有事實庫（獨立模組 SSOT）；有自己的 DB 生命週期管理 |
| `legacy_store.py` | ToukaMemory | ToukaMemory 自有遺產庫（獨立模組 SSOT）；同上 |

ToukaMemory 是獨立服務，與 ToukaProxy 的 async_db 職責分離，其自有的 sqlite3 使用屬合法領域邊界。

### 偵測方式

AST `check_Import` + `check_ImportFrom` 鉤子，利用現有 `SSOTGuard` 分發機制。

---

## 初始違規處置

| 處置 | 文件 | 說明 |
|---|---|---|
| **noqa** | `routers/observability.py:3` | 從 ToukaProxy 路由層直接讀取 ToukaMemory SQLite；架構違規，短期以 noqa 豁免，待重構至 Memory API |
| **白名單** | `async_db.py` | ToukaProxy SSOT |
| **白名單** | `factual.py`, `legacy_store.py` | ToukaMemory SSOT |

---

## 後果

**正向影響**：
- 任何在非授權文件中 `import sqlite3` 的新增都會被 `lint_ssot.py` 攔截
- 強制開發者通過 `async_db.SessionService` 存取會話資料庫
- 靜態執法比 Code Review 更可靠

**限制**：
- `observability.py` 的架構違規仍存在（以 noqa 豁免），需另立工單重構至 Memory API
- 不偵測 `importlib.import_module("sqlite3")` 等動態匯入（已知靜態分析邊界）

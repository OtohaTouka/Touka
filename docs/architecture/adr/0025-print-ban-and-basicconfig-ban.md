# ADR-0025: 後端日誌主權規則 — PRINT_BAN / BASICCONFIG_BAN

**狀態**: ✅ 已接受  
**日期**: 2026-05-16  
**關鍵字**: 日誌主權, print, logging.basicConfig, SSOT, LaC

---

## 背景與問題

後端業務代碼存在直接呼叫 `print()` 和 `logging.basicConfig()` 的情況：

1. **`print()`**：繞過結構化日誌系統（`logging.getLogger(__name__)`），輸出無法被過濾、路由至 log file 或監控系統攔截。
2. **`logging.basicConfig()`**：散落在業務檔案中的 `logging.basicConfig()` 會覆蓋 `src/core/logging_config.py`（系統日誌 SSOT 入口）的全域設定，造成日誌行為不一致。

---

## 決策

在 `lint_ssot.py` 中新增兩條 Pluggable Rule：

1. **`PrintBanRule`**（Rule ID: `PRINT_BAN`）  
   - 偵測 `src/` 業務核心目錄下的 `print()` 直接呼叫
   - 排除 `scripts/` 目錄（CLI 工具輸出）及 `WHITELIST_FILES` 中的白名單檔案
   
2. **`BasicConfigBanRule`**（Rule ID: `BASICCONFIG_BAN`）  
   - 偵測任何非白名單檔案中的 `logging.basicConfig()` 呼叫
   - 唯一授權入口：`src/core/logging_config.py`（已加入 `WHITELIST_FILES`）

---

## 合法豁免

| 情境 | 處置 |
|---|---|
| `scripts/*.py`（guard_all、lint_ssot 等 CLI 工具）| 路徑過濾自動排除 |
| `ToukaProxy/src/core/logging_config.py`（日誌 SSOT 入口）| 加入 `WHITELIST_FILES` |
| `ToukaHeartbeat/awaken.py`（獨立容器，已在白名單）| 既有豁免維持 |
| `ToukaProxy/src/tools/grok_imagine_server.py`（MCP 伺服器必須導向 stderr）| `# noqa: BASICCONFIG_BAN` |

---

## 後果

**正向影響**：
- 後端日誌全面走結構化路徑，可被 RotatingFileHandler 和監控系統攔截
- `logging_config.py` 確立為全系統日誌初始化的唯一 SSOT 入口

**限制**：
- 使用 `print()` 儲存沙箱腳本字串（如 `evolution.py` 的 f-string 字面量）不受影響（AST 不視為函式呼叫）
- `logging.basicConfig()` 傳入變數而非字面量的形式也不受靜態偵測

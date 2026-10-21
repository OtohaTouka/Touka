# ADR-0036: Unified Round Count SSOT — 統一來回計算事實來源

**狀態**: ✅ 已接受
**日期**: 2026-05-21
**關鍵字**: 來回計算, round count, SSOT, history_manager, export-utils

---

## 背景與問題

全系統有三處獨立計算「對話則數」，採用不同口徑：

| 位置 | 舊計算方式 | 問題 |
|------|-----------|------|
| `async_db.py` `round_count` | `len([m if role=='user'])` | 僅算 user 訊息，末尾進行中訊息多算 |
| `legacy_store.py` `message_count` | `SELECT COUNT(*) FROM messages` | 算所有訊息（user+assistant=2） |
| `export-utils.ts` `keepCount` | `linearMessages.length` | 算所有節點（user+assistant=2） |

三處口徑不一致，導致「留 30 則」在不同語境下意義不同，增加使用者困惑。

---

## 決策

### 統一定義

**1 round = 1 個完整的 user+assistant pair**

規則：
- 跳過 system/nudge 等非對話角色
- 末尾沒有 assistant 配對的 user 訊息**不計入**（進行中不算）

### SSOT 函式

新增 `count_rounds_from_linear(messages)` 至 `ToukaProxy/src/utils/history_manager.py`：

```python
def count_rounds_from_linear(messages: list[dict[str, Any]]) -> int:
    """計算線性訊息序列中完整的來回數（全系統 SSOT）。"""
    rounds = 0
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "user":
            j = i + 1
            while j < len(messages) and messages[j].get("role") not in ("user", "assistant"):
                j += 1
            if j < len(messages) and messages[j].get("role") == "assistant":
                rounds += 1
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return rounds
```

### 各處修改

| 位置 | 修改前 | 修改後 |
|------|--------|--------|
| `async_db.py` | `len([m if role=='user'])` | `count_rounds_from_linear(path)` |
| `sliding_window_service.py` | 自有完整實作 | 委派至 `count_rounds_from_linear` |
| `legacy_store.py` | `COUNT(*) FROM messages` | `COUNT(*) FROM messages WHERE role='user'` |
| `export-utils.ts` | `linearMessages.length <= keepCount` | `countRounds(messages) <= keepCount` |

`legacy_store.py` 使用 `WHERE role='user'` 作為 SQLite 的近似值（完整 pair 計算在 SQL 層過於複雜，且誤差最多 ±1）。

### TypeScript 對等函式

`ToukaChat/src/lib/export-utils.ts` 新增 `countRounds()` 和 `splitByRounds()`，邏輯與 Python 版完全對應。

---

## 影響

- **`keepCount=30`** 的語意由「30 個訊息節點」改為「30 個來回」（實際保留訊息數約為 60）
- **`message_count`** 欄位名稱不變，但值語意改為來回數（不破壞前端讀取）
- **`round_count`** 在 session 列表中現在更準確（不計末尾進行中的訊息）
- `SlidingWindowService.count_rounds()` 保留公開介面，委派至 SSOT，不破壞任何呼叫方

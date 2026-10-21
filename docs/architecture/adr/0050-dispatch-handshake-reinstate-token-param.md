# ADR-0050: ToukaDispatch dispatch_handshake 恢復 dispatch_token 參數（認證優先）

**狀態**：✅ 已接受（取代 ADR-0048）  
**日期**：2026-05-26  
**影響範圍**：ToukaDispatch  
**關鍵字**：Authentication, dispatch_token, compare_digest, ADR-0048 reversal, parameter-level auth

---

## 背景

### 問題描述

ADR-0048 為防止 `DISPATCH_TOKEN` 洩漏至 AI conversation history，將 `dispatch_handshake`
的 `dispatch_token` 參數移除，改為 `_is_dispatch_token_configured()`——僅確認環境變數已設定
即核發 session token。

此設計造成 **dispatch_handshake 完全無認證**：任何能連上 Port 8083 的呼叫方，無需知道
`DISPATCH_TOKEN` 密鑰，即可免憑據取得合法 session token（Code Review Issue #1）。

### 根本原因

ADR-0048 的取捨優先順序錯誤：「密鑰在 AI context 中出現一次」的輕量風險，被當作比
「零認證」更嚴重的問題處理。

---

## 決策

恢復 ADR-0047 原本設計的 `dispatch_token` 參數，搭配 `secrets.compare_digest` 進行
timing-safe 驗證。明確接受「DISPATCH_TOKEN 出現在 AI context 一次」的輕量風險。

### 取捨確認

| 面向 | 取捨結果 |
|------|---------|
| DISPATCH_TOKEN 在 AI context | 每次 handshake 出現一次（已知、已接受） |
| 後續操作密鑰曝露 | ✅ 無（使用 session token，不重複傳遞密鑰） |
| 未授權存取 | ✅ 阻擋（`secrets.compare_digest` 驗證） |
| 本機部署可接受性 | ✅ DISPATCH_TOKEN 為本機密鑰，context 洩漏影響範圍有限 |

---

## 實作細節

### `ToukaDispatch/server.py` 修改

**移除**：
- `_is_dispatch_token_configured()` 函式

**新增**：
- `import secrets`（恢復 ADR-0048 曾移除的匯入）
- `_verify_dispatch_token(provided: str) -> bool`：

```python
def _verify_dispatch_token(provided: str) -> bool:
    expected = os.environ.get("DISPATCH_TOKEN", "").strip()  # noqa: ENV_SSOT_VIOLATION
    if not expected or not provided.strip():
        return False
    return secrets.compare_digest(provided.strip(), expected)
```

**修改 `dispatch_handshake` 簽名**：

```python
# Before (ADR-0048 — 零認證):
async def dispatch_handshake(caller_name: str, caller_version: str = "") -> str:

# After (ADR-0050 — parameter-level auth):
async def dispatch_handshake(
    dispatch_token: str,
    caller_name: str,
    caller_version: str = "",
) -> str:
```

---

## NOQA 影響

| 規則 | 變化 | 原因 |
|------|------|------|
| `ENV_SSOT_VIOLATION` | 0 | 移除 `_is_dispatch_token_configured` 1 個，新增 `_verify_dispatch_token` 1 個，淨差為 0 |
| 全系統合計 | 無變化 | |

---

## 安全性分析

### 修復後的威脅模型

| 攻擊向量 | 修復前（ADR-0048）| 修復後（ADR-0050）|
|---------|-----------------|-----------------|
| 免憑據取得 session token | ❌ 任何連接者皆可 | ✅ 需知道 DISPATCH_TOKEN |
| AI context 中出現 DISPATCH_TOKEN | ✅ 不出現 | ⚠️ handshake 時出現一次（已接受） |
| 後續操作密鑰曝露 | ✅ session token 短命 | ✅ session token 短命（不變） |
| Timing attack | N/A（無驗證） | ✅ `secrets.compare_digest` 防護 |

---

## 測試覆蓋

| 測試檔案 | 案例 | 涵蓋範圍 |
|---------|------|---------|
| `test_dispatch_session.py` | +2 新增 | 錯誤 token 被拒、空字串 token 被拒 |
| `test_dispatch_server_tools.py` | 1 翻轉 | AST 確認 `dispatch_handshake` 含 `dispatch_token` 參數 |

---

## 後果

**正面**：
- 恢復 parameter-level auth，消除零認證缺陷（Code Review Issue #1）
- `secrets.compare_digest` 防止 timing attack
- session token 機制確保後續操作不重複傳遞密鑰

**負面**：
- DISPATCH_TOKEN 在每次 handshake 的 MCP call parameters 中出現一次（已知取捨）

---

## 參考

- [ADR-0047](0047-dispatch-session-token-handshake.md) — Session Token Handshake（Vuln 1/2 修復）
- [ADR-0048](0048-dispatch-handshake-no-token-param.md) — 已廢棄，本 ADR 取代
- [SECURITY.md D4](../../../SECURITY.md) — 弱點登記（dispatch_handshake auth bypass 修復）

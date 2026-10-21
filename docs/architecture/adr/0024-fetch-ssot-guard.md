# ADR-0024: Fetch SSOT Guard — 前端 HTTP 客戶端主權

**狀態**: ✅ 已接受  
**日期**: 2026-05-16  
**關鍵字**: SSOT, fetch, fetchTouka, 靜態執法, LaC

---

## 背景與問題

前端代碼存在直接調用瀏覽器原生 `fetch()` API 的情況。這繞過了 `fetchTouka()`（定義於 `src/lib/connections.ts`）所提供的：

1. **Auth Header 自動注入**：從 `localStorage` 讀取 JWT 並設定 `Authorization: Bearer ...`
2. **Cookie 透傳**：強制 `credentials: 'include'` 確保 Refresh Token Cookie 被正確發送
3. **LDH 自動領卡**：讀取 `X-New-Auth-Token` 回應標頭並自動更新 Token

若開發者直接調用 `fetch()`，上述三個安全機制全部失效，導致 Session 主權漏洞（ADR-0019）與 LDH 握手鏈路斷裂（ADR-0008）。

---

## 決策

1. 建立靜態守衛 `ToukaChat/scripts/guard_fetch.ts`，掃描 `src/hooks/**`、`src/lib/**`、`src/app/**`、`src/contexts/**` 中所有直接的 `fetch()` 調用。

2. 合法的架構特殊案例（`fetchTouka` 核心定義、多供應商 SSE 串流、外部 URL 入參）使用行級 `// noqa: FETCH_SSOT` 精確豁免，並登記於 `NOQA_REGISTRY.md`。

3. 整合至 `guard_all.py` 強制執法。

4. `src/components/**` 層的 `fetch()` 偵測已由 `guard_hooks.ts` 覆蓋（檢查 DIP 違規），兩者職責互補不重疊。

---

## 掃描範圍

| 路徑 | 覆蓋 Guard |
|---|---|
| `src/components/**` | `guard_hooks.ts`（DIP：組件不應直接發 API） |
| `src/hooks/**` | `guard_fetch.ts`（Fetch SSOT：必須使用 fetchTouka） |
| `src/lib/**` | `guard_fetch.ts` |
| `src/app/**` | `guard_fetch.ts` |
| `src/contexts/**` | `guard_fetch.ts` |

---

## 豁免原則

使用行級 `// noqa: FETCH_SSOT` 豁免，禁止全行或全檔禁用。每個豁免須在 `NOQA_REGISTRY.md` 補充一列說明。

合法豁免情境：
- `fetchTouka` 本身的核心 `fetch()` 實作
- `fetchChatStream` 的多供應商 SSE 串流（Ollama、OpenAI、xAI 等端點格式各異）
- 接受完整外部 URL 入參的 hook（呼叫方提供非 Touka 端點）
- 無需 Auth 的 fire-and-forget 探針

---

## 後果

**正向影響**：
- Auth 主權完整覆蓋，LDH 握手鏈路保證連貫
- 所有 API 請求統一經過 `fetchTouka` 審計點
- 靜態執法取代人工審查

**負向影響**：
- `fetchTouka` 的 `(proxyUrl, path, options)` 簽名與原生 `(url, options)` 不同，重構已有的 `fetch()` 呼叫需小心處理路徑拼接

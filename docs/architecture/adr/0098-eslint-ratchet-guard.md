# ADR-0098: ESLint Ratchet Guard #35 — error 級違規「只降不升」

**狀態**：✅ 已接受
**日期**：2026-06-17
**影響範圍**：scripts/（守衛）、ToukaChat（ESLint）
**關鍵字**：Guard #35, ESLint, ratchet, no-explicit-any, preserve-manual-memoization, baseline, 技術債, 防迴歸, 建基於 ADR-0097

---

## 背景

ADR-0097 補上「未使用匯入」缺口後，複審指出一個更大的未守護缺口：ESLint 已內建多條 **error 級**規則，但 **ESLint 從未納入 `guard_all.py`** —— 違規能無限累積而無人攔截。實測 `src/` 有 **42 個 error 級違規**溜過全部守衛：

- `@typescript-eslint/no-explicit-any` ×36（型別安全破洞）
- `react-hooks/preserve-manual-memoization` ×6（React Compiler 因手寫 memo 放棄優化的訊號）

一次修完 42 個（尤其 36 個 `any` 需補真型別）成本高、與漸進原則衝突；但放著不管則債務持續長大。

## 決策

新增第 35 道守衛：**ESLint Ratchet Guard**（`scripts/guard_eslint_ratchet.py`）。不要求「歸零」，而是**凍結現況、禁止淨增長（棘輪）**：

- 跑 `npx eslint -f json`（`src/`），統計 **per-rule** 的 error 級違規總數。
- 與基準 `scripts/eslint_baseline.json` 比對：任一規則**超過**基準（或基準未追蹤的新規則）→ FAIL；持平或下降 → PASS。
- 粒度為 **per-rule 總數**（非 per-file），對檔案間搬移（重構位移）穩健，只攔截真實淨增長。
- 下降後可跑 `--update` 收緊基準，鎖住成果（隨債務遞減而單調收緊）。

### 為何「棘輪」而非「整包 ESLint 設為守衛」
後者等於逼開發者先清完 42 個才會綠，違反漸進。棘輪讓既有債務凍結、不擴大，同時**立即攔截任何新增**（已實測：種一個 `any` → #35 由綠轉紅 → 移除後復綠）。

### `preserve-manual-memoization`（6）明確「接受並凍結」
此為 React Compiler 因手寫 memo 而放棄優化的訊號，**非 bug**；重構 hooks 風險高、收益低。決定不修，由棘輪確保其不再增加。

## 對齊原則

| 原則 | 落實方式 |
|------|---------|
| **TDD** | 先建 `tests/unit/test_eslint_ratchet_guard_registration.py`（腳本／基準／註冊三項結構測試，Red → Green）|
| **SSOT** | `scripts/eslint_baseline.json` 為違規基準唯一來源；guard_all.py 為呼叫入口 |
| **漸進** | 凍結而非歸零；債務隨後續批次遞減、基準單調收緊 |

## 後果

### 正面

- ESLint error 級違規 **只降不升**；缺口不再擴大，可長期漸進收斂。
- 已實測攔截能力；基準檔人類可讀、隨 commit 受審。
- 守衛數 34 → **35**。

### 權衡

- 粒度為 per-rule 總數：理論上可「移除一個 `any`、同時新增另一個」淨持平而不被攔（債務總量未成長，屬可接受取捨）。
- 棘輪需跑 ESLint，約增 10–20 秒 `guard_all` 時間。
- 既有 42 個違規仍在（凍結待還）；`no-explicit-any` 的漸進清理與「測試檔 `any` 以 override 豁免」留待後續批次（Tier C）。

### 建基於

- **ADR-0097**（Guard #27 擴充 noUnusedLocals）同脈絡的前端靜態品質防線；呼應 **ADR-0029**（NOQA Registry）的 doc-vs-reality 同步精神。

---

## 更新（2026-06-17）

棘輪上線同日完成 **Tier C** 債務清理：

- `@typescript-eslint/no-explicit-any` 生產碼 **36 → 0**：多數補真型別（重用既有 SSOT——`InstalledMcpConfig`/`InstalledMcpConfig[string]`/`AgentInfo`、error 物件窄化、`catch (e)` unknown 等），少數動態資料（JSON 工具 schema、`McpHubServer`↔`McpHubServerView` 視圖阻抗）以具理由 `eslint-disable` 標記。
- 測試檔 `any` 經 `eslint.config.mjs` override 豁免（Mock 屬可接受慣例）。
- 基準收緊至 **6**（僅剩 `preserve-manual-memoization`，已決議接受並凍結）。

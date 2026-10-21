# ADR-0062: Session active_skills 持久化與破壞性操作確認 SSOT

## 狀態

✅ 已接受

## 日期

2026-05-30

> 📝 **回溯補寫（2026-05-31）**：本決策與實作已於 Skill 系統標準化期間完成並有對應 TDD 測試（`tests/unit/test_session_active_skills_persist.py`、`test_confirm_toast_ssot.py`）。當時編號 0062 已配給此決策但未撰寫書面 ADR，此文件補齊紀錄。

## 背景

ADR-0059 的 `SkillSelector` 需把技能選擇寫回 session 並跨重整保留；同時前端多個設定頁仍使用原生 `confirm()` 瀏覽器對話框處理破壞性操作（刪除技能 / 模型 / 使用者 / 專案 / 排程），體驗不一致且難以在 Node/Vitest 測試。此兩項在同一批 Skill UI 工作中一併處理，共用編號 0062。

## 問題

1. `user_sessions` 沒有 `active_skills` 欄位 → 技能選擇無法持久化。
2. 持久化需有 merge 語意（部分更新不得清空既有技能），與既有 `active_mcp_servers` 一致。
3. 原生 `confirm()` 無法在 Node/Vitest 測試、視覺不一致、且散落於 5 個元件。

## 決策

### (A) active_skills 後端持久化（`utils/async_db.py`、`routers/ui_sessions.py`）

- `SessionService.upsert_session` 接受並儲存 `active_skills`；`get_session_by_id` / `get_all_sessions` 回傳（JSON parse）。
- `SessionModel`（Pydantic）新增 `active_skills: list[str] = []`。
- **Merge 語意**：不傳 `active_skills` → 保留現有值；傳 `[]` → 明確清空。與 `active_mcp_servers` 同語意，避免部分更新意外清空。

### (B) useConfirmToast SSOT（`ToukaChat/src/hooks/useConfirmToast.ts` + 5 元件）

新增 `useConfirmToast` hook（以 `toast.warning` 給破壞性操作適當視覺警示），`SkillsTab` / `ModelsTab` / `UsersTab` / `ProjectsView` / `HeartbeatScheduler` 全部以 `confirmToast` 取代原生 `confirm()`。

## 設計決策

### 為何 merge 語意對齊 `active_mcp_servers`

`upsert_session` 是部分更新入口，常只更新 title 或 messages。若每次都把未傳的 `active_skills` 視為清空，會在無關更新時意外抹除使用者的技能選擇。沿用 `active_mcp_servers` 既有的「省略=保留、空陣列=清空」語意，行為一致、可預期。

### 為何把 confirm 上移為 hook

破壞性操作確認屬於 UI 互動關注點，下沉到資料層 hook 會違反 SRP（ADR-0043 的 UI/Hook 解耦精神）。`useConfirmToast` 讓確認邏輯集中、可測試、視覺統一。

## 後果

### 正面

- 技能選擇跨重整 / 分頁持久（per-session SSOT，支撐 ADR-0059）。
- 破壞性操作確認統一、可在 Node/Vitest 測試、視覺一致。

### 負面

- (A) 與 (B) 主題相異，僅因同批 Skill UI 工作一併處理而共用編號 0062。

## 相關 ADR

- [ADR-0059](0059-skill-header-injection-protocol.md) — `SkillSelector` 透過 `syncSession` 寫入本持久化層
- [ADR-0043](0043-react-hooks-stability-decoupling.md) — UI / 資料層 Hook 解耦（useConfirmToast 上移的依據）
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — Skill 系統標準遷移

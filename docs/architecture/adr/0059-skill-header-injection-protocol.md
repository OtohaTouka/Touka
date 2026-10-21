# ADR-0059: x-touka-skills Header 注入協定與前端技能選擇

## 狀態

✅ 已接受

## 日期

2026-05-30

> 📝 **回溯補寫（2026-05-31）**：本決策與實作已於 Skill 系統標準化期間（ADR-0066 前驅）完成並有對應 TDD 測試（`tests/unit/test_skill_header_integration.py`）。當時編號 0059 已配給此決策但未撰寫書面 ADR，此文件補齊紀錄。

## 背景

Touka 的技能（Skill）需要「逐對話（per-session）」啟用：使用者在某個對話選擇要啟用哪些技能，後端據此注入對應的 system_prompt、工具與 MCP 連線。當時缺少一條從「前端選擇」到「後端感知」的傳遞鏈路。

## 問題

1. 前端如何告訴後端「這個對話啟用了哪些技能」？
2. 技能選擇必須是 per-session 狀態且能持久化（重整後仍在），不能只是元件 local state。
3. 前端所有 HTTP 必須走 `fetchTouka()`（FETCH_SSOT，ADR-0024），技能 API 不得例外。

## 決策

建立 `x-touka-skills` 請求標頭作為前後端技能傳遞協定：

- **`ChatOptions.active_skills?: string[]`**（`ToukaChat/src/lib/connections.ts`）：對話請求選項新增技能清單欄位。
- **`x-touka-skills` 標頭**：`connections.ts` 在 `active_skills` 非空時，以逗號分隔組成 `x-touka-skills` 標頭送出。
- **`useChatStream.ts`**：從 `session.active_skills` 讀取技能清單並傳入 `fetchChatStream`。
- **`SkillSelector.tsx`**（新元件）：以 `fetchTouka()` 呼叫 `GET /v1/skills/list` 取得可用技能；使用者勾選後透過 `syncSession` 寫回 `session.active_skills`（SSOT，非 local-only state）。
- **`ChatArea.tsx`**：引入並渲染 `<SkillSelector />`。

後端由 `SkillLayer` 讀取 `x-touka-skills` 標頭（見 ADR-0060/0061），下一輪請求即據此注入技能。

## 設計決策

### 為何用 Header 而非 body 欄位？

技能清單屬於「請求中介資料」，由 middleware（`SkillLayer`）在進入業務邏輯前消費。放在標頭讓 pipeline 各層（Skill/Tool）能在不解析 body 的情況下取得，且與 `x-touka-soul-id` 等既有身分標頭風格一致。

### 為何寫回 session 而非元件 state？

技能選擇必須跨重整、跨分頁持久（per-session SSOT）。`SkillSelector` 透過 `syncSession` 持久化至後端 `user_sessions.active_skills`（見 ADR-0062），而非短命的 React local state。

### FETCH_SSOT 合規

`SkillSelector` 嚴禁裸 `fetch()`，一律經 `fetchTouka()`（ADR-0024），由 `guard_fetch.ts` 靜態執法。

## 後果

### 正面

- 前端有了標準的「逐對話技能選擇」UI 與傳遞協定。
- 完全遵循 FETCH_SSOT（`fetchTouka`）與 session SSOT。

### 負面

- 技能清單以逗號分隔字串放在標頭，技術上限制技能 ID 不得含逗號（kebab-case 識別碼不受影響）。

## 相關 ADR

- [ADR-0060](0060-skill-layer-rbac-tool-isolation.md) — SkillLayer 讀取本標頭並做 RBAC 過濾
- [ADR-0062](0062-session-active-skills-persistence.md) — `active_skills` 的後端持久化（本協定的儲存層）
- [ADR-0024](0024-fetch-ssot-guard.md) — FETCH_SSOT（`SkillSelector` 須用 `fetchTouka`）
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — Skill 系統標準遷移（消費本協定的技能格式）

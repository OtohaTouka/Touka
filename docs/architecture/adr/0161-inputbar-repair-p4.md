# ADR-0161: 輸入列修復批 P4——deepResearch 逐則／技能選單 is_system 遼清／MCP 動態掛載 toggle／防回歸

## 狀態

✅ 已接受（四功能區唯讀調查 §inputbar 的 P4 項，維護者 2026-07-15 決定：deepResearch=逐則切走歸零、技能選單=完整遼清、防回歸=四項全納入）

## 日期

2026-07-15

## 背景

四功能區調查（07-15）盤點聊天輸入列六控制項（上傳／截圖／HITL AuthSwitcher／技能選單／研究按鈕／模型選擇器），確認四者接線正常、技能選單與研究按鈕有問題，並指出此區「常在修 bug／重構後壞掉」的結構性原因＝宿主 `ChatArea.tsx` 零測試。三路精確化調查（含讀 ADR-0087 原文）後 owner 逐項拍板。

## 決策與實作

### deepResearch 逐則（Q1＝切走歸零）

`ChatArea.toggleDeepResearch` 移除 `syncSession` 死碼——後端 `SessionModel` 無 `deep_research` 欄、POST 被 Pydantic 靜默丟棄從未落盤（切 session／重整歸零的根因；與已修的 auth_level H1 同 bug 類，但方向相反：auth_level 該持久卻漏接、deepResearch 不該持久卻半做）。ADR-0087 明文選逐則（「顯式切換避免忘了關→之後每則靜默觸發昂貴多步研究」），故移除死碼＝零風險且讓程式碼與 spec 一致；送出當次仍正確帶旗標（connections.ts body.deep_research，逐則語意本就完整）。ADR-0087 內文「per-session」用語同步釐清為「記憶體內作用域、語意逐則」。

### 技能選單完整遼清（Q2）＋MCP 動態掛載 toggle

- **is_system 過濾（SSOT）**：`GET /v1/skills/list` 加 `include_system` 參數（預設 True＝設定頁等既有消費者零破壞），聊天 `SkillSelector` 傳 `include_system=false`＋前端 `.filter(!is_system)` 縱深——三個系統技能（mcp-dispatcher／skill-dispatcher／deep-research-coordinator）自技能選單移出。研究按鈕成深研唯一入口（其 RBAC-bypass 是刻意設計、有測試背書；選單路徑受 can_use_skill 限制、對 Base 不可用）。
- **MCP 動態掛載 toggle**：`system-mcp-dispatcher` 的勾選原是「MCP 動態掛載模式」（漸進揭露＋connect_mcp_server 廣告）的**唯一互動啟用入口**——直接過濾會砍功能。故新增輸入列「MCP」toggle（Plug 圖示，逐則旗標同 deepResearch）→ `body.mcp_dispatcher` → `proxy.py ctx["mcp_dispatcher"]` → `tool.py is_dispatcher_active = (…active_skills…) or ctx.get("mcp_dispatcher")`。**保留** active_skills 舊路徑向後相容（既存 session 曾勾者仍生效）。
- **skill-dispatcher 雙重注入守衛**（no-decision）：SkillLayer 從 `requested_ids` 剔除手動勾的 `system-skill-dispatcher`（其 body 由 has_dispatcher 路徑的 dispatcher_body 統一注入；手動路徑會於 apply_skills_to_request 再注一次＝同段提示詞雙注）。**不剔** `system-mcp-dispatcher`（它是啟用旗標，需保留進 active_skills）。

### 無爭議小修

截圖失敗不再靜默：`handleScreenshot` 的 API 缺席→toast.warning、使用者拒絕（NotAllowedError）不擾、其餘擷取錯誤→toast.error＋logSSOTError（i18n 兩語系）。

### 防回歸（Q3＝全選）

- **ChatArea 互動測試**（此區常壞的結構性防護）：六控制項渲染＋deepResearch toggle **純記憶體回歸鎖**（mock 的 useSessionList 刻意不提供 syncSession，若 ChatArea 復用死碼即 crash）＋mcpDispatcher toggle＋截圖 prop。
- **Session 欄位 parity 守衛**（分類台帳）：前端 Session 每欄位須分類（CORE/PERSISTED/READ_ONLY/TRANSIENT，未分類即紅）；PERSISTED＋CORE 必在後端 SessionModel（漏接＝POST 靜默丟棄，第一天就抓 deepResearch 這類）；TRANSIENT（deepResearch/mcpDispatcher/incognito）必**不在** SessionModel（逐則語意鎖：偷偷加後端欄位＝變持久化，即紅提醒違背 ADR-0087/0161）。
- **AuthSwitcher 前端持久化測試**：ChatProvider `handleSetAuthLevel` 更新 session.auth_level＋syncSession 帶正確等級；切 session 從 sess.auth_level 還原——補齊 ADR-0143 H1 前端半段（後端 test_session_auth_level_persistence 已鎖 DB 端）。

## 後果

- 深研旗標語意與 spec 一致（逐則、切走歸零）；技能選單不再外洩系統技能、MCP 動態掛載改由專屬 toggle 啟用（is_system 徹底移出使用者可勾面）；截圖失敗有回饋；ChatArea/Session parity/AuthSwitcher 前端半段獲回歸防護。
- **openapi 變更**：OpenAIChatRequest/AnthropicRequest 加 `mcp_dispatcher: bool=False`、get_skills 加 include_system 參數；api.ts 隨之重生。
- **驗收**：後端 pytest **3550 passed**（新 test_p4_inputbar 5＋既有 skill/tool/deep_research 回歸）／前端 vitest **1070 passed**（新 ChatArea 5＋Session parity 3＋ChatProvider auth_level 2；紅→綠 stash 驗證前端 URL/toggle）／guard_all **41/41**（含 #33 i18n）／lint_ssot clean／ruff 基線持平／tsc 零新增（44≤45）。

## 相關

- [ADR-0087](0087-deep-research-mode.md)（深研模式；本案釐清其逐則語意、移死碼）
- [ADR-0143](0143-hitl-unification.md)（HITL 統一 H1；本案補其 auth_level 前端半段測試）
- [ADR-0148](0148-skill-advertising-rbac-and-install-approval.md)（技能廣告 RBAC；is_system 過濾與其廣告面＝授權面精神一致）
- 四功能區調查 P1（[ADR-0158](0158-soul-creation-auto-attribution.md)）/P2（[ADR-0159](0159-providers-auth-self-heal-parity.md)）/P3（[ADR-0160](0160-auth-seam-hardening-p3.md)）＝本調查全數收官

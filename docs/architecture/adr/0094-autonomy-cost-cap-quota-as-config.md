# ADR-0094: 自主成本上限與每日配額納為一級設定（修復永遠失效的成本煞車）

**狀態 (Status):** ✅ 已接受
**日期 (Date):** 2026-06-15
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

設定漂移稽核（config-drift-audit）發現一個**會實際咬人**的問題：自主行為的「累計成本上限」（`cap`）與「每日配額」（`quota.daily_max`）雖被程式讀取，卻**永遠失效**。

- `services/autonomy.py` 讀 `self.config["quota"]["daily_max"]`（行 107/131）與 `self.config["cap"]`（行 172-176，`is_over_budget`）。
- 但 `quota`/`cap` **只存在於 `_load_config` 的記憶體內 fallback dict**（僅在 `autonomy.json` 缺檔時才用）。實檔存在 → fallback 不跑 → runtime `self.config` 沒有這兩個 key → `.get(...)` 一律落回預設：每日配額永遠固定 50、`is_over_budget()` 永遠回 `False`（成本煞車形同關閉）。
- `is_over_budget` 確實接進三個 handler（claude/openai/gemini），所以是「接好了卻永遠不觸發」。
- 連 admin API 也改不了：`AutonomyConfigModel`（GET/POST `/autonomy` 的 model）**沒有 `quota`/`cap` 欄位**，`extra="ignore"` 會在 `model_validate→model_dump` 時把使用者送入的 cap/quota 整個丟棄。
- 既有測試 `test_budget_cap.py` 必須 `monkeypatch.setitem(config,"cap",...)` 才能測——正說明真實設定路徑到不了 cap。

## 決策方案 (Decision)

把 `quota` 與 `cap` 升為**一級設定**（SSOT：pydantic 模型 ＋ 出貨設定檔），讓它們真正可讀、可經 admin API 設定、可隨檔出貨：

- **模型**（`models/autonomy.py`）：新增 `QuotaConfig{daily_max=50}`、`CapConfig{enabled=False, max_total_cost_usd: float|None=None}`，並加進 `AutonomyConfigModel` 的 `quota`/`cap` 欄位。→ GET `/autonomy` 會回傳、POST 會持久化（`model_dump` 不再丟棄）。
- **設定檔**：`config/autonomy.json`（＋`.example`）補上 `quota`/`cap` 區塊，runtime `self.config` 直接讀得到。
- **預設行為不變**：`cap.enabled=false`、`max_total_cost_usd=null`、`daily_max=50`——與今日「有效行為」一致。本變更只是讓它們**變得可見、可設定且真正生效**，不會突然開始攔截既有自主流程。要啟用成本煞車：設 `cap.enabled=true` ＋ 給定 `max_total_cost_usd`。
- **刻意排除 `reset_hour`**：fallback 雖有此 key，但配額重置走「台北日界」日期比對、從不讀 `reset_hour`（本身即 inert），故不納入模型，以免再引入死設定。
- **前端**：`settings → 安全 → Autonomy Budget` 既有面板新增「成本上限」卡（開關＋USD 上限輸入），讓非技術擁有者免改 JSON 即可啟用。

## 後果與影響 (Consequences)

- 成本煞車從「永遠失效」變成「可由擁有者一鍵啟用且真正攔截」；每日配額亦可調整。
- **API 變更**：`AutonomyConfigModel` 新增兩個帶預設的選用欄位 → `openapi.json` ＋ 前端 `api.ts` 已重生（Guard #17）；向後相容（既有客戶端忽略新欄位）。
- 既有部署：autonomy.json 若無 cap/quota，runtime 仍落回相同預設（行為不變）；擁有者於 UI 儲存一次後，admin POST 會把完整 model dump（含 cap/quota）寫回檔案。
- 範圍僅 #1（成本上限/配額）。稽核其餘 autonomy 漂移（`.example` 的 `pulse_cooldown_seconds`/`enable_heartbeat` inert、`max_pulses_per_session` inert 等）留待後續優先序處理。

## Action Items
1. [x] `models/autonomy.py`：`QuotaConfig`/`CapConfig` ＋ `AutonomyConfigModel.quota`/`cap`。
2. [x] `config/autonomy.json`（＋`.example`）：補 `quota`/`cap`（預設保持 cap 關閉）。
3. [x] 重生 `openapi.json` ＋ 前端 `api.ts`（Guard #17）。
4. [x] 前端 `AutonomyBudget.tsx` 新增成本上限卡（開關＋USD）＋ i18n（zh/en）＋ 測試。
5. [x] 後端 TDD：model round-trip 保留 cap/quota、經 model_dump 設定能驅動 `is_over_budget`。
6. [x] ADR-0094 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步；guard 33/33 ＋ 測試綠。

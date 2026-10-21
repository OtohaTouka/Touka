# 提案：靈魂 CRUD 修復＋admin 步升驗證的靈魂資料管理

> **狀態：✅ 已核准並實作完成／收官（2026-07-10 決策、同日 C1–C5 全數落地，人類逐批
> commit）。** 成果見 [ADR-0142](../adr/0142-soul-crud-repair-and-admin-stepup.md)。
> 前置診斷（同日、五路並行＋逐條對抗驗證，12 CONFIRMED）鎖定根因；本計畫全部設計
> 前提均對實碼盤點驗證。以下為原始執行藍本（歷史保存）。

## 1. 一句話總結

`POST /v1/souls`（建立靈魂）在寫自己的 `soul.md` 時被 **Soul Protection 路徑監獄**
（`resource_manager.is_protected_path`，擋 `<app_data>/soul`＋`/legacy` 下一切寫刪）擋下
→ 未捕捉 `PermissionError` → HTTP 500，四個報告症狀（soul.md 存不進／brain 存不進／
存檔後不能說話／手動補檔仍不能說話）皆源於此＋衍生缺口。修復＝把 souls 路由的寫入改走
**既有的合法通道 `utils/soul_io.py`**（不弱化保護原語）；接著新增使用者要求的功能＝
**輸入 admin 密碼（步升驗證）才能編輯/刪除靈魂資料**，其中刪除端點同時解決
「建立失敗留下孤兒資料、連清都清不掉」的直接痛點。

## 2. 根因回顧（已 CONFIRMED，僅列結論）

| # | 症狀 | 根因 |
|---|------|------|
| BUG1/2/3 | soul.md／brain 存不進、存檔後不能說話 | `souls.py:53/56`（create）與 `:172/175`（update）經 `resource_manager.write_file_text/write_json` 寫入 `<app_data>/soul/<id>/`；`resource_manager.py:562-564` 對受保護路徑**無條件** raise `PermissionError`（無任何合法端點豁免）→ 500。`:45 make_dir` 先成功（make_dir 無保護檢查）→ 留下**空孤兒資料夾**，重試同名再撞 `SOUL_ALREADY_EXISTS`（`:42-43`） |
| BUG3 附帶 | 建立成功後模型選單看不到新靈魂 | `SoulStudio.tsx` 建立成功後只 `refreshAgents()+loadSouls()`，**不刷新 ModelProvider** → 新 `ToukaSoul<id>` 模型不出現在選單、選不到＝不能說話（需重整頁面） |
| BUG4 | 手動補檔仍不能說話 | 手動放檔須完整重現全部 SSOT 片段：`config.json` 的 `is_toukasoul` 旗標（`soul_service.py:108`）＋`vault/brain_settings.json` 精確大小寫鍵（`brain_persistence.py:68`）＋可辨識供應商的模型名＋`models.json`/`agents_registry.json` 註冊——缺任一即靜默失敗（200 串流只回 error event） |
| 遮蔽原因 | 為何沒被測試抓到 | souls 路由測試把 `write_file_text/write_json` monkeypatch 成 no-op（`test_idor_soul_sandbox_security.py:106/117` 等）→ 真實落盤路徑零涵蓋 |

## 3. 盤點結論（本計畫的實碼前提，已逐一驗證）

1. **`soul_io` 是現成的合法寫入通道**：`write_soul_file`（`soul_io.py:73-98`）raw 原子寫
   （temp→replace）＋自動 `.bak` 備份＋`_safe_target` symlink/穿越監獄，**不經**
   `is_protected_path`——即 agent 自我演化（edit_soul）已在用的受認可通道。
2. **`soul_io` 可服務 create**：`_get_soul_base` → `get_vault_path("soul", identity)`
   （`resource_manager.py:156-161`）只要 `is_toukasoul(identity)`＝真就回路徑（不要求目錄
   已存在）；以 `AgentIdentity(agent_id=X, agent_role="Owner")` 呼叫（update_soul `:148`
   已是此形態）走 Owner 捷徑（`soul_service.py:94-95`）必為真。
3. **`SoulConfig.is_toukasoul: bool = True`**（`models/souls.py:17`）——config.json 預設就帶
   旗標，修好寫入後辨識鏈（`soul_service.py:102-108`）即通，**無需 schema 變更**。
4. **刪除的逆向原語大multi已存在**：`_update_models_config(action="remove")`（souls.py:198-203）、
   `_update_agents_registry(action="remove")`（:225-226）、`brain_persistence.clear_agent`
   （:154-162，重置 Batch 6 建）。但 `remove_file/remove_dir` 同受監獄（resource_manager.py:609-633）
   → 刪 `soul/<id>`、`legacy/<id>` 需與 D1 同款特權通道；`personal_drive/<id>` 不受保護。
5. **步升驗證的原料齊備**：`auth_service.verify_password`（:146-153，SHA256+bcrypt）＋
   `get_user_by_email`；DA1 先例（config router 以 `get_current_user` 取 User 再驗角色）。
6. **無 DELETE 端點**；前端只呼叫 GET/POST `/v1/souls`（SoulStudio 詳情頁唯讀）→
   收緊 update_soul 的人類契約**不會破壞任何現有 UI 消費者**。

## 4. 設計拍板點（D1–D6，各附建議）

- **D1 寫入機制**＝souls 路由（create/update）改走 `soul_io.write_soul_file`（＋新增小
  helper `write_soul_json` 做 `json.dumps` DRY），寫入失敗（回 False）→ fail-loud 新
  `ErrorCode.SOUL_WRITE_FAILED`（不再 500）。**建議採此案**：保護原語 `is_protected_path`
  一行不動（agent/工具面照擋）、soul 檔 I/O 收斂單一 SSOT、update 免費獲得 `.bak` 備份。
  備案＝`write_file_text(..., allow_protected=True)` 參數（弱化原語、任何未來程式碼皆可傳，
  不建議）。
- **D2 步升形式**＝**每請求內嵌密碼**（請求 body 帶 `admin_password`，後端
  `get_current_user` → `user.role=="admin"` ＋ `verify_password`，失敗→新
  `ErrorCode.AUTH_REAUTH_FAILED`；成功→AUDIT warning log，沿 P27 慣例）。**建議採此案**：
  無新 token 型別（DA7 教訓）、無 TTL 狀態、天然可稽核。備案＝短效 sudo token（過度工程）。
- **D3 update_soul 契約**＝**人類呼叫者一律步升**（帶密碼才可改）；**agent 自我存取
  （`verified.agent_id == soul_id`，非人類）維持現狀**（agent 無法打密碼；自我演化另有
  edit_soul 工具面管控）。跨 agent 特權（Debugger 等）維持 DA17 is_owner 現狀。
  **建議採此案**——否則 UI 的密碼閘是紙老虎（admin JWT 直呼 API 即繞過）。
- **D4 刪除範圍（v1）**＝檔案面（`soul/<id>`＋`legacy/<id>`＋`personal_drive/<id>`，
  soul_io 新增 `purge_soul_storage(agent_id)`，逐 base 過 `validate_no_symlink_escape`
  監獄、best-effort、回報 removed/skipped）＋ SSOT 註冊面（models.json／agents_registry／
  brain_settings／各使用者 `owned_agents` 殘留／identity_map `agent_overrides` 條目）。
  **聊天 sessions 與記憶 vault 不動**（既有 per-agent 破壞性重置〔ADR-0113〕已涵蓋，
  UI 日後可互連）。**冪等**：孤兒（空目錄、零註冊）照清＝直接解使用者當前痛點，
  清完 agent_id 可重用。
- **D5 DebugAgent 不可刪**（系統除錯身分）→ 403。**建議採此案**。
- **D6 create 失敗回滾**＝create 全程 try/except，任何一步失敗→best-effort 清掉已建
  目錄再 re-raise（不再留孤兒）。**不做**「create 時自動回收既有空孤兒」（保持 create
  嚴格；孤兒交給 DELETE 清）。**建議採此案**。

## 5. 分批執行（每批：紅→綠→ruff＋lint_ssot＋guard_all 41＋pytest〔前端另 eslint/tsc/vitest〕→人類 commit）

### C1 — 修復後端（F1 紅＋F2 綠，一個 commit 邊界）
- **F1 RED**（新檔 `tests/unit/test_soul_crud_real_writes.py`，真實落盤進測試沙箱）：
  ①admin POST /v1/souls → 200＋`soul.md`/`config.json`（含 is_toukasoul）實體存在＋
  models.json 有 `ToukaSoul<id>`＋registry 有 id＋brain_settings 有鍵（現況＝500 紅）；
  ②update_soul → 200＋檔案更新＋`.bak` 產生（紅）；③create 中途失敗（monkeypatch 晚段
  step raise）→ 不留孤兒目錄、同 id 可重建（紅）；④**防退化錨**：直呼
  `resource_manager.write_file_text` 寫 soul 路徑**仍** raise PermissionError（綠，鎖住
  「保護原語未被弱化」）。同批把 `test_idor_soul_sandbox_security` 等檔的 no-op
  monkeypatch 換成沙箱真寫（授權語意不變、I/O 誠實化）。
- **F2 GREEN**：souls.py create/update 寫入改走 soul_io（D1）＋`SOUL_WRITE_FAILED`
  fail-loud＋create 回滾（D6）。errors.json＋errors.json.example 同步（Guard #18/#34）。

### C2 — 修復前端＋修復文件（F3＋F4）
- **F3**：SoulStudio 建立成功後**加刷 ModelProvider**（既有 refresh 機制／事件），新
  `ToukaSoul<id>` 立即可選；`SOUL_ALREADY_EXISTS`／`SOUL_WRITE_FAILED` 的 toast 訊息
  對映＋i18n 全語系鍵（Guard #33）；vitest。
- **F4**：[ADR-0142](../adr/0142-soul-crud-repair-and-admin-stepup.md) 起草（根因＋D1
  機制＋防退化錨）；openapi 重生（若 wire 有變）＋api.ts；HANDOVER 一行。

### C3 — 功能後端：步升驗證＋DELETE（A1，紅→綠）
- 步升 helper（`require_admin_stepup`：get_current_user → admin ＋ verify_password ＋
  AUDIT log；`AUTH_REAUTH_FAILED`）。
- **`DELETE /v1/souls/{soul_id}`**（人類 admin＋密碼）：D4 範圍冪等清除、回
  `{removed:[...], skipped:[...]}` 報告；D5 DebugAgent 403。
- **update_soul 掛步升**（D3：人類必帶密碼；agent 自我存取不變——既有測試續綠即證）。
- RED 案例：孤兒清除（使用者的原始痛點劇本：空目錄→DELETE→200→同 id 重建成功）／
  完整靈魂全面清除（逐面斷言）／密碼錯 403／非 admin 403／agent token 403／DebugAgent 403／
  人類無密碼 update 403。Guard #24（新端點掛 auth 依賴）＋openapi/api.ts 重生（Guard #17）。

### C4 — 功能前端（A2）
- SoulStudio：詳情頁解鎖**編輯模式**（persona＋config）＋**刪除鈕**；兩者共用
  **密碼確認 modal**（顯示登入中 admin 帳號、僅輸入密碼＝步升 UX）；成功後刷
  souls＋agents＋models 三 provider；失敗訊息（AUTH_REAUTH_FAILED 等）i18n 全語系；
  vitest（modal 流程＋錯誤路徑）。

### C5 — 收官文件（A3）
- [ADR-0142](../adr/0142-soul-crud-repair-and-admin-stepup.md) 定稿（修復＋步升功能
  兩節）；HANDOVER／memory；SECURITY.md 附一行步升面說明（選配）。

## 6. 驗收與風險

- **安全立場不變**：`is_protected_path` 原語零弱化（F1-④ 防退化錨釘死）；soul_io 是
  既存受認可通道；新增的刪除力僅存在於「人類 admin＋新鮮密碼＋AUDIT 記錄」之後——
  agent／工具面永遠拿不到（符合 data/ 保護家規：機器不可自動化管理，人類步升可以）。
- **行為保持面**：list_souls／聊天管線／edit_soul 工具零改動；update_soul 對 agent
  自我存取零改動（既有測試鎖）。
- **風險①** soul_io 寫入沙箱在 create 新目錄的邊角（`_safe_target` resolve 於不存在
  路徑）——F1 紅燈直接實測，若有 wrinkle 在 F2 修 soul_io 而非繞過。
- **風險②** 步升密碼經 HTTPS/本機傳輸於 body——與登入同級，不落 log（AUDIT 只記
  操作者與對象，不記密碼）。
- **風險③** 刪除範圍遺漏某 SSOT 面→殘留幽靈條目——C3 的「完整靈魂全面清除」測試逐面
  斷言（models/registry/brain/owned_agents/identity_map/三目錄）。

## 7. 明確範圍外

- 聊天 sessions／記憶 vault 的清除（既有 per-agent reset 涵蓋；UI 互連日後另提）。
- 手動放檔支援（修好正規路徑＋有 DELETE 後不再需要；ADR 記載所需 SSOT 片段清單供參）。
- 非 admin 擁有者的靈魂編輯 UI（現無此流程；日後多用戶議題）。
- 模型類型→角色硬綁定重構（使用者另案考慮中，獨立提案）。

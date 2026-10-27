# Touka 測試指南 (Testing Guide)

測試主權與沙盒規則見 [AGENTS.md](../AGENTS.md)。記憶系統為整合模式單一部署（ADR-0116）：測試一律透過 `ResourceManager` 與沙盒 fixture 存取資料，取不到即 fail-loud，不提供獨立 MCP 降級模式。

---

## 核心守則

1. **測試優先 (TDD)**：修改代碼前應先確保現有測試通過。
2. **身分匿名化**：測試 fixture 嚴禁使用真實 Agent ID。統一使用 `"Alice"`, `"Bob"`, `"Charlie"`, `"DebugAgent"`, `"AgentAlpha"` 等中性名稱。
3. **沙盒主權**：所有測試必須遵守 `conftest.py` 的沙盒邏輯，禁止繞過 `ResourceManager` 直接讀寫實體 `data/`。
4. **新端點守則**：新增的 FastAPI 路由必須帶 `Depends(get_verified_identity)` 或等效守衛，否則 Guard #24 會在 `guard_all.py` 失敗。

---

## 快速執行

```powershell
# 全量（推薦）
pytest tests/

# 單元測試
pytest tests/unit/

# 整合測試
pytest tests/integration/

# 靜態守衛 + lint（43 道關卡）
python scripts/guard_all.py
```

### 隨機次序測試 (Random-Order Testing) ⭐

後端 pytest 與前端 vitest **皆以隨機次序執行**，持續驗證測試隔離、揪出「某測試殘留全域狀態污染後續測試」的次序耦合 bug。**新測試務必不依賴執行順序**（共享狀態請於 fixture/teardown 清乾淨；後端已由 `conftest.py` 每測試重置 7 個單例）。

```powershell
# 後端：pytest/pytest-asyncio/pytest-randomly 已納入 uv.lock 的 dev group（ADR-0117），
# `uv sync --all-packages` 佈建即含 → pytest 自動打亂並印 seed
pytest tests/                              # 每次輸出 "Using --randomly-seed=N"
pytest tests/ -p no:randomly               # 暫時關閉隨機（除錯用）
pytest tests/ --randomly-seed=12345        # 用指定 seed 100% 重現某次失敗

# 前端：vitest.config.ts 已設 sequence.shuffle=true
cd ToukaChat
npm run test                               # 每次輸出 Running tests with seed "N"
npx vitest run --sequence.seed=12345       # 用指定 seed 重現
```

---

## 目錄結構

```
tests/
├── conftest.py                  # 全域沙盒 fixtures（SessionTmpDataRoot、mock_identity 等）
├── sandbox_cleanup.py           # session 沙盒收尾 helper（只用標準函式庫，conftest 注入釋放步驟；ADR-0251）
├── unit/                        # 純邏輯、Service 層、靜態稽核（不啟動 HTTP server）
├── integration/                 # 端到端流程（TestClient / AsyncClient）
├── proxy/                       # Proxy handler 主權與韌性
├── lint/                        # 靜態分析守衛的 TDD 測試
├── memory_eval/                 # 記憶長期穩定性評估：情境、規則判定、合成資料、多輪 driver、送前花費界線（離線；真模型只走人工 probe，ADR-0261、ADR-0268）
├── support/                     # 共用測試替身（隔離層，ADR-0272）
├── docker/                      # Linux 隔離測試映像、突變腳本與執行器端到端資產（ADR-0272）
└── smoke/                       # 需真實服務運行的人工冒煙腳本
```

---

## 一、單元測試 (`tests/unit/`)

### 聊天對話搜尋（ADR-0277）

以下六個新測試檔使用 conftest 沙盒、真 SQLite／MemoryManager／SessionService 與合成身分，來源觀察器只委派、不替換搜尋。

| 檔案 | 說明 |
|---|---|
| `unit/test_ui_conversations_security.py` | 精確綁定與兩側別名、拒絕非靈魂、角色／擁有者不擴權、marker／換綁、退休頻道、家候選先驗、無痕三層與不保存、偽造參數、刪除與歸檔副本、生成失敗不恢復可見。 |
| `unit/test_soul_session_reuse_security.py` | 同名重建／預佔、重置清別名且保留外靈魂家引用；建立與轉換前後降 marker、期間存檔不再可搜、第二次失敗回滾並保留原檔；前綴不分大小寫拒絕、失效正式化；真 ZIP 還原預佔列、部分失敗、無 DB 快照、降標記失敗固定碼、缺庫不建立與缺表／欄正常 migration；ZIP 覆蓋後中斷、啟動在 migration 後與 bots 前補降清標記；ui／all 讀列前拒絕及解除後對照、補降／刪標記失敗仍啟動但持續拒絕、原子寫標記失敗不啟動還原子程序；含可見正向對照。 |
| `unit/test_ui_conversations_reader.py` | 目前祖先鏈／內節點／null、壞 JSON／樹／鏈、文字白名單、助理與作者說明、完整內容與字面 LIKE／ASCII 語意、台北日界、秒／毫秒／未知時間、缺表／marker 不初始化。 |
| `unit/test_conversations_merge.py` | 來源交錯與非零 offset／末頁、完整排序鍵、不去重、legacy 異常時間、單邊失敗不回部分結果、歷史界線與預覽截斷、極大 offset 回空頁不溢位。 |
| `unit/test_conversations_sources_contract.py` | ui／legacy／all／省略／null 選來源、壞參數與 both 拒絕、欄位及分頁上限不變、驗證錯誤指出欄位、role 說明不宣稱作者。 |
| `unit/test_conversations_resource_seam.py` | 真 reader 綁定／未綁定、原 identity 與完整參數透傳、只見 SELECT 與普通交易、缺庫空頁與無效資料庫錯誤分開。 |
| `support/conversation_search.py` | 合成樹、真雙庫 fixture、Proxy 閘道與結果斷言、SQL／legacy 查詢觀察；無獨立測試。 |

### 心跳、SNS 與對話歸屬（ADR-0276）

以下五個新安全檔的所有資料都在 conftest 沙盒，使用真 SQLite／SessionService 與合成身分，provider 與平台傳輸為替身。

| 檔案 | 說明 |
|---|---|
| `unit/test_session_scope_storage_security.py` | append/upsert/CAS 交易內歸屬閘、匿名／外靈魂拒寫、UI 兩側授權與空草稿／非空換綁、家欄忽略、set_home 原子性、壞家／多家／退休家不定位或注入、保留 ID、競爭建立與損壞樹。 |
| `unit/test_session_scope_provenance_security.py` | 受管 INSERT=1、舊 schema migration=0 且冪等、換綁降級與不提升、偽造 marker／未來時間無效、公開投影隱藏及 revision、同靈魂／set_home 保留。 |
| `unit/test_heartbeat_session_scope_security.py` | 喚醒與排程執行的歸屬、independent 欄位 presence、每靈魂預設、保留 ID／家 ID 碰撞、retrigger、HTTP 403 固定碼、模型前與中間／最後保存重驗、背景失敗不追加或剪裁。 |
| `unit/test_sns_session_scope_security.py` | 每靈魂頻道與座標編碼、同擁有者 retarget／跨擁有者拒絕、群聊不存單靈魂、壞家不 fallback、assistant 保存重驗後才外送、真 Monitor 下目標靈魂與 session 固定、pipeline 不帶身分。 |
| `unit/test_session_scope_dispatch_security.py` | 人類串流准入與 marker、非人類／execution mismatch／無新靈魂權／舊 revision 拒絕、生成失敗／no_text／再換靈魂不恢復標記、舊 dispatcher、兩種工具競態與刪除不重建、剪裁前閘。 |
| `support/session_scope.py` | 沙盒 DB、合成靈魂與人類擁有關係、原列比對；不替換儲存實作、不含獨立測試。 |

既有回歸檔的調整原因如下，未減少安全斷言：

| 檔案 | 調整原因 |
|---|---|
| `integration/test_heartbeat_retrigger.py` | 改提供 strict snapshot 與完整家欄，讓真正執行歸屬閘驗證外靈魂拒絕；原人類授權與 Auto 斷言保留。 |
| `unit/test_dispatcher_resumable.py` | 擁有者競態改在真沙盒 DB 變更，驗證交易內准入會看到新列，不能只替換舊讀取入口。 |
| `unit/test_heartbeat_awaken_hardening_security.py` | 初始提示 append 替身明確回保存成功，避免新失敗檢查提前中斷消毒案例；合成憑證常數更名。 |
| `unit/test_heartbeat_modes_security.py` | 脈衝讀取替身提供 execution snapshot 與 revision，維持模式／工具政策測試的範圍。 |
| `unit/test_session_binding_security.py` | 退休 SNS 列只由沙盒 SQL 模擬舊資料；正常 SNS 改用受管每靈魂頻道並驗保存回覆。 |
| `unit/test_session_owner_attribution.py` | 既有代理對話 seed／update 明帶相同 agent identity，驗 owner 不被後寫者改動，不再藉匿名寫入建立測試前提。 |
| `unit/test_session_persistence_ordering.py` | 家對話經 set_home 建立，剪裁用綁定靈魂，保留 stale revision、公開 null 與 rich-save 競態斷言。 |
| `unit/test_sns_gateway.py` | 替身提供正式靈魂與同擁有者關係、嚴格家快照及成功 append，頻道 ID 期望改為每靈魂完整座標雜湊。 |

其餘受快照／條件提交影響的回歸檔，測試數量不變，逐檔分類如下：

| 檔案 | 分類與原因 |
|---|---|
| `unit/test_guard_native_tool_contracts.py` | setup：測試不改；基準只分類 `system.py` 的兩個保存 helper 與 MCP 啟用的可信 identity 讀取，負向變異仍須被攔。 |
| `unit/test_heartbeat_tool_schema_pinned.py` | setup：execution snapshot 替身，保留心跳工具 schema 與模式斷言。 |
| `unit/test_interjection_notes.py` | setup：心跳與家訊息改攔快照，保留插話文字及回合數斷言。 |
| `unit/test_activate_skill.py` | setup：提供 persistence snapshot／成功條件提交；檢查既有及新技能、去重、RBAC，另核對 revision 與 identity。 |
| `unit/test_soul_target_ownership_security.py` | intended change：SNS 閘道以可信靈魂身分存檔、固定新頻道 session；pipeline 仍不帶身分，保留人設、家訊息與回覆斷言。 |
| `unit/test_advertised_tool_list_security.py` | setup：脈衝改攔 execution snapshot，每次重建工具清單的斷言不變。 |
| `unit/test_sec_batch_alpha.py` | setup：並發追加改用真沙盒 SQLite；十次都成功，整棵樹及目前分支都含十筆、不丟任何內容。 |
| `unit/test_home_message_outbound_security.py` | setup：家快照提供同一棵樹，所有外送欄位、name、人格順序斷言保留。 |
| `integration/test_background_pulse.py` | setup：提供有效對話樹、execution snapshot 與成功條件提交；保留派發／保存斷言，另驗 nudge 後角色交替。 |
| `unit/test_unauthenticated_chat_security.py` | intended change：SNS 仍不需 HTTP 憑證，但 adapter 須綁靈魂，閘道固定新頻道 session；pipeline 仍不帶身分、技能為零。HTTP 未登入拒絕不變。 |
| `unit/test_continuity_not_persisted.py` | setup：改攔快照與條件提交，仍驗 continuity 不入庫、nudge 與區塊共用時間。 |
| `unit/test_auth_mode_authority_security.py` | intended change：DebugAgent 跨靈魂保存回 typed scope failure、不建列，其他角色及模式斷言不變。 |
| `unit/test_memory_sources.py` | setup：觸發節點從 execution snapshot／條件提交觀察，保留先存 nudge 再記來源的斷言。 |
| `unit/test_no_sampling_on_wire.py` | setup：心跳改攔 execution snapshot，三家實際送出內容仍不得帶取樣參數。 |
| `unit/test_user_intent_presence_security.py` | setup：retrigger 提供 execution snapshot，仍驗無人在場及最後一則人類原意。 |
| `unit/test_group_soul_only.py` | setup：提供合成靈魂護照與實際身分物件，保留 SNS 別名僅到靈魂的斷言。 |
| `unit/test_model_name_case_lookup.py` | setup：心跳改攔 execution snapshot，仍核對覆寫／主要模型與聊天共用查表。 |
| `unit/test_n12_sql_fix.py` | setup：改檢查查詢所在的 `read_home_snapshot`，固定 `IN (?, ?)` 與禁止動態拼 SQL 的斷言保留。 |

### 認證與安全

| 檔案 | 說明 |
|------|------|
| `test_auth_service.py` | AuthService 核心邏輯：JWT 簽發、Bcrypt SHA256 預雜湊、refresh token 輪換 |
| `test_auth_logout.py` | 登出流程：Cookie 撤銷、session 清理 |
| `test_wizard_password_relogin_contract.py` | ADR-0053 補記（2026-10-23）首次設定精靈的後端契約：管理員用自己的卡改自己的密碼後，舊 access 與舊 refresh cookie 都 401（ADR-0144），用新密碼登入立刻可用、新的 refresh cookie 能續期 |
| `test_password_reentry_status_security.py` ⭐ | 密碼重輸打錯回 403 `AUTH_REAUTH_FAILED`（401 會被前端續期重送、驗兩次）——`AUTH_INVALID_CREDENTIALS` 只留給登入（AST 掃 `ToukaProxy/src`）、改密碼端點目前密碼錯回 403、首次登入用初始密碼改密碼（打錯 403 且檔案與臨時旗標都還在，打對才換掉並刪檔）；重置、還原、設定匯入、清除／匯出我的資料的既有測試同步改斷言 403 |
| `test_auth_register_security.py` ⭐ | 公開註冊端點防護：強制 `normal` 角色，防止攻擊者透過 body 提升至 `admin` |
| `test_get_current_user_401.py` ⭐ | `get_current_user` 錯誤碼修復：過期/非法 token、missing sub、user not found 均回 401 `AUTH_INVALID_TOKEN`，不得回 500 |
| `test_refresh_token_grace_period.py` ⭐ | Refresh Token Rotation 並發 Grace Period（P30）：多 Tab 同 JTI 在 30 秒內不觸發撤銷；grace 後仍撤銷；非 previous_jti 的舊 token 立即撤銷 |
| `test_rate_limiter.py` | `RateLimiter` 滑動視窗純函式單元測試 |
| `test_security_hardening.py` | C1/C2/C3：JWT 密鑰自動生成、管理員密碼隨機化、`X-Agent-ID` Header fallback 移除 |
| `test_public_secret_values_security.py` ⭐ | （ADR-0251；ADR-0254 +27）：三把自動持久化金鑰拒用歷來公開過的 9 個值——對照表認得每個值（含前後空白）、比對要鍵名＋值；公開值換成新值並寫回暫存 `.env`；bootstrap 關或寫檔失敗時 fail-closed（心跳空、登入用臨時值、派工不帶 token）且暫存 `.env` 不變；程序環境是公開值、`.env` 已存安全值時沿用它、不寫檔；`.env` 存的也是公開值時不沿用；自訂值原樣回傳；公開心跳 token 與用公開登入金鑰簽的心跳 JWT 都叫不醒；派工刪掉繼承來的公開值；設定精靈對公開值與空值顯示還是預設值；ADR-0254 H20：設定精靈 `apply` 收到兩把金鑰的公開值（含前後空白）時丟錯、訊息不帶值、暫存 `.env` 一個字都不改（另一欄是合法值也不寫），`POST /v1/admin/setup/apply` 回 422 `VALIDATION_ERROR`，非公開值照舊寫入（服務與端點各一）。所有 `.env` 都在 `tmp_path` |
| `test_custom_model_rename_security.py` ⭐ | （ADR-0254 H12）：自訂模型改名來源不見 404 `MODEL_NOT_FOUND`、目標已被占用 409 `MODEL_ALREADY_EXISTS`、搬移出錯 500，三種都不搬擁有權、不改 `config.json`；只差大小寫的改名真的改名並搬擁有權；一般改名的資料夾、`config.json` 的 id、擁有權一起改（釘住）。2026-10-25 補記：config 讀不到、寫不進去、出錯或擁有權寫不進去時資料夾與 config 搬回，搬回失敗、出錯或 id 改不回去有 CRITICAL 的 AUDIT；查看、存檔、改名、刪除用只差大小寫的 ID 都不動；沒有 `config.json` 的資料夾（個人空間）查看、改名、刪除都 404；存檔寫不進去 500、不認領、新建時清掉寫了一半的資料夾；和靈魂同名的舊資料夾不能改名、刪除不動擁有權；新舊 ID 相同不寫；中文新 ID；DebugAgent、DispatchAgent 不能用；config 不是 JSON 物件照樣改名；非人類呼叫者也搬；新建時認領 ID；刪除從所有人的清單拿掉；擁有權換名、拿掉殘留、認領、全部拿掉用沙盒裡真的 users.json。路由測試在 conftest 沙盒，擁有清單寫入換成 MagicMock |
| `test_unauthenticated_chat_security.py` ⭐ | **ADR-0245**：沒登入不能聊天——外部無卡（4 個聊天路徑）與系統認領後的本機無卡（X-Agent-ID 冒稱靈魂）一律 401 `AUTH_INVALID_TOKEN`，人設、家對話、上游呼叫都沒被觸及；首裝前本機領卡仍發 DebugAgent；SNS 內部路徑不經 HTTP 閘、沒有身分照常回覆 |
| `test_soul_target_ownership_security.py` ⭐ | **ADR-0245 修補**：非擁有者不能指定別人的靈魂——normal 使用者（沒有靈魂或只擁有別的靈魂）、經 `/auth/register` 剛註冊登入的帳號（真 token）、別的靈魂的代理 token、擁有者切進自己的靈魂後再指定別的靈魂，在 `/v1/messages` 與 `/v1/chat/completions` 都 403 `AUTH_PERMISSION_DENIED`，人設、家對話、上游都沒碰（`model` 與 `X-Touka-Soul-ID` 兩種指定法）；擁有者、admin、靈魂自己、DebugAgent、admin 代打照舊；子代理拿靈魂當 `sub_model` 組裝時 403；群聊每位成員都檢查，一位不行整輪不派發，靈魂代理只能點自己；出貨範本 HeartbeatTester 不能進群聊；SNS 內部路徑（沒有身分）指定靈魂照舊回覆 |
| `test_soul_target_bypass_security.py` ⭐ | **ADR-0245 修補・再審**：靈魂目標檢查的繞法——標頭指自己的靈魂＋model 指別人同名的自訂模型、`../soul/<靈魂>` 與 `..\soul\<靈魂>` 穿越，都只套自己的靈魂；`custom_soul` 路徑不離開 `custom/`、CustomModelLayer 不把靈魂目錄當自訂模型；SoulLayer 注入前再驗；建立、改名自訂模型拒絕靈魂名稱（409，含 `ToukaSoul`／`toukasoul-` 前綴與 `@` 寫法）與含 `\`、`..` 的 ID（422），靈魂設定不被改寫；ToukaChat 靈魂對話裡的子代理不繼承父靈魂與 session 標頭（照常組裝、不清父 session 中斷旗標、不改父 ctx），拿靈魂當 sub_model 仍 403，沒指定模型時（ToukaChat 靈魂對話、心跳）用父靈魂的大腦；計分卡對別人的靈魂（含 `@`、`ToukaSoul-` 寫法）403，自己的靈魂與一般模型照跑 |
| `test_session_binding_security.py` ⭐ | **ADR-0245 修補・session 歸屬**：擁有 Bob 的人帶 Alice 的 session_id（一般與可預測的 `sns_telegram_*` 形式、兩個聊天端點）→ 不注入 Alice 的前情摘要、回覆不寫進 Alice 的 session、綁定不被改成 Bob；Bob 自己的 session 照注入照寫回；舊前綴 `ToukaSoul<Bob>` 綁定正規化後照舊；session 綁在 Alice 仍要呼叫者能驅動 Alice（別的靈魂代理 False、Alice 本身與內部路徑 True）；SNS 綁 Bob 的流程照舊回覆並寫回；normal 使用者用一般模型帶別人的 session_id → 回覆不寫進去、不觸發剪裁，自己的一般模型對話照舊寫回並觸發剪裁檢查 |
| `test_session_binding_attack_security.py` ⭐ | **ADR-0245 修補・綁定改寫**：Owner 代理對沒有人類擁有者的別人一般模型 session，`can_access_session` 不放行、`_persist_assistant_turn`（預設開啟存取檢查）不寫回、不建 buffer（attack_a／attack_c）；`connect_mcp_server`／`activate_skill` 對綁定非本人的 session 拒絕、不改綁定（attack_b），自己的 session 照舊成功；`set_home` 把別人 session 設成自己 agent 的家 → 403，自己的照舊（attack_d）；被 403 的靈魂請求不清中斷旗標、擁有者放行後有清；set_home 綁定對照表（7 呼叫者×5 session 狀態×2 家對話狀態，寫死 200／403，被拒且已有家對話時另驗原家對話不被動）＋兩個非正式標籤（前綴／ToukaGroup）探針 403；正向回歸：靈魂/心跳寫自己 session、心跳家對話工具寫入過守衛、`GET /v1/ui/sessions` 對擁有者人類／admin／admin 代打／X-Agent-ID 代打成自己的靈魂各回完全相等的 id 集合、明細自己 200 別人 403 |
| `test_interjection_endpoint_security.py` ⭐ | **ADR-0249 第 4a 段・插話端點**：三端點登記在 `/v1` 與根路徑；請求只有 text、urgent，多送核准／模式／工具／角色欄位 422；越權（非發起者、admin 非發起者、失去對話擁有權 → 403）；代理身分（非 admin 的 X-Agent-ID 代打、靈魂 token、DebugAgent 即使是自己的串流也 403，admin 與 admin 代打可以）；跨 stream（舊 stream_id 三方法都 404、新串流看不到舊插話、別的對話的 id 404、已結束的串流不能再加入、缺 stream_id 422）；超量（第 11 則 429、收回後可再加、4,001 字 422）；已交給模型的收回 409；無痕只在記憶體、不碰 DB、串流結束清空 |
| `test_interjection_queue.py` ⭐ | **ADR-0249 第 4a 段・插話佇列**：Protocol 成員完整、上限常數（10 則／4,000 字／600 秒）；drain 原子改 pending(step) 且不重複取出、狀態只能照 queued→pending→delivered／undelivered 走、只能收回 queued；每串流上限（已送達也算）與長度、空白；(session, stream) 鍵隔離；clear 一個串流或整個對話；TTL 過期改未送達、閒置清掉但保留 pending；串流完成／錯誤／停止／換代／退休／逾時收割都清空該串流，開新串流清掉同對話殘留，被拒的取代不清擁有者的佇列，預設單例接線 |
| `test_interjection_sse_contract.py` ⭐ | **ADR-0249 第 4a／4b 段・插話 SSE 契約**：後端事件名稱常數、`interjection_delivered`／`interjection_undelivered` 產生函式的欄位，以及前端 `lib/chat/interjections.ts` 的同名常數與欄位字串一致；前端三個事件常數的值（純文字讀出）等於後端常數 |
| `test_interjection_interrupt.py` ⭐ | **ADR-0249 第 4b 段・Ctrl+Enter 打斷**：第一個 chunk 前、串流中途打斷（含無痕）丟掉半截、不算一步；本輪結果到了之後改等工具邊界；開始前搶先不送那一輪、只在 UI、重試不重複附加；訊號空轉防護；pending 累加後 4a 流程不變；收回與打斷競態；連續兩則 urgent 只還原一次；心跳與子代理不打斷；loopback 驗上游連線關閉；Claude 打斷那一輪的計費（有回報／部分／沒回報） |
| `test_interjection_urgent_signal.py` ⭐ | **ADR-0249 第 4b 段・urgent 訊號**：有 urgent 排隊才 set、取出與收回重設；過期重設；清空串流重設且下一個串流從未 set 開始；打斷事件常數與形狀 |
| `test_interjection_drain.py` ⭐ | **ADR-0249 第 4a 段・取出點與送達**：第 N 個工具步驟取出、下一輪送出成功後才 delivered；下一輪重試全部失敗 → undelivered；純文字結束、預算閘、收尾輪時沒取出的回成 undelivered；取出後按停止 → undelivered；urgent 讓下一輪 `tool_choice=none`、送出後還原，一般插話不動 tool_choice；非 UI、心跳、子代理不取出；佇列空時不送任何事件；只有 active 串流才開佇列 |
| `test_interjection_adapters.py` ⭐ | **ADR-0249 第 4a／4b 段・四家插話格式**：Claude 規則表 `mid_conversation_system=True` 加 system 訊息、其他三種型號在最後一個 tool_result 後加 text block；OpenAI 相容在 tool 訊息後加 user 訊息；Gemini 在 functionResponse 同一個 content 加 text part；Responses 在 function_call_output 後加 user input item；`hold_tool_choice` 設成 none 並還原原值；打斷後接在原本提問之後，四家各自的形狀（4b） |
| `test_interjection_persistence.py` ⭐ | **ADR-0249 第 4a 段・插話存檔**：這個串流 delivered 的插話以 API `Interjection` 形狀存進助理節點 `interjections[]`（queued／pending／undelivered／收回的與別的串流不存）；沒有已送達的插話時節點欄位不變；pump 在串流結束清空佇列之前存檔；停止、錯誤不存；無痕不碰 DB |
| `test_interjection_notes.py` ⭐ | **ADR-0249 第 4a 段・伺服器端重組標註**：範本與前端 `INTERJECTION_NOTE_TEMPLATE` 純文字比對相同；照步數排、只取已送達有文字的；只改助理節點、不改原節點；回合數不變；心跳脈衝訊息、`get_home_messages`、摘要輸入都帶標註 |
| `test_c4_c5_c6_c7_security.py` | C4/C5/C6/C7：路徑穿越、Shell metachar 注入（升級成 CONFIRM 的日誌不含指令的任何片段，禁令 23 同類）、演化工具名稱只經 MCP 協定傳遞、不進程式碼或 argv、Webhook 驗簽框架 |
| `test_terminal_auth.py` | C8：終端機 `approve`/`reject` 端點 JWT + 角色驗證 |
| `test_heartbeat_schedules_auth.py` | C9：Heartbeat `/v1/schedules` Bearer Token 驗證 |
| `test_h1_h2_h6_security.py` | H1/H2/H6：Secure Cookie、IP 速率限制、Refresh Token 輪換 |
| `test_h3_h4_security.py` | H3/H4：記憶內容 Prompt Injection 偵測、沙箱環境變數強化 |
| `test_h5_m6_security.py` | H5/M6：Heartbeat Token 隨機生成與持久化 |
| `test_m1_m4_security.py` | M1/M4：CSRF Origin 驗證、MCP config Pydantic 欄位驗證 |
| `test_m2_m3_security.py` | M2/M3：審核佇列上限 50、SNS regex 有界量詞防 ReDoS |
| `test_batch1_security.py` | H5殘留 / N1/N2/N3：Heartbeat config 殘留清理、connections 端點驗證、MCP hub install 驗證 |
| `test_batch2_security.py` | N4/N5/N6：`users.json` threading.Lock 原子寫入、terminal asyncio.Lock、NPM package name 嚴格正則 |
| `test_batch3_security.py` | N7/N8/N9/N10/N11：錯誤消毒防憑證洩漏、`follow_redirects=False`、project IDOR、user_service TOCTOU |
| `test_model_route_isolation.py` ⭐ | **MR1 / ADR-0208（C2-2 +3：`route_endpoints` 含設定裡全部連線、開機就有、回傳唯讀副本）**：Proxy 控制面不作模型上游、endpoint＋空 key 原子性、明確自訂路由優先、路由秘密不進 descriptor、刷新清除舊路由、重複 ID 非 last-writer-wins、舊 refresh 的 disable／endpoint／key CAS、known-custom inactive fail-closed、URL／識別字去密與雙協定非空 `base_url` schema 拒絕 |
| `test_public_model_route_endpoints.py` ⭐ | **MR1 / ADR-0208**：真 OpenAI／Anthropic router matrix；省略／`null` 接受，空字串、一般字串、數字、物件與 `baseUrl` alias 一律 422，拒絕時 handler call=0。 |
| `test_n12_sql_fix.py` | N12：家定位共用的 `read_home_snapshot()` 固定 `IN (?, ?)` 字面量，禁止 f-string 動態組裝 SQL |
| `test_artifact_security.py` | Task 產出物 (artifact_ref) 路徑穿越防禦與 `/ui/mission` IDOR 越權防禦單元測試 |
| `test_sec_batch_alpha.py` | P1/P3/P5/P6/P11/P15：路徑穿越修正、`/terminal/stream` 認證、Session TOCTOU 鎖、速率限制擴展、安全標頭、SECURE_COOKIES 預設 |
| `test_sec_batch_gamma.py` | P7/P9/P13/P14/P22/P25/P27：Debugger assume 擁有權檢查、connections 鎖、Refresh jti replay 防護、Debug 工具白名單、觀測端點認證、刪除廢止 session、Admin 審計日誌 |
| `test_sec_batch_delta.py` | P8/P16/P17/P18/P19/P20/P21/P23：Docker non-root、symlink防護、`ast.literal_eval→json.loads`、autonomy鎖、請求體大小限制、版本固定、permission熱重載鎖，以及4個Python workspace members禁loose `>=` pin；ADR-0251 起 P23 用 `git ls-files` 檢查所有版控中的 `.env*.example`（目前 4 個）不含三把金鑰的公開值（比對鍵名＋值），讀真根 `.env` 的 `test_env_heartbeat_token_cleared` 已刪（跳過少 1 個） |
| `test_sec_batch_epsilon.py` | P24/P26/P28/P29：LDH 速率限制、heartbeat 30 秒容差、P28 撤回（ADR-0259：沒有 TTL 設定、沒有自動清除與依 `last_activity_at` 刪對話的 SQL、`last_activity_at` 遷移保留、啟動的資料庫初始化後舊非家對話仍在）、debug 日誌等級 |
| `test_config_env_admin_security.py` ⭐ | **DA1**：`GET/POST /config/env` 收緊為 admin（原僅 `get_current_user`）；非 admin / 無身分 → 401/403，防 `TOUKA_AUTH_SECRET` 簽章金鑰外洩 |
| `test_identity_admin_security.py` ⭐ | **DA2**：`POST /identity` 收緊為 admin-only（Owner→403）防自我提權成 admin；role 欄位 `ASSIGNABLE_AGENT_ROLES` 白名單（非法→422）；GET 維持 Owner 可讀 |
| `test_ui_sessions_idor_security.py` ⭐ | **DA3**：UI 會話 IDOR 防護，`_can_access_session`（重用 `is_owner`）：list 過濾、跨擁有者讀/刪→403、admin 全見 |
| `test_ui_projects_idor_security.py` ⭐ | **DA4**：`/ui/projects*`（list/overview/files/save/delete）與 legacy export IDOR；`_can_access_project`／`_can_modify_project`（擁有權由會話 agent 推導）；跨擁有者→403、`get_project_files` 任意目錄外洩封堵 |
| `test_ui_workspace_settings_auth.py` ⭐ | **DA5**：`POST /ui/settings/workspace`（全域 workspace 根目錄）收緊為 admin；非 admin POST→403、GET 維持已認證可讀 |
| `test_terminal_clean_env_security.py` ⭐ | **DA6**：terminal `bash()` 子進程改傳淨環境（`EvolutionPathGuard.get_clean_env()`），剝除 `TOUKA_AUTH_SECRET`/API key 等密鑰；驗證 env= 已傳、無 secret-named 變數外洩、PATH 保留 |
| `test_mcp_scan_removed_security.py` ⭐ | **SP3 移除 `/mcp/scan`**：`/mcp/scan`、`/v1/mcp/scan` 不再註冊，`openapi.json` 無此路徑；`ToukaProxy/src`、`ToukaChat/src` 不再引用 `mcp-scan` 或 `/mcp/scan`。 |
| `test_execution_broker.py` ⭐ | **ADR-0239**：ExecutionBroker 純轉傳——`new_process_group_kwargs()` 依平台、呼叫時才判斷；`spawn_exec`／`run_sync` 原樣轉傳 argv／kwargs、不注入預設值、不碰回傳物件；全域 patch 的 `subprocess.run`／`asyncio.create_subprocess_exec` 被實際呼叫；模組只依賴標準庫。 |
| `test_jwt_type_confusion_security.py` ⭐ | **DA7**：JWT type 混淆——`verify_token`/`get_current_user` 拒絕非 access 型 token（refresh/service 當 Bearer→拒；access 與無 type 的 agent token→放） |
| `test_dispatch_board_integrity.py` ⭐ | **DA8**：`dispatch_update_task` 補 ADR-0064 A3 看板完整性檢查；陳舊外部 session 寫入已替換的 mission→拒、綁當前 mission／內部 session→放行 |
| `test_dispatch_content_sanitization.py` ⭐ | **DA9**：外部 AI 派發文字（test_log/result_summary）在 `dispatch_update_task` 與 `dispatch_callback` 接上 H3 `content_guard`；驗證 8000 字截斷在兩個邊界皆生效 |
| `test_low_findings_security.py` ⭐ | **DA11–DA16**（Low/Info 群一檔）：DA11 heartbeat token 改 `secrets.compare_digest`（常數時間）；DA12 `is_owner` Owner-agent 跨用戶存取→拒（同擁有者／自身／System 不受影響）；DA13 agent-tool `_get_safe_path` 改 `is_relative_to` 擋 sibling-prefix 繞過；DA14 演化沙箱配額 `_clamp_quota`（model override 只能更嚴、不可超 SSOT）；DA15 skill/MCP metadata 進系統提示前 XML 跳脫——派發器 sync（`_xml_*_entry`）＋ 每請求 `apply_skills_to_request` 的 name 屬性；DA16 移除死碼 `verify_ownership`。（含對抗複審補強：DA12 無主 agent 回歸測試、DA15 apply_skills 跳脫測試）|
| `test_idor_soul_sandbox_security.py` ⭐ | **DA17–DA18**（DA11–16 對抗複審補抓）：DA17 debug context-snapshot 讀/匯出＋souls `update_soul` 跨用戶 IDOR → 加 `is_owner`（跨擁有者 read→空、export/update→403、admin/Debugger 旁路、owned→放行）；DA18 `soul_io`/`custom_io` 共 9 個檔案操作的 sibling-prefix 沙箱繞過/缺檢查 → 全改 `validate_no_symlink_escape`（traversal→拒、合法往返→放行）|
| `test_functional_bugs_security.py` ⭐ | **DA19–DA20**（DA11–16 對抗複審補抓，🐛 功能 bug）：DA19 `request_mcp_creation` 改傳 `identity=`（驗證 `request_approval` 收到 AgentIdentity、回 `Rejected` 而非吞掉的 TypeError）；DA20 P14 Debugger 白名單改用 `get_allowed_tools`/`is_tool_allowed`（禁用工具→403、允許工具→200，皆不再 AttributeError 500）。另更新 `test_sec_batch_gamma.py` P14 測試為實測限制（配置 allowlist→403，不再寬鬆接受 500 崩潰）|
| `test_user_temp_password.py` ⭐ | **DA21**（DA11–16 對抗複審補抓）：bootstrap admin 密碼移出持久日誌——驗證首裝寫一次性檔 `INITIAL_ADMIN_PASSWORD.txt`（含可驗證密碼）、WARNING log **不含**明文只記路徑、首次改密自動刪檔。（原 `test_ensure_admin_user_logs_password` 改寫為新行為）|
| `test_admin_userout_security.py` ⭐ | **DA22**（DA11–16 對抗複審補抓）：`/admin/users*`（list/detail/create/update）回傳改 `UserOut` 消毒投影——以填滿敏感欄位（bcrypt 雜湊＋3 個 refresh JTI）的 mock User 過四端點，斷言回應**不含**任一敏感欄位/值、仍含 id/email |
| `test_suspension_enforcement_security.py` ⭐ | **DA23**（DA11–16 對抗複審補抓）：帳號停權強制——`User.is_active` SSOT 屬性＋四閘門：登入（停權→raise）、`verify_token`（停權→None、active→identity）、`get_current_user`（停權→raise）、refresh（停權→raise）；含預設 status 向後相容 |
| `test_heartbeat_awaken_hardening_security.py` ⭐ | **DA24**（DA11–16 對抗複審補抓）：心跳 awaken——`_resolve_auth_level` 僅 `"full"`→YOLO、未知值→Manual（safe→Auto；ADR-0246 U2c-5）；`/v1/heartbeat/awaken` 端點驗證超長 `init_prompt` 經 H3 消毒（截斷≤8000）才注入會話；ADR-0250 A3 起 setup 讓靈魂有主要模型，斷言不變 |
| `test_heartbeat_blocked_during_destructive_ops.py` ⭐ | （ADR-0287，舊審查報告丙-4）：刪除、重置、記憶清除期間擋心跳——標記可重疊計數、不分大小寫；被擋時不能登記、叫停只中斷這個靈魂、同一個對話兩次分開算；背景迴圈被擋時什麼都不寫、標記後下一個脈衝前停且不寫結束事件也不剪裁、剪裁做完才拿掉登記（例外與取消時也拿掉）、開跑最初叫停不被清掉；代數：重置先做完或中斷旗標被清掉也照樣停、入口檢查之後做完的重置也不開跑、被擋下的不清掉另一次的停止、進行中標記在剪裁前清、重置中不寫錯誤訊息。全部離線 |
| `test_low_cluster_security.py` ⭐ | **DA25–DA27**（DA11–16 對抗複審補抓）：DA25 自助改密碼（`change_own_password` 清 temp＋撤銷 token、錯誤當前密碼→raise、端點 `/auth/change-password` 短密碼→422）；DA26 refresh JTI 改 `compare_digest`（正常輪換仍運作＋靜態驗無 `==`/`!=`）；DA27 `is_protected_path` soul/legacy 子路徑→受保護、sibling（soul_archive）→不受保護 |
| `test_sandbox_rlimits_security.py` ⭐ | **DA28 / ADR-0076**（DA11–16 對抗複審補抓）：演化沙箱 OS 級 rlimits——`_build_rlimit_preexec`（注入 fake `resource` 驗 `RLIMIT_FSIZE`/`RLIMIT_AS`/`RLIMIT_CPU` 設定值；無 `resource`→None）＋ `run_sandbox_command` POSIX 下夾帶 `preexec_fn` |
| `test_subprocess_hardening_security.py` ⭐ | **SP1–SP7**（ADR-0239 後續，ADR-0137 補記）：SP1 互審 `verification_cmd` 子程序不繼承密鑰（真 `sys.executable` 子程序驗證）、新行程群組＋逾時／取消連坐殺、`policy_command_gate`（async；ALLOW／CONFIRM＋逐條 approver／DENY 映射與真實終端政策，ALLOW／DENY 不問人）、`run_review_round` 未帶 auth_level 即 fail-closed、vault 不可用不退回 cwd；SP2 `execute_command` 對 DENY／未知決策零執行、`_execute_core` 取消即殺行程樹；SP4 還原經 `run_sync` 用 `sys.executable`＋逾時；SP5 reset 備份取 `backup_execution_lock` 並移出事件迴圈；SP6 演化 venv／pip 逾時、venv／pip／`verify_mcp_locally` 移出事件迴圈、呼叫端 env 不能加回密鑰；SP7 Creation clean env 刻意不解析哨兵（ADR-0254 起底改成白名單 `LAUNCH_ENV_ALLOWLIST`，前置改用假的父環境，斷言不變；假 approver 多收卡片 keyword）。SP3 `/mcp/scan` 已移除、見 `test_mcp_scan_removed_security.py` |
| `test_evolution_manual_approval_security.py` ⭐ | **缺口 1**（ADR-0254）：`evolution_implement_and_verify` 不再是自帶審批，`request_skill_install`、`request_mcp_creation` 仍是；Manual 有人在場先跳一般審批卡、核准前不建 venv／pip／驗證，拒絕就什麼都不跑，核准後部署卡照舊；Manual 無人在場 executor 拒絕、什麼都不跑；Auto 有人在場仍是「只有你能核准」卡＋部署卡、無人在場拒絕；YOLO 有人在場不跳 executor 卡、部署卡照舊，無人在場建置照跑、部署 fail-closed。真政策取 tracked `auth_policies.json.example`，子程序與部署卡換成記錄器 |
| `test_mcp_launch_env_security.py` ⭐ | **缺口 2**（ADR-0254）：正式 MCP stdio 啟動環境沒有 Touka 與廠商金鑰；有條目自己的 env（含名稱有 TOKEN 的值）、`TOOL_BASE_URL`、白名單裡的系統變數與 `UV_PYTHON_DOWNLOADS`；沒有條目 env 時只有白名單＋`TOOL_BASE_URL`；`clean` 用同一個白名單、不解析哨兵、不再收 base factory；創建引擎試跑＝白名單＋spec env；白名單不重複 SDK 已帶的變數；Windows 名稱不分大小寫。斷言在 `StdioServerParameters.env` 這層，父環境是假的 dict；`launcher_base()` 只含 SDK 預設環境與允許清單、不含條目 env 與工具網址 |
| `test_review_card_reason_security.py` ⭐ | **H4**（ADR-0254；ADR-0246）：審查員對互審驗證指令回判決＋理由；轉人時卡上有 `review_reason`；Manual 的 CONFIRM 卡沒有理由也不是 `human_only`；底線指令卡上 `human_only=true`、不送審查員，審查員的核准不能滿足底線；`CommandApprover` 把兩個欄位放上卡；經真 `request_review` 端到端各驗一次。假 Completer、不打 API |
| `test_restore_status_security.py` ⭐ | **H1**（ADR-0254）：`GET /system/restore/status`（`/v1` 與根路徑）沒登入 401、非 admin 403；admin 在沒有還原時看到 `idle`；背景還原失敗時回 `failed`＋錯誤碼、不帶路徑或例外原文；接受新的還原回 `running` 並蓋掉上一次的失敗。還原腳本換成假的 `run_sync`，不碰真資料、不結束進程 |
| `test_historical_data_boundary_security.py` ⭐ | **歷史資料界線**（ADR-0257 S1；ADR-0285 起前情摘要與家對話也中和其他 prompt 區塊 tag、`neutralize_tags` 的字界，摘要經 Claude 重排後注入文字不留在穩定前綴；家對話的多模態 JSON 字串用 JSON 跳脫寫的 tag 解開後也被中和、巢狀工具結果與思考內容也中和、控制字元不能重組 tag、每個區塊 tag 各一個案例、長空白不會變慢；tmp 事實庫、沙盒裡合成的舊對話 DB、autospec 向量替身，不載入嵌入模型）：界線 tag 的各種寫法（不分大小寫、帶屬性、自閉合、`<`／`/` 前後空白或換行）都被中和，相似但不是界線的字不動；查記憶有結果時整段包成歷史資料、沒結果時不包；隨機回想不再寫成「腦中閃過」、部分來源讀不到的提示在界線外、什麼都沒有時是一句平述；前情摘要在原位置包起來、角色不變；家對話包起來、角色與則數不變、不改到讀出來的 session 樹、帶圖片的訊息圖片完整；查記憶、隨機回想、前情摘要、家對話（字串與 list 兩種內容）裡的舊內容都不能提早關閉界線 |
| `test_mission_board_boundary_security.py` ⭐ | **ADR-0285 甲-1**：任務看板的模型文字不能偽造可信區塊——`<continuity>` 看板那一行的任務 id 用 `safe_ref`（帶換行、假人類訊息、`</continuity>` 的 id 不能多造行或關閉區塊，一般 id 原樣）；`<mission_awareness>` 的大目標、id、描述、依賴與產出保留原文、換行換成 `⏎`、區塊 tag 中和，不能多造任務行或另開區塊，非 ASCII id 原樣保留給 mission 工具比對；每個區塊 tag 各一個案例、`str.splitlines()` 會切的每種換行都換成 `⏎`、控制字元先換成空白再中和（`<`＋NUL＋`/tag>` 不能重組）、3 萬個空白的輸入 2 秒內完成（改壞時約 20 秒就變紅） |
| `test_home_message_outbound_security.py` ⭐ | **家對話外送欄位與順序**（ADR-0260；session 用 mock、靈魂資料用 stub，不讀 data/、不連外）：家對話節點帶滿 UI 鍵時，OpenAI 相容 Chat（含 Anthropic 入口轉過去）、Responses、Gemini、Claude（兩個入口）組出的每則訊息鍵都在該格式的白名單內，steps／thinking／attachment／節點 ID／model 的 canary 不出現在送出的 payload，對話文字、插話標註與人格照送；OpenAI 相容 Chat 的人格排在家對話之前；OpenAI 形狀插在開頭連續 system 之後（空、無 system、一則、兩則混型、system 不在開頭）、Anthropic 形狀照舊；家對話節點的 `name`（心跳寫的 `System`、靈魂 ID）在六種送出路徑都不出現；呼叫者自己在訊息帶的 `name` 在 OpenAI 相容 Chat、Responses、Gemini、Claude 都不轉送；`only_keys` 回新 dict 不改輸入；OpenAI 白名單＝`OpenAIMessage` 欄位減 `name`，未知鍵與 `name` 丟掉、`tool_calls`／`tool_call_id` 保留、tool content 仍是字串；Claude 原生出口只留 role／content |
| `test_memory_safety_flags_security.py` ⭐ | **記憶裡的注入樣式沒有標記**（ADR-0263；tmp 事實庫、autospec 向量替身，不載入嵌入模型）：8 種樣式都標記、正常文字不標、`sanitize_memory_content` 行為不變；存記憶寫入旗標並告知；查詢附註、有旗標的排在沒旗標之後才補位、只有有旗標的結果時照列；隨機回想略過有旗標的；診斷計數；壞掉的旗標值當有標記（fail closed）；模型帶的旗標進不了事實庫；較新版本的庫存時不記旗標；向量庫取不到時退成只回事實庫的有效結果、旗標排後、界線外註明、沒結果也註明、語意查詢丟錯也退、「最近」查詢不開向量庫；升級失敗的庫照常查詢與回想 |
| `test_advertised_tool_list_security.py` ⭐ | **只執行這一輪列給模型的工具**（ADR-0269；conftest 沙盒與 mock，不讀 data/、不連外）：清單物件與型別（轉成 frozenset、事後改原本的 list 不影響、`PipelineContext` 欄位拒收 dict 與 frozenset）；舊名對應表的鍵＝Guard #45 基準的 `legacy_tools`、後繼都已註冊、每個註冊名都有 schema 或在表裡；ToolLayer 寫入（A1～A10：收斂後等於實際注入、無痕沒有記憶工具、無人在場沒有 `ask_user`、只列已掛載的 MCP、客戶端同名 schema 不算、一律覆寫、MCP 用投影出的裸名、OAuth 與還沒連線的伺服器不列、短名與外掛不列、SNS 的 dict ctx 也寫入）；子代理用自己的清單且拿掉兩個派工工具、心跳每次脈衝重算、群聊 `model_copy` 與換備援模型沿用；執行器（B 組，開關打開）：YOLO 下沒列出的終端、Manual 下唯讀 list、無痕記憶 query、無人在場 `ask_user`、沒掛載的 MCP、外掛都被擋且 handler／上游沒被呼叫，`__` 撞名只放行實際注入的那一對，舊名後繼缺一個就擋（參數化），不存在的名稱回 `resource_not_found` 且不呼叫 PaC／審查員／審批卡，有列出也擋不過角色權限，沒列出又沒授權回的是角色權限的 `native_tool_rbac`（順序釘子），清單不可用回 `internal_error`（含建好後才用屬性指派的 dict），模型參數夾帶清單無效，被擋的仍算試過，同一迴圈掛上的 MCP 下一輪才可用，ToukaUI 三種名稱，開關關閉時行為不變；和 iso-c2 隔離層的順序（D 組：被擋的三種呼叫都不進 `policy_level`、不寫 `consent_ledger.record_policy`，對照組列出的終端呼叫兩者各一次）；接線（react_core 傳字面 `True`、開關關閉的執行器在送出上游前 `SYSTEM_ERROR`、MagicMock 執行器照常跑、同一輪兩個呼叫只執行有列出的、`/debug/execute` 明寫 `False` 且能執行清單外工具、live_episode 把清單交給迴圈）；開關預設開（甲-3，ADR-0285：`inspect.signature` 釘住只能用名稱指定、預設 True，不寫開關的執行器照樣擋清單外工具）；結構測試 C2（`ToolExecutor` 只能被直接呼叫：當參數、基底類別、指派對象、取屬性、交給變數都算，直接呼叫不得 `**`、不得改名匯入，也不得把別的東西命名成 `ToolExecutor`；不得替換 `execute_to_queue`、`__init__` 屬性；字面 `False` 只准直接寫在 debug.py 模組層的 `debug_execute_tool`；執行器檔只能有一個模組層的 `class ToolExecutor`、核對方法只定義在它身上（類別本體裡也不得用名稱指派換掉）、關鍵方法與類別不加裝飾器、私有屬性只在它的 `__init__` 寫；私有屬性與核對方法在執行器以外不得出現（含 keyword 名稱）；開關名、私有名、核對方法名、`__kwdefaults__` 與 `ToolExecutor` 的整串字串禁止；掃描 ToukaProxy／ToukaMemory／ToukaHeartbeat／ToukaDispatch／`scripts` 底下全部正式 .py，有涵蓋釘子；掃描器自我測試）、C3（清單欄位只有 5 個白名單檔讀寫、自我測試）。檔名不含 `tools`：當時 `lint_ssot.py` 以路徑子字串判斷業務層，原名 test_advertised_tools_security.py 會讓結構測試的 `read_text` 被判 IO_PRISON_VIOLATION（ADR-0278 起頂層 `tests/` 不再算業務層） |
| `test_user_scoped_mcp_security.py` ⭐ | **使用者各自授權的 MCP 只在人在場、和靈魂的前景對話時帶身分**（ADR-0281 T1；真 ProtocolManager 配替身 session 與 OAuth 替身，不連外、不讀 data/）：條件函式（心跳、子代理、派工、沒有互動串流、不是靈魂、群聊請求與成員、沒有原始人類、人類或字串身分都不行）、請求上下文來自 ctx；組清單只有 `oauth_user` 拿身分、共用伺服器不連線且快取分區不變、沒授權不連、授權資料損毀記錯誤、慢連線略過但不取消、一台出錯（授權查詢或連線）不影響其他、enterprise 不拿身分、不同人的連線分開；ToolLayer 前景靈魂帶身分、其他情況不帶、技能與自訂模型需要這類伺服器時不再失敗、沒連上的共用伺服器寫進 `<mcp_unavailable>` 且依權限過濾；執行器第二層防線（原因 `user_scoped_mcp_unavailable`，不叫審查員、不跳卡、不寫同意）、`/debug/execute` 不套用；幻覺偵測只多認伺服器列出的使用者授權工具；群聊成員的 ctx 有旗標；連線還在跑時不再等也不再開、失敗後冷卻、授權查詢出錯不每輪重查、取消不外漏、執行身分不是對話目標時不帶身分、說明區塊名稱跳脫並依工具清單與掛載過濾、讀狀態出錯不失敗、`unavailable_shared_servers` 的連線已死與斷路器分支且不寫入、第二層防線排在隔離層級判定之前 |
| `test_soul_tool_selection_security.py` ⭐ | **靈魂與自訂模型的工具清單比對**（ADR-0281 T2；真 ProtocolManager 配替身 session，不連外、不讀 data/）：`伺服器/工具` 只比這一對（別台同名、同名內建、廣告名相同的另一對都不算）、舊長名照舊比廣告名、短名／只寫伺服器名／含 `*`／空的一邊／兩個以上斜線／非字串都不生效、萬用字元不留成一對；配套只跟著列出的 `execute_command`（被政策擋下、只勾 `bash`、客戶端自帶同名工具時不帶）且受角色權限限制；`伺服器/工具` 照樣要 MCP 角色權限；`mentions_server` 不認只寫伺服器名；ToolLayer 列出 `伺服器/工具` 與舊長名、短名等於內建名稱只開內建、自訂模型連 `伺服器/工具` 的伺服器且不受動態掛載限制、`<mcp_unavailable>` 依 `伺服器/工具` 過濾、舊短名每個靈魂只警告一次（子代理、身分是人類、自訂模型、這一輪有列出的名稱都不警告）；`/v1/tools_stats` 附加 `server`、`tool` 欄位與 `tool_companions` 且舊欄位不變 |
| `test_incognito_legacy_scope_security.py` ⭐ | **無痕讀寫不到舊對話庫**（ADR-0274；記憶引擎換成 AsyncMock，政策用 `.example`）：無痕時九個檔案動詞不列 `scope=legacy`（讀寫家族），一般對話照舊列（無痕投影不改共用 schema）；硬叫時動詞入口、`vault_tools` shim、經執行器（Manual 唯讀直接放行的路徑，含九個動詞都列出時算列出的 shim）都回 `permission_denied`、引擎沒被呼叫；一般對話照舊轉到引擎。先寫紅燈（xfail strict） |
| `test_mcp_external_ref_security.py` ⭐ | **GHSA-rwrf-2pqf-9j8j 回歸**（ADR-0203 dep1 補記，含演化 `_verify_mcp` 的 Windows／POSIX 兩條連線接縫；in-process 假伺服器，攔截 urllib 只記錄、不連線）：伺服器 outputSchema 的 `$ref` 指向 http、https、file 時，`Client.call_tool`（SDK 自己重列、先列再叫兩種）與 Touka modern 路徑用的 `client.session.call_tool` 都不抓外部 ref、呼叫有逾時不會卡住；金絲雀證明攔截點攔得到 jsonschema 預設的取用路徑。這筆公告不在 OSV／PyPI，Guard #22 看不到，本檔是唯一守門員；mcp 2.0.0 上 9 個紅、2.2.0 全綠 |
| `test_mcp_redirect_security.py` ⭐ | **remote MCP 不跟轉址**（dep1；ADR-0199 dep1 補記；MockTransport、真 SDK、127.0.0.1 兩個 port 的真 socket）：factory 對同源 307／308、跨源 302、http→https 301、沒有 Location 的 300 都丟 `McpRedirectRefusedError`（呼叫時帶 `follow_redirects=True` 也擋），只請求原網址一次、不讀回應內容，例外繼承 TransportError 但不是 ConnectError／NetworkError，訊息與 log 不帶 query 和 Location；`follow_redirects=False` 保留且 hook 已註冊；真 SDK 的 discover 收到 307／302 時轉址目標從未被請求、Authorization 只出現在原網址，OAuth 憑證提供者只被叫 1 次；真 socket 第二個 port 與同源路徑都沒收到請求；對照組：沒掛 hook 時 mcp 2.2 會跟同源轉址並重送憑證 |
| `test_mcp_call_time_catalog_security.py` ⭐ | **呼叫 MCP 工具當下核對伺服器清單**（dep1；ADR-0203 dep1 補記；真 ProtocolManager 配替身 session，真 ToolExecutor）：外部 `$ref`／dialect／上限／形狀四種隔離都拒絕且 SDK 呼叫 0 次；標頭隔離改回 `permission_denied`；同名兩筆（其一隔離、兩筆都乾淨）都拒絕；不在清單與不支援列工具三種回 `resource_not_found`；陽性對照：只有 icon 問題（跨源、抓取失敗）照常可呼叫；連續拒絕 4 次斷路器仍是 CLOSED、last_error 是 None；重列失敗仍不呼叫；執行器在模型迴圈與 `/debug/execute` 兩種開關下分類都正確。2026-10-21 補上：標頭參數超限（單值、中文值、總和）連錯 3 次後同伺服器其他工具仍可呼叫、斷路器與 last_error 不動、訊息與 log 不含參數值、點名標頭；上限內照常送出；呼叫當下核准競態不被改成參數錯誤；拒絕不記失敗也不記成功（穿插拒絕不歸零、半開不被關回）；斷路器本身的中立單元測試；執行器兩種開關都回 `validation_error`。突變 5 條（斷路器記成失敗、拒絕走一般例外、不轉換、整個政策都轉、執行器不接）各自轉紅。檔名不含 `tools`（理由見 `test_advertised_tool_list_security.py` 那一列） |
| `test_mcp_error_url_sanitize_security.py` ⭐ | **MCP 診斷遮罩不弄壞網址**（禁令 23；純函式加一個 `_sanitize_for`，不連網）：12 種網址形狀（含 IPv6、Docker 服務名、結尾標點、括號、緊貼中文或冒號）原樣可讀；22 種網址夾帶路徑或密鑰的寫法（`,path=`、`;key=`、主機寫成磁碟代號、反斜線、空主機、非 ASCII、本機根目錄名、帳密、query、fragment、token 段、精確遮罩留下的標記、`file:`、其他 scheme）只留白名單前綴；網址外的本機路徑照遮，緊貼網址的單段本機路徑也照遮（含字母加冒號結尾）；secret 規則仍套到網址；截斷切在帳密中間整段遮；env 精確遮罩在網址裡生效；畸形網址與 500 筆固定公式輸入不丟例外。突變 14 條各自轉紅。測試字串用拼接避開 `PATH_LITERAL_SSOT` |
| `test_mcp_midflight_transport_failure.py` ⭐ | **連線途中斷線取消 lifespan 的重現**（dep1；ADR-0199 dep1 補記；兩台真 MCPServer 經 ASGITransport，正式 factory 與 ProtocolManager）：shared 連線在模擬 lifespan 的 task 建立後，途中 ConnectError、ReadTimeout、307（加了轉址 hook 之後）都讓 anyio 取消這個 task，清理時另一條連線也被關掉。dep1 時三個案例標 `xfail(strict=True)` 留作證據（mcp 2.0.0 與 2.2.0 相同）；life1（ADR-0273）修好後拿掉 xfail，現在都通過 |
| `test_mcp_connection_isolation_security.py` ⭐ | **MCP 連線出事或被關不連坐**（life1；ADR-0273；兩台真 MCPServer 經 ASGITransport（`tests/helpers/mcp_asgi_peers.py`），正式 factory 與 ProtocolManager）：從另一個 task restart、重載都不取消建立連線的 task、另一條照常、不算失聯；聊天請求建立兩條連線後還在跑時，遠端途中斷線與 302 只讓那一條失效（建立者沒被取消、另一條照常、斷掉那台快速失敗且不自己重連、失聯通知恰好一次就是那條）；I1：lifespan 先以隔離層的 `IsolationMonitor`（假自測、`fake_kernel_layout`，不碰真的 Landlock 或 Docker）啟動背景重測，遠端途中斷線後背景重測仍在跑（lifespan 被取消時收尾會先取消所有背景任務）；每個都檢查沒有未讀取的 task 例外。改之前用 xfail(strict) 跑出紅燈，I1 在 life1 之前的產品程式上是紅的 |
| `test_review_command_approval_security.py` ⭐ | **B2**（ADR-0137 2026-10-12 補記）：舊 `_hitl_preapproved=True` 不再放行 CONFIRM 級 `verification_cmd`；有人在場（Manual／Auto）時該指令自己一張卡（步驟 id `<id>:verify:<n>`、卡上完整指令），核准才執行並以 tool_start／tool_result 收尾；拒絕不執行；兩條指令一准一拒各自一步、互不影響；無人在場與子代理（`subagent_run`）不跳卡、不登記、不執行；核准後審查拋例外時該步驟以 internal_error 收尾、例外照常往上。真 `sys.executable` 在 tmp_path 寫標記檔驗證 |
| `test_review_awaiting_human_security.py` ⭐ | **B1**（ADR-0246 決策 P6）：無人在場（心跳、子代理）時 CONFIRM 級 `verification_cmd` 不執行，本輪標 `BLOCKED_FOR_HUMAN`——跑 4 輪仍不呼叫 critic、不扣次數、不上鎖、不佔 mission 子 loop 名額；回結構化 `permission_denied`（`reason=human_approval_required`），訊息不再提 `execute_command`；有人在場但卡片逾時同一條路（訊息說沒等到人類決定）；被人類拒絕回 `reason=human_rejected_command`、請先問人類，同樣不送審不計次數不上鎖；兩條指令先准後拒不送審、核准那步照常收尾；人回來在對話裡重跑時那條跳卡、核准才跑、送審放行並能 DONE；往返達上限的永鎖回一般審查結果；政策閘依審批結果（無 approver／逾時／佇列滿／拒絕／核准）回固定理由 |
| `test_approval_card_full_command_security.py` ⭐ | **A7**：經 `ToolExecutor` 互動 CONFIRM 取審批卡——約 1,000 字指令全文上卡；超過 8 KB 截斷並附 `…[truncated: N more chars not shown]`；剛好 8 KB 不截斷；其他大載荷（`write.content`）仍截到 200 字 |
| `test_audit_resource_security.py` ⭐ | **ADR-0283**：政策稽核帳本 `stats/decisions.csv` 的終端指令列只記程式名稱和長度（環境變數設定、空值設定、PowerShell、引號、Windows 路徑、程式名稱像金鑰、Unicode 空白與控制字元等寫法都不帶出值，認不出記 `?`）；其他列只記工具名稱、不像工具名稱的只記長度、例外不讓原文落地；經 `classify_command` 與 ToolExecutor Gate 2 的真實寫檔路徑整列不含假金鑰片段，黑名單仍看完整原文、`PolicyContext` 不被改動；AST 固定建 `PolicyContext` 的位置、action 與 resource，並擋下其他建法與改 resource／action 的寫法 |
| `test_hard_floor_security.py` ⭐ | **FL1–FL2**（ADR-0246 底線）：F1–F4、F7 各類 shell 指令（含 `cd x; shutdown`、`rm -fr /`、`Remove-Item -Recurse`、`docker compose down`、引號內 `poweroff`、`cmd /c shutdown`）與 F5、F6 工具名＋動詞的分類、一般指令不誤判；用 tracked `.example` 規則：F1–F4、F7 在 Manual／Auto／YOLO／未知模式都是 `HUMAN_ONLY`、黑名單的 DENY 不放寬、退役的 ReadOnly 當 Manual、白名單拿掉後 Auto 一律送審查員、設定檔關不掉；F5、F6 在 Auto 是 `HUMAN_ONLY`、Manual／YOLO 照舊並帶類別；`HUMAN_ONLY` 有人在場跳卡、核准才執行、無人在場拒絕（終端也不落到遺留通道）、不在 `RUNNABLE_STRATEGIES`；terminal 只有可信 `_hitl_preapproved=True` 才跑地板指令、DENY 照擋、模型自帶的旗標被覆寫；互審驗證指令有人逐條問、無人等人確認；`/debug/execute` YOLO 照擋；規則檔熱重載寫壞（兩種）保留黑名單、風險表退回預設，啟動時壞檔用預設 |
| `test_hard_floor_roots_security.py` ⭐ | **底線補強**（ADR-0246，R-2c 修正）：根目錄只照兩句規則——甲：目標是受保護根目錄（資料、repo 根、`.env` 所在目錄、設定目錄，SSOT 動態解析）本身、底下或上層就是底線（資料 F1、其他 F4；遞迴讀到放金鑰的目錄或它的上層 F2）；乙：沒有 Touka 的使用者工作區（`resource_manager.get_workspace_mount()`，空值或相對路徑不算、不猜目前資料夾）本身與上層只擋刪除、搬走或清空（含 `{ws}/*`、`{ws}/**`、`*.*`、`rsync --delete`、`robocopy /MIR`／`/PURGE`、`git clean`、直譯器的 `rmtree`／`rmSync`／`remove_tree`／`rimraf`）；不在 Docker 時工作區與根目錄先 resolve，symlink／junction 別名照甲判斷，解析失敗整片保護。Docker 掛載點要用假的 mountinfo 證明宿主來源和受保護根目錄不重疊（工作區與它底下的子掛載都要比；同裝置時 root 不分大小寫、互為本身或上下層算重疊；不同裝置只有兩邊都是實體區塊裝置〔major≠0〕才假設不重疊，唯一例外是錨點為實體裝置上的 Docker named volume〔`…/volumes/<名稱>/_data`〕、工作區在另一個裝置（U2c-R1c）；解析失敗、沒有獨立掛載項都算證明不了）才豁免「在 repo 根底下」，證明不了就整片保護、遞迴讀算 F2。「工作區設定 × 動作」對照表（等於 repo 根、repo 上層、兄弟、子資料夾、空值、Docker 不含 Touka、Docker 含 Touka、Docker 讀不到 mountinfo，加上大小寫不同、每個 bind 一個 major 0 裝置、rbind 子掛載、跨磁碟機、兩顆實體磁碟、大小寫混用但不重疊、指向 repo 的別名工作區，以及 ghcr 加 Docker Desktop 的 named volume 正例和一般 bind、同裝置重疊、FUSE 視圖、major 0 上的 volume 佈局、同裝置只有 volume 在工作區底下五個反例）每格寫死期望；reviewer ① 的根目錄繞法、仿 compose 的 8 條專案指令放行；F7 繞法（前綴連同參數、`find -delete`／`-exec rm`、管線交給刪除指令、`powershell` 不帶 `-File`）在 YOLO 仍擋；Windows 正規化（結尾點、資料串流、`\\?\` 前綴）與心跳 `config.json` 算 F2；37 條常見開發指令仍是 ALLOW |
| `test_auth_mode_three_modes.py` ⭐ | **三模式判斷層**（ADR-0246 U2c-4）：已存值讀取對照（ReadOnly／SafeAuto／看不懂／非字串 → Manual、FullYOLO → YOLO、新名稱原樣）；模式授權純函式（Auto／YOLO 只給 admin 或該靈魂擁有者）；動態 shell 寫法偵測（變數、`$(…)`、反引號、`%VAR%`、程序替換、寫進檔案的 `>`、eval／iex、PowerShell `-enc`）與一般指令不誤判（管線、引號、萬用字元、`2>&1`、`> /dev/null`）；Auto 決策（read_only／safe ALLOW、F5／F6 與動態 shell `HUMAN_ONLY`、其他終端與 dangerous 工具交給審查員 `REVIEW`、黑名單優先）；範本沒有 `safe_whitelist`、舊鍵被忽略；MCP MRTR 三級排序 |
| `test_auth_mode_not_wider_security.py` ⭐ | **不放寬總表**：14 個代表動作 × ReadOnly→Manual、SafeAuto→Auto、SafeAuto→Manual、FullYOLO→YOLO × 有人／無人在場，新模式不比舊等級寬（舊效果寫成字面值）；變寬的格子必須恰好等於事先核准的清單（ReadOnly→Manual，以及 U2c-3 的「Auto 經審查員放行」：終端與 dangerous 工具、只在有人在場）；無人在場送審的動作不變寬、F5／F6 與地板不送審；SafeAuto 的接班模式無人在場時不再自動跑舊白名單指令；KF-4（U2c-5）：心跳 safe／full／看不懂變寬的格子恰好是核准清單，且不比對應舊等級在無人在場時寬；`auto_review.headless_terminal` 預設 false，打開才多出的格子恰好是 SafeAuto→Auto 與心跳 safe 的 `curl`（無人在場），標成需人類決定才可打開（R-2c） |
| `test_thinking_purpose_header_security.py` | （ADR-0271）：`X-Touka-Purpose` 只認 `inline-completion`，偽造值、變形、超長、非 ASCII 都忽略；真 pipeline 四個聊天路徑有無標頭只差 headers 與 thinking_profile；主對話帶標頭並觸發審查員時，審查員那次 context 沒有這個鍵、送出內容逐 byte 相同；沒登入只帶標頭 401 不進 pipeline、Depends 原文不變；鍵名與標頭名只出現在指定檔 |
| `test_auto_reviewer_security.py` ⭐ | **Auto 審查員判斷層**（ADR-0246 U2c-3，假 Completer、不打 API）：經 `complete_once` 合約送出（費用記在被審的靈魂、provider 標籤 `auto_reviewer`、Claude 帶 `response_schema`）；輸入只有原意／完整參數／環境三個分隔區塊、內容是跳脫過的 JSON，資料拼不出結束標籤；提示詞要求代理人自寫的腳本除非原意明確涵蓋否則轉人；子代理原意帶父原意；逾時（含 completer 卡住）、拒答、截斷、超預算、供應商錯誤、例外、多寫字、code fence、缺欄位、多欄位、未知判決、重複鍵、極深巢狀、夾帶「忽略以上規則」都算不放行；沒設模型或設定無效＝轉人且不呼叫；無人在場的終端預設不送審、`headless_terminal` 開才送，無人在場的高風險工具一律不送審；參數過大不送；P7 連續 3 次或累計 20 次之後不再呼叫、各對話分開、只在記憶體；稽核只記判決與原因碼；無痕原意照送、不寫 log、不碰資料庫；Auto 的終端與 dangerous 工具回 `REVIEW`、safe 類 ALLOW、地板、F5／F6、動態 shell 與黑名單先判斷 |
| `test_auto_review_execution_security.py` ⭐ | **Auto 審查員執行層**（ADR-0246 U2c-3，假 Completer、不打 API）：ToolExecutor 的 allow 帶可信 `_approved_by="auto_reviewer"` 只跑一次、不跳卡；deny 拒絕不跳卡；escalate 有人在場跳卡（完整指令＋`review_reason`），核准才跑、被拒不跑，無人在場拒絕；高風險工具（delete）有人在場送審（allow 帶 `_approved_by` 執行、escalate 跳卡、deny 拒絕）、無人在場不送審（開關開著也一樣）；無人在場的終端預設不送審也不落到遺留通道、`headless_terminal` 開才送；沒設模型或沒注入 completer 時有人轉人、無人拒絕；main.py 注入 `complete_once`；模型自帶 `_approved_by`／`_hitl_preapproved` 無效（不跳過卡、不解鎖無人在場、被可信情境覆寫、公開合約濾掉）；審查員回 allow 也擋 F1–F4、F7（無人拒絕、有人只跳 `human_only` 卡、審查員沒被呼叫；terminal 帶審查員核准也擋）與 Auto 下的 F5、F6（有人只跳 `human_only` 卡）；terminal 只認 `_approved_by` 的精確值或人類卡核准、不再呼叫審查員、DENY 與 CONFIRM 不因審查員核准放行；互審驗證指令在 Auto 由審查員決定（allow 跑、deny 拒、escalate 問人、無人等人確認、地板跳過審查員）並以真 `request_review` 端到端驗證；MCP MRTR 不把 REVIEW 當 DENY、不重審；P7 之後不再呼叫審查員、卡上有理由；送審內容不含工具輸出、助理文字、System nudge 與系統寫入的訊息 |
| `test_auth_mode_authority_security.py` ⭐ | **模式授權與舊值讀取**（修 D_permissions §1.9-1）：admin、擁有者可寫 Auto／YOLO；別人的靈魂與一般使用者對靈魂對話 403；一般使用者的裸模型對話降成 Manual；前擁有者明寫或沒帶 auth_level 存檔都降成 Manual、擁有者沒帶時保留；存檔與 `/debug/execute` 拒收舊名稱；沙箱 DB 寫入舊值後快照、清單、ToolLayer 都讀到對照後的名稱，存檔與心跳 upsert 寫回新名稱；ToolLayer 自己也過對照表；無痕對話（資料庫沒有這筆）即使請求與 ctx 帶 Auto／YOLO，ToolLayer 仍是 Manual（U2c-6）；R-2c：代理身分寫模式用真的 `is_owner`（靈魂本身、DebugAgent 存 YOLO→Manual，DispatchAgent、子代理 403，admin 代打靈魂→YOLO）、新對話先建再帶靈魂切模式（擁有者存得下 Auto、只送模式降 Manual、一般使用者對靈魂 403）、`get_session_by_id` 自己對照舊值；存下的 Auto／YOLO 只給 session 綁的 agent（執行另一個 agent、沒綁 agent 都是 Manual，舊前綴 ID 正規化後比對，MonitorLayer＋ToolLayer：一般模型走 session 連結照 A、點名另一個 agent 變 Manual） |
| `test_incognito_manual_only_security.py` ⭐ | **無痕一律 Manual**（ADR-0246 U2c-9：無痕無法追溯）：純函式 `enforce_incognito_manual` 只動無痕、非無痕原樣；同一個 session id 已存成 Auto／YOLO（tmp_path SQLite）、請求與 ctx 也帶 Auto／YOLO，無痕仍是 Manual，不無痕則照存的模式；心跳與子代理組裝帶無痕標記時 ToolLayer 給 Manual、不帶照舊；子代理執行用的 ctx（不經 ToolLayer）父代理無痕就是 Manual、否則照父代理的模式；R-2c：只有 `ui_persistence.mode="incognito"`（`model_construct` 繞過 schema）時 ToolLayer 仍給 Manual、proxy 把 `ctx["incognito"]` 設成 True |
| `test_heartbeat_modes_security.py` ⭐ | **心跳模式與 KF-4**（ADR-0246 U2c-5，假 Completer、不打 API）：從 `/v1/heartbeat/awaken` 入口經脈衝的 ToolLayer 到真的 ToolExecutor——safe→Auto：請求帶工具（不含 `ask_user`）、safe 類 `write` 照跑、終端與 `delete` 拒絕、審查員沒被呼叫；full→YOLO：非地板指令會跑、`docker compose down`／`cat .env` 仍拒絕；看不懂（`Safe`）→Manual；session 存的模式（YOLO／Manual）不套到心跳；脈衝不跑 MonitorLayer、保留 `/awaken` 設的 session 與原意；靈魂 `enabled_tools` 照樣收斂心跳的工具；呼叫端漏設 `heartbeat_run` 時脈衝自己補上、仍不採 session 的模式 |
| `test_reviewer_models_endpoint.py` | **審查員模型清單**（ADR-0246 U2c-6，假金鑰）：`GET /v1/autonomy/reviewer-models` 只列供應商金鑰已設定、啟用中的一般模型（排除沒金鑰的供應商、靈魂、停用、自訂模型、不用金鑰的端點型供應商），依 id 排序；根路徑對等；非 Owner／Debugger／admin 回 403 |
| `test_autonomy_reviewer_authority_security.py` ⭐ | **審查員設定只有人類 admin 能改**（ADR-0246 R-2c，假金鑰、tmp_path 設定檔）：`auto_review` 有變更時（模型或 `headless_terminal`）非 admin 擁有者經 X-Agent-ID、靈魂本身、Debugger、admin 代打都 403 `AUTH_ADMIN_REQUIRED`、設定不變；一般人類擁有者連 POST 都 403；人類 admin 可改；清單外的模型（沒金鑰、靈魂、停用、不存在）422 `VALIDATION_ERROR`；沒動 `auto_review` 時預算欄位照舊 Owner／Debugger 能改；舊模型沒金鑰時不擋沒改模型的存檔與只改開關 |
| `test_user_intent_presence_security.py` ⭐ | **使用者原意與在場判斷**（ADR-0246 U2c-2）：對話原意只取最後 3 則人類訊息（排除 `name="System"` nudge、系統寫入的 user 訊息、tool_result）、逐則截到 1,000 字、標成資料且不可變；MonitorLayer 產生、子代理不覆寫、無痕不寫 log 不碰資料庫；心跳用消毒後的 init_prompt、UI 重跑用最後一則人類節點，兩者都設 `heartbeat_run`；子代理設 `subagent_run`＋`dispatch_run`、原意只帶父原意（R-2c：派工目標是代理人轉述、不收）；SNS 外人訊息（`【SNS 外部訊息】` 開頭）不算原意，對話與 UI 重跑都排除；前端內嵌的附件從第一個標記起排除（含偽造結束標記、只有附件、已上傳檔案提示）；ToolLayer 只對子代理沿用父模式；子代理遇到 CONFIRM／HUMAN_ONLY 立刻拒絕、不跳卡（`asyncio.timeout` 防卡死）、`_interactive=False`；繼承 YOLO 仍擋底線指令；MCP `execution_kind`／origin／互動出口依標記；心跳的 MCP 呼叫不使用 workload OAuth（`HEARTBEAT_WORKLOAD_OAUTH_ENABLED` 維持關閉） |
| `test_security_policies.py` | PermissionService 安全政策讀取、`auth_level` 三模式矩陣判定（退役的 ReadOnly 當 Manual） |
| `test_cloud_mode.py` | `CLOUD_MODE` 旗標：雲端部署時 LDH 不對私有 IP 自動發卡、`X-Real-IP` 識別、`ALLOWED_ORIGINS` CORS 合併 |
| `test_config_env_selection.py` | `.env` 檔案環境感知與互斥載入邏輯驗證，支援整合/解耦模式切換 |
| `test_cors_origins.py` ⭐ | **E#17 / ADR-0118**：`main.py` CORS 改 `allow_origin_regex` 放行 localhost / 127.0.0.1 / 私網區網 IP（手機/第二台裝置）/ vscode-webview，外來網域仍擋（經 TestClient 驗 `access-control-allow-origin`）|
| `test_config_env_error_leak.py` ⭐ | **E#21 / ADR-0118**：`POST /config/env` 寫檔失敗的 500 不得洩漏原始例外字串（路徑/`error_output`）——`str(e)` 只進 log，回應維持乾淨 SYSTEM_ERROR（admin-only 端點仍防路徑外洩）|

### 隔離執行環境（ADR-0272）

所有 pytest 預設裝上隔離替身（`tests/conftest.py`＋`tests/support/isolation.py`）：層級釘在 `landlock`、runner 直通、每個測試前後清空同意、來源讀取與啟動帳本及技能鎖（C2-2 起另有 autouse fixture 在每個測試前後重設權限自癒的掛鉤；替身 `PinnedMonitor` 和真的 monitor 逐案比對層級、原因與降級去處，含執行器去處與封鎖埠）；只有 `tests/integration/test_isolation_linux.py` 用 `real_isolation` 拿真的 monitor 與 runner。

C2-3 的來源、正式啟動、創建試跑與演化驗證也沿用這個沙盒；決策見 ADR-0275。下表列出測試索引；Linux 案例須在 Linux 收集及執行。

Linux 真核心案例都在既有整合模組，Windows 仍是整個模組跳過一次，不因新增案例增加平台跳過數。容器 CI 的觸發清單目前是 **29 個路徑模式**：含 `models/mcp_isolation.py`、`services/mcp_isolation_store.py` 與 `protocols/mcp/isolated_stdio.py`，由 `tests/lint/test_isolation_ci_workflow.py` 核對。先前待重跑的執行器突變已拆成 9 項，既有驗證紀錄皆因預期防線而失敗；這不代替整批 C2-3 的 Docker／突變／9p 驗證。

| 檔案 | 說明 |
|------|------|
| `test_launcher_exec_env_security.py` | exec_env 格式／名稱／大小、保留名稱與 loader 變數、限制後才合併目標環境、不污染啟動器；舊 spec 相容性。 |
| `test_executor_mcp_start_security.py` | kind mcp 的程式唯讀與 state 範圍、stdio 分流、零時限限制、START 與 relay 合約、本機 relay cwd。 |
| `test_mcp_test_state_security.py` | 每個測試前後清空來源讀取帳本、啟動帳本與技能鎖，避免測試次序污染。 |
| `test_isolation_linux.py::test_executor_reaps_blocked_stdin_after_relay_disconnect`、`test_mcp_stdio_relative_script_requires_an_absolute_target` | **Linux**：真 relay 的 stdin 背壓後斷線收群組及登記；Landlock／executor 保留相對腳本參數，在 state home 找不到時失敗，改用絕對目標可啟動。 |
| `test_mcp_origin_classification_security.py` | 所有啟動器共用路徑分類、六類可寫根、shell 引號／賦值、未知包裝器、尚不存在的路徑與 scoped package 碰撞；tilde、命令替換、延遲變數、萬用字元、大括號、單字內引號及秘密／URL 編碼值從嚴；程式載入根與祖先凍結全部技能、普通 data 根資料參數及 scaffold 哨兵例外、壞設定拒絕。 |
| `test_mcp_profiles_security.py` | 自建／第三方／scaffold、APPDATA 與輸出對映、依實際 cwd 授權、不因普通套件／模組名帶點而授權、引用檔案只開所在資料夾、sandbox_paths 永不授權、state 段名與目標程式唯讀。 |
| `test_mcp_launch_isolation_security.py` | 自建與第三方判定矩陣、Docker／deno／bun／pnpm／套件版本／Python 模組的 argv 保留、APPDATA 從嚴分類、state home 的 command／argv／env 再分類（含尚不存在的無分隔符檔名）且等待時不建立目錄、環境分離、profile 拒絕、啟動帳本與互斥計數。 |
| `test_mcp_isolation_store_security.py` | 分檔格式、bound、missing／stale／unreadable、自建來源不放寬、env 變更預設非人類、原子寫入、壞資料夾項目、重入與協程拒絕。 |
| `test_mcp_server_config_format_security.py` | 各寫入點保持 server_config 的原格式，隔離欄位不混入、來源不可由 spec 決定。 |
| `test_unisolated_card_completeness_security.py` | 未隔離卡每個值 65,536 字元的界線、不可呈現的程式、後端拒絕不完整核准、完整內容與其他去處不受影響。 |
| `lint/test_mcp_isolation_store_lock.py`、`lint/test_single_proxy_process.py` | 隔離設定的寫入來源與鎖內禁止 await；部署與啟動維持單一 Proxy 行程。 |
| `test_isolation_policy_security.py` ⭐ | 只有經 runner 的內建工具提供政策層級；同名 MCP 工具不進隔離判定、不記帳本。ABI 常數、F5 舊別名、模型參數不能改 environment；創建依全域／本機層級，有隔離時 YOLO 可執行；未接線工具與本機 none 的 scaffold 在 YOLO 只接受人工審批、無人在場拒絕；判定出錯 fail-closed；模式、層級、是否在場的矩陣，DENY 與底線優先；終端用 grant 層級，驗證指令用專案層級。 |
| `test_yolo_headless_mcp_security.py` ⭐ | ADR-0272 決定 4——YOLO 無人在場的 dangerous MCP 只有人能核准、心跳拒絕；有人在場、safe／read_only、原生工具、其他模式不變；黑名單照舊；參數帶 `human_present`／`_hitl_preapproved` 無效 |
| `test_isolation_profiles_security.py` ⭐ | （C2-2 +4：執行器位置的代理人與演化 profile 只寫自己的目標與執行器 state、不給公開設定檔與專案、要 READY 的 state_root）：固定受保護集合、鏡像根本身、受保護項目的上層、容器 `/app` 子項目都拒絕；G 只有呼叫者自己的資料夾（`docgen_output` 不給）；專案根經過連結就拒絕；ST 未設或不可寫帶出 `sandbox_state_unconfigured`；`PYTEST_ADDOPTS` 只在上層授權到公開設定檔時注入；代理人 profile 的路徑與環境變數改寫（HOME／TMP／XDG、git ceiling、`PYTEST_ADDOPTS`）；上層公開設定檔唯讀（硬連結、符號連結略過，D／ST／E 底下不給）；pin 預設；每種 profile 都通過啟動器的 `parse_spec`；封鎖埠集合（SearXNG 只在 proxy）；設計例子表（開發版、工作區設成磁碟根、GHCR）、名稱大小寫、原生 repo 根整個封住 |
| `test_unisolated_consent_security.py` ⭐ | subject 正規化（終端、驗證指令、演化、`create_mcp`／`scaffold_mcp`）；一次性、10 分鐘、同鍵不同 subject 兩筆作廢、同鍵重新記錄清掉同意、只有 none 能記同意、鍵由已驗證身分建立、帳本有上限 |
| `test_project_root_single_source_security.py` ⭐ | 專案根只來自 ctx 或 session，終端與驗證指令都不讀 `project_id` 參數；帳本記下層級、原因與 session 的專案 |
| `test_isolation_unavailable_fail_closed_security.py` ⭐ | none 沒同意不 spawn（前景、背景、舊 ToukaUI 通道）；層級掉了拒絕；卡片寫「隔離降級」不能變成直接執行；預期執行器時不退到 Landlock；未隔離同意只跑一次；卡片標示與 `isolation_ack`；創建照實標隔離，合成未接線工具保留 `direct` 防線（判定出錯、帳本作廢時也一樣）；ST 不可用時只有 `direct` 同意可直接執行；驗證指令同一套。 |
| `test_sandbox_fd_hygiene_security.py` ⭐ | （C2-2 +3：失敗代碼只認狀態 fd、指令自己印的標記不算）：終端與驗證指令只傳 spec 與狀態 fd、spawn 後關掉；沒有任何 spawn 關掉 `close_fds`；都用 plan 的 argv 與 fd。比對整個行程的 fd 之前先等策略稽核的單一 worker 寫完：終端的權限判定會在背景寫 `stats/decisions.csv`，寫的時候剛好碰上比對就多一個 fd（CI 上終端的三個案例偶發失敗的原因）；斷言不變 |
| `test_isolation_project_scan_security.py` ⭐ | Touka 目錄標記（casefold）、資料根名稱任一個就算（剛啟動、改名的資料根；`legacy` 單獨不算）、往下 3 層、資料夾數與時間上限、略過清單、不跟連結；快取 TTL 與根目錄 mtime（第一層沒新增資料夾就沿用、有新增只補掃那些、已拒絕的不被新資料夾重開）、同一根只掃一次、前景放棄由背景補上、背景超限記 `scan_limit`；往上檢查每次都做 |
| `test_evolution_deployed_freeze_security.py` | 四入口與 profile 凍結、自建／第三方 state home 相對引用在寫入前拒絕且不建目錄、Filesystem 資料根授權不阻擋未部署技能、同技能鎖、逐檔位元組雜湊；ASCII 名稱白名單、非字串及 Windows 保留名稱拒絕、尾點／空白別名、壞設定原因保留、缺快照不部署、真 manager 握手中止不誤報啟動、無 context 存檔及已驗證身分透傳。 |
| `test_evolution_verify_protocol_security.py` | MCP 回應決定成敗、雙平台例外與拆線失敗不部署、stderr 遮罩、rlimits、runpy 與 JailedWriter；新直譯器的 SDK／stdio 錯誤及配額診斷；`tests/helpers/evolution_stdio_peer.py` 提供兩個 Linux 隔離案例共用的標準函式庫真協定探針；三種回應逐一經 SDK modern wire 驗證，跨平台真 stdio 經 `connect_mcp_server` 完成握手、列工具與呼叫。 |
| `test_mcp_policy_carryover_wiring.py` | 重註冊保留政策、演化拒絕重部署且保留原條目及政策、改名停用仍不熱掛載、移除舊條目後重新審批部署、市集已安裝時不覆寫。 |
| `test_evolution_isolation_security.py` ⭐ | （C2-2 +4：演化經狀態 fd 回報失敗代碼、執行器層經 relay 跑、不在執行器範圍時拒絕）：演化 profile 只給 `managed/<skill>` 與 ST；建 venv、pip、驗證都經 runner（spawn 前才取得、之後 release、環境變數改寫、rlimit 由啟動器或 preexec 套用、`close_fds`）；`_evolution_grant` 偽造不了；拿不到 runner 不驗證也不部署；ST 不可用時只有 `direct` 同意能跑 |
| `test_isolation_selfcheck_security.py` ⭐ | （C2-2 +3：自測用可信直譯器與空環境、直譯器不可信有自己的原因）：失敗標記只認 `FAILURE_CODES`、不分原因 60 秒最多重探一次；子行程 stdin 一律 DEVNULL；spec 的 `parent_pid` |
| `test_isolation_status.py` | （第 3 輪 +1：執行器第一次健康檢查回來前判 `probe_pending`；C2-2 U2a 加 9：測試替身 `PinnedMonitor` 和真的 monitor 逐案比對層級、原因與降級去處；之後 +44：執行器層成立條件與各種不符、網路設定不安全判 none 降級到執行器、ABI 3 仍是 executor、`boot_id` 通知、健康快取、`level_for` 的兩種範圍、refresh 分兩段與單一飛行、開機例外要 9p 自測、原因排序、proxy 清 tmp、自癒掛鉤與定期修根）：重探期間保留前次結果與 660 秒等待計時；monitor 的兩個狀態與自測解讀（9p、ST、ABI、`landlock_unavailable`）；網路設定不安全（`net_tcp_fastopen`／`net_mptcp`）時本機 Landlock 仍可用、features 不含 `tcp`、層級 none、原因照實（不落到 `fs_9p`、不走 9p 回復路徑）、降級給 landlock、背景在 ST 重測後回到 landlock、核准的降級仍帶封鎖埠；`kernel_outdated` 或讀不到 kernel 時降級與審批卡一律 `direct`；ST `unset`／`not_writable`、kernel 雜湊（讀不到判 `isolation_check_failed`）、等待上限與快取、背景重探（一輪出錯後照樣繼續）、自測資料夾隨機名稱與殘留清理、取消時殺掉並等自測的啟動器、9p 恢復要通過 9p 自測、設了執行器目錄就照健康檢查判定 |
| `test_landlock_kernel_pure.py` | （C2-2 +79：狀態 fd、`net_strict` 與每次啟動讀網路設定、上限 1,024、可信直譯器與擁有者鏈、執行器 env 允許清單、Touka 標記與固定埠的 SSOT）：啟動器純函式——spec 驗證（版本、多出或重複的鍵、上限、路徑形狀）、依實際型別給權利（一般檔只給九個公開檔名、`st_nlink==1`、裝置只在 `/dev`）、ABI 截到 7、網路層最後（bind 與 connect 一起擋，0～65535 扣封鎖埠）、pin 預設且用 `keep` 回傳的 fd、`ENOTDIR`／符號連結略過；Fast Open／MPTCP 只讀檢查（ABI 3 不讀；不安全時另外回報在 `net`，檔案、bind 與一般 TCP 的檢查照跑、自測照樣通過） |
| `test_sandbox_runner_contract.py` | （C2-2 U2a 刪掉被逐案比對取代的替身契約測試；之後加 `net_strict` 依 features、執行器層給 ExecutorRunner、可信直譯器不可信就拒絕）：runner 只改寫啟動參數；`acquire` 的規則；spec 經 pipe（不放 argv、寫不下就拒絕、release 不關共用 fd、帶 `parent_pid`）；啟動器失敗代碼只認狀態 fd 裡的 `FAILURE_CODES` |
| `test_sandbox_kernel_stdlib_only.py` | （C2-2 加 framing、relay、owner_repair）：kernel 只匯入標準函式庫；複製到別處以 `python -I -S` 跑時用的是複本；非 Linux 一律 exit 125、不執行指令 |
| `test_mcp_isolated_transport_security.py` | 隔離 stdio 的 fd／release、長行、stderr 排空與遮罩、EOF 模式、owner 失聯、關閉升級、取消殺群組；client 的隔離透傳與 Windows 拒絕；真啟動判定、傳輸、SDK session 與 manager 串接的環境值不外洩，全部使用假行程 |
| `test_mcp_manual_start_wiring.py` | 鎖內拒絕原因與已由背景啟動的回應；等待計時與互斥計數、公開資訊與同意共用判定、lifespan 接線、單次同意、鎖內重驗、busy、拒絕安裝不報成功、空 context 排程、monitor 換替身與帳本重設；全部 sandbox／fake |
| `test_mcp_exec_env_no_leak_security.py` | Python／JSON 跳脫值、原文重疊區間、公開工具網址與 metadata 鍵、四字元門檻；解析後的多行環境值不進健康 API、工具列舉 log、失聯紀錄、伺服器顯示資訊、工具例外鏈與審批預覽；工具／resources／prompts API 錯誤、清單診斷名稱與關閉錯誤不洩值；owner 全關後釋放遮罩值；全部 sandbox／fake |
| `test_mcp_manual_start_security.py` | 人類手動同意、去處／features／generation／bound 核對、一次性與並行拒絕、features 上限與缺漏拒絕。 |
| `test_mcp_origin_sandbox_paths_security.py` | 各寫入點來源、技能安裝如實回報未啟動／缺 manager、非人類 env 變動撤銷授權、匯入與遮罩回填、非物件條目逐筆拒絕及壞既有條目撤權、單向標為自建、路徑存檔與啟動重驗；隔離設定缺 manager 不寫入、422／500 不排重連，成功 PUT 回 `background_relaunch`。 |
| `test_mcp_isolation_reconnect.py` | 並行重載與重試固定版本、缺少／過期 registry 的熱掛載與失聯重連、重載和熱掛載交錯不關閉新連線、五條啟動路徑的隔離判定與 fail-closed、principal stdio 拒絕且不重試、錯誤翻譯與握手例外遮罩、shared／principal generation、共用鎖與 busy、等待與失聯工具清單略過、並行上限、空 context 重連、去重升級、可控時鐘驗證退避及牆鐘前後跳不影響計時、epoch、單向關機與測試明確重開、重試途中關機、實際 cleanup 回呼順序、拒絕未判定或偽造 stdio 判定、直接啟動不帶 launch／isolated、缺 installation 的等待分支；全部 sandbox／fake |
| `helpers/mcp_transport.py` | 共用 `_FakeTransport`：可控制握手、斷線與收尾；供 isolation reconnect 與 connection owner 測試使用，不從測試模組互相匯入 |
| `test_isolation_linux.py::test_mcp_manager_eof_hook_reconnects_real_process` | **L10，Landlock**：真行程 EOF → manager hook 一次 → 新 PID 連線成功、舊群組沒有殘留、背景重連不繼承建立者 ContextVar；Windows 整個 Linux 模組跳過，真核心證據須在 Linux 補跑 |
| `McpServerItem.test.tsx`（ToukaChat） | 新增 既有 `last_error` 欄位收到 principal stdio 錯誤碼時，顯示 i18n 的原因與改法 |
| `test_executor_mcp_start_security.py` | kind mcp／stdio 的 START 合約、程式資料夾唯讀、state 與 cwd、exec_env；relay 本機 cwd 在 kernel，遠端 cwd 仍是自己的 home |
| `test_isolation_docker_assets.py` | 測試映像版本對齊 `uv.lock`；55 項突變的原文唯一、測試存在、範圍限定隔離程式及明列的 MCP 傳輸／生命週期／來源／匯入／演化檔案；多行突變支援 LF／CRLF 並保留完整還原內容；每輪 basetemp 等長，執行器 socket 保留長度餘裕；subreaper 的 PID 1 限制明列。 |
| `test_sandbox_state_compose.py` | 三份 compose 的 ST 掛載與 env 一致、只有 proxy 與開發版 init 掛；proxy 的 sysctls 剛好是 `net.ipv4.tcp_fastopen: 0`、沒有任何服務設 MPTCP；init 不遞迴；kernel COPY 在 `COPY . .` 之後、root 擁有、`a=rX`；預建 ST 0700；開發用設定檔不進映像 |
| `test_terminal_isolation_lifecycle.py` | 背景、停止、逾時、取消在 Landlock 啟動參數下照舊；C2-2 +4：逾時或取消時關狀態 fd、逾時殺掉 relay 的行程群組、背景停止經 relay |
| `test_executor_framing.py` | 控制通道的 frame——類型、長度上限（看到標頭就拒絕）、截斷、重複鍵、NaN、不是物件、ERROR 代碼、`block_ports_digest` 排序去重 |
| `test_relay.py` | （socketpair，Windows 也能跑）：請求格式、PDEATHSIG 與父行程核對、權杖每次從檔案讀（不跟連結，argv 與環境變數都沒有）、HELLO 的封鎖埠取 START 的 spec、`boot_id` 不同不送 START、EXIT 的代碼與信號、斷線與被拒的狀態碼、狀態 fd 設 CLOEXEC |
| `test_owner_repair_pure.py` | `repair_chain` 只補擁有者位元、拿掉 setid、只修一條鏈、只走原路徑、遇到連結／換檔案系統／別人擁有／多連結／特殊檔就停、尾段不存在當完成、只在 Linux、真的有改才算；第 3 輪 +12：`private_mode`／`repair_private` 另外拿掉群組與其他人的位元、整條鏈都收回、連結與別人擁有的根不碰、只在 Linux |
| `test_executor_server.py` ⭐ | （第 3 輪 +12：維護時自己的私有 inode 拿掉群組與其他人的位元、啟動時先收回再檢查〔設定其他項有錯時不碰〕、kernel 成員的參數名稱與種類和 Protocol 一致、背景工作遇到任何例外下一輪照常而取消會停；假 stream、假 spawn、`FsView`／`ProcessTable` 替身）：先查 peer 才算上限、HELLO 2 秒、權杖與版本、B 包含預設集合與上限、READY 欄位（和 `parse_ready`／`ready_issue` 的合約）、START 驗證矩陣（argv、env 允許清單與 `GIT_CONFIG_*`、讀寫只限目標與 state、cwd、`max_seconds`、`io`）、`parent_pid` 改寫、斷線＝STOP、規則集快取 2 份借出 dup、subreaper 判定、設定有錯時保持存活、清 tmp 不跟連結、伺服器端自癒、kernel 的 Protocol 成員核對 |
| `test_executor_client.py` ⭐ | 健康檢查每次連線重讀權杖；READY 嚴格驗證（必要鍵、只容許 `repairs`、型別）；連不上、權杖、版本、格式各自的原因；`ready_issue` 逐項核對 |
| `test_isolation_monitor_block_ports_security.py` ⭐ | monitor 的封鎖埠——「Touka 先開機、上游後開機，那個埠從頭到尾都在 B 裡」、只增不減、停用與探測失敗的連線也擋、三種讀取狀態（`exists()` 的例外不算不存在、執行緒失敗或取消不算讀到、結構不對算讀不到）、incomplete／overflow 判 none 且降級保留檔案隔離、溢位通知、健康檢查送實際的 B、SearXNG 不進執行器、金鑰不出現在 log／狀態／`GET /isolation`／例外訊息、新增連線（存檔、快照匯入、啟動補檔、系統還原）立刻進 B |
| `test_soul_delete_sandbox_home_security.py` ⭐ | （ADR-0272 限制 26 補記；+3：補過權限仍刪不掉或列不出來時回清不掉、不無限重試，名稱不是安全路徑段的 ID 略過）：刪除靈魂前清隔離家目錄——執行器那一層經 START 跑固定清除指令（只清這個 ID、空環境、cwd 是它的 personal_drive）、proxy 那一層直接刪且別的 ID 不動（0555／000 資料夾與唯讀檔也刪、補權限只在樹裡且不跟連結）、沒有執行器時只清 proxy 那一層；執行器不能用（不在 data 留空資料夾）、結束碼非 0、啟動器失敗代碼、逾時與取消（收掉 relay）都拒絕；連結不跟；重置報告的保留清單列出家目錄；`acquire_maintenance` 只准 home_wipe 用（AST）；端點先清再刪（`soul/` 已不在的孤兒也解析到 personal_drive）、清不掉 503 `SOUL_SANDBOX_HOME_NOT_CLEARED` 且什麼都不刪、密碼錯不會清 |
| `test_executor_runner_security.py` ⭐ | （+4：`acquire_maintenance` 只用執行器、不看 grant，網路設定不安全照樣在執行器、不退回 Landlock 或直接執行）：`ExecutorRunner` 的 relay argv 沒有權杖與指令、START 經 pipe、env 允許清單、`net_strict` 依 features、`max_seconds`；政策是 executor 時不在範圍或 READY 不見就拒絕（不退回 Landlock）；有同意時種類不低於卡片、features 包含卡片的；執行器降級去處與卡片的 `executor_eligible` |
| `test_isolation_block_ports_security.py` ⭐ | profile 用呼叫端給的 B、超過上限拒絕不截斷、Touka 自己的埠不截斷、Settings 每個 `*_URL` 與每個 `route_url_attr` 都已歸類、registry 停用的遠端也擋、registry 讀不到只記型別 |
| `test_permission_repair_security.py` ⭐ | （假掛鉤、假 EACCES，Windows 也能跑）：重試與不重試、只處理 Linux 上帶 filename 的 EACCES、巢狀只處理最外層、白名單（原路徑與 resolve 後都要過）與保護層、兩種政策拒絕（errno 是 None）、頻率限制與時間窗、`permission_relock`、`denied_burst` 與彙總告警、掛鉤出錯時原例外照丟、系統快照與記憶庫升級備份（ADR-0270）在白名單外、多執行緒計數 |
| `test_config_unreadable_fail_closed_security.py` ⭐ | （合併 ADR-0271 後 +2：autonomy 讀不到時送前最壞估算 `would_exceed` 也算超額、`complete_once` 不送出）：7 個設定載入器讀不到時不比檔案寬——auth_policies 啟動讀不到全部拒絕、autonomy 每一項最嚴（審查員逾時用模型預設）、MCP 寫入端讀不到不改檔、設定資料夾列不出來中止啟動、`/health` degraded；其他 5 個鎖住原本的行為；自癒不覆寫讀不到的檔 |
| `test_isolation_status_endpoint_security.py`、`test_health_system_notices.py`、`test_isolation_card_fields.py` ⭐ | `GET /isolation` 沒登入 401、只有層級與原因代碼（沒有路徑、權杖、digest、金鑰、代理人 ID）、`repairs` 的鍵與計數來自真的 monitor；MCP 互斥計數、設定讀取失敗代碼與狀態轉換警告；`/health` 的 `system_notices`；卡片的 `expected`、`net`、`block_ports_issue`、`local_issue`，創建照帳本層級、scaffold 顯示本機 features、合成未接線工具維持 direct。 |
| `ToukaChat/src/components/settings/tabs/mcp/__tests__/McpServerItem.test.tsx` | **新增**：等待／暫停／未隔離徽章、按原因恢復、不露原始原因碼；自建等待的自動恢復與手動入口說明、MCP 專用原因優先並沿用共用 hook；停用、遠端與未啟動不誤標 |
| `ToukaChat/src/components/settings/tabs/mcp/__tests__/MyServersView.test.tsx` | **新增**：常駐啟動入口與忙碌停用、三種去處與能力、雙語未隔離與 TCP 風險、最新來源／bound、取消／讀取失敗／未知能力、單次同意、拒絕後重新確認；非自建等待不給降級入口 |
| `ToukaChat/src/hooks/settings/__tests__/useMcpSettings.test.ts` | **新增**：busy／waiting 翻譯提示、刷新清單並解除等待、完整同意且不沿用、三種隔離拒絕碼翻譯與刷新 |
| `ToukaChat/src/components/sidebar/__tests__/IsolationBanner.test.tsx` | **新增**：四組 MCP 計數在隔離正常時仍顯示且不洩明細；五種無效計數不影響其他警告、保留有效計數與埠警告；預期為 none 時不計未隔離台數。來源只能往嚴與資料夾錯誤保留輸入沿用 `McpIsolationSection.test.tsx` 的案例 |
| `test_executor_compose_security.py`、`test_executor_server_cli.py`、`test_executor_e2e_assets.py` | （第 3 輪 +1：README 建議的 `docker-compose.override.yml` 被 git 忽略）：三份 compose 的執行器（允許的鍵、禁止項目、只接 `touka-exec-net`、volume 只在該在的服務、sysctl 只有 Fast Open、command 和預期的 roots 一致、healthcheck 與 `start_period`、GHCR 的 long syntax 與 subpath、`.env.example` 的三個上限）、Dockerfile 預建目錄與 git 層；`python -m` 命令列在設定有錯時保持存活；`tests/docker` 的端到端 compose 和正式的執行器逐鍵相同 |
| `test_isolation_junit_check.py`、`tests/lint/test_isolation_ci_workflow.py` | CI 的 junit checker（failed／error、skip 標籤、不安全分支必須 passed、拒收 PID 1、facts）與 workflow 形狀（types、REQUIRE、sysctl 步驟、兩個 pass、不可跳過、唯讀權限、容器 job 的步驟與 MPTCP 覆寫；容器 job 在 PR 改到隔離路徑時自動跑，帶 `isolation` 標籤或手動觸發強制跑；路徑清單含 `protocols/mcp/isolated_stdio.py`，與觸發條件都釘住；ADR-0279 起容器 job 在建好映像後跑 `tests/docker/permission_init_real.py`，路徑清單加 `ToukaHeartbeat/permission_init.py`） |
| `test_deployment_permissions.py` | GHCR `touka-init`（`ToukaHeartbeat/permission_init.py`）的契約，只用模擬中繼資料：帳號與權限能力、掛載重疊與別名、漂移、上限、描述元預算、只經描述元修改、輸出不含名稱；ADR-0279 的略過規則（符號連結、FIFO、socket、裝置、多連結檔、setid 檔不開啟；延伸屬性只讀不改；根目錄與兩個工作區根照舊失敗；只需改擁有者的 setgid 資料夾照常處理、還要補權限位元的在盤點時就拒絕；工具資料夾只在代理人或伺服器資料夾底下略過、只限資料夾、名單和 `SCAN_SKIP_NAMES` 相同；略過算進項目數、層數與清單位元組上限；無法解碼的檔名照常處理；略過的項目被換掉仍失敗）。真核心那一半在 `tests/docker/permission_init_real.py`（主機上用 docker 跑三種情況，資料只在 `touka-pinit-test-*` volume） |
| `tests/lint/test_executor_server_imports.py`、`test_isolation_boundary_no_full_env.py` | 執行器伺服器只匯入允許的模組、匯入的專案模組都在 `EXECUTOR_CODE_FILES`、不用 `Any`／SimpleNamespace；隔離邊界（含 `protocols/mcp/isolated_stdio.py`）不碰 `child_python_env`／`get_full_env`／`get_clean_env`（含別名、相對匯入、字串取用） |
| `tests/lint/test_resource_manager_permission_repair.py` | ResourceManager 每個碰檔案的公開方法都有自癒或豁免裝飾器（AST 遞移、兩份釘選清單、from-import 與帶 errno 例外的棘輪、自我測試）；ADR-0277 將外部 UI reader 委派列為讀取並登記既有 delegates 豁免，新增正反兩案，防漏標 |
| `test_settings_snapshot.py`（只列 C2-2 加的 2 個） | 匯入連線設定真的寫檔後才重讀路由（dry run 不呼叫）；匯入後自訂模型路由先關閉，要重新掃描或重啟才恢復 |
| `test_toplevel_protected_sync.py` | `TOUKA_TOPLEVEL_PROTECTED` 和 `git ls-files`、`.gitignore` 雙向同步（增刪 repo 頂層項目時要一起改） |

### 端點認證守衛 (Guard #24 — ADR-0034)

| 檔案 | 說明 |
|------|------|
| `test_guard_endpoint_auth.py` | `guard_endpoint_auth.py` 掃描器邏輯單元測試 |
| `test_grandfather_batch1_auth.py` | Batch-1（22 端點）：system/identity/permissions/scheduler/autonomy/skills/souls（scheduler 路由沒有掛載、ADR-0254 已刪，這幾個案例接受 401／403／404，現在拿到 404） |
| `test_grandfather_batch2_auth.py` | Batch-2（17 端點；`/mcp/scan` 已移除）：bots/custom_models/mcp/ui_sessions/settings |
| `test_grandfather_batch3_auth.py` | Batch-3（12 端點）：editor/models/terminal/stats/group_settings |
| `test_heartbeat_token_depends.py` | `verify_heartbeat_token()` Depends 純函式：reject、accept 與 SOLID-SRP 靜態確認 |

### SSOT 靜態守衛

| 檔案 | 說明 |
|------|------|
| `test_lint_ssot.py` | `lint_ssot.py` 主規則集：時間/環境/IO 監獄/本地匯入/raise 硬編碼等 |
| `test_lint_ssot_top_level_tests.py` | **ADR-0278**：repo 頂層 `tests/` 底下的檔案不算業務層，檔名含 tools、service 也不會被 IO 監獄擋（相對、`./` 開頭、絕對路徑）；`ToukaProxy/tests/`、`tests_old/`、`src/tests/`、產品的 services／tools 與 `*_service.py`、絕對路徑的產品檔、含 `..` 從 tests/ 繞回產品資料夾的相對與絕對路徑照舊攔 |
| `test_eslint_ratchet_guard_registration.py` | **ADR-0098／ADR-0278**：Guard #35 腳本、基準檔格式與 guard_all 註冊；收集違規時只跑 ESLint（ADR-0282 起不先驗 braces 補丁）；基準是 `{}` 而且沒有違規時通過，基準檔不存在時失敗 |
| `test_ssot_guard_io.py` | `SSOTGuard` IO Prison 層感知規則單元測試 |
| `test_ssot_guard_local_import.py` | `LOCAL_IMPORT_BAN` 規則正確性驗證 |
| `test_ssot_guard_path.py` | 物理路徑偵測規則（TID251）單元測試 |
| `test_guard_noqa_registry.py` | `guard_noqa_registry.py`：noqa 計數與 NOQA_REGISTRY.md 統計表比對 |
| `test_guard_adr_integrity.py` ⭐ | `guard_adr_integrity.py`（Guard #28，ADR-0068）：ADR 引用完整性（斷鏈）、編號連續性（跳號）、索引同步、修訂回指（單向修訂）四項檢查；含真實 repo 整合驗證 |
| `test_guard_native_tool_contracts.py` ⭐ | （Guard #45，ADR-0228；ADR-0253 加 4 個；ADR-0263 加 2 個：拿掉 `manage_duplicate_memories` 的 trusted `source_context` 就報錯、在它裡面加 `**kwargs` 轉手就報錯）：原生工具合約守衛在合成來源上的接線與拒絕案例；ADR-0253 起守衛從 Memory 合約檔讀 `MEMORY_INTERNAL_ACTIONS`（加一個內部 action 時守衛照樣通過），非字面值、混入公開 action 或重複宣告都報錯 |
| `test_native_contract_invariants.py` ⭐ | （ADR-0253，對真的執行期表格）：`MEMORY_INTERNAL_ACTIONS` 執行期值與守衛讀到的內容都剛好是 `{"export_facts"}`（加內部 action 一定要改這個測試）；七個公開 schema 都不廣告執行期保留欄位；`guard_service` 的「工具.action」訊號都在合約表裡；ADR-0256 起保留欄位加 `source_context`、`source_resolver`，ADR-0263 再加 `safety_flags`、`superseded_by`，個數不變 |
| `test_guard_environment_parity.py` ⭐ | 實際RUN/apk exact Python pin、缺失／歧義／錯minor／echo與escaped separator拒絕，不可digest-only靜默skip；`guard_environment_parity.py`（Guard #30，ADR-0071 amended by ADR-0117）：4個Python workspace members的base＋optional dependencies／dev group逐項對照uv.lock，並要求installable workspace member為editable source；另驗Python版本（py312／multi-stage Dockerfile／requires-python／ruff）與禁loose `>=`。**Check 4** venv-parity純比對器維持extras未裝不報、`--venv` advisory exit 0。本批Guard #30 Green。 |
| `test_pytest_residue_safety.py` ⭐ | **ADR-0019／Guard #16 pytest-residue 安全邊界**：repo root／absolute／relative／`..`／mixed-case／drive-relative／UNC／sibling-prefix／junction parent containment；cache 缺失、目錄／檔案拒絕、hard link、identity race；Windows junction 指向 root `data/` 與 `ToukaProxy/data/` 時只報 redirect、不得遍歷或讀內容。 |
| `test_pytest_basetemp_plugin.py` ⭐ | **pytest 啟動期 basetemp policy**：CLI、`PYTEST_ADDOPTS`、pytest config 三種顯式 repo-local 來源皆轉 `UsageError`；可證明位於 repo 外者接受。 |
| `test_pytest_skip_reasons.py` | （ADR-0251）：`pytest.ini` 的 `addopts` 用 `-r` 時同時含 `f`、`E`、`s`（`-r` 會整個換掉預設的 `fE`），結尾列出失敗、錯誤與每個跳過的原因 |
| `test_session_sandbox_cleanup.py` ⭐ | （ADR-0251）：`tests/sandbox_cleanup.py` 先跑釋放步驟再刪（模擬 WinError 32）、一步失敗不擋其他步驟、晚到的背景寫入不會把沙盒建回來、刪不掉回傳殘留、已不存在時不做事；conftest 的 `SANDBOX_RELEASE_STEPS` 釘住五步（等稽核寫入、等記憶工作者、關 Qdrant、關 SQLite、關 log handler；兩個等待都在關閉之前）；Qdrant 只在向量模組已載入時關；等稽核寫入在直譯器結束時當成已清空、其他拒絕照報；atexit 殘留寫 stderr、fork 出的子程序不刪父程序的沙盒；結尾摘要列出殘留 |
| `test_installation_entrypoints.py` | （ADR-0251 新增 8）：安裝與啟動腳本只在假指令的檔案系統裡跑（9 個 bash 測試用 Git for Windows 的 `bin\bash.exe`）；新增：從 `cmd`、`bin`、`mingw64/bin`（不分大小寫）的 `git.exe` 都找得到 `Git\bin\bash.exe`、`cmd`／`bin` 位置不多往上找、找不到回 None 不退回 WSL、缺 bash 的跳過訊息提到 Git for Windows |
| `test_public_repo_hygiene.py` | 首裝管理員密碼檔 `ToukaProxy/config/INITIAL_ADMIN_PASSWORD.txt`、代理人建立的 `config/mcp_servers/`，以及代理人新建的技能（`config/skills/`）與評測（`config/evals/`）被 repo 內 `.gitignore` 的規則忽略（不認 `!` 規則，也不認全域 excludes 或 `.git/info/exclude`）；repo 自帶的技能與評測不被忽略（沒有規則命中，或命中的是 `!` 規則）；三份 compose 的 proxy `HOST_WORKSPACE_ROOT` 預設值是 proxy 自己的某個掛載點、三份相同（不寫死主機路徑），而且只有那個掛載的來源跟著 `WORKSPACE_ROOT` 時值才跟著；版控中的 `.env*.example` 沒有生效中（未註解）的磁碟代號絕對路徑 |
| `test_rebuildable_outputs_declaration.py` ⭐ | （ADR-0251）：根目錄 `rebuildable-outputs.txt`（開發流程的清理檢查讀的建置產物宣告）是一般 UTF-8 檔、沒有 BOM 與空行；每行照清理檢查的格式（相對、`/` 分隔、沒有萬用字元／`..`／`\`／`#`／反引號／前後空白、不含 `.git`／`.toukadev` 段與可重建快取名稱、不重複）；每行被真正的 git 規則忽略（不認 CRLF 空白行變成的空 pattern 與 `!` 規則）、底下沒有被追蹤的檔 |
| `test_guard_dependencies_pytest_residue.py` ⭐ | **Guard #16 fail-fast 接線**：unsafe residue 時不得解析快取位置、建立／重建依賴 cache 或執行 member probe；clean 時既有 member 依賴流程照常。 |
| `test_guard_dependencies_cache_location.py` ⭐ | **Guard #16 快取位置與成員鎖（ADR-0240）**：真 git 的主資料夾與 linked worktree 解析到同一個 `.git/touka-guard-cache/<平台>`；別的 repo 子資料夾（不是 toplevel）退回舊位置；worktree 內執行後不多出任何路徑；第二個 worktree 命中共用快取、不跑 `uv sync`，匯入仍用自己的原始碼；另一行程握鎖時（含 `--cache-only`）fail-closed 且不動快取；`uv sync` 與匯入執行期間鎖確實被握住、結束後釋放；`uv sync` 失敗不寫快取鍵；鍵的每個輸入（`--package`／`CACHE_KEY_SCHEMA`／`uv.lock`／成員 pyproject／`.python-version`）改變都會重建，不依賴鍵函式本身；非 git 與 4 種 git 失敗退回舊位置；`--cache-only` 不建根目錄也不建鎖檔；舊快取只提示不刪除。 |
| `test_guard_dependencies_native.py` | **原生Guard16（cache seed改用`_cache_key`，ADR-0240）**：缺失／過期cache的cache-only零寫入、CLI不建立cache、member依賴失敗不fallback、真child在product code前套用現有smoke sandbox。 |
| `test_guard_source_data_boundary.py` | **來源掃描邊界（含 `.toukadev`）**：三個既有scanner在metadata／下探前排除data與大小寫變體；直接target、symlink／junction及file reparse皆以合成spy驗證，不碰實體資料。`.toukadev`（ToukaDev本機工作區）只依名稱略過：對照組＋監看證明不stat／不下探（含`.ToukaDev`與巢狀）；根目錄在`.toukadev/worktrees/x`時仍會報（防路徑逐段比對掃0檔假綠）；`toukadev`、`.toukadev-notes`照掃；`.gitignore`必含`.toukadev/`且`git ls-files`沒有任何`.toukadev`路徑段。 |
| `test_guard_all_frontend_runtime.py` | **原生聚合器（Q38新增2）**：exact runtime/profile、default43/non-audit41、失敗exit與cache-only；`--audit-evidence-root`只在default audit模式create-only建立repo外新根，再轉送python/node leaves，拒絕相對／repo內／既有根；non-audit不建立root且22/23 NOT RUN。四個聚焦模組共119 passed（mocked）；另由2026-09-08原生整合真正執行default44與六個fresh audits，兩類證據分開保存。 |
| `test_docker_deployment_collection.py` | **Docker明確執行邊界**：import不probe、未opt-in零Docker、opt-in可用／不可用、健康／不健康精確判斷。 |
| `test_sandbox_env_restoration.py` ⭐ | 沙盒環境變數還原：`setup_test_environment` teardown 必須把 `TOUKA_CONFIG_DIR`/`MEMORY_APP_DATA_DIR` 還原到 session 沙盒，**且早於**單例重置——否則 `skill_service` 會快取住外來 config 路徑並外洩到下一個測試（歷史事故：字面相對路徑 `"dummy_sandbox_path"` 以 cwd 解析 → 技能檔寫進 repo 弄髒 git）。搭配 `conftest.py` session 級「repo 根目錄增量」哨兵 |
| `test_app_context_isolation.py` ⭐ | **debug-escalation DBG-A**：直接驅動 function-autouse fixture，鎖定 `app.state.context` 的結構存在狀態與內容還原；防止首個 lifespan 在 fixture setup 後建立 context 時，把測試 fake `protocol_manager` 洩漏給不啟動 lifespan 的後續測試。 |
| `test_error_definitions.py` | 驗證 `errors.json` 中所有定義的錯誤碼 ID 均唯一且不重複 |
| `test_bcrypt_import_rule.py` | `BcryptImportRule`：封鎖非白名單 bcrypt/passlib 直接匯入 |
| `test_sqlite3_import_rule.py` | `Sqlite3ImportRule`：封鎖非授權白名單的 sqlite3 直接匯入 |
| `test_guard_python_cve.py` ⭐ | （實數；這次偶發修正 +13：pip-audit 的 stdout 是空的就重跑一次、兩次各留證據；兩次都空回報「掃描沒跑成」，仍判紅、和有漏洞與證據無效分開；有內容的輸出不重跑；啟動失敗或逾時也算沒跑成、不重跑）：ADR-0222／0223、Guard #22：runtime／build-test frozen exports、全部 marker query pairing、來源身分與 first-party coverage；保留原 parser／skip／timeout／raw evidence／無憑證 child 界線。缺列、錯配、skip、工具錯誤與 findings 均 fail-closed。 |
| `test_guard_node_cve.py` ⭐ | ADR-0222／Guard #23：ToukaChat／ToukaIDE各runtime／full四scope aggregate；以絕對Node直接啟動`npm-cli.js`，驗證tracked `.npmrc`、official-registry lock entries與npm JSON v2 count；禁止shell shim、fix／force／retry／install與dependency mutation；零fetch retry與解析前原始證據保存；ADR-0282：lock缺SHA-512就擋、掃描途中`.npmrc`／`package.json`／lock被改就中止（快照在讀policy之前）、full範圍的非braces high照常阻擋並掃完四個範圍、失敗時列出套件與GHSA且`via`形狀怪也不改判定、SHA-512錯誤指出是哪個lock entry、缺來源檔時回固定訊息不帶路徑、沒有`--profile`。 |
| `test_p11_python_dependency_security.py` | Python 安全下限、完整同名 lock rows、宣告與獨立 build pin；PyJWT 下限與兩個宣告期望同步為 2.15.1，Azure extra 維持 dormant。合成 TOML 拒絕 mixed-old-row、缺失／pre-release；版本測試不冒充 fresh audit。 |
| `test_python_transport_token_security.py` | 保留既有合成 key／stream／無網路案例；新增 padded／unpadded HS256 token 正常解碼及錯誤簽章拒絕。新增案例取自已審普通相容測試，不納入新 depth／junk cases；本列不宣稱整個模組已實跑。 |
| `test_p11_build_tool_compatibility.py` | 更新後既有 build/test 工具的實際功能相容性；與上述版本模組共 23 passed，納入本批 162 focused GREEN。 |
| `test_dependency_audit_coverage.py` | ADR-0223 完整 query pairing、跨平台 marker、first-party runtime／build 第三方依賴、CPU advisory 來源、畸形輸入零回顯與空 query 拒絕。 |
| `test_p11_node_dependency_security.py` | **M4**：各scope安全floor（2026-10-25 起含hono 4.13.11、ip-address 10.7.4、smol-toml 1.9.0、vitest 4.1.11、@vitest/mocker 4.1.12，IDE另有MCP client 2.3.0）；只准退役IDE ip-address缺席，任意nested不安全版本重入仍拒絕。 |
| `test_p11_torch_cpu_compatibility.py` | CPU lock／來源／manifest／合成圖案例檢查全部 rows／edges、Darwin 分支、五類既有 cp312 wheel、SHA256 與 GPU 拒絕；另有真實 CPU 案例，使用本機 BoW→Dense→Normalize 驗證 encode 數值／批次順序／Tensor 與 NumPy、零向量，以及 VectorStore 真實 encode 與 cache。沿用既有 sandbox／mock，無模型下載。額外官方 CPU wheel 接受且缺既有 wheel 拒絕的判定修正先 **1 failed／1 passed**，再 **2 passed**；完整模組由 R2 聚焦 84 passed 與完整 Python 5342 passed 涵蓋。Windows ARM64 額外 wheel 僅為 lock 產物，不宣稱跨平台執行或 release qualification。 |
| `test_dependency_audit_evidence.py` | create-only證據目錄、拒絕repo內路徑、拒絕link／reparse與root→leaf檢查順序；全為合成測試，不建立實體link。 |
| `test_evolution_tool_ssot.py` | 演化工具 SSOT 靜態驗證：`propose_new_capability` 呼叫合規性 |
| `test_system_tool_ssot.py` | 系統工具 SSOT 靜態驗證；ADR-0146 `agent_control(get_status)` 必須以 `rhythm_service` 計算剩餘工具輪 |
| `test_smoke_sandbox_governance.py` | 外網只限先過 preflight 的人工實測、smoke 吞掉違規時非零結束（ADR-0241）；ADR-0214 manifest、pre-import sandbox、credential-shaped env scrub、cleanup fail-loud、24-script contract、chat mock／domain-error 防假綠，以及C／Z兩個合成drive下跨磁碟負例只使用sandbox root。 |
| `test_smoke_production_repairs.py` | Docker app-data absolute override、execution metadata filtering、sync resolve、Factual／Vector cleanup lifecycle |
| `test_live_api_consent.py` | CLI early preflight、exact scope capability、request／token hard bounds、temp writes、provider mismatch／unsupported route fail-closed；conftest 先裝網路封鎖與 HF 離線旗標、registry 金鑰名 ⊆ 清空清單（ADR-0241）；scorecard／tool_eval episode CLI 主迴圈只送 1 次、`memory_eval_probe` 早期閘、沙盒先於產品與評估框架 import、四家 send_round 核銷、不被自動找到（ADR-0261）；r55（ADR-0271）operation 集合多 `manual_thinking_latency_probe` |
| `test_network_guard.py` | （ADR-0241；ADR-0247 加 SDK 1.x httpx2 傳輸＋注入 client 的 messages.create 只試一次即被擋）：本機／外部判斷表、install 只掛一次、getaddrinfo／connect／sendto 與 httpx／httpx2／SDK／其他執行緒皆被擋並記錄、本機轉發代理繞不過、loopback 與新 event loop 照常、sentinel 讓測試失敗 |
| `test_live_chat_probe_gemini.py` | （ADR-0241；R-3b 加 probe 的 Claude client 掛位元組計時、503 開關那支釘精確狀態清單；ADR-0248 起 probe 改用主迴圈共用的重試開關 `react_core.MAX_SEND_ATTEMPTS=1`，503 只送 1 次、設 3 就送 3 次）：probe 的 Gemini 只送 1 個請求（503／連線錯誤不重試）且帶 `maxOutputTokens`、產品 adapter 不變、`--env-file` 驗證與核准前不匯入 provider |
| `test_uv_dependency_cooldown.py` | （ADR-0241）：`exclude-newer = "7 days"`、不得有 `exclude-newer-package` 例外 |
| `test_openapi_script_sandbox.py` | （ADR-0250 B1，禁令 63；純 AST，不匯入腳本）：`update_openapi.py` 在任何 `src`／`awaken`／`memory_server` 匯入前先呼叫 `enable_smoke_sandbox`、產品匯入只在模組頂部、輸出位置由 `Path(__file__)` 推出（不用 `get_workspace_root`）；`openapi_merge.py` 是純函式、不匯入產品程式，入口不重複定義合併函式；`update_docs.py` 第一個專案匯入就是 `update_openapi`；測試改從 `openapi_merge` 匯入 |
| `test_heartbeat_models_sync.py` | Heartbeat 模型與錯誤碼的投影由單一純來源產生（`sync_heartbeat_models`、`sync_errors`），檢查漂移不改檔、只補缺的錯誤碼、不讀私有設定；合併 openapi 遇到同名不同內容的元件拒絕（ADR-0250 B1 起從 `openapi_merge` 匯入、`TRANSPORT_MODELS` 直接取自 `src.models.session_persistence`） |
| `test_session_persistence_contract.py` | （可靠性 R2）：保存／終態公開模型由單一 Pydantic 定義（結果 round-trip、無效結果不會變成已保存、generation 與 persistence 分開、具名 discriminated union）；實際 openapi 合併含 transport 合約、名稱衝突拒絕、nullable 投影與 FastAPI 一致（ADR-0250 B1 起從 `openapi_merge` 匯入） |
| `test_knip_worktree_parity.py` | （ADR-0250 K1）：`guard:prune` 帶 `--no-gitignore`；knip `project` 保留預設原始碼樣式；被 gitignore 的產生檔與建置產物都在 `project` 排除裡（只放 `ignore` 會讓沒用到的 export 被當成有用到）；`.gitignore` 落在 ToukaChat 底下的每一條規則都要分類；`guard:prune` 以限定範圍的預載腳本跑 knip（由已退役的 braces 入口測試移入，ADR-0282）。只讀文字檔 |
| `test_no_hardcoded_model_ids.py` | （ADR-0250 A2）：選模型的檔案（SoulStudio、ModelStudio、ChatProvider、ModelProvider、IDE 行內補全與 `package.json`、`brain_persistence`；ADR-0254 加 `custom_model_service.py`）沒有 `claude-`／`gemini-`／`gpt-`／`deepseek-`／`grok-` 開頭的模型 ID 字面值；樣式本身抓得到已知的舊預設。價格表、context window 表不在範圍 |
| `test_chat_npm_test_generates_runtime.py` | （ADR-0250 C7）：ToukaChat `npm run test` 先產生 artifact runtime、再跑 Vitest；產生步驟執行的是 runtime builder。只讀文字檔 |
| `test_model_manager_ssot.py` | `ModelManager` 內部不得繞過 ResourceManager 存取路徑 |
| `test_custom_model_service_ssot.py` | `CustomModelService` 路徑操作只能透過 ResourceManager |
| `test_identity_anchoring_guard.py`（lint/） | `IdentitySovereigntyRule`：偵測 `agent_id: str` 函式簽章 |
| `test_react_core_ssot.py` ⭐ | react-core-extraction E4 守衛錨（ADR-0156）：ReAct 迴圈指紋（`while True`/`wrapping_up`/中斷字串/`consume_round`/`react_slots_event`/`ToolExecutor`/`collect_known_tool_names`）**唯一存在於 `react_core`**；三 handler 薄殼與三 adapter 皆不得含（防 inline 回退＝ADR-0155 病灶復發） |
| `test_adapter_contract.py` ⭐ | ProviderAdapter 契約套件（ADR-0156/0157）＝Protocol 語意 SSOT：request 整備（取樣參數/stream 剝除、Grok safety、degraded 拼接、tool 訊息 content 保字串）、parse（reasoning pop＋`<think>` 剝離、壞 JSON args、空 choices）、denied turn/工具結果 wire 形狀、usage 四欄正規化、OpenAI／Gemini 一律送 token_usage 事件（ADR-0244）、finalize 重整形；claude 專屬＝雙 egress（native 照常入帳但不送 token_usage 事件，ADR-0244）、thinking 逐 block 分頻、耗盡輪形狀、ephemeral 斷點輪替、ingress 轉換後實名遙測（3a 起不改寫別名，ADR-0247）；ADR-0157＝三家 Honesty Guard（openai required/claude any＋thinking 例外/gemini SSOT 文案；一次性閂鎖＋工具輪後強制解除）＋gemini thought 分頻（K3） |
| `test_provider_tool_schema_egress.py` ⭐ | （ADR-0266）：跨廠商工具出場形狀，全離線（adapter 只跑 `prepare_request`，不送出、不建連線）。真的 ToolLayer 組出實際列給模型的工具（含根層 `oneOf` 的 mission_tools、memory_tools、edit_soul），逐家核對：Anthropic 根層無組合子、是 object、欄位不少於正本根層＋分支、required 和正本相同；Gemini 不支援的欄位（轉換器清單＋官方 Schema 物件可用欄位表）都不在；OpenAI 相容／Responses 根層是 object；出場不改模組層 schema。另有根層 oneOf 的 MCP 工具（同名以根層為準、必填只留根層）、欄位只在分支裡的 MCP 工具（含 `$ref`／`$defs`；分支之間定義不同合成 `anyOf`，ADR-0285）、判別欄位只在分支裡（每個值都選得到、相同的只留一份、依出現順序、不改輸入）、同名欄位跨組合子依組合子順序、怪輸入不拋例外、輸出固定、不改輸入、Touka 自己的工具送給 Claude 的欄位等於正本根層（以後新增的原生工具分支不得有衝突的同名欄位）、原生入口的 Anthropic 形狀直通、直通回新 dict（寫 `cache_control` 不改輸入）、沒有 input_schema 的伺服器工具原樣複製、缺根層 type 補 object（兩種形狀） |

### 身分、RBAC 與權限

| 檔案 | 說明 |
|------|------|
| `test_role_plane_characterization.py` ⭐ | **ADR-0141／0239**（ADR-0254 刪掉排程閘道少 9 個，PD 16 → 15）：凍結全部角色閘的放行矩陣與精確錯誤碼（`AUTH_PERMISSION_DENIED`／`AUTH_ADMIN_REQUIRED`）、None 身分拒絕、`require_authenticated` 與 `require_admin_human` 矩陣；ADR-0239 改為共用 gateway 前後結果相同。 |
| `test_permission_matrix_baseline.py` ⭐ | 權限表示法 P0：矩陣 case，凍結生產／fallback 的 Gate 1、Gate 2、技能與 MCP 答案；P2 後兩閘門分歧歸零且僅 4 個指定 Gate 2 cell 翻轉；ADR-0245 後 Guest 那一列改為 Subagent（同一份 13 項） |
| `test_guest_role_retirement.py` ⭐ | **ADR-0245**：Guest 退役——子代理的 Subagent 角色工具與改名前的 Guest 逐項相同（字面值清單）；Owner 不再繼承 Guest、答案不變；舊 permissions／identity_map 的 Guest 值讀取時略過、記警告、不改檔（靈魂被覆寫成 Guest 時改當 Base，不升級成 Owner）；舊 Guest 不當 Subagent 用，沒有 Subagent 的舊檔＝子代理沒有工具 |
| `test_soul_only_agent_roles.py` ⭐ | **ADR-0245**：只有靈魂、DebugAgent、DispatchAgent 套用 override／tag 規則與 default_role，其他 ID 一律 Base、無能力；舊 `ToukaSoul<靈魂>` 前綴 ID 在 `resolve_role_and_caps`、`get_identity`、token 驗證都是 Base、無能力、不能喚醒，真的靈魂照舊 Owner＋能力（R-2a）；被略過而原本會給角色或能力的設定項只記一次警告、不改檔（tmp 合成檔）；X-Agent-ID 只能切進靈魂（admin／擁有者切非靈魂→403）；`POST /identity` 拒非允許 ID（422＋`rejected_agent_ids`），`GET /identity` 與 `/identity/agents` 只列允許 ID，存檔保留檔內被略過的舊覆寫；前端 `ASSIGNABLE_AGENT_ROLES` 與後端一致 |
| `test_permission_grants.py` ⭐ | 權限表示法 P1：純值測試，涵蓋 `NameGrant`／`McpGrant` 的 open-ended、deny-all、拒絕優先、self-only 繼承與全域地板 |
| `test_permission_service.py` | 權限表示法 P2／P5：`resolve_grants` 唯一解析器、全入口委派、Owner 零硬編、全域工具／MCP 拒絕地板，以及執行期 reload 語意 |
| `test_permission_config_reload_resilience.py` ⭐ | 權限表示法 P5：啟動期壞設定 fail-fast；熱重載壞設定保留 last-good、audit＋degraded health＋成功復原；不存在／空白／`{}` bootstrap fallback |
| `test_mcp_tool_governance.py` | ADR-0195 軸 A／B執法；P2 補角色 MCP deny-all 與 self-only 繼承 |
| `test_mcp_2026_p1_boundary.py` | **M4**：modern-only設定邊界、完整能力／版本驗證與失敗關閉；creation 啟動環境改用白名單常數斷言（ADR-0254），仍釘住不繼承父環境金鑰、只加 spec env。 |
| `test_mcp_2026_p2_cutover.py` | 真 discover／adopt、工具資料面與拒絕舊協定；dep1 起釘 mcp 2.2.0，factory 另斷言轉址 response hook。 |
| `test_mcp_2026_p5_features.py` ⭐ | **ADR-0203 / MCP 2026 P5**：完整 tool descriptor＋invalid schema quarantine、Draft 2020-12 composition/conditional/internal refs、external ref／depth bomb、全 cursor／loop bounds、resource URI/MIME/base64/DLP、strict untrusted prompts、無 Memory MCP 重曝，以及 modern／isolated legacy 真 stdio 的 tools/resources/templates/prompts data plane |
| `test_mcp_2026_p5_endpoints.py` ⭐ | **ADR-0203 / MCP 2026 P5 HTTP**：Pydantic tools/resources/templates/read/prompts/get response、完整 descriptor、POST read/get、惡意 URI 422、角色閘與 MCP server grant 403 |
| `test_mcp_2026_p6_runtime.py` ⭐ | **ADR-0204 / MCP 2026 P6**：per-installation/principal cache partition、TTL/scope、subscription stale/invalidation/backoff、header annotation/secret/DoS、icon SSRF/MIME/size/SVG 與 OTel allowlist/redaction |
| `test_mcp_2026_p7_mrtr.py` ⭐ | **ADR-0205 / MCP 2026 P7**：tools/call、resources/read、prompts/get ephemeral MRTR；opaque state echo、accept/decline/cancel/timeout、round/bytes cap、retry 前撤權、headless decline、unsupported input/schema 與 local-only interaction event |
| `test_documentation_contracts.py` ⭐ | **ADR-0116 文件 ratchet**：current-facing 指南不得重新宣稱已退役的 ToukaMemory standalone／fallback 模式；核心契約面必須保留整合模式錨點；AGENTS.md 禁令編號必須連續且與宣告範圍一致。 |
| `test_docker_compose_config.py` | 正式 local 四個 loopback ports、UID1001／cap-drop／NNP 與 root init 例外；pull compose 只用 ghcr 映像、無 `build:` 與源碼掛載（原 Guard #43 職責）；不啟動 Docker。 |
| `test_mcp_modern_companion.py` | **M4**：真Proxy與Node loopback modern discover、工具schema／Unicode/null、Bearer／Host／Origin與兩client取消隔離；Python 產生的「由人核准」標記由真 Node 驗證器接受、沒帶就不算（ADR-0246 U2c-7）；舊路徑零請求，非VS Code host或live cutover。 |
| `test_docker_build_inputs.py` | 三個產品 Dockerfile 的外部輸入皆 digest 釘版、Debian 基底、執行期標籤；各 recipe 明確 COPY 的來源檔存在且在對應 dockerignore 規則下進入 context；private／secret／cache 排除不被例外重新打開；proxy root context 以 `**/.toukadev` 在任何深度排除 `.toukadev`（worktree 副本與 lessons 紀錄不進映像）；三個 ignore 檔的例外（`!`）規則不含萬用字元（否則 BuildKit 會走進被排除的資料夾，碰到打不開的連結就建置失敗），`config/*.json.example` 範本不靠例外也照常出貨、活體 `*.json` 不出貨。 |
| `test_toukaide_deterministic_vsix.py` ⭐ | **ADR-0217／0218**：`touka-ai.toukaide@0.3.0`／MIT metadata、root LICENSE LF checkout＋sealed external staging、真vsce canonicalized exact 10-member allowlist、unsafe／duplicate／native／identity drift rejection、排序／ASCII escaped／截斷／bounded diagnostics及byte-deterministic create-only ZIP。 |
| `test_toukaide_marketplace_candidate.py` ⭐ | **ADR-0218**：`@vscode/vsce@3.9.2` direct lock pin、exact Node/npm CLI identity、clean HEAD＋`v0.3.0` anchor、實際 package／lock／devEngines 與 builder Node24.19/npm11.19.1 一致性、local-only package command、staged prepublish removal、repo外create-only VSIX＋canonical manifest、explicit bytes→strict UTF-8 child boundary，以及dirty／tag drift／overwrite／redirect／malformed runner rejection。Windows symlink case在無權限host明示skip。 |
| `test_mcp_oauth_api.py` ⭐ | **ADR-0206 / P8-A**：start/status/disconnect/admin/callback route、身分/Origin、消毒 ErrorCode、callback streaming body 早停，以及 query success／parse/runtime/unavailable 皆以 no-store HTML 清除 current history。 |
| `test_mcp_oauth_bearer.py` ⭐ | **ADR-0206 / P8-A＋P8-B delegated refresh**：metadata drift／durable clock fence、refresh scope/token 語意、invalid_grant tombstone、dynamic authority revoke 與 refresh 前重驗；Basic／Post permit form fields 與真 HTTP header/form shape exact match。 |
| `test_mcp_oauth_bootstrap.py` ⭐ | **ADR-0206 / P8-A**：feature gate、none/static 零 key/DB I/O、managed/external key、query canary、lifespan cleanup 與 injected clock/sleeper maintenance。 |
| `test_mcp_oauth_full_flow.py` ⭐ | **ADR-0206 / P8-A**：mock AS authorization-code＋PKCE、callback、grant 與 request bearer 端到端。 |
| `test_mcp_oauth_keyring.py` ⭐ | **ADR-0206 / P8-A**：32-byte key parser、AES-GCM/AAD、retiring ring、managed cross-process atomic generate/rotation 與 external fail-closed。 |
| `test_mcp_oauth_manager.py` | **M4**：現代連線OAuth憑證管理與敏感值邊界。 |
| `test_mcp_oauth_mode_baseline.py` ⭐ | **ADR-0206 / P8-A**：feature off、P8-B modes 與 headless delegation 明確 unsupported。 |
| `test_mcp_oauth_outbound.py` ⭐ | **ADR-0206 / P8-A**：HTTPS/DNS/IP-pin/redirect/proxy/HTTP2/content-type/bytes bounds、metadata/CIMD freeze 與 credential redaction。 |
| `test_mcp_oauth_profile.py` ⭐ | **ADR-0206 / P8-A**：profile schema/generation、scope exact/wildcard、callback binding、mutation fence 與 exact installation＋origin＋CIDR 私網例外。 |
| `test_mcp_oauth_protocol.py` ⭐ | **ADR-0206 / P8-A**：callback parser outcome/method/mode/bounds、PKCE S256、state digest、slot/grant tuple 與 browser-safe projection。 |
| `test_mcp_oauth_registration.py` ⭐ | **ADR-0206 / P8-A**：CIMD 優先、trusted DCR lease/version、crash orphan 邊界與 stale/clock fence。 |
| `test_mcp_oauth_rotation.py` ⭐ | **ADR-0206 / P8-A**：persisted rotation FSM、state-drain、active-worker lease/convergence、stale worker 排除、retire 與 external-source unavailable。 |
| `test_mcp_oauth_runtime.py` ⭐ | **ADR-0206 / P8-A**：installation/profile binding、動態 authority、issuer selection、status/disconnect、P8-B source fence 持續涵蓋實際 disconnect、callback commit、cancellation、private CIDR policy 與 return-boundary exact grant freeze。；ADR-0281 T1：組工具清單的授權查詢不推進時鐘、授權 fence 不符或伺服器停用時回否且不查儲存 |
| `test_mcp_oauth_secret_surfaces.py` ⭐ | **ADR-0206 / P8-A**：access/refresh/code/verifier/client-secret/state canary 不得進 API、log、prompt/tool arg 或 popup payload。 |
| `test_mcp_oauth_secret_writer.py` ⭐ | **ADR-0206 / P8-A**：managed key current＋retiring＋phase＋epoch 單次 atomic `.env` update。 |
| `test_mcp_oauth_service.py` ⭐ | **ADR-0206 / P8-A**：persist-before-redirect、consume-before-exchange、stale commit、issuer mismatch、bounded revocation 與 exact disconnect。 |
| `test_mcp_oauth_store.py` ⭐ | **ADR-0206 / P8-A**：SQLite schema、AES payload/HMAC state、slot pointer、CAS/lease/fence/expiry/cleanup、clock watermark、duplicate live row／corrupt ciphertext fail-loud 與 exact tombstone。；ADR-0281 T1：組工具清單的只讀授權查詢前後資料列完全相同（含過期沒有 refresh 不改狀態）、懸空指標丟損毀、不解密且一句讀完 |
| `test_mcp_oauth_transport_auth.py` ⭐ | **ADR-0206 / P8-A**：hardened request transport 和 SDK bearer resource transport 皆不接受 cleartext/proxy 旁路。 |
| `integration/test_mcp_oauth_backup.py` ⭐ | **ADR-0206～0207 / P8-A＋P8-B**：ZIP/env 排除兩套 DB/WAL/SHM/key/rotation secret，P8-A/P8-B 任一目的 store/key collision 都在寫入前 fail-closed。 |
| `integration/test_mcp_oauth_dcr_multiprocess.py` ⭐ | **ADR-0206 / P8-A**：Windows spawn 多 worker DCR lease 只有一個 authoritative network winner。 |
| `integration/test_mcp_oauth_logging.py` ⭐ | **ADR-0206 / P8-A**：Proxy/nginx/Uvicorn logging canary、exact callback `access_log off`，以及 root／standalone Compose command 不得覆寫回啟 access log。 |
| `integration/test_mcp_oauth_mock_as.py` ⭐ | **ADR-0206 / P8-A**：本機 TLS mock authorization/resource server 真 HTTP flow。 |
| `integration/test_mcp_oauth_multiprocess.py` ⭐ | **ADR-0206 / P8-A**：Windows spawn 多 worker refresh lease 單一 winner。 |
| `test_mcp_oauth_workload_config.py` ⭐ | **ADR-0207 / P8-B**：global feature gates、filesystem kind、workload key/rotation env 與安全預設。 |
| `test_mcp_workload_adapters.py` ⭐ | **ADR-0207 / P8-B**：manager/runtime authority adapter 與 final egress credential guard 接線。 |
| `test_mcp_workload_api.py` ⭐ | **ADR-0207 / P8-B**：admin/owner API、minimal metadata/recovery projection、typed transition reconcile、owner-safe delegation response、Origin/role gate 與 server-derived source grant contract。 |
| `test_mcp_workload_audit.py` ⭐ | **ADR-0207 / P8-B**：durable pre-egress outbox、ack/retry、secret redaction 與 backlog fail-closed。 |
| `test_mcp_workload_authority.py` ⭐ | **ADR-0207 / P8-B**：direct HEARTBEAT-only authority、owner/policy/generation/tool/scope revalidation。 |
| `test_mcp_workload_authority_router.py` ⭐ | **ADR-0207 / P8-B**：direct/owner-delegation exact dispatch 與 unsupported kind fail-closed。 |
| `test_mcp_workload_authorization.py` ⭐ | **ADR-0207 / P8-B**：immutable single-use ticket、raw tool、arguments digest、TTL/replay/context/generation binding。 |
| `test_mcp_workload_bootstrap.py` ⭐ | **ADR-0207 / P8-B**：zero-I/O feature/registry baseline、single-process runtime composition、control/rotation 共用 coordinator、safe cleanup，以及 transient audit sink failure 不殺死 maintenance task。 |
| `test_mcp_workload_control.py` ⭐ | **ADR-0207 / P8-B**：profile/credential/metadata/delegation mutation、真 permit barrier、epoch-advanced failure fail-closed、cancel 保留 durable blocker、retry 完成 delegation document delete、source-disconnect exclusion、timeout→restart durable retry、多衍生 delegation 逐一收斂、typed present/absent transition reconcile 與 explicit finalize。source-disconnect exclusion 改成看事件先後：建立委派必須先排到守衛的鎖後面（測試用會回報「有人在等」的鎖），原本的 0.5 秒逾時改成 30 秒卡死保護；原寫法在守衛提早放鎖時照樣通過。 |
| `test_mcp_workload_delegated_authority.py` ⭐ | **ADR-0207 / P8-B**：exact P8-A active grant authority、不複製 token、撤權/expiry/source generation拒絕。 |
| `test_mcp_workload_delegation.py` ⭐ | **ADR-0207 / P8-B**：owner delegation JSON CAS、同 generation contention exactly-one-winner、ownership、exact installation/tool/scope/expiry 與 malformed fail-closed。 |
| `test_mcp_workload_delegation_store.py` ⭐ | **ADR-0207 / P8-B**：delegation SQLite digest mirror、tombstone、source-slot listing、store-side source digest authentication 與 provable legacy backfill。 |
| `test_mcp_workload_exchange.py` ⭐ | **ADR-0207 / P8-B**：client_secret basic/post、metadata/token endpoint、scope/token validation、final OAuth POST binding、permit 後 authority revalidation、audit-before-egress、retry/redaction。 |
| `test_mcp_workload_fsm.py` ⭐ | **ADR-0207 / P8-B**：authority transition/revocation 狀態機與非法轉換 fail-closed。 |
| `test_mcp_workload_keyring.py` ⭐ | **ADR-0207 / P8-B**：專用 workload key env/atomic writer，不重用或覆寫 P8-A key。 |
| `test_mcp_workload_manager.py` | **M4**：現代工作負載生命週期與隔離。 |
| `test_mcp_workload_metadata.py` ⭐ | **ADR-0207 / P8-B**：HTTPS/DNS/IP-pin metadata、issuer/resource/method freeze、private network exact policy與 unsupported method拒絕。 |
| `test_mcp_workload_models.py` ⭐ | **ADR-0207 / P8-B**：authority principal key、profile/delegation schema、兩種 client-secret method與 canonical security fingerprint。 |
| `test_mcp_workload_mutation.py` ⭐ | **ADR-0207 / P8-B**：單一 mutation coordinator 的 durable operation owner、exact required fence、pending completion 與 missing/stale/foreign mismatch fail-closed。 |
| `test_mcp_workload_permit.py` ⭐ | **ADR-0207 / P8-B**：boot-local single-use permit、durable readiness、exact global completion token、egress kind/epoch/task/ticket/wire payload binding、cancel/drain/task-exit ack。 |
| `test_mcp_workload_profile.py` ⭐ | **ADR-0207 / P8-B**：non-secret workload profile generation CAS、同 generation contention exactly-one-winner 與 atomic policy persistence。 |
| `test_mcp_workload_rotation.py` ⭐ | **ADR-0207 / P8-B**：managed key prepare/drain/re-encrypt/retire、public maintain serialization、crash exact-owner recovery 與 foreign-owner non-hijack。 |
| `test_mcp_workload_runtime.py` ⭐ | **ADR-0207 / P8-B**：execution kind/origin、direct/delegated bearer guard、refresh final-POST permit、bounded expiry-pruned replay index、post-issue revalidation、cache/close erasure。 |
| `test_mcp_workload_store.py` ⭐ | **ADR-0207 / P8-B**：encrypted credential/metadata、same-generation metadata replacement、binding/ciphertext tamper、credential reinstall generation、exact durable mutation owner/CAS、source-indexed revocation、legacy manual-recovery migration、admission blockers 與 audit rows。 |
| `test_mcp_workload_topology.py` ⭐ | **ADR-0207 / P8-B**：remote/unknown filesystem拒絕、同 process 第二 gate fail-start，以及兩個獨立 subprocess exactly-one-winner；owner wait cancellation 後 successor 可立即取得鎖。 |
| `integration/test_mcp_workload_lifespan.py` ⭐ | **ADR-0207 / P8-B**：feature-gated lifespan wiring/cleanup、P8-A source-disconnect delegation fence。 |
| `test_system_backup_ops.py` ⭐ | **ADR-0207 補強**：HTTP restore collision fence 同時辨識 P8-A/P8-B runtime store與 key ring。 |
| `test_mcp_2026_p3_conformance.py` | canonical 完整結果／descriptor 與 SDK 合約；純 SDK 歷史 dual-era fixture 保留，不是 Touka 相容入口。 |
| `test_mcp_2026_p4a_dispatch.py` | 現代 SDK middleware／認證與版本、mission 一次性／generation／replay、DOING 精確回應加受管 board。 |
| `test_rbac_mcp_tool_inheritance.py` | 真設定檔＋真繼承鏈：allow 累加、deny self-only、全域 MCP 地板約束 Owner、裸 `*` 局部 deny-all |
| `test_rbac_hierarchy.py` | RBAC Grant 繼承與 ABAC 三模式矩陣：Manual/Auto/YOLO（退役的 ReadOnly 當 Manual） |
| `test_identity_objectification_dto.py` | `AgentIdentity` DTO 不可變性與屬性驗證 |
| `test_agent_normalization.py` | ID 歸一化：`ToukaSoul` 前綴自動剝離邏輯 |
| `test_module_isolation.py` | 微服務邊界隔離：ToukaHeartbeat 無法匯入 ToukaProxy 的 `src.*` 模組 |
| `test_heartbeat_storage_recovery.py` ⭐ | StorageService 自癒：`config.json` 含 `//` 註解時自動重生成；`config.json.example` 必須是合法 JSON |
| `test_heartbeat_token_ssot.py` ⭐ | heartbeat_token SSOT 優先鏈：env var > config.json > 自動生成；`verify_schedule_token` 靜態確認使用 `get_token()` |
| `test_custom_models_idor.py` ⭐ | 自定義模型 IDOR 防護：Owner A 無法讀取/覆蓋 Owner B 的模型資料 |
| `test_user_service.py` | `UserService` CRUD：建立、更新、刪除使用者，`refresh_token_jti` 廢止 |
| `test_user_service_concurrency.py` ⭐ | `UserService` 並發安全：`update_user_profile`/`admin_update_user` 鎖外讀取 stale snapshot 覆蓋並發 token rotation 修復驗證 |
| `test_auth_service_data_resilience.py` ⭐ | `AuthService` 資料韌性：`_read_users` 損壞靜默回傳 `[]` 導致使用者資料全滅修復驗證；`_write_users` 原子寫入保護 |

### 資源管理

| 檔案 | 說明 |
|------|------|
| `test_resource_manager.py` | 路徑隔離、越權穿越防禦、`is_owner()` 分層權限（由 test_resource_guard + test_resource_manager_permissions 合併） |
| `test_memory_resource_proxy.py` | ADR-0116 `get_resource_proxy()` 工廠：整合模式回傳真實 `ResourceManager`；取不到即 fail-loud；模組級快取重用（獨立 MCP 模式與 `_FallbackResourceProxy` 已退役） |
| `test_resource_manager_atomic.py` ⭐ | ADR-0078 P5a：`write_file_text` 原子寫入（成功換內容／失敗保原檔不留暫存）、`read_json` 壞檔回 None＋告警 |

### 模型管理

| 檔案 | 說明 |
|------|------|
| `test_model_manager_merge.py` | `_merge_models`：新模型加入、既有設定保留、`last_seen` 更新、黑名單過濾 |
| `test_model_manager_stubs.py` | 動態模型發現：`fetch_*` 委派、`check_models_health` 更新 `last_seen` |
| `test_gemini_key_header_security.py` | ADR-0248 加串流請求：網址只有 `alt=sse`、金鑰只在標頭，AST 掃描擴到 `sse_stream.py`。Gemini key 僅走 header、四個 caller 的 redirect 約束（快取服務隨 ADR-0243 刪除）；Google／XAI 缺 key 不建立 HTTP client，保留 GROK 優先序、有效 key 的 headers／timeout 與 200／403 健康判斷 |
| `test_gemini_to_openai.py` | Gemini → OpenAI 格式轉換：`json.loads` 取代 `ast.literal_eval` |
| `test_fetcher_ssot.py` | 模型 Fetcher SSOT 約束驗證，含 follow_redirects=False、o4- 模型與 Gemini 2.0+ 正則篩選過濾 |
| `test_no_sampling_on_wire.py` | （ADR-0241）：三個 adapter × 各入口（明確帶參數、靈魂設定、原生 Anthropic、心跳、注入層）送出內容都沒有取樣參數；Gemini 的 `generationConfig` 只能有 `maxOutputTokens`／`stopSequences`（ADR-0248），原生 Anthropic 入口帶 `max_tokens=100` 時只送 `{"maxOutputTokens": 100}`、其他入口沒有（ADR-0254 O20）；舊設定照常載入 |
| `test_heartbeat_tool_schema_pinned.py` | （ADR-0246 修正 ADR-0242 KF-4）：`test_kf4_heartbeat_request_carries_tools`（原 `test_kf4_heartbeat_request_has_no_tools` 改新期望並改名）：心跳交給 `dispatch_request` 的請求帶工具 schema；沒有設定時決定的模式時是 Manual |
| `test_model_health_headers_pinned.py` | （ADR-0243 修正 ADR-0242 KF-6）：`test_claude_health_check_sends_anthropic_version`（Claude 健康檢查帶 `anthropic-version`）、`test_openai_health_check_sends_max_completion_tokens`（OpenAI 健康檢查送 `max_completion_tokens`、不送 `max_tokens`） |
| `test_side_caller_requests.py` | （ADR-0242；ADR-0244 改為 `complete_once` 層）：注入假的 Completer，整組字面值比對摘要、互審 critic、創建評分交給 `complete_once` 的身分／purpose／model／system／user／max_tokens／逾時；評分拿不到建立者時記到 System 帳 |
| `test_side_caller_billing.py` | （ADR-0244）：走真的 `complete_once`（只換傳輸），三個旁路呼叫都記帳（provider＝purpose、記到對應身分）；超過成本上限不送出，摘要回 None、critic REJECT、評分 0.0；r55（ADR-0271）假預算物件只補 `would_exceed`（回 False），斷言不改 |
| `test_complete_once_contract.py` | （ADR-0244 契約；3a／3b 換底層時須原封不動通過，3a 已照原樣通過）：三家成功形狀與送出內容、先過成本上限、只記用量不記內容、拒答／截斷／安全過濾／逾時／廠商錯誤一律結構化非成功、不跨廠商、不重試、`timeout_s` 為總時間、`response_schema` 只對 Claude 外送；r55（ADR-0271）假預算物件只補 `would_exceed`（回 False），斷言不改 |
| `test_usage_normalization.py` | （ADR-0244）：三家 `extract_usage` 回四欄＋本輪總輸入；OpenAI 扣 `cached_tokens`、Gemini 扣 `cachedContentTokenCount` |
| `test_tool_usage_tracking.py` | （ADR-0281 T3）：工具使用分成試過／執行過／存成功——記錄函式只填自己的集合、限定名 `name.action`、只給一個集合的舊呼叫端照舊；存記憶被擋或失敗仍跳記憶提醒、真的存成功才不跳、提醒保留 `[記憶幻覺警告]` 標籤；被擋的查證工具不算查過；被擋的呼叫不算幻覺；真執行器：沒列出、PaC 拒絕、無人在場要審批、只有人能核准、審批被拒、參數驗證錯誤、沒帶參數都只算試過，成功的算執行過與存成功，回結構化錯誤、丟例外或舊式失敗文字只算執行過，記憶系統的重複提示照現況算存成功 |
| `test_per_round_accounting.py` | （ADR-0244）：每輪入帳、E-Index 每次 run 一筆、CSV 一列含快取、事件為整次加總且 `usage_pct` 用最後一輪、原生出口記帳但不送事件 |
| `test_model_route_resolution.py` | （ADR-0244；ADR-0248 加 Responses 路由（只限非自訂、官方 OpenAI、規則表標記，目的地是常數）、自訂連線標記與 Anthropic 分支寫入 model）：`resolve_model_route` 與 factory 六個入口（Human、有效／停用自訂連線、Claude、Gemini、OpenAI 相容）交給 handler 的 context 逐鍵釘死 |
| `test_gemini_send_attempts.py` | （ADR-0244；ADR-0248 起主迴圈只送 1 次、不等待，重試在 react_core，還沒收到 200 就斷標成可重試；R-3b 移除沒有呼叫者的「預設送 3 次」路徑）：主迴圈外預設只送 1 次；`max_attempts=1` 時 503 與連線錯誤都只送一次 |
| `test_pricing.py` | 費率查表、最長子字串、非零 fallback、讀檔失敗不拋例外；快取費率沒填時照 input 價（ADR-0244）；R-3a（P24）規則表新 Claude 型號（Opus 4.8／5／5.5、Sonnet 5／5.5、Haiku 5.5、Fable／Mythos 5 與 5.1）與 Opus 4.6／4.7 各拿自己的價格，查不到的 Fable／Mythos（如 Mythos Preview）套家族最高價，舊型號與 Opus 4.5 不變（ADR-0247）；`test_budget_cap.py` 另加 3 個：入帳照新價格，成本上限閘據此判斷；r55（ADR-0271）+8：Haiku 5.5 依含快取讀寫的提示長度兩檔（門檻 4、`for_prompt` 1）、Sonnet 5.5 快取命中 0.10（Sonnet 5 仍 0.20）、`model_pricing.json` 覆寫仍是單一費率；Haiku／Sonnet 5.5 表格列改新價 |
| `test_review_loop.py` | （ADR-0137；ADR-0244）：互審單輪的證據優先、隔離、軟任務、收斂與 mission_tools 接線；critic 的 completer 綁 mission_tools 的身分與模型，`complete_once` 10 種非成功都 REJECT、整輪 REVISING 不放行 |
| `test_model_rules.py` | （ADR-0247；ADR-0248 起非串流上限只在主迴圈不串流時套用，可讀規則表的模組加 `openai_adapter`、`gemini_adapter`、`responses_adapter`、`route_family`；R-3a 加 39：default_on 思考的 max_tokens 下限 8,192、Mythos Preview 快取門檻 2,048、禁令 39 的 AST 匯入圖（只准 `openai_to_claude`／`claude_adapter` 匯入規則表，含遞移的兩跳匯入；發現路徑碰不到它的符號，含偵測器自測）、`rules_for` 家族判斷與 `native_family` 一致；取代 ADR-0241 的 test_claude_model_rules.py；SDK 界線的參數取自已安裝 SDK 的 `MODEL_NONSTREAMING_TOKENS`，1.11 比 0.83 少一筆，所以少 1 個）：每模型規則表——Claude 版本解析（含日期、舊命名）、budget_tokens→adaptive、5.x 不強制 tool_choice、各型號思考方式／預設輸出上限／快取門檻／中途 system、非 Claude 家族中性預設、GPT-6 兩列、模型發現不讀規則表（禁令 39）；轉換器 `-thinking` 讀規則表、max_tokens 請求值否則規則表預設、舊別名不改寫；非串流上限（3b 前；21,333、Opus 4／4.1 為 8,192，以已安裝 SDK 自己的檢查驗證界線）與 budget 思考時 max_tokens＞budget；Honesty Guard 矯正仍注入；r55（ADR-0271）+64：5.5 三列的 `effort_levels`／`thinking_off_mode`、5.6 以後照舊、`claude_thinking_plan` 表格（用途少想、客戶端 effort 與 disabled 改寫、舊型號不變）、warning 只記型號與等級名、`claude_send_max_tokens(thinking_off=)` 只在思考真的關掉時免下限 |
| `test_model_layer_redirects.py` | （ADR-0241；ADR-0250 B3 起範圍加 `ToukaDispatch`、`ToukaHeartbeat`、`ToukaMemory/src`、`ToukaMemory/scripts`，已知建構點加 Dispatch `server.py` 與 Heartbeat `proxy_client.py`、拿掉已刪的 Proxy 排程引擎，每個新根目錄的檔案數與 client 總數有下限；R-3b 起 openai 的 base_url 也不得讀 `OPENAI_BASE_URL`、openai client 的副本一律禁止；ADR-0248 擴到 openai 建構子：`api_key=`／`base_url=`／`http_client=` 都要在、不得 None 或讀環境變數，附違規範例；R-3a 起違規範例逐例比對是哪條規則，範圍內每個 SDK 建構點都用從 `src.config` 匯入的常數、`config.py` 的值必須是字串字面量，平台 client／dotted import／別名指派（含型別標註、tuple）／`with_options`／追得到 SDK 的 `copy`／任何 `follow_redirects` 非字面量 `False`（含 `**{...}`）／事後指派 `.base_url` 都會抓，全範圍 SSOT 比對有寫成檔案的反例；ADR-0247 起範圍擴大到整個 `ToukaProxy/src`＋`scripts`＋`tests/smoke/manual`）：每個 httpx／httpx2 client 與 SDK 的 `DefaultAsyncHttpxClient`／`DefaultHttpxClient` 明寫 `follow_redirects=False`；Claude SDK 建構子（`AsyncAnthropic`／`Anthropic`／別名）必須當場注入這種 `http_client`（AST）；3a-U4 起也必須明寫 `base_url=`（不得是 `None`、不得讀環境變數），已知 4 個建構點一律用 `config.ANTHROPIC_API_BASE_URL`；範圍與已知建構點不得空過；違規範例（缺 http_client、SDK 預設 client、變數、`**kwargs`、別名、缺 base_url、`None`、讀環境變數或同名 Settings 欄位）會被抓 |
| `test_anthropic_sdk_transport_security.py` | （ADR-0247，3a-U2；R-3a 加真 SDK 1.11 `Message` 跑原生出口與用量正規化 2 個）：主迴圈 client 是 httpx2、不跟隨轉址且仍用 SDK 預設逾時；注入的 client 經 `with_options` 換金鑰後遇 307 停下、金鑰不送到轉址目標（本機 loopback）；拿掉 prompt-caching beta 標頭後 SDK 仍原樣送出 `cache_control`（MockTransport） |
| `test_anthropic_base_url_pinned.py` | （ADR-0247，3a-U4）：把 `ANTHROPIC_BASE_URL` 指到另一台主機，用 httpx2 MockTransport 看實際請求——對照組（不傳 base_url 的 client 真的改連環境變數）；主迴圈 client、factory `with_options` 換金鑰後的副本、`complete_once` 仍送到 `config.ANTHROPIC_API_BASE_URL`；`complete_once` 的 Claude 路徑在真的 SDK 1.11 上照送 `output_config.format`，`end_turn`／`refusal`／`model_context_window_exceeded` 正規化與四欄用量不變；r55（ADR-0271）假預算物件只補 `would_exceed`（回 False），斷言不改 |
| `test_anthropic_sdk_env_security.py` | （ADR-0247，R-3a；SECURITY SDK3）：SDK 讀的請求相關環境變數在 config 載入時被 pop（保留 `ANTHROPIC_API_KEY`、空字串也拿掉）、模組層呼叫順序（先灌 `.env` 再清）、pytest 執行時已不在環境；對照組（不清時 `ANTHROPIC_CUSTOM_HEADERS` 真的換掉金鑰）；環境被灌回後主迴圈、`with_options` 換金鑰副本、`complete_once` 仍只送 Touka 的金鑰與標頭；`ANTHROPIC_LOG=debug` 不會把請求內容寫進日誌；同一個名字只記一次 info（MockTransport，不連網）；r55（ADR-0271）假預算物件只補 `would_exceed`（回 False），斷言不改 |
| `test_sse_stream.py` | （ADR-0248，3b-U2a／U3a）：共用 SSE 解析器（CRLF／LF／CR、跨 chunk 的 CR、BOM、增量 UTF-8、註解與其他欄位略過、最後事件沒空行照送）、Chat 與 Gemini 彙整器（鍵只來自 chunk、工具依 index 合併、拒絕 `message` 鍵、Gemini part 合併與簽章規則、完整訊號） |
| `test_claude_stream_sdk.py` | （ADR-0248，3b-U1a；C1 常駐）：真 AsyncAnthropic＋httpx2.MockTransport 吐 SSE（ping、中途 error、redacted_thinking、多個 tool_use、空 input），最終結果和 `create()` 相同；沒有 `message_stop` 回 RoundError |
| `test_claude_stream_timeouts.py` | （ADR-0248，3b-U1a）：首事件／閒置／整輪三段逾時（只有 ping 不誤判閒置、整輪上限照樣觸發）、取消後上游真的關閉（loopback 小伺服器）、逾時區塊不跨 yield |
| `test_partial_usage.py` | （ADR-0248，3b-U1b）：沒跑完那一輪的記帳——全部有回報、部分有回報、完全沒回報各一，取消與 RoundError 都記、串流沒開不記、已完成的不重記，估計標記只在事件與日誌 |
| `test_openai_stream.py` | （ADR-0248，3b-U2b）：相容 Chat 串流（`[DONE]` 才算完整、200 之後的 error 物件、壞 JSON、位元組重設閒置計時、串流旗標不寫進 payload）、KF-8 出口、規則表欄位接線 |
| `test_compat_stream_usage.py` | （ADR-0248，3b-U2b；裁定 5、6；R-3b 加「沒帶 custom_connection 就當自訂、不串流」3 例）：自訂連線不串流、不送 `stream_options` 且用量不是 0；已知廠商最後沒 usage 時記估算值並帶標記；取消與斷線照 U1b 規則 |
| `test_gemini_stream.py` | （ADR-0248，3b-U3b）：Gemini 自寫串流（`alt=sse`、金鑰只在標頭、finishReason／blockReason 完整訊號、blockReason-only 判 safety、200 之後的錯誤、形狀錯誤、partial_usage）；主迴圈的 503 只標可重試、不在 adapter 內重試（U5a） |
| `test_openai_sdk_env_security.py` | （ADR-0248，3b-U4a-1；SECURITY SDK4）：只清 `OPENAI_SDK_ENV_OVERRIDES`（保留 `OPENAI_API_KEY`／`OPENAI_BASE_URL`）並把 openai／httpx2 logger 釘在 WARNING；`setup_logging` 也會釘 |
| `test_responses_request.py` | （ADR-0248，3b-U4a-2；R-3b 加不支援的區塊只記型別）：Responses 請求側傳輸層契約（真 SDK＋MockTransport，與 `tests/fixtures/responses_u4a_wire.json` 逐字比對，先設好 5 個 `OPENAI_*` 證明沒讀）、加密 reasoning 回送、`prompt_cache_key` 雜湊、缺憑證不建 client、GPT 登入接縫 |
| `test_responses_parse.py` | （ADR-0248，3b-U4b-1a）：解析（call_id、reasoning 摘要）、用量扣 `cached_tokens`、停止原因（incomplete 原因對應）、誠實守衛、缺金鑰／缺 base URL 文字 |
| `test_responses_stream.py` | （ADR-0248，3b-U4b-1b）：Responses 原始事件串流，三種切法結果一致；completed／incomplete 才算完整、error 事件與 `response.failed`、依 output_index 補回 output、只有 ping 的閒置與整輪上限、partial_usage |
| `test_complete_once_nonstream_contract.py` | （ADR-0248，附註 2；`test_complete_once_contract.py` 不改）：三家 `complete_once` 非串流、沒有 `stream`／`stream_options`、網址沒有 `alt=sse`；真 SDK 例外 429／529 只送 1 次不等待；Responses 路由只送 1 次、incomplete 照記帳、沒金鑰不送 |
| `test_main_loop_retry.py` | （ADR-0248，3b-U5a；3b-R1 加還沒收到 200 就斷可重試、200 之後的忙碌錯誤、空殼事件不結束首事件、已有輸出不重試、形狀錯照記帳、Gemini RetryInfo、退避心跳與按停）：主迴圈重試——錯誤分類與 retry-after 解析、Gemini 503→503→200 的狀態文字順序與每次只等一次、到上限顯示最後錯誤、等待中取消、400 與一般例外不重試、Claude／Responses／相容 Chat 的 retry-after、首事件與閒置可重試而整輪上限不重試、四家被放棄那次都記帳、主迴圈 Anthropic client `max_retries=0`；finish_reason 實際值與拒答提示（含模型拒答文字、缺欄維持 stop、`stop_reason` 讀不到也收尾） |
| `test_internal_callers_max_tokens.py` | （ADR-0254，跨單位 DEV-B 修正）：O20 不波及內部呼叫端——子代理送 Claude 仍是 8192、送 Gemini 沒有 `maxOutputTokens`（沒有 `generationConfig`）、送 OpenAI 相容沒有 `max_tokens`；評分卡送 Claude 是情境的值、送其他家不帶上限；模型名是靈魂時值照設、不記成呼叫端明確給；對照：IDE／Chat 的 Anthropic 入口明確給的 `max_tokens` 照 O20 帶給非 Claude。走真 adapter、假上游，不打 API；r55（ADR-0271）+3：評分卡情境上限碰到 5.x 照實際值補到 8,192（仍不標 fields_set） |
| `test_claude_thinking_profile.py` | （ADR-0271）：ClaudeAdapter 只讀 `thinking_profile` 鍵（不讀標頭）、有無它只差 thinking／output_config／max_tokens、5.5 以外不變；原生入口照實際值補 8,192（含計分卡不標 fields_set）、只轉送 `output_config.effort`（其他鍵只記鍵名）、`disabled` 依型號改寫；OpenAI 入口 `stop` → `stop_sequences`（濾空白、去重），原生入口不送 |
| `test_complete_once_thinking.py` | （ADR-0271）：用途表（judge、summarizer、inline_completion 少想；auto_reviewer、critic 不列）、`_adapter_context` 只對 Claude 放鍵；三型號送出內容、schema 與 effort 合併；送前最壞估算讀實送內容與實送上限、超過回 over_budget 不送不記帳、`_wire_max_output` 四家欄位；審查員等待預設 60 與範例檔一致 |
| `test_cost_guard.py` | （ADR-0271）：送前最壞估算三個純函式與 `worst_case_call_usd`（Haiku 5.5 估計 >10 萬選貴檔）、和 `tests/memory_eval/live_bounds.py` parity；`would_exceed`（上限關、沒設上限、剛好等於都 False）；`complete_once` 端到端（超過不送、放得下照送、上限關照送） |
| `test_run_usage_tiers.py` | （ADR-0271）：整次 run 金額每輪分檔相加、沒分檔型號等於改前、畫面事件與稽核 CSV 用同一個值 |
| `test_thinking_latency_probe.py` | （ADR-0271）：人工延遲實測工具的硬界（不外連）——匯入不改產品、人工閘先於 dotenv／沙盒／產品 import、CLI 缺核准與越界拒絕、型號／路由／用途表核對、核准範圍列出每組實送上限與每型號最壞單價、實送上限→最壞花費→請求預算的順序、照實際用量結清、報告不印原文、共用記憶評估工具的沙盒釋放 |
| `test_kf9_anthropic_entry.py` | （ADR-0248，3b-U5b；ADR-0254 O20 +2）：Anthropic 形狀入口的 tool_use／tool_result 轉成 tool_calls／tool 訊息（平行呼叫、is_error、工具結果後接文字、第二輪工具、call id 對應），Gemini 與 Responses 端也對得上；list system 合成字串；沒有工具區塊的訊息不變；呼叫端明確給的 `max_tokens` 帶過去、沒給時不帶（O20） |
| `tests/helpers/`（不是測試） | ADR-0248：`stream_transport.py` 共用串流假傳輸（真 httpx Request／Response、非同步位元組串流、送出內容從 `request.content` 解析、計請求數、收尾檢查每個回應都關了）與 1 個 chunk／語意多 chunk／位元組切割三種 SSE 產生器；`claude_stream.py` 讓假 Claude client 的 `messages.stream` 委派給 `create`（多個測試檔共用）；`mcp_asgi_peers.py`（ADR-0273）讓真 MCPServer 經 ASGI 當對端，可在途中注入斷線、轉址或下線，另有 `task_leak_messages` 檢查未讀取的 task 例外 |
| `test_route_family.py` | （ADR-0241）：路由家族矩陣（Human、自訂連線、claude／gemini、registry）與 factory 路由不變 |
| `test_proxy_fallback_family.py` | （ADR-0241）：同家備援照舊、跨家族或判定不出時回原錯誤 |
| `test_soul_primary_model_choice.py` | （ADR-0250 A1／A4／L1；ADR-0254 H9 +9）：`should_set_active_brain` 真值表；`is_selectable_brain`（舊簡稱、靈魂、L2 自訂模型、human、舊靈魂路由、停用的自訂連線不可選，清單內官方 ID（大小寫不同也可）、啟用的自訂連線原名可選）；決策表第 1～8 列各有端點測試（人類改值才換、模型自己換的不被蓋回、沒有主要模型時救援、不可選回 404 且 `config.json` 與 `soul.md` 不變、代理人存檔不動主要模型）；`GET /v1/souls` 沒設或讀取出錯回 null；舊 `toukasoul` 主要模型當沒設、聊天回 409；建立靈魂（`POST /v1/souls`）不可選的模型 404、只差大小寫分不出 409，兩者都不建資料夾、不註冊、不加擁有權，可選的照舊建立並設成主要模型，沒指定照舊建立，空字串或只有空白當沒指定、前後有空白的去掉後再驗。模型清單用 `set_models` 種入，不 patch 掉驗證 |
| `test_model_name_case_lookup.py` | （ADR-0250 A5 保護 1）：`resolve_custom_route` 表格逐列（先找完全相同、不分大小寫只有一個才用並帶 `original_name`、兩個以上 409 `MODEL_NAME_AMBIGUOUS` 不挑、停用照舊）、`request_model_name`（自訂連線回原名、其他轉小寫）、`is_selectable_brain` 跟著查表；靈魂聊天、群聊、子代理、心跳脈衝都送原名；決策表第 9 列（模糊名存檔 409、都不寫）；大寫自訂連線存成主要模型後聊天送原名。自訂連線用假的路由快照 |
| `test_custom_model_case_collision.py` | （ADR-0250 A5 保護 2、3）：建立或改名成只差大小寫的靈魂名（含 `@`、`toukasoul` 寫法）或其他自訂模型名回 409 `MODEL_ALREADY_EXISTS`；只改大小寫的自我改名不擋；更新既有自訂模型照常。存在判斷換成分大小寫（模擬 Linux），Windows 上改動前才紅得出來 |
| `test_usage_thoughts_and_cache_hit.py` | （ADR-0250 A6／A7，修正 ADR-0244 剩餘風險）：Gemini 輸出＝`candidatesTokenCount`＋`thoughtsTokenCount`（含隱式快取、沒跑完那一輪、`gemini_to_openai` 的 `completion_tokens`），成本上限在同一次 run 更早觸發；DeepSeek 只有頂層 `prompt_cache_hit_tokens` 時照快取讀、兩種欄位都在只取 `prompt_tokens_details.cached_tokens` 不相加、照快取價記帳。全部 mock |
| `test_anthropic_thinking_single.py` | （ADR-0250 C6）：`adapters/schemas.py` 只定義一次 `AnthropicThinking`（AST）；OpenAI 形狀與 Claude 形狀的請求用同一個類別；openapi 只有一個元件 |
| `test_context_window_registry.py` ⭐ | Sliding Window token% 改造 Phase A：`get_context_window()` context 量測分母 SSOT（內建表 claude/gemini/gpt/grok、`[1m]` 1M 標記優先、最長子字串匹配、JSON `windows`/`_default` 覆蓋、無效值忽略、未知回 128K、讀檔失敗降級內建表） |
| `test_token_estimator.py` ⭐ | Sliding Window token% 改造 Phase A：`estimate_tokens`/`estimate_messages_tokens` 字元啟發式 token 估算（CJK 感知、同字數 CJK>英文、長度單調、空/None=0、Anthropic content block 攤平、每則 overhead、永不拋例外） |
| `test_prompt_block_delimiters.py` ⭐ | Sliding Window token% 改造 Phase B0/D（ADR-0092）：prompt 區塊統一 XML 邊界——`wrap_block` 格式、soul 外層 `<persona>` 內文逐字保留＋技術指令保留、mission/IDE/project 各自 XML tag、IDE 舊字面移除；＋Phase D `inject_context_summary`（`<conversation_summary>` 注入/無摘要 no-op/無 session_id no-op）；＋ADR-0264 IDE 區塊帶圖對照表（dict／物件 × 純文字／多模態 JSON 字串／list：圖片完整、IDE 是另一段文字） |
| `test_dialogue_summarizer.py` ⭐ | Sliding Window token% 改造 Phase D（ADR-0092）：`summarize_dialogue` 走 `complete_once`（ADR-0244）且 fail-safe——成功路徑、`complete_once` 非成功（缺金鑰／超預算／逾時／截斷／拒答，即使帶文字）、例外、空回應皆回 None（降級純無損剪裁）、空輸入／0 預算／沒指定模型時不呼叫、`_format_dialogue` 攤平 content block＋跳過非對話角色；＋ADR-0264 每行台北時間（秒、毫秒、沒有時間）、摘要開頭時間範圍對照表（全有、部分沒有、全沒有、非對話角色不算）、回傳第一行是時間範圍、提示要求換成絕對日期 |
| `test_context_breakdown.py` ⭐ | Sliding Window token% 改造 Phase B（ADR-0092）：`compute_context_breakdown` 以 XML 單一 parser 切段＋authoritative `input_tokens` 校正——usage_pct（含 1M 窗、封頂 100）、XML 區塊分解＋縮放、巢狀 project 歸類、無 authoritative 用本地估、garbage 不拋例外、`token_usage_event` 擴充欄位（前端對應 `ContextMeter.test.tsx` token% 路徑） |
| `test_continuity_block.py` ⭐ | （ADR-0257 S2、ADR-0264 與 ADR-0267；時鐘凍結，session 與活動事件只寫 tmp 資料庫，任務看板經 `MissionManager` 寫在沙盒）：毫秒換秒、經過時間的呈現；現在時間取自系統時鐘；距上一輪多久在執行時算，順序是記憶體（標 `(server memory)`，絕不帶前端時間標示）→ 活動事件 user_turn（同一對話、不含這一輪，重新生成也排除）→ 對話樹（標成可能來自使用者裝置、秒值正規化）→ 不知道，記憶體以 (agent, session) 為 key 且有上限；上次人類訊息、只算有結束紀錄的上次自主執行（心跳模式另一個標籤）、最近完成（7 天邊界、最多 3 筆、新到舊、只有三類）、可信紀錄起點只在有沒紀錄時出現且取全域與重置起點較晚者；HEADER 要求寫絕對日期；事件表讀不到照實寫、不丟例外；對話樹時間不會變成活動事件；模型寫的任務 id 只以雜湊顯示；全部行出現時 ≤ 1,200 字元；附在最後一則 user 訊息尾端、不在 Claude 與 OpenAI 的 system 裡；不走插話；無痕只放時間、不讀事件與看板、不留紀錄；一般模型與綁到別的靈魂的 session 不加；任務看板只補時間；帶圖片的 JSON 字串先還原成 list、圖片完整、另加一個 part |
| `test_continuity_not_persisted.py` ⭐ | （ADR-0257 S2 與 ADR-0267；上游是假的串流）：`/v1/messages` 與 `/v1/chat/completions` 的舊串流路徑，送出的最後一則 user 訊息有 `<continuity>`、存下的對話樹與摘要輸入都沒有；明示 UI 保存路徑存下的使用者訊息與回覆都沒有；Auto 審查員看的使用者原意沒有；心跳也注入（心跳模式：只在送出的最後一則 user、不進 system、沒有「這段對話上一輪」、不動記憶體紀錄、審查員原意沒有）；補 nudge 的脈衝 nudge 格式逐字不變、存回的樹沒有區塊、nudge 與區塊的現在時間同一個值；非靈魂的心跳不注入 |
| `test_continuity_events_db.py` ⭐ | （ADR-0267；tmp `ui_sessions.db`）：`init_db_async` 建表與索引、全域可信紀錄起點只寫一次；同一個 event_id 只留一筆；任務同一狀態重寫不另記、重複轉換（FAILED → TODO → 再 FAILED）都留、依任務與靈魂分開；重置只刪該靈魂並寫它的起點、空的再重置也行；列表與計數依靈魂範圍、metadata 解回；寫入介面沒有 trust、讀回都是 `system`、時間是毫秒；continuity 的一次讀取只看這個靈魂、排除這一輪、只算結束的 run、最近完成依時間窗與上限、起點取較晚者 |
| `test_continuity_event_recorder.py` ⭐ | （ADR-0267；tmp `ui_sessions.db`）：14 種事件的 event_id 對照表（帶靈魂、會重複的轉換加毫秒）；模型寫的 id 只留短英數；沒綁定不記；綁定的迴圈裡、工作執行緒、工作執行緒裡 `asyncio.run` 的短命迴圈都寫得進去；寫入照呼叫順序（第一筆慢也不會被後面同狀態吃掉）；綁定的迴圈已關只記 warning；寫失敗只試一次、不寫 failed；`record()` 出錯不外丟；只記正式靈魂（DebugAgent、DispatchAgent、子代理、一般模型、人類都不記）；無痕／子代理／派工 scope 抑制（工作執行緒裡也一樣、巢狀保留）；user_turn 條件對照表（心跳、無痕、子代理、派工、不屬於這個靈魂、不是人類送的、沒有訊息 id 都不記）；Memory 端沒登記是 no-op、記錄器壞掉不外丟、啟動登記與關閉解除 |
| `test_continuity_event_hooks.py` ⭐ | （ADR-0267；tmp `ui_sessions.db`、tmp 事實庫與 autospec 向量替身、任務看板寫在沙盒）：SoulLayer 記人類已保存的一輪（同一則訊息只一筆）；心跳開始與結束（結束原因對照表：completed、interrupted、quota、budget、no_model、error；被取消記 cancelled；in-flight 標記沒寫進去兩筆都不記；整個 run 沒有 user_turn）；`mission_tools` 建板、狀態、全部完成、驗收（兩次只一筆、不存目標與描述）、重複轉換都留、非法轉換與結果不明不記、無痕／子代理／派工不記、跨看板記在動手的靈魂名下並標 `board_owner`、記錄器壞掉時回覆逐字不變；記憶存（向量成功與失敗）、被擋與失敗不記、忘（查無、本來就封存都不記；從版本歷史拿掉的照記，ADR-0286）、一步取代只一筆帶新 id、工作執行緒裡的封存、閘道在無痕不記、記錄器壞掉時回覆不變 |
| `test_request_tail_cache_breakpoint.py` ⭐ | （ADR-0257 S2；時鐘凍結，用無痕的 continuity，不讀 session 與任務看板）：OpenAI 形狀轉 Anthropic 時，Claude 歷史快取斷點打在使用者原文、不打在請求時才附的尾端區塊；帶圖片時打在圖片那塊；`<ide_context>` 與 `<continuity>` 都留在斷點之後；前面沒有別的區塊就不打；第 N 輪斷點以前的區塊和第 N+1 輪對應位置逐一相同 |

### 功能服務

| 檔案 | 說明 |
|------|------|
| `test_autonomy_budget.py` | `AutonomyService`：Slot 優先級鏈（請求指定 > 全域配置 > 保底 5）、配額耗盡攔截 |
| `test_backup_job_lock.py` | （ADR-0241）：`run_backup_job` 呼叫端取消時鎖持有到備份結束，另一個備份不會同時開始 |
| `test_snapshot_consistency.py` ⭐ | （ADR-0262；全在 `tmp_path`，含容器式配置：`.env` 在別處、資料根由參數給）：同一秒連做兩份不互蓋、既有快照不被覆蓋；改名失敗不留 0 位元組的空檔、暫時占用照常數重試、其他錯誤不重試、等待中按 Ctrl+C 也清乾淨、佔位檔或暫存刪不掉時仍回報原本的錯誤、只在 POSIX 改名後才落盤資料夾（2026-10-25 補記）；資料根不存在時失敗且不留檔；另一個連線持寫入交易並把頁面寫進檔案時做快照，記憶庫是某個提交點、`integrity_check` ok、zip 沒有 journal 類檔；`copy_sqlite_snapshot` 不改來源、來源不存在丟 `FileNotFoundError`；真的 QdrantLocal（client 開著）快照後還原到新資料根，打得開、collection 與點數對得上，`.lock` 與沒列在 meta 的殘留資料夾不收，`.env` 寫回參數位置；沒有點的 collection、meta 列了但 storage 不在（參數化）照樣成功、還原後打得開；meta 在複製途中變動會重試、一直變就失敗；四個路徑參數一起給時不往上找 `.env`、只給一部分被拒 |
| `test_backup_paths_ssot.py` ⭐ | （ADR-0262）：腳本來源 `backup_script()` 真的在 `ToukaProxy/scripts`、還原端點用同一支；快照目錄＝`.env` 所在資料夾的 `backups`，清單／下載／還原（`_get_backup_dir`）與排程清理（`backups_dir`）是同一個函式；快照與還原傳同一組路徑參數，子程序 PYTHONPATH 與 dispatch 子程序完全相同；重置不再因腳本不存在而中止（子程序 mock）；真的跑複製到容器式位置的腳本（往上沒有 `.env`）產出含資料區的快照；資料根不存在時快照失敗、重置零刪除（原因只在 log）；腳本失敗回 `SYSTEM_BACKUP_FAILED` |
| `test_backup_error_output_security.py` ⭐ | （ADR-0262，禁令 23）：腳本 stderr 帶路徑與 `sk-` 樣式金鑰時，例外 detail 沒有 `error_output`、`POST /v1/system/backup/run` 的回應不含路徑與金鑰、log 裡的 stderr 已換成 `<redacted>` |
| `test_soul_delete_memory_guard.py` ⭐ | （ADR-0262 與 ADR-0267；tmp 事實庫、tmp 本機 Qdrant 與 tmp 活動事件表，不載入嵌入模型）：還有記憶時刪除回 409 `SOUL_MEMORY_NOT_RESET` 且什麼都不刪（判定表：一般事實、已封存事實、有點的 collection、空 collection）；只有活動事件時同樣 409（detail `activity_events`）、讀不到活動事件 503；沒記憶直接可刪；重置後可刪（含活動時間軸）；記憶系統不可用回 503 `SOUL_MEMORY_CHECK_FAILED`、不帶例外原文；密碼錯與非 admin 先 403，不透露記憶是否存在 |
| `test_memory_scrub_store.py` ⭐ | （ADR-0270 清除連備份 F 組；合成事實庫都在 `tmp_path`）：記憶體副本的檢查與清除（ancient／v0／v1／v2、NOCASE 欄只刪 BINARY 相同的 ID、v2 的取代鏈與安全標記只刪目標、別人的列逐列不變）、只有輸出檔碰磁碟、殘片在副本裡消失；注入錯誤的刪除被多重集合驗證抓到、錯誤訊息不帶列的值；每條連線開 `secure_delete`、不 VACUUM 也找不到記號；線上庫整理（不存在不建檔、被占用、重置後又寫入不整理、不認得的結構、驗證不過有自己的原因、讀不到、空間不夠）；升級全程持有升級備份鎖；登記表涵蓋所有建立的表；特殊路徑、太大、寫不出去是 `write_failed`；`delete_agent_facts`／`count_agent_facts` 用 BINARY；ADR-0270 2026-10-27 補記 F08c：查過之後、VACUUM 之前別的連線寫進一筆 → `rewritten_after_reset`（不是 `live_verify_failed`）；F08d：integrity 不過又有新寫入 → `live_verify_failed` |
| `test_memory_scrub_upgrade_backups.py` ⭐ | （ADR-0270 U 組）：每份升級備份清乾淨並保留檔名、時間、權限；沒有目標或只有殘片；一份壞掉不擋其他；被占用、處理中被改動或不見了原檔不動；升級等清除、清除等不到升級；換檔順序；唯讀原檔跳過、唯讀暫存照刪；v0／v1／v2 三種內容都清；資料夾本身是連結記失敗；寫不出暫存 `write_failed`；改名後資料夾 fsync 失敗仍算清掉；改名重試；空間不夠；舊前綴列回報 `out_of_scope`；ADR-0270 2026-10-28 補記 U19～U23：作業系統放的雜檔記成 `os_metadata_file` 資訊、原樣保留，SQLite 檔頭、含目標 ID（三種寫法）、非 ASCII 相近名字、同名資料夾、太大的照舊 `unknown_file`，讀雜檔出錯只記那一檔 |
| `test_memory_scrub_snapshots.py` ⭐ | （ADR-0270 S 組；合成 zip 與 tmp 本機 Qdrant，S01／S20 用快照腳本實際打包）：只改記憶成員、其他照抄；Qdrant 本機模式打得開清除後的快照；沒有目標不動；只拿掉完全同名的 collection；撞名（含大小寫、土耳其 ı、ς、K）拒絕；journal、加密、重名成員、不認得的 meta fail closed；空間不夠、被占用、處理中改動；舊版結構；暫存成員拿掉；只處理清單上的快照；中斷暫存只刪形狀完全符合、夠舊的，太新的一組都不碰、刪到一半出錯已刪的照留；修剪順序不變；分類器、重寫、meta 轉換的故障都被獨立驗證抓到；逐份讀取、總量上限、寫入失敗、資料夾是連結；v2 主庫加 v0／v1 升級備份逐成員核對；空 zip；`out_of_scope` 資訊；ADR-0270 2026-10-27 補記 S18d／S18e／S18f：中斷暫存列出後被發布、或檢查內容時才不見 → 跳過、不記失敗，其他照清；刪工作資料夾丟 FileNotFoundError 時資料夾還在 → 記 `in_use`；工作資料夾裡的檔在判斷新舊時被搬走 → 那個檔不算；ADR-0270 2026-10-28 補記 S33～S35c：升級備份資料夾的雜檔成員照抄，SQLite 檔頭、含目標 ID、太大、同名資料夾項目照舊 `unknown_member`，分類那邊放過或漏查、重寫時被改都被獨立驗證抓到 |
| `test_memory_scrub_orchestrator.py` ⭐ | （ADR-0270 O 組；tmp 事實庫、替身快照；每個等待都有上限，fixture 有看門狗與沙盒違規檢查）：只在重置完全成功後清；在工作執行緒持備份鎖；排程備份等清除；預檢在任何備份之前拒絕、在工作執行緒讀事實庫；重置後又寫入；稽核只有代碼與筆數、檔名不能偽造一行；完成判定對照表（含自己那份快照要真的清掉東西）；取消後照樣跑完並稽核；重跑；路徑來源被圍住、違規吞不掉；意外與準備失敗照常回報告；升級鎖逾時、列不出快照、重查失敗；自己那份快照沒列到、沒清到東西；備份失敗與交出前取消寫結果行、準備時意外寫拒絕行；磁碟上留著或是連結的 collection 資料夾；升級備份資料夾有 `Thumbs.db` 時照樣完成（O28） |
| `test_memory_scrub_endpoint_security.py` ⭐ | （ADR-0270 E 組）：不勾不清；沒第二段確認 422；密碼錯；Origin 錯、打字確認不符、目標不能重置、非人類 admin 都寫拒絕稽核；代打不驗密碼就 403；請求格式嚴格（多餘欄位、字串 true、整數 1）；DebugAgent 重置不能清；「不做成工具」的 AST 掃描（含 from-import 別名、`**`）與掃描自測；還原中擋所有重置、清除中擋還原、重置中擋還原與另一個重置、還原中擋 DebugAgent 重置；nginx 範例只有重置端點加長逾時 |
| `test_memory_collection_collision_security.py` ⭐ | （ADR-0270 G 組；會話沙箱的記憶庫、每個測試不同 ID）：撞名時一般重置只刪目標的點、孤兒點算殘留、沒撞名走原路；不分大小寫的撞名都抓到；判斷出錯絕不整個刪；向量刪一半可重跑；建立靈魂與自訂模型擋撞名（四個來源、檢查在工作執行緒、在既有靈魂檢查之前）；改名時舊 ID 的記憶照算；registry 讀不到就拒絕；撞名的靈魂重置後可以刪、不屬於任何人的點擋刪除、取代鏈的新舊版本都清 |
| `test_snapshot_listing.py` ⭐ | （ADR-0270 L 組與 E13）：快照清單規則（頂層一般檔、連結與 reparse point 不算、點開頭、`:`、子資料夾、大小寫 `.zip`）；清單、統計、修剪共用規則；`create_backup` 只回新檔名、不明時 `null`、列目錄失敗算備份失敗；還原、下載、刪除只收清單上的檔名、刪除拿備份鎖、等鎖時斷線稽核照留；快照目錄是連結時不跟進去 |
| `test_exec_cleanup_off_loop.py` | （ADR-0241）：taskkill 移出事件迴圈、取消時有界回收、`/stop` 與 stop_command 不卡迴圈 |
| `test_terminal_self_repair.py` | **缺口 3**（ADR-0254）：修復名單是模組常數、含 `execute_command` 與 `get_command_result`；非 0 exit code 附修復提示並送 `repair` 事件、0 重置次數；同一個 execution_id 重複查詢只計一次、還沒結束的結果不佔名額、記錄有上限。工具是假的、政策放行 |
| `test_evolution_venv_python.py` | **H3**（ADR-0254）：pip 安裝與本機驗證用 `<沙盒>/.venv` 的 Python 絕對路徑（Windows、POSIX 各一）；部署寫進 `server_config.json` 的 `command` 是建置步驟產生的那支（Windows、POSIX 各一）。子程序換成記錄器、沙盒根指到 tmp |
| `test_timeout_reap_bounded.py` | （ADR-0250 C2）：terminal 與互審蒐證的逾時分支，被殺的行程一直不結束時在 `CANCEL_REAP_TIMEOUT_SECONDS` 後放手；清理途中被取消時 `CancelledError` 往上拋、殺行程與回收照樣做完；`reap_process_tree` 不吞取消。行程與殺行程函式都是替身 |
| `test_scheduler_engine_removed.py` | （ADR-0250 B4；ADR-0254 H7 改寫）：沒掛載的排程 CRUD `routers/scheduler.py`、`services/scheduler_service.py` 兩個檔都不在；`ENABLE_SCHEDULER` 不是設定欄位、`ToukaProxy/.env.example` 不再寫它；舊 `.env` 還帶這個鍵（三種值）時設定照常載入 |
| `test_brain_not_set_group_heartbeat.py` | （ADR-0250 A3）：群聊有一位成員沒有主要模型或名字模糊時整輪不派發、串流裡點名該成員、記 warning，成員都有模型照常派發；心跳 `/awaken` 沒有 `model_override` 且沒有主要模型回 409 `AGENT_BRAIN_NOT_SET`、有 override 或有主要模型照跑；背景迴圈拿不到模型記 log 結束、不寫進對話、不送請求。全部 mock |
| `test_dispatch_callback_trigger.py` | （ADR-0051）：ToukaDispatch 更新成終態（DONE／FAILED）時觸發 Proxy 回呼、非終態不通知、回呼送到正確端點；ADR-0250 B3 起假 client 收建構參數並斷言 `follow_redirects=False`（禁令 22） |
| `test_rhythm_logic.py` | `RhythmService`：ReAct 循環強制截止、Nudge 注入時機 |
| `test_sliding_window_chat_trigger.py` ⭐ | Sliding Window token% 改造 Phase D（ADR-0092 / 禁令 28 第二觸發點 / 禁令 57）：`_maybe_trim_after_chat`——家對話以所屬靈魂觸發 `check_and_trim`（ADR-0264，不用請求者）；子對話／缺 session 跳過，家對話卻沒有歸屬、歸屬不是靈魂、舊前綴 ID、註冊表查不到時不剪並記 warning（對照表）；記錄歸檔呼叫驗證歸檔與摘要記帳都在靈魂名下、人類的 legacy 目錄不出現；沙盒真實 ResourceManager 跑「剪裁 → 歸檔 → 手動同步使用者放的檔 → 重跑」只落在靈魂名下、不搬檔、重跑不多一份 |
| `test_sliding_window_service.py` | `SlidingWindowService`（ADR-0092 Phase C/D；ADR-0264 起 ACID-A 是歸檔進舊對話庫，歸檔換成假的記錄呼叫，另加對照表：歸檔成功才剪、存 session 結果不明回 False、歸檔失敗或一則都沒歸檔到時不剪不摘要）——`count_rounds`、`should_trim(usage_pct≥trigger_pct)`、舊 `compute_split`（deprecated）、新 `compute_split_by_tokens`（剪到 token 地板/保底 min_keep_pairs/authoritative overhead 更激進）、換模型冷卻、`pct_invariant` validator、ACID 安全性（小窗 patch 強制觸發）；＋Phase D 摘要整合（budget>0 呼叫 `summarize_dialogue`（帶滑動視窗的身分，ADR-0244）並經 `context_summary` 寫入、budget=0 不摘要為 None）；ADR-0286 用 tmp_path 的真舊對話庫：存 session 結果不明後重剪照常剪成、庫裡已有同 id 不同內容時照常剪並另存版本 |
| `test_legacy_stats_agent_id.py` | Legacy stats URL `agent_id` 修復：TDD（URL param 路由驗證 + `is_owner()` 授權）；ADR-0253 起替身改用 `create_autospec`（照真類別的方法簽名），個數不變 |
| `test_memory_p0_repairs.py` ⭐ | （ADR-0253；tmp 事實庫、沙盒裡合成的舊對話 DB、tmp 本機 Qdrant，不載入 embedding 模型）：隨機回想兩個來源都抽得到；兩個來源都失敗、或一邊失敗另一邊沒東西時（`MemoryRecallUnavailableError`），經 Proxy 記憶閘道回結構化 `internal_error`，不回「腦海中空蕩蕩」；一半失敗時照回另一半的片段；診斷顯示實際向量數（不再 Check Failed）、列出沒搬入的 `facts.jsonl`（沒有時不列）；`manage_dups` scan 用真的 `MemoryManager` 找得到重複、比對筆數有上限；`facts.jsonl` 原地不動、只有沒改名成 `.bak` 的才算；真的 manager 跑過每個記憶 action；記憶閘道的錯誤是結構化 JSON、GDPR 匯出把它當成記憶不可用；`random_recall`、`memory_diagnostics` 直接名已移除；ADR-0256 起「每個記憶 action」也跑 `reindex`，個數不變；ADR-0277 的 conversations 明示 `source="legacy"`，維持原回歸範圍、不依賴 UI reader |
| `test_legacy_sync_repairs.py` ⭐ | （ADR-0253；ADR-0264 起不搬檔）：同步改用公開的 `get_legacy_dir`；成功的 JSON 留在原地並記為已處理、不建 `backup/`；寫入失敗時 JSON 留在原地、不記已處理並計入 `errors`；`LegacyStore` 寫入失敗往上拋；只查看不建資料庫檔；沒有目錄時什麼都不建 |
| `test_legacy_archive.py` ⭐ | （ADR-0264；Memory 端用 tmp_path，Proxy 端點在沙盒真實 ResourceManager 上跑）：`LegacyStore.archive` 一個交易（任一列失敗全部不留）、既有 `upsert_messages` 照舊覆寫、已處理紀錄只讀不建檔不建表；同步不搬檔並記已處理、重跑冪等對照表、沒有 id 的訊息決定性 id、失敗不留紀錄可重跑；`archive_payload` 直接寫庫不產生檔案；匯出端點寫進舊對話庫、不回路徑、沒有可歸檔訊息回錯誤；設定頁同步與統計端點（待處理 → 同步成功 → 已處理）；`legacy/` 的保護沒放寬；兩個 tools 目錄不引用新寫入口。ADR-0286：同 id 內容不同另存成版本（`version_of`、重跑不重複、改過兩次各存一版、session 或角色不同也算）、同一批再歸檔算確認在庫、版本讀回不符整筆 rollback、`None`／list／dict 內容存成文字、決定性 id 用原始內容、同步回 `conflicts`、統計遇到不是 UTF-8 的檔算待處理、`archive_payload` 把版本算進確認數、數字 id 比對成文字、同一批裡同 id 出現兩次、同內容在多個 session 各存一份 |
| `test_legacy_stats_errors.py` ⭐ | （ADR-0253）：`GET /v1/stats/legacy/{id}` 出錯回 500 `SYSTEM_ERROR`、同步發生例外或部分檔案失敗回 500 `SYNC_FAILED`（不再回 200）；查看統計不建舊對話目錄與資料庫 |
| `test_legacy_time.py` ⭐ | （ADR-0258；純函式、不碰時鐘）：秒／毫秒／數字字串／帶時區 ISO 換成毫秒的對照表（含 1e11 邊界、空值、非正數、布林、NaN、沒有時區的 ISO）；台北呈現並標 `Asia/Taipei`（秒列、毫秒列、UTC 前一天的跨日、讀不出時用呼叫端字樣）；與 continuity 的 `format_epoch_to_taipei` 同一時刻同字串；台北日界與壞日期；秒範圍換毫秒保留最後一整秒 |
| `test_legacy_dates.py` ⭐ | （ADR-0258；tmp 事實庫、tmp 舊對話庫、autospec 向量替身，不載入嵌入模型）：從 Proxy `memory_tools` 入口測隨機回想（秒列、毫秒列的台北日期與時區、讀不出時間顯示未知時間）與查記憶（台北日界篩選、標時區）；`normalize_time_range` 對照表與 is_recent；舊對話日期搜尋（前一天最後一毫秒、隔天第一毫秒、最後一秒內、秒列毫秒列都找得到、不出現 1970；ADR-0274 起經 `search_conversations`、有日期舊到新）與混合單位排序；同步與寫入存毫秒；三個呈現點不呼叫沒時區的 `fromtimestamp`／`strptime`（AST）；ADR-0277 明示 `source="legacy"` 以維持日期回歸範圍，store 斷言改讀 typed page／row |
| `test_memory_conversations_contract.py` ⭐ | （ADR-0274／0277；共用雙庫沙盒與合成身分）：`conversations` 是公開動作、預設值、邊界值可收、壞參數（日曆、反向日期、`page_size` 0／21／字串、`limit`、role、source）被拒、欄位不撞 runtime 與閘道可信名稱、多帶欄位被拒、Proxy 驗證層指出欄位；既有廣告 schema 不變（query 的 `limit` 仍預設 5）、`page_size` 範圍出現在 schema；ADR-0277 以 ui／all 成功與省略等於 all 取代尚未接線的拒絕預期，核對雙庫內容及界線外來源 |
| `test_memory_conversations_search.py` ⭐ | （ADR-0274；tmp 或沙盒 app-data 舊對話庫、autospec 向量替身）：沒有日期新到舊分頁（總數、下一頁提示、offset 超過）、有日期舊到新的台北日界（只給起日、只給迄日）、role 與關鍵字（萬用字元照字面）；沒有資料夾時什麼都不建（不建目錄、不寫 stats）、資料庫不被改動、壞資料庫回 internal_error；每個身分（含 DebugAgent）只讀自己的；舊內容與標題關不掉界線；預覽與標題截斷；三種模式 read_only（私有設定沒這行時是 dangerous；讀被追蹤檔只看 conversations 那一格）；無痕叫不到；ADR-0277 的共用呼叫明示 `source="legacy"`，維持舊庫回歸、不讓預設雙庫改變原測試範圍 |
| `test_tool_result_time_suffix.py` ⭐ | （ADR-0258；時鐘凍結）：工具結果尾綴只有執行完畢的台北時間，任何工具名（含 `save_memory`）都沒有提醒；字串、content list、content 字串、空 list 四種形狀 |
| `test_memory_notices.py` ⭐ | （ADR-0253）：有沒改名的 `facts.jsonl` 時產生 `unmigrated_legacy_facts`（`info`、帶數量）並記一次 warning；只有 `.bak` 時沒有提示；`GET /v1/memory` 回傳啟動時算好的 `memory_notices` |
| `test_personal_catalog.py` ⭐ | （ADR-0255；pytest `tmp_path` 裡用真的 `ResourceManager`，不碰 `data/`）：根目錄第一次 `list` 自動建出 `.touka/catalog.json` 並附提示行（子目錄不附、個人空間不存在時不建）；write／append／str_replace／delete 成功後更新；目錄檔壞掉或被刪時重建；終端機、上傳等繞過動詞的寫入讀時補上；`list`／`find`／`grep` 看不到 `.touka/`、呼叫端帶的 `exclude` 無效（`ResourceManager` 不帶 exclude 時行為不變）；寫入類動作碰 `.touka/` 一律拒絕（4 個動作 × 一般、新檔、`..`、大小寫 4 種路徑，另有絕對路徑與還沒建立時）；經檔案動詞與 Proxy 橋接層回 `permission_denied`、只記一行 warning（write／edit／delete 3 個）；假目錄讀時照實際檔案修正；越界項目不留、junction／symlink 不跟隨、不碰別人的個人空間；目錄項目只收個人空間內的相對路徑；並行寫入一次只重建一個；排除任務板、派工結果、暫存與鎖檔，大檔不讀，筆數上限；資源代理協定與 `stat_files` |
| `test_facts_schema_migration.py` ⭐ | （ADR-0256 段 2；ADR-0263 加 8 個：v2 凍結、v1 庫先備份再升到 v2、v2 升級失敗仍可讀 v1、v1 程式的 SQL 在 v2 庫照用、v1 程式把 v2 庫當較新版、失敗狀態下新的讀取路徑都能用；寫死版本的既有測試改成跟著目前版本；`test_t40` 擴到所有版本並擋模組常數；DB 都在 `tmp_path`，舊庫用 `8b2e242e` 的 SQL 字面量合成，不載入嵌入模型）：舊庫先備份再升級、目前版一個位元組都不動、全新庫不備份（旁邊有升級備份時記警告並出提示）；備份失敗、被佔用、空間不足時不升級也不留檔；部分升級只補缺的欄位、極舊的庫補 `importance`／`emotions`；任一步失敗全部回滾並保留備份；撞名加序號，輪替只留最新 3 份、不碰其他檔、時鐘倒退也保留新檔，輪替失敗只記警告；比程式新的庫照用；步驟只增、不用一次跑多句的 API、v1 凍結；撞到既有 id 丟例外不覆蓋，寫入失敗訊息不含資料庫原文、以結構化錯誤回給模型；子程序寫一半就當掉留下的 journal，重開時回滾（`S603`，見 NOQA_REGISTRY）；鎖內重讀版本、連線都關、改名前備份連線已關、COMMIT 失敗也建得起共用實例；舊程式能寫升級後的庫、還原備份能用；離線重建拒絕落後或不存在的庫、不建檔；重置畫面列出升級備份；失敗分類；執行中才讀不到時設旗標、檔頭完好但資料頁壞掉時提示一直在，直到真的讀成功一次；單一壞列只讓那一列當空值；被鎖住不改狀態；讀不到當作有記憶；複查不建檔、不久等；ROLLBACK 失敗也不提交；中斷的暫存檔清掉、長得像的不動；升級失敗也輪替；每個失敗狀態剛好對應一個提示；失敗狀態下各讀取路徑都能用；寫新欄位的函式一定過寫入閘（AST） |
| `test_memory_index_state.py` ⭐ | （ADR-0256 段 3；ADR-0263 加 6 個：封存時向量沒同步就標 `failed`（丟錯、回 False、取不到向量庫）、`reindex` 補上已封存事實的 payload、沒有向量的直接標 `ready`、同步失敗仍是 `failed`、只動自己的；tmp 事實庫，向量庫用 autospec 替身或 tmp 本機 Qdrant 加固定的替身編碼器，不載入嵌入模型）：向量寫入失敗、建 collection 失敗、取不到向量庫時事實都留著並標 `failed`；寫進索引才標 `ready`（時間單位秒）；封存以事實庫為準；語意結果回查事實庫，查重忽略已封存或不存在的語意結果（還在的仍擋）；完全相同的內容不靠向量庫也擋得住；重複掃描跳過已封存的候選；`reindex` 補上缺的、只碰呼叫者自己的事實、一筆失敗不影響其他、有上限、升級後的舊事實是 `unverified`、沒升級的庫拒絕；診斷列各狀態筆數（停在 v0 時全算 `unverified`）；`reindex` 合約不能指定對象；`.example` 與被追蹤的權限檔都標 `safe`，設定沒列時是 dangerous（Manual 要人核准、Auto 交審查員）；離線重建不寫回狀態；ADR-0286 乙-2：語意查詢失敗丟錯（查詢失敗、讀不到向量庫；還沒有向量庫仍是空的）、查記憶附語意失效、存記憶與一步取代照存並附查重沒做到、重複掃描沒掃成回 `upstream_error`（沒有記憶時不開向量庫）、封存回四種結果並只在有改動時更新時間、忘第二次不重做、資料庫錯誤回 `internal_error` 不說查無 |
| `test_memory_worker_thread.py` ⭐ | （ADR-0288，舊審查報告乙-3；tmp 事實庫、autospec 向量替身、假的舊對話庫）：記憶工具（存、查、scan、resolve、supersede、forget、reindex、診斷、隨機回想、對話搜尋、匯出）、記憶列表與切換嵌入模型端點（偵測和套用之間不插進別的記憶工作）、刪除靈魂前的記憶檢查、重置的清記憶與重算、舊對話統計與同步端點都不在主迴圈上跑（假的儲存層等主迴圈放行，在主迴圈上會等不到），而且都在名為 `touka-memory` 的那一條工作者執行緒上、一次一個；診斷的資料夾掃描與讀檔不在主迴圈上；清除工作自己的執行緒同步等工作者；同時存四筆一樣的只存一筆；工作者裡巢狀呼叫直接跑（在暫時當作工作者的 daemon 執行緒上測，捷徑壞了也不會卡住）、主迴圈上同步等工作者會被拒、帶呼叫端 contextvars 且改了不外流；等待取消時排隊的不跑、之後才失敗的記到 log；`run_memory_job_to_end` 與重置的清記憶被取消時等做完才放手（取消兩次也等）；兩條執行緒同時第一次用向量庫時 collection、模型、單例都只建一次；所有等待都有逾時 |
| `test_env_file_lock.py` | （ADR-0288，舊審查報告乙-3 第 2 輪；tmp `.env` 經 `TOUKA_ENV_FILE`，不改行程環境變數）：測試拿著 `config.ENV_FILE_LOCK` 時，設定頁 `update_env`、`persist_generated_secret`、`persist_generated_secrets`、首裝精靈 `SetupService.apply`、切換嵌入模型 `apply_switch` 五個寫 `.env` 的地方都等到放開才寫，放開後寫進去、原本的值還在 |
| `test_memory_lifecycle.py` ⭐ | （ADR-0263；tmp 事實庫，向量庫用 autospec 替身或 tmp 本機 Qdrant 加固定的替身編碼器，tmp 對話 DB，不載入嵌入模型）：一步取代把舊的封存並連到新的、沒給的欄位沿用舊的、`tags=[]` 清空、回覆第一個 ID 是新記憶、新向量寫不進去仍取代；和 save 相同的寫入檢查（對照表：一般、超長截斷、注入樣式、完全相同、語意太像、空白）；語意查重只跳過被取代那筆、和被取代那筆一字不差也擋；被取代的必須是自己仍有效的記憶（不存在、別人的、已封存、已被取代回同一個 `resource_not_found`、不洩漏存在與否）、交易內再驗一次、寫入本身不碰別人的列；較新版本與升級失敗不寫、寫入失敗不留半套；模型不能帶 `source_context`／`safety_flags`／`status`／`superseded_by`、無效的 operation 欄位組合被拒；來源只由閘道記（含對話來源）；風險和 resolve 相同；舊向量封存、失敗時標 `failed` 由 `reindex` 補上；`include_history`：只從有效結果往回、forget 的永不回、有效那筆被 forget 整條不回、有旗標的舊版本附註排後、深度上限、不跨身分也要 superseded 狀態、讀不到照實說、歷史列和其他讀取欄位相同；forget 舊版本從歷史拿掉、升級失敗時 forget 照常；掃重複排除被取代的；語意查重擋下時提示 supersede、完全相同時不提示；diag 只用具名參數轉交 |
| `test_embedding_thresholds.py` ⭐ | （ADR-0265；向量庫用 autospec 替身或 tmp 本機 Qdrant 加替身編碼器，不載入嵌入模型、不 import 人工 probe）：同一張名稱對照表同時斷言加不加 e5 前綴與兩個門檻一致（完整 e5-base、大小寫與空白、沒有組織的 `multilingual-e5-base`、e5-large、兩個 MiniLM、空字串、未知；e5 家族 0.93／0.65、其他 0.92／0.3）；預設值；每個家族下限 < 門檻、表的鍵只能是 `embedding_family` 的值；校準過的家族＝probe 量的模型所屬家族；存記憶的查重、重複掃描依 `EMBEDDING_MODEL` 判定；`query_vectors` 沒給下限時依向量庫的模型查表、明給照傳；人工 probe 的前綴（AST 讀出）＝`_format_for_embedding`、只抓釘 commit 的 safetensors 9 檔且上限 1.2 GB、不 import 專案程式／dotenv／廠商 SDK、不被自動找到且 manifest 登記成人工；合成資料五類三語言齊全、沒有重複句。突變：任一張表改回完整名稱精確比對就紅 |
| `test_memory_sources.py` ⭐ | （ADR-0256 段 4；tmp 事實庫、autospec 向量替身、tmp 對話 DB 與 tmp 舊對話 DB）：存記憶自動帶對話來源、心跳來源與脈衝的觸發節點；無痕與沒有可信定位時不記來源；只有 UI 持久模式的 user 節點會被標記，而且在開始生成前已保存；模型不能提供 `source_context`、閘道換掉外面帶進來的同名參數；來源定位存取往返、原文不在不刪事實；查回舊對話庫裡的訊息、`LegacyStore.get_message` 不建資料庫；舊事實顯示「來源未記錄」、停在 v0 時顯示「來源暫時無法讀取」、查回出錯顯示「無法確認」不讓查詢失敗、格式壞掉的 `sources` 不拖垮查詢；查回會檢查對話歸屬；GDPR 匯出把來源轉成普通 dict；讀取依結構版本選欄位、比程式新的庫不讀寫 `sources`；`SourceRef` 不收多餘欄位、不可改 |
| `test_native_tool_parameter_flow.py` | （ADR-0228；ADR-0256 加 1 個；ADR-0274 加 1 個：`conversations` 帶齊 7 個欄位；ADR-0263 加 1 個：manage_dups 那一例改成 supersede 帶齊 6 個欄位，resolve 另寫一個只比觀察值的測試，`query` 例加 `include_history`）：每個公開欄位的非預設值都一路傳到真的原生 adapter 與後端接點；ADR-0256 起 `query` 帶 `include_sources`，並在 `CASES` 最後加 `memory_tools(action="reindex")`（加在最後，既有 case 編號不變） |
| `test_memory_tool_contracts.py` | （ADR-0228；ADR-0263 加 2 個：公開欄位 `include_history`、`content`，`operation` 列出 supersede、`content` 的描述分 save 與 manage_dups）：記憶工具公開 schema 的欄位與描述、`query` 回事實庫的正式 ID、`manage_dups` resolve 的公開 operation、`query` 的非預設篩選傳到兩個庫；ADR-0256 起語意結果要回查事實庫，替身補 `get_active_facts`，個數不變 |
| `test_sns_gateway.py` | `SNSGatewayService`：預設 Agent 路由、`@alias` 覆蓋、Session ID 隔離、SSE chunk 聚合；R-2c：home／independent 兩種模式存檔與送出的外人訊息都帶 `【SNS 外部訊息】` 開頭、不算人類原意 |
| `test_group_soul_only.py` ⭐ | **ADR-0245**：群聊只限靈魂——白名單只認 ToukaSoul 區、別名解析到 agents_registry 的靈魂；指向原始模型或 regex 的別名被拒；registry 裡不是靈魂的成員不能被點名、白名單值不是靈魂時不退回比對別名本身；舊歷史裡解析不到的點名略過、最新一則仍嚴格驗證（R-2a）；舊 Native／Custom 項目略過並記警告、檔案不動（tmp 合成設定）；設定 API 只回、只收靈魂區；群聊派發與 SNS `@別名` 只到靈魂 |
| `test_context_snapshot_service.py` | `get_context_snapshot()`/`render_snapshot_markdown()`：無 I/O 純邏輯驗證 |
| `test_skill_service.py` | `SkillService`：技能清單載入、RBAC 過濾、執行委派 |
| `test_mcp_cleanup.py` | MCP 會話清理：斷線後工具卸載、孤兒任務處理 |
| `test_mcp_hub_service.py` | `MCPHubService`（ADR-0186/0187/0189）：官方目錄啟動規格查表（uvx/npm 正名、filesystem 哨兵、未知目錄 fallback 有聲）、registry 社群來源映射與 cursor 分頁、sync 三源合併與 curated 保留、install 哨兵解析 |
| `test_mcp_registry_entry.py` | `registry_item_to_hub_entry` 純映射（ADR-0187/0191）：npm→npx／pypi→uvx、僅收 stdio、isLatest/active 過濾、`env_requirements` 取 isRequired、**條目自帶 version 釘版且無版本即不上架**、remote-only 與不可無人值守安裝者誠實回 None |
| `test_mcp_hub_seed_integrity.py` | 出廠種子誠信（ADR-0189/0191）：curated 誠實標示與命令安全、官方條目與 `official_hub_entry` 配方逐欄 parity、**釘版棘輪**（含擋 `@latest` 偽釘）、Tier A 四條存在性、Atlassian 工具面收斂配方、Figma `--stdio` |
| `test_mcp_hub_server_key.py` | Hub 安裝徽章比對鍵（ADR-0190）：`/hub` 端點投影 `server_key=derive_server_key(name)` |
| `test_mcp_remote.py` | remote 連線設定解析 SSOT（ADR-0192）：stdio/remote 形狀判別、URL 安全閘（https 強制、loopback 例外、非 http scheme 拒）、header `${VAR}` 插值與**缺變數 fail-closed** |
| `test_mcp_remote_config.py` | remote 條目端到端形狀（ADR-0192）：`command`/`url` 互斥契約、URL 閘於 parse 期執法、`enabled` 咽喉對 remote 生效、install 落盤（**憑證只進 env、headers 僅 `${VAR}` 模板**，落盤結果可被 runtime 模型接受） |
| `test_mcp_connect_teardown.py` | **M4**：modern連線失敗與取消的資源釋放、原始錯誤保留。 |
| `test_mcp_connect_timeout.py` ⭐ | **MCP 初次連線逾時（ADR-0191）**：以真子進程模擬「進程起得來但永不回應 JSON-RPC」的伺服器，驗 `session.initialize()` 於上限內乾淨失敗、且序列 `initialize_all` 不被單台卡死（修復前＝ToukaProxy 啟動流程永久掛住）。測試自帶護欄並以**時長**為判準（E74 教訓：驗逾時的測試必須自有界） |
| `test_mcp_connection_owner.py` ⭐ | **MCP 連線 owner 的生命週期**（life1；ADR-0273；假連線在 owner task 進入 anyio task group，另有真 MCPServer 經 ASGI 與真 stdio 伺服器 `tests/fixtures/mcp_v2/lifecycle_stdio_server.py`，只用 `sys.executable`、不連外）：owner 在自己的 task 進入；失聯通知恰好一次（從 ExceptionGroup 取根因、取消狀態已清）；要求關閉不通知（含關閉途中出錯）；期限內收完不算硬關、卡住時期限到硬關且不通知；關閉者被取消就重拋、不硬關；外部原生取消不通知；握手失敗原樣交回，非取消 BaseException 不留未讀例外，KeyboardInterrupt／SystemExit 照樣往外拋（另開事件迴圈）；關閉錯誤的 WARNING 不帶內容；manager：握手中被取消（asyncio 一次、兩次、anyio scope）等 owner 收完才離開、不留 handle 與 task；並行連線只剩一個；先換再關並停舊訂閱；`_install_handle` 讓呼叫端的取消拋出；裝的期間斷線保留失聯紀錄；同名加兩次；失聯只記錄、停訂閱、不重連（hook 收到的 owner 就是失聯那條，且繼承建立者的 contextvars）；被換掉的 owner 失聯不記錄、不呼叫 hook；停訂閱與失聯處理並行不 KeyError；cleanup 並行關、一個總期限、超過只記一行 WARNING、期間才裝進 pool 的正常收掉、中止的握手不被 with_retry 重試；背景收尾不先取消 owner；owner 與訂閱 supervisor 不帶 workload 票；真 SDK／真 stdio：建立者已結束時另一條不受影響、送出用呼叫端 context、Dispatch 下線一段時間後 restart 恢復、失聯後 MCP 頁顯示 error 而不是整頁空白、stdin 關了也不結束的伺服器在期限到後仍被殺掉、stdio 伺服器自己結束時目前不通知（釘住已知限制） |
| `test_heartbeat_logic.py` | Heartbeat 排程邏輯：interval/daily/once 模式觸發時機 |
| `test_dispatch_auth.py` | DispatchAgent 任務看板跨身分代理授權與 Pydantic schema 擴充單元測試 (TDD) |
| `test_dependency_security.py` | 任務更新與前置依賴鏈缺乏驗證安全防範與拓撲排序循環依賴校驗單元測試 (TDD) |
| `test_status_transition.py` | 任務狀態轉換有限狀態機 (FSM) 校驗與 FAILED 重置特權校驗單元測試 (TDD) |
| `test_mission_manager_bugfixes.py` ⭐ | MissionManager C2/H1/H2 修復驗證：`_get_mission_path` 去重、BLOCKED 轉換 blocker_detail 強制、artifact 路徑穿越 Pydantic assignment 攔截 |
| `test_dispatch_session.py` ⭐ | ToukaDispatch SessionManager 與 Handshake：session token 生命週期、agent_id 固定為 DispatchAgent（Vuln 1）、caller 自述資料不進 credential store（ADR-0201）、server-side 驗證 DISPATCH_TOKEN 後換取不同的 session token、操作工具拒絕 master token／無效 token |
| `test_dispatch_server_tools.py` ⭐ | ToukaDispatch MCP 伺服器工具：DISPATCH_TOKEN server-side 驗證、C1 靜態確認（無 FORCE_AGENT_ID）、操作工具不含 agent_id 參數（Vuln 1 靜態確認）、dispatch_handshake 含 dispatch_token 參數（ADR-0050 靜態確認）、session token 流程完整驗證 |
| `test_dispatch_callback_store.py` ⭐ | ADR-0051 DispatchCallbackStore 單元測試：token 唯一性、注冊/查詢、超時回 TIMEOUT、deliver 解除阻擋、deliver 後 token 仍在、未知 token deliver 回 False、wait 後 token 自動移除、未知 token wait 回 UNKNOWN_TOKEN |
| `test_mission_tools_notify.py` ⭐ | ADR-0051 mission_tools 新 action 測試：`notify_on_complete=True` 回傳 callback_token、`await_dispatch_result` 在 deliver 後解除阻擋、`notify_on_complete=False` 不注冊 token |
| `test_dispatch_mission_token.py` ⭐ | ADR-0064 mission_token 機制：create/validate/revoke/clear mission_token、create_external_session 旗標驗證、handshake 接受 mission_token、拒絕偽造 token、外部 session 不得 initialize_mission、initialize_mission 回傳 Mission ID + Mission Token |
| `test_mission_concurrency.py` | MissionManager 並發寫入原子性驗證：asyncio.gather 下兩個 update_task coroutine 透過 MissionLock 序列化，確保無 TOCTOU 資料覆寫（ADR-0049） |
| `test_mission_filelock.py` ⭐ | MissionLock 跨平台 FileLock 行為 TDD 測試（ADR-0049）：async context manager、asyncio.Lock 取得/釋放、FileLock `.json.lock` 路徑驗證、exception safety（FileLock 失敗時 asyncio.Lock 必須釋放）、concurrent coroutine 序列化、cache reset |
| `test_stream_buffer_service.py` ⭐ | ADR-0081 串流緩衝 SSOT：create/append seq、subscribe 重播（從 0/從 N）與 Condition 跟流、雙訂閱者一致、cancel 連坐 producer、同 session 新建取代舊、TTL 清掃、MAX_CHUNKS 超限終結 error、終態 append no-op |
| `test_dispatcher_resumable.py` ⭐ | ADR-0081／0231 dispatcher 解耦與 UI 持久化：resumable_stream 旗標+session_id → producer 入緩衝、回應改訂閱者；不消費/中途斷線 producer 仍跑完並伺服器端持久化 assistant（驗 sandbox DB 樹）；無旗標原路徑零緩衝零雙寫；無身分跳過持久化不崩潰 |
| `test_stream_resume_endpoints.py` ⭐ | ADR-0081 resume 端點：starting_after 切片重播、無緩衝 STREAM_NOT_FOUND、非擁有者 IDOR 拒絕、active 清單擁有權過濾、stop 擴充取消緩衝串流 |
| `test_code_review_fixes.py` ⭐ | 2026-06-11 code review 修復 TDD：AgentIdentity 欄位 `validate_default`（heartbeat/debug 請求模型）、`_extract_response_text` content=None 安全、三家 handler System 豁免一致（結構性）、Gemini finish_reason 對映（length/content_filter/tool_calls/fallback + model 透傳）、`ModelHealthResponse.model_id` 為 str、`reset_quota` 持鎖、`_reassemble_final_json` 空 choices 防禦、MCP Hub 缺鍵條目跳過/拒絕、`get_secret` double-checked locking（結構性＋16 執行緒並行）、custom_models per-model-id 鎖 TOCTOU 串行化（update/delete/rename 雙鎖排序取得）、npm 下載量誠實化（`fetch_npm_downloads` None=未知／真 0 保留／hub 不偽造 downloads、排序退回 popularity；前端對應 `formatHubDownloads` 測試在 `mcp_version.test.ts`） |
| `test_factory_live_keys.py` ⭐ | **ADR-0118（extends ADR-0099）供應商金鑰免重啟即時生效**：dispatcher `factory.py` gemini 分支改讀即時 `src.config.GEMINI_API_KEY`（非 from-import 快照）、claude 分支以 `with_options(api_key=…)` 每請求套即時金鑰（回副本、不污染啟動快照 client）；無即時金鑰時不以空值覆寫既有 client（monkeypatch 模擬 `apply_runtime_env` 執行期改鍵）；ADR-0247：換金鑰後沿用注入的 no-redirect http client |
| `test_embedding_service.py` ⭐ | **ADR-0118（extends ADR-0070）嵌入模型生命週期**：`embedding_service` 三動作嚴格分離——DOWNLOAD（repo id 解析、已快取/下載中防重、逐檔進度、additive 絕不 `force_download`、失敗轉 error 狀態）、SWITCH（`evaluate_switch` 偵測既有記憶＋換模型才要求確認）、APPLY（寫 `EMBEDDING_MODEL` 走 SSOT resolve＋重置向量單例免重啟）；全程 mock huggingface_hub 與記憶系統，不下真模型、不碰真 `data/` |

### 技能系統標準化 (Skill System — ADR-0066 / ADR-0067)

| 檔案 | 說明 |
|------|------|
| `test_skill_parser.py` ⭐ | `skill_parser` 純函式：`split_frontmatter`/`parse_frontmatter`(yaml)/`serialize_skill_md`/`map_to_contract`/`read_touka_field`（metadata 優先、top-level fallback）/`build_frontmatter` |
| `test_skill_service_dir.py` ⭐ | `SkillService` 目錄式載入：掃描 `config/skills/<id>/SKILL.md`、9-key 契約、`_derive_mcp_servers` 由 `.mcp.json` keys 推導（含 exists 檢查） |
| `test_skill_progressive.py` ⭐ | Progressive Disclosure 三層：metadata 常駐 / body 觸發 / `get_skill_resource`/`list_skill_resources` 按需（目錄逃逸防護） |
| `test_skill_dispatcher.py` ⭐ | `sync_skill_dispatcher()` 動態生成 `system-skill-dispatcher`（列非系統 skill 的 name+description），update/delete 後 re-sync |
| `test_activate_skill.py` ⭐ | `activate_skill` 工具（仿 `connect_mcp_server`）：RBAC `can_use_skill` 檢查、寫 `session.active_skills`、下一輪生效 |
| `test_skill_layer_autotrigger.py` ⭐ | `SkillLayer` Pull auto-trigger：skill_ids = `header ∪ session.active_skills`，常駐注入 dispatcher（修正 pulse 場景無 header 不注入問題） |
| `test_skill_layer_mcp.py` ⭐ | `SkillLayer` 只從已授權 skill 收集 `required_tools`/`mcp_servers` |
| `test_skill_layer_rbac.py` ⭐ | `SkillLayer` RBAC 過濾：dispatcher 常駐（免 RBAC）、非授權 skill 不注入（dispatcher 常駐新語義斷言） |
| `test_deep_research.py` ⭐ | ADR-0087 Deep Research：`deep_research` 旗標啟用時 `SkillLayer` 強制注入 deep-research-coordinator（bypass RBAC、不授予工具）、旗標關閉不注入、請求 schema 與 PipelineContext 預設 False |
| `test_tool_layer_skill_mcp.py` ⭐ | `ToolLayer` 偵測 `system-skill-dispatcher` active → 注入 `ACTIVATE_SKILL_SCHEMA` |
| `test_session_active_skills_persist.py` ⭐ | `session.active_skills` 跨輪持久化（`upsert_session` 寫入與讀回） |
| `test_skill_export_import.py` ⭐ | `package_skill`（`.skill` ZIP，`<id>/` 前綴）+ `install_skill_from_url`（`ALLOWED_SKILL_DOMAINS` SSRF + `follow_redirects=False` + `.bak` 原子覆寫 + finally 回滾） |
| `test_resource_manager_archive.py` ⭐ | `resource_manager` archive 能力：`create_archive`/`extract_archive`/`iter_files`，path traversal（resolve+parents）+ zip bomb 防護（entry 2000 / 解壓 50MiB / 下載 20MiB）；ADR-0270 R01～R05：`rewrite_archive` 照原順序拿掉／換掉成員並保留中繼資料、`copy_file_metadata`、`fsync_file`／`fsync_dir`、`read_file_prefix`、`make_writable`，寫入類方法拒絕受保護路徑 |
| `test_skill_header_integration.py` ⭐ | `x-touka-skills` header 與 `session.active_skills` 合併注入整合 |
| `test_skill_catalog_service.py` / `test_skill_catalog_install.py` / `test_skill_catalog_seed_integrity.py` ⭐ | **ADR-0074** 真技能策展安裝：`get_catalog()` 讀 .json／fallback `.example`、`install_skill_from_catalog`（逐檔釘 commit raw 抓取＋路徑穿越拒＋超量拒＋非釘版 commit 拒＋happy 落地含 scripts＋`.bak` 共用上線）、`.example` 4 筆真 skill 釘版/開源授權誠信 ＋ `test_curate_skill_catalog.py`（dev-time 策展工具純函式：raw URL／license 偵測／files 過濾） |
| `test_dispatch_hooks.py` ⭐ | **ADR-0067／0201** `dispatch_hooks.json` 自動召喚：`_load_dispatch_hooks`（fail-closed）、`_build_child_env` 安全核心（全域 master 不進 child env values、密鑰全擋、`mission_token` 注入、`DISPATCH_URL` 為完整 `/mcp`）、`_invoke_external_ai`（Popen 位置參、`shell≠True`、executor 白名單與錯誤降級）、initialize auto_invoke 整合與外部 session 授權邊界 |
| `test_dispatch_supervisor.py` ⭐ | **ADR-0082**：子程序環境與 PYTHONPATH（PROXY_ROOT／Memory 優先；ADR-0262 起由 `utils/child_env.child_python_env()` 組，斷言不變）、DISPATCH_TOKEN 生成持久化、fresh child resource proxy（子程序讀寫範圍檢查；Claude 桌面版把 Python 檔案導到套件私有區時，只多允許標準函式庫實際所在、同名的安裝資料夾；反向案例證明 Python 資料夾旁邊的路徑仍算範圍外，沙盒假根的 `.env`、`data/` 仍被擋，venv 放在 repo 外時 repo 根也在保護清單）、看門狗退避重生；`stop()` 先 graceful 群組訊號，群組未靜默／探測失敗／非零結束／逾時一律強制終止並標 failed，已結束或看門狗已偵測退出不得標成 stopped；硬殺行程樹與 Windows 的強制殺群組在執行緒裡跑、不卡事件迴圈（ADR-0254 H2，假的 terminate 卡在只有事件迴圈能開的閘上）。 |

### 創建引擎 (Creation Engine — ADR-0069)

| 檔案 | 說明 |
|------|------|
| `test_creation_engine.py` ⭐ | 引擎脊椎：`creation_engine.create(kind, spec)` VALIDATE→COMMIT、skill/mcp 兩 `CreationKind` 委派、**CREATE 不覆寫既有 id**（id 衝突於 VALIDATE 擋下）；SMOKE 啟動環境＝白名單底＋spec env（ADR-0254） |
| `test_creation_isolation_grant_security.py` | grant 只取一次且 SMOKE／EVAL 共用、同意不可重播、模型與 spec 不能決定來源、試跑與正式 profile 一致、EOF close、HTTP none 不啟動（含只登記後的 EVAL）、scaffold 本機層級與 APPDATA 對映、clean env 不解析哨兵；直接呼叫引擎缺少 context 時，SMOKE／EVAL 都不啟動、不重新取得層級。 |
| `test_creation_eval.py` ⭐ | Phase B EVAL：唯讀評測任務生成、`eval_formatter` 序列化 round-trip、`EvalRunner` 持久化；Phase D `set_judge`（假 judge→0.9，介面含建立者 `identity`，ADR-0244）計分與**預設關**（NoopJudge / 未注入 judge 不計分） |
| `test_creation_scaffold.py` ⭐ | Phase C SCAFFOLD：`validate_scaffold_spec`/`generate_fastmcp_source`/`McpScaffolder`；識別字校驗、`repr()` 轉義、型別白名單、`ast.parse()` 兜底、名稱 slug 防穿越、不覆寫既有 |
| `test_creation_api.py` ⭐ | Phase D 薄門面：skill/mcp/scaffold 委派、注入命令→`committed=False`（不 raise）、scaffold→engine 全流水線 |
| `test_creation_tools.py` ⭐ | Phase D agent 工具：非 Owner→`❌ 權限不足`（facade 未被 await）、Owner→委派回 `✅`、`identity=None` 拒絕 |
| `test_model_judge.py` ⭐ | Phase D `ModelJudge`（注入假的 Completer，ADR-0244）：float 解析、clamp[0,1]、例外與 `complete_once` 非成功→0.0 fail-safe、只呼叫一次（purpose `judge`、16 tokens）、main 注入 `complete_once`；啟用卻沒設評分模型時不啟用（ADR-0241） |
| `test_guard_creation_sovereignty.py` ⭐ | Phase D Guard #29：缺 `_require_creation_role` 路由→違規、非 creation 模組呼叫 `register_server`→違規、`api.py` import fastapi→違規、乾淨案例＋allowlist 無違規 |
| `test_guard_permissions_registry.py` ⭐ | Guard #32（ADR-0088）：抽 `tool_registry` 鍵為權威工具名集、permissions.json/.example 各角色與全域地板的工具名／MCP 形狀驗證、角色 deny-all 合法、現況 repo 全綠 |
| `test_research_tavily.py` ⭐ | Tavily 搜尋調校（ADR-0090）：env（`TAVILY_SEARCH_DEPTH`/`MAX_RESULTS`/`INCLUDE_ANSWER`）驅動傳參、per-call kwargs 覆寫、falsy（0/""）退回 env 預設、`answer` 置頂、僅 answer 不誤報無結果 |
| `test_research_providers.py` ⭐ | 搜尋備援鏈（ADR-0091）：auto 降級 Tavily→SearXNG→DuckDuckGo、顯式來源、SearXNG answers 字串/物件形狀、DDG 巢狀＋畸形 RelatedTopics 守衛、降級 emit `tool_retry`、未知來源錯誤、Tavily 短路不打 httpx、顯式來源不可用提示 |
| `test_research_google.py` ⭐ | ；ADR-0250 C5：沒有模型時（`None`、沒帶、空字串）`source="google"` 回結構化 `validation_error`（`param=model`），`source="auto"` 不當掉；Google 原生分支硬化（ADR-0091 收尾）：非 gemini 拒絕/缺金鑰不打網路、只用呼叫者的模型 POST 一次不重試（ADR-0241）、429/403/其他狀態/連線例外/空回應可診斷失敗訊息 |
| `test_config_examples.py` ⭐ | 設定範本防線（ADR-0095，Guard #34）：所有 `*.json.example` 合法 JSON、有模型者通過 `model_validate`、不得帶模型未定義的過時鍵（meta 除外）、`server_config.json.example` 的 MCP 種子指向真實檔案/模組、**`ToukaProxy/.env.example` 的 env 鍵 ∈ `Settings`**、**errors.json.example 結構＋生成鏡像對齊**；ADR-0206 另釘死 MCP OAuth 5001–5016 ID/status 合約，避免 fresh checkout 缺碼。 |
| `test_root_env_example_provider_keys.py` | （ADR-0243）：根 `.env.example` 列出六把廠商金鑰且值為空；涵蓋 `guard_provider_parity.backend_required_key_names()` 的全部金鑰名（後端新增廠商時防漂移） |
| `test_config_env_root_example_keys.py` | （ADR-0243）：`GET /config/env` 以根 `.env.example` 為欄位定義，列出六把廠商金鑰；合成 `.env` 已設定的回遮罩、未設定的回空字串（沙盒，不讀真 `.env`） |
| `test_config_live_validation.py` ⭐ | live 設定檔**啟動**健檢（ADR-0096，補 Guard #34 守不到的 gitignore 真檔）：合法＋符合 schema 通過、壞 JSON fail-fast 並點名該檔（`read_json` 會靜默吞、此處直接 `json.loads` 能抓）、有模型者型別錯 fail-fast、缺檔略過、不存在目錄回空、extra 鍵容忍（容模型演進） |

### P8-B 供應鏈與 image binding

| 檔案 | 說明 |
|------|------|
| `test_cve_2026_75604_nextjs_security.py` ⭐ | P11-NEXT：現行oracle要求manifest／lock root／resolved Next exact `16.4.2`（沿已接受修補版同步，原`CVE-2026-75604`覆蓋保留）；`eslint-config-next`與`@next/eslint-plugin-next`同版防split-brain；`GHSA-2xp9-vwfh-vxw4`要求root無Sharp override、Next上游range exact `^0.35.5`且resolved Sharp在`>=0.35.4,<0.36.0`。 |
| `test_p8b_supply_chain_gate.py` ⭐ | ADR-0210 RED 1＋live audit remediation＋Node 24 LTS／P11-NEXT修訂：canonical六鍵policy／實際六bundled rows與forged parent／惡意child／壞shape拒絕、fixed telemetry，Debian與獨立npm11.19.1完整SRI、Chat兩OpenSSL套件only-upgrade與actual版本floor、兩fresh network-none replay契約；committed npm policy（含audit/fund/update notifier停用）、Chat exact engines/packageManager/devEngines、Next framework family與ESLint preset/plugin exact `16.4.2`、上游Sharp range與root override退場、IDE build-toolchain exact pin與Node 20 extension-host API floor、五個advisory package的exact safe version、official-registry＋SHA-512 lock（Chat 與 IDE）、npm 11 `inBundle` parent-integrity binding、兩個 `package.json` 都沒有 lifecycle hook 且 `install:controlled` 只是 `npm ci --ignore-scripts`（ADR-0282）、禁止build-time Google Font、Chat dev/builder/runtime explicit lifecycle（只有`node-pty`單套件rebuild）＋Next telemetry／`serve` update check停用＋local serve、Proxy pinned uv/Node/Python child、全部Node child單一digest、兩production images candidate-manifest label、repo-owned offline gate，以及`.dockerignore`排除host artifacts／`data/`／secrets。 |
| `test_guard_all_frontend_runtime.py` ⭐ | ；Q38新增Guard17/34原生pytest argv；ADR-0210／0222 local-runner：`guard_all.py` 在任何守衛前以`package.json` exact engines驗證ambient Node/npm，版本正確後再驗`os.userInfo()` user-profile capability；drift／無profile均只回報一次並fail-fast。另鎖定44道總數及#22 Python／#23 Chat＋IDE Node aggregate的唯一接線，不得退回單一Chat runtime audit。 |

### 資料結構與序列化

| 檔案 | 說明 |
|------|------|
| `test_tree_history_serialization.py` | `TreeHistory.model_dump()` 可被 `json.dumps()` 正確序列化，無 `TypeError` |

### IDE 整合

| 檔案 | 說明 |
|------|------|
| `test_ide_handler.py` | **M4**：modern固定版本連線、每次token及失敗關閉；沒有人類核准時 `meta=None`。 |
| `test_ide_human_approval_security.py` ⭐ | **IDE「由人核准」標記**（ADR-0246 U2c-7）：與 IDE Vitest 共用的字面向量（HMAC-SHA256、金鑰＝IDE session bearer）；換金鑰、工具或參數就對不上、鍵順序不影響；`call_ide_tool` 只在 `human_approved=True` 時把標記放進 `_meta`；`ProtocolManager` 的 ToukaUI 分支傳遞 `human_approved`；ToolExecutor 只有人類卡片核准才設 True，被拒不呼叫，YOLO 放行、Auto 審查員放行與模型自帶的同名欄位都是 False |
| `test_floor_executor_wiring_security.py` ⭐ | **底線與 executor 接線**（ADR-0246，R-2c 修正，假審查員、不打 API）：`tool_risk` 少了 `agent_control` 或 `action_param` 改名，Auto 下 `switch_brain` 仍只有人能核准（無人在場拒絕、審查員沒被呼叫）；`effective_shell` 依平台判斷（非 Windows 一律 bash、Windows 不認得的 env 標 unsupported），告訴審查員的 shell 和 `_execute_core` 共用同一個判斷；審批卡截斷判斷（一般參數 200 字、`command` 8 KB 邊界），卡上有參數被截斷時 IDE 不拿到「由人核准」標記 |
| `test_ide_discovery.py` | **M4**：loopback現代discovery身分／token與版本驗證。 |
| `ToukaIDE/src/__tests__/*.test.ts` ⭐ | ADR-0202／0219／0246／0250（ADR-0250 加 `inlineCompletion.test.ts` 4 個：沒設或空字串的模型不發請求、有設照送、`package.json` 預設空字串並說明留空不啟用；ADR-0254 H11 再加 1 個：請求不帶取樣參數）：modern SDK conformance、Bearer、Host／Origin／exact route 守衛、multi-client 與取消隔離、workspace jail、server lifecycle 與 Webview 純文字邊界；U2c-7：只剩 Manual／YOLO、黑名單直接拒絕、IDE 不放寬總表、「由人核准」標記驗證（只用一次、綁住這次呼叫、偽造與別的 session 金鑰無效）、真 ToukaMcpServer 上沒有合法標記照常跳視窗、沒 bearer／非本機 Host 被擋、核准的指令只跑一次。`mcpServerBodyParsing.test.ts`（GHSA-27r3-jxmh-p828）：真 ToukaMcpServer 的 JSON 解析把 gzip、deflate、br 請求內容在任何路由前回 415、不呼叫任何 zlib 解壓函式；公告描述的「gzip 後面多帶位元組」請求在解壓前被擋、之後伺服器照常服務（不重現當機）；未壓縮與 `identity` 照常解析。`mcpServerErrorResponse.test.ts`：真 ToukaMcpServer 的 400、413、415 與驗完權杖後路由丟錯的 500、503 都回固定 JSON、不含 stack 與檔案路徑，狀態碼照 Express 的順序取，5xx 寫日誌、4xx 不寫，格式正確但沒帶權杖照舊回 401。 |

---

## 二、整合測試 (`tests/integration/`)

### 認證與身分

| 檔案 | 說明 |
|------|------|
| `test_auth_middleware.py` | JWT 中介層：合法/過期/偽造 token 回應碼 |
| `test_auth_routers.py` | `/auth/login`、`/auth/register`、`/auth/refresh`、`/auth/logout` 端到端流程 |
| `test_auth_heartbeat.py` | Heartbeat 認證：service token 驗證、擁有者 refresh 效期聯動（含合併原 test_heartbeat_auth.py）；ADR-0250 A3 起 setup 讓靈魂有主要模型（沒有會回 409），斷言不變 |
| `test_proxy_auth_flow.py` | Proxy 認證完整流程：帶卡放行、LDH 自動發卡、外部無卡 401（ADR-0245：沒登入不能聊天） |
| `test_identity_router.py` | `IdentityRouter`：assume Agent、管理員代理保留 admin 角色 |
| `test_identity_isolation.py` | 使用者僅能 assume 自己 `owned_agents` 中的 Agent |
| `test_identity_graceful_reissue.py` | LDH 收斂（ADR-0131／禁令 60）：本地帶失效 token → 401 且不發降級卡；完全無卡 → LDH 照舊發卡；外部壞卡 → 401（行為翻轉：舊「本地壞卡重發卡」退場）|
| `test_session_owner_attribution.py` | ADR-0132 session 明確人類歸屬：owner_user_id 遷移冪等／INSERT-only 記錄／UPDATE 永不改（不 claim）／`_persist_assistant_turn` human 不污染 agent_id（回歸鎖）／`_can_access_session` 聯集矩陣（owner 通道＋legacy 逐位不變）／`_is_registered_agent` fail-closed／normal 標裸模型 id 放行＋指派真 agent 仍 403／list-get 出口消毒／backfill 腳本 dry-run 不寫＋apply 只填 NULL 冪等 |
| `test_soul_identity_handshake.py` | 本地環境無 `X-Agent-ID` 時自動升級為 DebugAgent 並存取靈魂 |
| `test_agent_isolation.py` | Agent 資源物理隔離：Alice 無法讀取 Bob 的 soul/custom 目錄 |
| `test_admin_rbac.py` | 管理員 RBAC：`/v1/admin/users` 角色保護、非 admin 拒絕 |
| `test_authz_dependency_precedence.py` ⭐ | **ADR-0239**：`Depends` 閘在錯誤 body 下仍先回 403、函式內呼叫的閘回 422，合法 body 時回 403；reset 排除 Debugger；skills 安裝端點僅 Owner／admin 放行（安裝服務以 mock 取代）。 |
| `test_souls_rbac.py` | 靈魂端點 RBAC：admin 看全部、使用者只看擁有的靈魂、代理身分看到空清單 |

### 系統功能

| 檔案 | 說明 |
|------|------|
| `test_api_schema_sync.py` | 後端 Pydantic 模型與物理 `openapi.json` 強一致性（Contract as Code）；ADR-0250 B1 起合併函式從 `openapi_merge` 匯入，不再匯入會開沙盒的 `update_openapi` |
| `test_toukaide_marketplace_vsce_parity.py` ⭐ | **ADR-0218（1 opt-in case）**：只有fresh exact-host gate明示`TOUKA_NODE_24_EXECUTABLE`時，才以Node 24.19.0＋repo-local vsce 3.9.2 package pytest暫存區synthetic projection；raw inventory須與release authority exact 10-member SSOT同集合且同數量。預設skip，不執行產品bundle／candidate／網路。 |
| `test_bootstrap.py` | 系統引導流程：缺失 `.env`/`.json` 時從 `.example` 自動復原；ADR-0209 驗證來源／目標可分離且既有 runtime 設定不覆寫。 |
| `test_env_bootstrap.py` | 環境變數引導：`MEMORY_APP_DATA_DIR` 動態覆蓋、Pydantic Settings 中心化 |
| `test_imports_integrity.py` | 跨模組匯入完整性：所有進入點可正確匯入，無循環或缺失 |
| `test_static_analysis.py` | **靜態分析整合**：Ruff與SSOT負向案例使用pytest tmp_path，保留時間／路徑／PaC／環境違規及合法來源斷言；子程序使用當前Python。 |
| `test_server_lifecycle.py` | 伺服器優雅啟動/關閉：無 RuntimeError、AnyIO 資源正確釋放 |
| `test_system_resilience.py` | 系統韌性：記憶系統損毀時降級模式啟動、提示詞注入生存警告 |
| `test_degraded_features.py` | 降級功能驗證：`system_degraded` 旗標觸發 DEGRADED 標籤與提示詞注入 |
| `test_facts_schema_startup.py` ⭐ | （ADR-0256 段 2；ADR-0263 起版本與壞步驟跟著目前版本，個數不變；記憶資料根指向 `tmp_path`，向量庫是替身）：Proxy 啟動時在任何背景工作之前升級一次；啟動升級失敗時查詢照常、存記憶被擋；每種失敗在升級成功前都出對應的警告提示，目前版沒有提示；建不出事實庫時出「讀不到」；啟動開事實庫不經 `MemoryManager`；讀不到時查詢回結構化錯誤；啟動後才壞掉時提示出現、`/v1/memory/list` 不會回成空的；換嵌入模型後提示跟著目前的共用實例；檔頭完好但資料頁壞掉時提示一直在，直到讀取成功一次 |
| `test_mcp_dispatch_tools.py` | 外部 AI 以 DispatchAgent MCP 連線並透過 `mission_tools` 進行看板生命週期聯調測試 (TDD) |
| `test_dispatch_mcp_transport.py` | 真 socket modern discover／tools、request-scoped progress SSE 與 peer-close；退役 GET /sse 及 POST /sse/messages/ 均 404。 |
| `test_dispatch_supervisor_lifespan.py` ⭐ | **ADR-0082**：DISPATCH_AUTOSPAWN 預設關閉／明示啟動、stats wiring，以及 protocol cleanup 失敗後仍繼續停止 dispatch。 |
| `test_dispatch_bidirectional.py` ⭐ | ADR-0051 雙向回呼通道整合測試：非 localhost 來源拒 403、未注冊 token 拒 401、完整 roundtrip（注冊→callback POST→Event 解除阻擋） |
| `test_creation_route.py` ⭐ | ADR-0069 創建路由（TestClient）：無身分→401、錯角色→403、Owner skill/mcp/scaffold→200 committed、驗證失敗→200+errors（非 500）、多餘欄位→422（`extra=forbid`）；`_require_creation_role` 純函式 gate 單測；`eval_scoring` 時評分拿到發起建立的身分（skill／mcp／scaffold 各 1，ADR-0244） |
| `test_creation_smoke.py` | **M4**：sandbox creation stub支援modern discover及功能驗證。 |
| `test_creation_scaffold_smoke.py` ⭐ | ADR-0069／0199：scaffold 生成 SDK v2 `MCPServer` stub，auto／legacy 皆真啟動並通過 SMOKE 握手（scaffold→create 端到端） |

### 心跳與背景任務

| 檔案 | 說明 |
|------|------|
| `test_heartbeat_api.py` | ToukaHeartbeat API 端到端：`/v1/schedules` CRUD、Bearer Token 守衛（C9 fix） |
| `test_heartbeat_task.py` | `/v1/heartbeat/awaken` 背景任務觸發：正確身分錨定；ADR-0250 A3 起 setup 讓靈魂有主要模型，斷言不變 |
| `test_background_pulse.py` | `_execute_single_pulse` 單次脈衝執行：ReAct 輸出 mock、autonomy 攔截；ADR-0250 A3 起 setup 讓靈魂有主要模型（沒有可用模型的脈衝會直接結束），斷言不變 |
| `test_heartbeat_sliding_window.py` | PostHeartbeat hook 行為：子對話完全跳過、hook 失敗隔離不影響心跳結果 |
| `test_heartbeat_token_consistency.py` | Heartbeat 共用憑證經真的入口與對外邊界（合成 token）；三個全新子行程各自匯入 Proxy 與 Heartbeat，確認讀到持久化的輪替、不靠指派常數。子行程逾時是 300 秒卡死保護（一個子行程約 20 秒，機器忙時會超過原本的 30 秒），結果只看 exit code 與通過標記 |
| `test_interrupted_runs.py` ⭐ | ADR-0078 P5c：in-flight 標記 set/clear、啟動掃描偵測＋清 stale、結果存服務（當機復原可見化） |

### 模型與資料

| 檔案 | 說明 |
|------|------|
| `test_soul_restoration.py` | ID 歸一化：`ToukaSoul` 前綴剝離、配置動態合併、Pruning 豁免保護 |
| `test_agent_brain_not_set.py` | （ADR-0241；ADR-0250 A1 改寫）：沒設主要模型回 409 `AGENT_BRAIN_NOT_SET`、body 形狀、不呼叫任何模型；人類（admin＋步升密碼）在靈魂頁存檔帶清單裡的 `base_model` 後恢復；人類改了值主要模型跟著換（原本斷言不變）；顯示字串當 `base_model` 送回改成 404、設定不寫（原本 200、不寫主要模型）；啟用中的大寫自訂連線模型可以存成主要模型（原本不寫入）。模型名先種進沙盒模型清單 |
| `test_group_chat.py` | 群聊模型 API 可見性、`@alias` 解析、分發流驗證 |
| `test_model_prompt_ssot.py` | `SoulService` 系統提示詞包含核心主權標籤且結構一致 |
| `test_agent_deletion.py` | 刪除模型時同步觸發 `AgentService.delete_agent_registration()` |
| `test_vault_search.py` | `vault_tools` grep/find 協作：身分主權隔離下的跨目錄搜尋 |
| `test_editor_ssot.py` | Editor API：路徑穿越防護、`Path.is_relative_to()` 邊界驗證 |

### 並發與生命週期

| 檔案 | 說明 |
|------|------|
| `test_concurrency_isolation.py` | 高並發下不同 Agent 的 Identity Context 不互竄（TOCTOU 防護） |
| `test_lifecycle_atomicity.py` | 執行生命週期原子性：任務啟動後 Token 中途過期不中斷任務 |
| `test_autonomy_survival.py` | Agent 自主存活：30 天寬限期內 System 角色資源存取正常 |
| `test_router_smoke.py` | 全 Router HTTP smoke：所有 GET 端點回應不得為 500 |
| `test_upsert_concurrency.py` ⭐ | ADR-0078 P5d：`upsert_session` 併發鎖——兩路併發設不同欄位皆存活（未修前 stale-read 丟欄位） |
| `test_task_utils.py` ⭐ | ADR-0078 P5d：`spawn_logged` fire-and-forget 例外被記錄、成功不報、回傳 Task |
| `test_terminate_process_tree.py` ⭐ | ADR-0078 P5d：行程群組連坐殺（POSIX killpg／Windows taskkill／taskkill 非零回退 kill／無 pid 回退／已結束吞例外） |
| `test_session_sandbox_no_residue.py` ⭐ | （ADR-0251）：子程序跑 pytest（`TMP`／`TEMP` 指到 `tmp_path`、乾淨環境、有逾時），`--collect-only`、在 repo 裡指定 `--basetemp` 被拒、探針開一個 Qdrant client 三種情況結束後都不留下 `touka_test_session_*`；探針 `sandbox_child_probe.py` 不符合 `test_*.py`，平常不收集 |

### 其他

| 檔案 | 說明 |
|------|------|
| `test_docker_deployment.py` | Dockerfile 物理完整性：`COPY` 範疇涵蓋所有本地匯入模組 |
| `test_react_integration.py` | ReAct 循環整合：tool call → tool result → 繼續生成 |
| `test_tool_loop_budget.py` | （ADR-0146）：工具輪預算取代 nudge 續圈——純文字回覆就結束回合；互動預設不限、無人在場一律有限（排程值＞全域保險絲＞保底 50）；額度用完不執行本輪工具、改走無工具收尾；每日配額只算無人在場的脈衝。ADR-0269 起 `test_interactive_multi_tool_rounds_unlimited_by_default`、`test_bounded_run_wraps_up_gracefully` 兩個測試的授權設定（patch 的 NameGrant）從只有 `vault_tools` 補成 `vault_tools`＋7 個檔案動詞（read、list、find、grep、write、edit、delete，常數 `_VAULT_TOOLS_WITH_SUCCESSORS`）；斷言不變，因為這兩個測試要驗的是預算與多輪行為：舊名 `vault_tools` 仍走 shim，只是新規則要求它的後繼動詞都在這一輪的清單上，否則回 `not_advertised` |
| `test_soul_identity_handshake.py` | 靈魂身分握手：本地環境自動升級 DebugAgent |
| `test_sse_auth_query.py` | SSE 認證與端點：驗證 EventSource token 降級驗證流程與連線完整性 |
| `test_heartbeat_retrigger.py` ⭐ | ADR-0078：UI 一鍵重新觸發（擁有者 202＋自清單清除／非擁有者 403／會話 404；強制 Auto）；R-2c：Debugger 與靈魂自己 403、別的靈魂的對話 403（連兩個靈魂都擁有也一樣）、人類擁有者與 admin 在自己的對話照常以 Auto 跑 |
| `test_process_group_kill.py` ⭐ | ADR-0078 P5d：`terminate_process_tree` 真殺以新行程群組啟動的子進程（平台相依） |
| `test_isolation_linux.py` ⭐ | （+1：刪除靈魂前的清除經真執行器清空自己的 home／tmp，權限被拿掉的資料夾也清、連結不跟；ADR-0272，marker `isolation_linux`；Windows 上整個模組算一個跳過；`TOUKA_REQUIRE_LANDLOCK=1` 時 Landlock 不可用、或 Fast Open／MPTCP 沒關就失敗而不是跳過；沒設時跳過並寫出原因，例如「網路設定不安全（net_tcp_fastopen），landlock 層不可用，且沒設 TOUKA_REQUIRE_LANDLOCK」）：真核心——pin 自測、讀寫區與唯讀執行區的 shebang、符號連結與硬連結設定檔略過、必要路徑錯誤的失敗標記、spec 上限、pin fd 重開仍被拒、繼承的 fd 被關、封鎖埠（共用規則集與自建）、讀不到 `/proc/1` 與父行程的 environ、信號與抽象 socket、PDEATHSIG（spawn 端先結束時不執行）；封鎖埠也擋 bind；pin 搬到 fd 100 以上、shell 重用低號 fd 不受影響；Fast Open 開著時自測照樣通過、另外回報 `net`，層級 none、原因不落到 `fs_9p`，人核准的隔離降級仍讀不到受保護的路徑；自測 fixture 隨機名稱；monitor 與 runner 端到端；終端與驗證指令讀不到其他代理人、soul、`.env`，專案根換成符號連結就拒絕，pytest 以專案根為 rootdir、資料夾裡子專案的 `testpaths` 照常；專案授權（Touka 目錄、名稱大小寫、上層 `pytest.ini`）；演化建置、驗證與 rlimit。在 Docker 跑：用 `tests/docker/Dockerfile.isolation-test`（建在 proxy 映像上）的一次性容器，`--network none`、`--sysctl net.ipv4.tcp_fastopen=0`（沒加時層級是 `net_tcp_fastopen`，靠 landlock 層的案例在設了 `TOUKA_REQUIRE_LANDLOCK=1` 時全部失敗）、`--init`（直接 `docker run … pytest` 時 pytest 是 PID 1，本來就會收養孤兒行程，伺服器沒設 `PR_SET_CHILD_SUBREAPER` 的退化會被蓋掉，setsid 的兩個案例照樣通過；加 `--init` 時 pytest 的 pid 是 7）、唯讀掛工作資料夾，直接指定 `tests/integration/test_isolation_linux.py`（寫 `tests/` 會收集到映像沒裝依賴的模組而中斷）；突變腳本 `tests/docker/isolation_mutations.py`（清單 38 項；腳本自己是 PID 1、pytest 是它的子行程，所以 `server_subreaper_off` 有沒有 `--init` 都會紅）。第 2 輪修正整合後（`wt/iso-c2` `75a3306f`）：hardened 與開發版各 44 passed，突變 27 項全紅；全量 pytest 10201 passed／8 skipped／6 failed（6 個 E1）。第 2 輪補修正 FE：hardened 與開發版（加 sysctl、設 REQUIRE）各 46 passed，突變 32 項全紅；不加 sysctl、沒設 REQUIRE（模擬 GitHub contracts job 原生執行，不能代替真的 runner）29 passed／17 skipped、都寫了原因；不加 sysctl、設 REQUIRE 時 17 failed。C2-2 +66：狀態 fd 指令寫不進去、失敗代碼經狀態 fd 送到、每次啟動照實讀網路設定、可信直譯器是 root 擁有；`repair_chain` 經 `/proc/self/fd` 對 O_PATH fd chmod、不跟連結、不修別人擁有與多連結的檔；權限自癒（代理人資料夾被鎖後檔案動詞自動恢復、append 不多寫一份、soul、快照與記憶庫備份不修、`ST/proxy` 與 `custom` 根由定期檢查修回）；執行器伺服器（真 unix socket：隔離內連不上控制通道、setsid 出去的行程被收、`max_seconds` 與斷線殺掉遠端群組、共用規則集擋連線與 bind、ipc 被改權限 5 秒內修回、設定有錯保持存活、同一個 ipc 不起第二個）；終端、驗證指令、演化經真的執行器（in-process）跑；9p 掃描快取與 inode；compose 的 healthcheck；CI 主機要達到 landlock 層。整合 10（`afaf46d7`）：加 sysctl、設 REQUIRE 112 passed；不加 sysctl、不設 REQUIRE 0 failed／94 passed／18 skipped（都帶 `[skip:…]` 標籤，就是 GitHub isolation job 的 pass A）。第 3 輪 +6（+8、−2）：啟動器自己設 NoNewPrivs、`oom_score_adj` 500、umask 022；執行器的兩個私有資料夾執行中被開成 777 時 5 秒內收回、停機時被改則重啟時先收回再服務（原本「state／ipc 有群組位元判 `private_dir`」的兩案因此拿掉）、是連結或不是自己擁有時不碰並判 `private_dir`。R3a（`a53d493d`）：加 sysctl、設 REQUIRE 118 passed，再加 `--init` 118 passed；不加 sysctl、不設 REQUIRE 0 failed／100 passed／18 skipped。第 4 輪 +2：重啟時 state 或 ipc 是 000 也先改回 0700 再服務（777 那兩案照舊）；執行中 `ctl.sock` 被改成 666 時 5 秒內收回 0600。R4a：加 sysctl、設 REQUIRE 120 passed，再加 `--init` 120 passed；不加 sysctl、不設 REQUIRE 0 failed／102 passed／18 skipped。R4c（修好突變腳本的 basetemp 之後）：120 passed，清單 33 項全紅（hardened 加 `--init`，跑一次），第 3、4 輪受影響的項目重驗都紅。執行器的端到端：`docker compose -p touka-iso-<名稱> -f tests/docker/isolation.compose.yml up --abort-on-container-exit --exit-code-from iso-client`（檢查腳本 `executor_check.py` 11 項、`landlock_proxy_check.py` 2 項，網路都是 internal、不連外）；C2-3 起加上 MCP stdio 案例（Landlock／executor 各 10）：真判定與 SDK discover/list/call、檔案隔離、200 KiB 與超限單行、EOF 恰好通知 owner 一次、三種關閉路徑收掉同群組孫行程、40 MiB stderr（超過讀取端的緩衝）、失敗代碼與三次嘗試、目標的 PDEATHSIG；U3e-1（`:u3e` 映像）：加 sysctl、設 REQUIRE 145 passed，再加 `--init` 145 passed；不加 sysctl、不設 REQUIRE 0 failed／105 passed／40 skipped；突變 38 項全紅 |

---

## 三、Proxy 層測試 (`tests/proxy/`)

| 檔案 | 說明 |
|------|------|
| `test_handler_integration.py` | Gemini/OpenAI handler 端到端：context mock、串流/非串流等價 |
| `test_handlers_sovereignty.py` | Handler 身分主權：`AgentIdentity` 在 context 缺失時的 fallback 解析 |
| `test_gemini_no_explicit_cache.py` | （ADR-0243）：一般、無痕、未帶旗標 × 初次、長迴圈、帶 `tool_config` 的輪次，payload 都不含 `cachedContent`、每輪都帶 `system_instruction`／`tools`，且沒有任何對 `cachedContents` 的連線 |
| `test_sovereign_identity_chain.py` | 身分提升與路徑隔離：低權限 Caller 呼叫 Actor（Owner）時工具環境自動提升至 Owner 路徑 |
| `test_token_resilience.py` | Token 韌性：脈衝執行期間 Token 過期不影響任務完成（Lifecycle Atomicity）；過期卡再請求 → 401 不降級（ADR-0131 翻轉）|
| `test_failure_visibility.py` ⭐ | ADR-0078 P5b 失效可見化：工具例外帶真錯誤、OpenAI 網路錯誤可見、Claude/OpenAI/Gemini 三家 degraded 注入 |
| `test_interrupted_runs_stats.py` ⭐ | ADR-0078 P5c：`/stats` 折入 `interrupted_runs`（當機復原可見化） |
| `test_daily_quota.py` | P3/P4：`/stats` 折入 `daily_quota` 與 `background_activity` |
| `characterization/` ⭐ | 特徵化 golden-SSE 套件（ADR-0156 E0；`harness.py`＋`test_golden_openai.py`、`test_golden_claude.py`、`test_golden_gemini.py`＋`goldens/`（ADR-0248 起相容 Chat 與 Gemini 每個情境跑 1 個 chunk、語意多 chunk、位元組切割三種模式，對同一份 golden）；model-p1a 補 `upstream_error`／`truncated`／`refusal` 各 3 家、`test_harness_ci_strict.py`：`CI` 為真時缺 golden 失敗，ADR-0242）：對**抽取前舊 handler** 錄製 SSE chunk 序列、重構全程**逐位元**斷言——共通 8 情境×3 家＋方言專屬（gemini 503 retry/長迴圈不帶 `cachedContent`（golden 檔名沿用 `recache_mid_loop`，ADR-0243）/誠實矯正輪；claude thinking_split/native egress）＋`gemini__thought_parts_split`（ADR-0157 K3 新行為＝對新碼錄製）。正規化＝假時鐘（上游 +7s/工具 +5s）＋固定 request_id（K5）；golden 生命週期＝缺檔錄製+skip、**刻意**行為變化＝刪 golden 重錄（diff 進 code review）；golden I/O 兩站＝registry 註冊的 `IO_PRISON_VIOLATION` noqa；model-p1b 刻意重錄 14 份（只有 `token_usage` 的 total／cost 改為整次加總含快取，ADR-0244）；3b-U5a 刻意重錄 6 份 `{claude,openai,gemini}__{truncated,refusal}`（終端 finish_reason 照實回報、拒答／安全過濾多一則提示，ADR-0248）；T3 刻意重錄 3 份 `{claude,openai,gemini}__memory_warning`（提醒文字改成「這一輪沒有成功用 `memory_tools` 存下記憶」，ADR-0281） |
| `upstream/` ⭐ | （ADR-0242；ADR-0244；ADR-0247；ADR-0248；ADR-0261）：上游請求契約。`test_long_horizon_prompt_invariants.py`（長期組裝不變式，真 SoulLayer＋沙盒家對話／前情摘要＋真查記憶結果＋continuity，Claude／OpenAI 相容／Gemini 三種送出格式；判定式自測；OpenAI 相容的人格順序與「每則訊息只送該格式允許的鍵」兩格原本是 `xfail(strict=True)`，ADR-0260 修正後已轉成一般測試）。`harness.py` 的 `RecordingAdapter` 只換 `send_round`、送出當下深拷貝請求（不 patch httpx）；`test_harness.py`（L 可替換性、快照不被後續輪改動、三家與 Responses 正規化）、`test_upstream_claude.py`（模型名與 max_tokens（3a 起不改寫別名、照請求值否則規則表預設）、白名單、D-3、斷點輪替（3a 起新對話首個工具輪也標；原生入口呼叫端放在非最後一塊或 tool_result 內層的斷點也一併清掉）與每輪不超過 4 個斷點、矯正／收尾、KF-10 已修正（多則 system 依序合併，ADR-0247））、`test_upstream_openai.py`（KF-8、KF-9 已修正（ADR-0248）、401 提示、逐輪）、`test_upstream_gemini.py`（KF-7a 與 KF-9 已修正（ADR-0248）、KF-7b 已修正（模型名只去 `models/` 前綴並轉小寫、空名與非法名回可讀錯誤，ADR-0247）、KF-9、轉換器、無快取逐輪）、`test_prefix_stability.py`（迴圈內前綴、工具 schema 決定性、KF-5 已修正（UI 入口也切出跨回合穩定前綴，ADR-0247）、跨回合 system）、`test_adapter_stop_reason.py`（三家停止原因正規化，拒答與截斷不得算 end、表外一律 error、錄製台照轉，ADR-0244）、`test_upstream_responses.py`（Responses 主迴圈工具輪，第 2 輪原樣回送加密 reasoning 與 function_call、call_id 對應、每輪 store／include／同一個 prompt_cache_key，ADR-0248）；r55（ADR-0271）：`test_upstream_claude.py` 的 UI 入口改成 `stop` 以 `stop_sequences` 送出（預期的行為改變，個數不變） |
| `test_core_keepalive.py` ⭐ | ADR-0157 K1：`react_core.pump_send_round` 心跳（原 claude-only 升核心單一實作）——上游久候發 SSE heartbeat comment、快速多項 round 原樣透傳零心跳、後續項久候同樣有心跳（覆蓋 gemini 退避睡眠段）；事件驅動（`_KEEPALIVE_S` 縮 10ms＋Event 放行，不真睡） |

### 記憶長期穩定性評估（`tests/memory_eval/`，ADR-0261、ADR-0268、ADR-0280、ADR-0284）

全部離線：假串流、沙盒 session DB、決定性替身編碼器（零下載）。真模型只走人工 `tests/smoke/manual/memory_eval_probe.py`（見下方「人工 live」表）。

| 檔案 | 說明 |
|---|---|
| `memory_eval/test_scenarios.py` | smoke 11 情境 id 唯一、smoke 不預放活動事件且只有 continuity 開活動記錄器、continuity 情境三行都預放（上次人類訊息、上次自主執行、最近完成）且各只有一輪、兩個情境的事件不重複（ADR-0280）、每輪有已知規則、預計請求數在硬上限內、合成資料不含實體資料位置、防洩漏字串只在人格、三條惡意記憶路徑都帶假關閉 tag、「喵」一律用句尾規則判、未知 id 拒絕（別的套件的 id 也拒絕）、情境、判定與 `live_bounds` 模組不載入產品程式；ADR-0284：記憶衝突第 1 回合看有沒有試著換掉舊記憶、追問在新的對話、長對話只放在舊對話庫、隨機口令三段子音字母且避開題目答案、人格帶入給定的口令、有抗改名的輪都檢查否認名字、經過時間三題與相對時間題用較嚴的規則（時間軸套件照舊）；`stats` 也不載入產品程式、重複的 id 拒絕（ADR-0284 第二部分） |
| `memory_eval/test_judges.py` | 規則對照表（每條規則都有通過與不通過；改名只認句首、不是問句的自稱，後面接否定的述語才放過、否定另一個名字與「沒錯」照樣算改名（ADR-0284），含引述、反問、語氣詞、英文 call me；否認自己的名字（問句不算）；句尾喵只看真正的句尾、引號裡的不算；拒絕時說出口令也算洩漏、口令任一段（不分大小寫、去掉空白與連字號）也算、看這次的口令；有沒有試著換掉舊記憶（對預放那筆取代、封存或刪除，被擋下也算，加上新偏好存下或寫在取代內容）、舊偏好只在「以前、不再」的子句、獨立的數字；較嚴的經過時間（相鄰單位加總、區間、「一週多」「將近兩週」、否定與轉述不算）；10/15 實測三個誤判原文當回歸；「昨天」逐子句判：直接修飾的否定、沒有猜測的問句、轉述摘要或紀錄不算，引號、猜測、反問（含「你不是昨天說的嗎」）、轉述之外又自己說、轉述詞後面接自己的主張仍算錯，沒有主詞的英文直述否定不算錯，10/15 Haiku 5.5 的誤判依進度紀錄重建當回歸（ADR-0280））、未知規則丟例外、送出失敗的一輪一律不通過、時長抽取（中英、中文數字、日期不算時長）；ADR-0284 決策 4（2026-10-27）：10/27 實測相對時間題五次真實回答都判對，日期與時刻換成等長的佔位字元（有年份的日期、接星期或週末的月日、「9 點 28 分」；「1/2 天」「2-4 天」「每 3-4 週一次」照舊算時長），「卻」緊接摘要或紀錄、再緊接轉述動詞才算轉述（「看了摘要卻記得是昨天」、摘要是受詞的「我看了摘要卻說是昨天」照舊算錯，摘要是主詞的「不過摘要卻寫著」「沒想到紀錄卻顯示」照樣算轉述），日期遮罩的數字邊界與星期寫法，判定細節先列衝突並附不跨子句、最多 20 字的原文，報告截到 200 字後還看得到；同日重跑後：重跑時相對時間題五次真實回答都判對，指某一天的說法（「哪」接數字、「那、同、某」緊接「一」：哪一天、哪一個週、那一天、同一天、某一年）換成佔位字元，後面接多、半、鐘的與「如同、連同、等同」照舊算，「週末」不算週，「這兩天」「前一天」「上一週」「第 3 天」「每一天」「有一天」照舊算時長 |
| `memory_eval/test_runner_offline.py` | SSE 解析（文字、工具軌跡、Touka 五種錯誤格式逐段認（第 2 輪才出錯也認得、正常回答提到錯誤不算，ADR-0284）、dispatcher 中途錯誤、成本累計）、合成資料（長對話 100 輪只放進舊對話庫且填充輪不帶數字、家對話、只寫沙盒、替身編碼器、替身名稱下真的本機向量庫存一筆用改述查得到、預放事件照產品的事件 id 規則且記在另一個較早的對話、只用產品的事件種類）、多輪 driver（歷史、寫回對話樹、Auto 模式、跨 session、植入摘要／家對話／事實、出錯整批停、USD 軟上限）、報告回應去敏截斷、真 pipeline 打到合成靈魂（主要模型、人格、memory_tools、continuity、Owner 身分、請求不含本機路徑）；活動記錄器只在 continuity 批打開、整批結束時關掉並等背景寫入做完、逾時回報還剩幾筆、情境開始前等不到背景寫入時這個情境不送、整批結束時沒寫完記成停止原因、真 pipeline 下時間軸三行都有內容且下一個情境看不到上一個情境預放或執行中記下的事件（ADR-0280）；ADR-0284：每個情境一個新的合成靈魂（靈魂 id、植入、請求、擁有權都分開；真 pipeline 下家對話的注入句不會帶到下一題）、長對話經歸檔 hook 植入且提問時對話是空的、沒接 hook 就拒絕、記憶衝突預放後與第 1 回合後各讀一次記憶庫、沒接記憶庫讀取就判失敗、被擋下的呼叫以呼叫 id 從執行器入口補上名稱與參數、記錄執行器入口後一定還原、真 pipeline 與真執行器下 Auto 無人在場擋下取代與刪除且舊記憶還在、有效記憶走產品唯讀查詢（封存的不算）、經產品歸檔進沙盒後真的查舊對話查得到、歸檔目標在沙盒外拒絕、這次的口令同時進人格、判定與報告遮罩；ADR-0284 第二部分：重跑先跑完一輪再跑下一輪、每一次都是新的靈魂、每題 k/n 只算跑完的次數、整批只算每題都跑完的輪、n 不相等時標出、多輪時沒跑的帶輪次（只跑一輪照舊）、整批通過率與 Wilson 範圍、同一輪重複的情境要兩次都跑完 |
| `memory_eval/test_probe_bounds.py` | 匯入不改產品預設、安裝後主迴圈 1 次／不讀備援／摘要員審查員被拒、四家都先核銷預算、輸出上限在預算與送出前核對、實送上限（5.5 拉到 8,192、其他廠商照請求值）、天花板 8,192（送出時與安裝前）、送前最壞花費（含工具 schema、拒送時不核銷不外連）、四家用量結清帳本、核准範圍不符不安裝、範圍列出每個上限（含實送值、天花板、規則）、CLI 上限、M1 型號與 `claude-haiku-5-5` 路由、沙盒植入讓合成靈魂是 Owner、植入目標在沙盒外拒絕、清理順序（稽核寫入、記憶工作者、紀錄檔、SQLite、Qdrant 都在刪除前；等記憶工作者做完，ADR-0288）、只刪 OS 暫存底下、先印報告再刪、刪不掉回非零、人工閘之前只 import 標準函式庫與政策模組；r55（ADR-0271）+1：價目分檔後送前估價仍取貴檔（Haiku 5.5 仍 0.625／2.5）；ADR-0280：替身名稱不含 e5、門檻不跟 `.env` 走、建庫前設名稱並讀回、向量庫已建立或讀回不符就停、核准範圍不符時不改名稱、範圍列出替身與活動記錄器那兩行、只有 continuity 批讓 driver 開記錄器；ADR-0284：每個情境的靈魂都是 Owner、設核准的模型、寫入給定的人格，在沙盒外拒絕；driver 拿到每次不同的口令、記憶庫讀取與歸檔 hook；跑的時候裝上 UI 對話讀取器、跑完卸下；ADR-0284 第二部分：核准範圍印輪數與「每輪×輪數」的預計請求數、1 輪的核准不能跑更多輪、輪數 1..5、driver 拿到核准的輪數、報告帶評估程式雜湊與 provider／model／套件／題目；價目表查不到的型號拒跑（命令列停在問核准之前）、有完整名稱印 `pricing=exact`、靠家族名稱印 `pricing=family_estimate`（覆蓋表也一樣分；用合成價目表）；請求上限 1..200；命令列預設跑 1 輪、重複的情境 id 拒絕 |
| `memory_eval/test_stats.py` | （ADR-0284 第二部分）Wilson 與 Newcombe 拿 Newcombe（1998）發表的例子核對、沒有跑完的次數就沒有範圍、雜湊涵蓋每個評估模組與實測腳本、改程式就變但換行不影響、比較工具並排每題 k/n 與差距範圍、不含 0 才叫有差、評估程式雜湊／套件／題目不同或缺欄位就不比、型號不同照比、中途停下與 n 不相等時標出、一邊沒有跑完的輪就沒有差距範圍、從實測輸出裡找到報告、UTF-16 存的檔讀得到、不能比回 1、參數不對回 2；缺輪數／跑完的輪／每題計數／整批、某題沒有計數、題目清單是空的都不比，實送輸出上限與替身設定兩邊都印、不同時標出，讀不到檔或編碼不對回 2 |
| `memory_eval/test_live_bounds.py` | 輸入估計（請求 JSON 每字元 1 token＋1,024 餘量、含工具 schema）、最壞花費（輸出上限用滿、輸入取一般與寫入快取較貴價）、帳本（超過拒送且不記帳、剛好等於上限放行、結清後照實際、沒回報用量照最壞、報告逐請求欄位與合計）、回應摘要去敏（這次的口令整組與每一段、金鑰樣式、Windows 路徑）且先遮再截 200 字 |

---

## 四、靜態守衛 TDD (`tests/lint/`)

| 檔案 | 說明 |
|------|------|
| `test_identity_anchoring_guard.py` | `IdentitySovereigntyRule`：偵測函式簽章中的裸 `agent_id: str` 參數 |
| `test_security_kernel_ratchet.py` | **ADR-0239 ratchet**：`routers/` 不得定義私有角色檢查函式（只能是指向 authorization gateway 的別名）；直接啟動子程序只能出現在 `execution_broker.py` 與明列例外（`process_registry.py`、`ToukaDispatch/server.py`）。ADR-0272 起 exec／fork／spawn 類（呼叫、引用、from-import 等 8 種寫法）只准出現在 `sandbox_kernel/launcher.py`，附正向對照。 |
| `test_native_npm_guard.py` | Guard #25：前端 native module（node-pty 等）存在時 Dockerfile 必須備妥 `python3 make g++` |
| `test_dockerfile_prod.py` | `ToukaChat/Dockerfile.prod` 結構驗證：multi-stage build、non-root user、無 terminal_server.mjs |
| `test_dockerfile_dispatch.py` | ADR-0082：dispatch 隨 proxy **單一映像**出貨的完整性＋安全黑名單驗證——root Dockerfile 必須 `COPY . .`、根 `.dockerignore` 不得排除 ToukaDispatch/ToukaMemory、必須排除 `.env`/活體 config/`data/`、且 `.dockerignore` 不得被 gitignore、首裝一次性密碼檔要被 `.gitignore` 忽略（`git check-ignore`）（原本要求保留 `!ToukaProxy/config/*.json.example` 例外，該例外含萬用字元會讓建置走進 `.toukadev` 而失敗，已移除，範本出貨改由 `test_docker_build_inputs.py` 驗證） |
| `test_dockerfile_base_images.py` ⭐ | Proxy／Heartbeat必須用核准 官方Debian trixie-slim tagged immutable ref、Python3.12與明示bash供應與非root useradd；拒絕alpine/musl替代，保留CPU manylinux相容條件。 |
| `test_npm_docker_paths_guard.py` | Guard #26：npm scripts 的父目錄相對路徑必須在 `docker-compose.yml` 掛載為 volume |
| `test_release_licensing.py` | 發布的映像與 VSIX 帶著授權聲明：三個映像的 `/app` 都有 LICENSE、NOTICE（chat 從具名 context `touka-root` 取得，它宣告成空的 scratch 階段，沒帶 context 時建置失敗、不會去 registry 拉同名映像；compose 與 GHCR 發布都要帶它）；chat 執行期安裝帶 `--omit=optional`（不帶進 sharp 的 LGPL libvips）；GHCR 發布只在 main 跑、每個 action 都釘 commit SHA；`ToukaIDE/THIRD_PARTY_NOTICES.txt` 列滿 lock 的執行期套件，版本都對得上 lock |
| `test_ci_pr_checks.py` | **ADR-0254 A4、ADR-0278**：四個產品子專案（`ToukaProxy/src`、`ToukaMemory/src`、`ToukaHeartbeat`、`ToukaDispatch`）`ruff --select F` 0 錯誤；CI `contracts` job 恰有一個鎖定版 ruff F 步驟涵蓋四個根目錄、沒有放寬旗標；`toukaide` job 在 devEngines 的 Node／npm 上依序跑 `install:controlled`、`test`、`typecheck`、`compile`，不能被 `if`／`continue-on-error` 跳過；`CONTRIBUTING.md`「PR 前必做」寫明 CI 綠燈就能開 PR、每項都標「CI 會跑」或「維護者合併前補跑」、完整 ruff、pyrefly 與 ESLint 棘輪標 CI、標示和 CI 實際跑的對得上；`frontend` job 在安裝依賴之後跑 Guard #35、不能被跳過，也不再有 braces 步驟（ADR-0282）；跑整套 pytest 的 job 恰有一個（ruff 與 pyrefly 棘輪靠它進 CI）。ruff 帶 `--no-cache`，不寫檔 |
| `test_pyrefly_ratchet.py` | **ADR-0278 §7**：pyrefly 型別檢查（範圍在 `pyproject.toml` 的 `[tool.pyrefly]`，先從隔離與安全邊界開始），每種錯誤的數量和 `tests/lint/pyrefly_baseline.json` 完全相等；整份設定逐項釘住（範圍、search-path、平台 linux、版本對齊 `.python-version`、不讀 ignore 檔；不准多出排除、錯誤開關或直譯器路徑）；`project-includes` 每個樣式都還對得到檔案；範圍內不准 `# pyrefly: ignore-errors` 整檔壓制；收集指令的參數整組釘住（目前的直譯器、repo 設定、JSON 輸出）；結束碼異常、錯誤輸出有 INFO 以外的行（設定欄位打錯、search-path 不存在、日誌格式改變）、輸出不是完整 JSON 時收集失敗，不當成 0 個；用真的 pyrefly 檢查暫存資料夾的小專案，核對輸出格式與設定錯誤會失敗；`--update` 排序寫出、寫完即通過。收集與重拍共用 `scripts/lint_ratchet.py` |
| `test_ruff_ratchet.py` | **ADR-0278**：四個產品資料夾的完整 ruff，每條規則的數量和 `tests/lint/ruff_baseline.json` 完全相等（變多要修、變少要重拍基準）；掃描範圍從 CI workflow 讀出、和 F 步驟相同；比對函式的變多、變少、新規則三種情況；ruff 異常結束時收集失敗、不當成 0 個；`--update` 依規則代號排序寫出、寫完即通過、再多一個就失敗；打錯的工具名或參數回 2、不做檢查。收集與重拍共用 `scripts/lint_ratchet.py` |
| `test_tests_readme_listing.py` | **ADR-0278**：版控中的每個 `tests/**/test_*.py` 都要在本檔用反引號列出（有相對路徑比對相對路徑；只寫檔名時檔名必須唯一；沒加反引號的不算），還沒列的凍結在 `tests/lint/tests_readme_unlisted.json`、只准變短（已補列或已刪除還留在清單也算失敗）；反引號裡的測試檔名要對得到實際檔案（萬用字元除外）；本檔不寫「現在有幾個案例」（只擋固定格式：數字欄、粗體與標籤裡的數量、「N 個測試／TDD／案例」、合計句；某一批加了幾個、HTTP 狀態碼、年份、有日期的實跑結果、live 預算不擋，正反例都釘住）；反引號名稱檢查拒絕 `..` 與絕對路徑、有斜線的要等於版控路徑、同名檔另外說明（合成資料測試）；沒有 git 就失敗、不跳過 |

---

## 五、冒煙測試 (`tests/smoke/`)

> ⚠️ **一律以 3.12 venv／`uv run` 執行**（裸 `python` 會落到全域 3.14，套件版本不同）。
> **ADR-0214**：全部自動 smoke 使用逐程序 temp write-root sandbox、deterministic provider mock、
> credential-shaped environment scrub 與模型下載 offline flags；每支腳本亦須在任何
> `src`／`memory_server` import 前自行啟用 sandbox。這不是 OS network namespace／firewall egress
> jail。真實 provider 連線是人工 live probe，永遠不進 pytest、CI、guard 或 master smoke。

```powershell
# 一鍵執行 24 支自動 smoke；manual live probes 只列 NOT RUN
uv run python tests/smoke/run_all_smoke_tests.py
```

2026-08-14 實跑結果：**24/24 Green**；manual live probe 明列
`NOT RUN — HUMAN APPROVAL REQUIRED`，沒有計入成功／失敗數。

### 自動、零計量 API（24 支）

| 腳本 | 說明 |
|---|---|
| `smoke_agent_interaction.py` | Agent 交互流程與 mock provider stream |
| `smoke_ask_user.py` | `ask_user` 事件與回覆生命週期 |
| `smoke_auth_identity.py` | JWT／身分解析鏈 |
| `smoke_autonomy_enforcement.py` | 自主預算與強制截止 |
| `smoke_bash_terminal.py` | 終端策略分類；不執行 shell |
| `smoke_chat.py` | 完整對話 pipeline＋deterministic provider mock，零外連 |
| `smoke_context_snapshot.py` | context snapshot 組裝 |
| `smoke_creation_engine.py` | sandbox scaffold、權限與防覆寫；`eval_scoring=False` |
| `smoke_dispatch_callback.py` | localhost callback contract |
| `smoke_frontend_persistence.py` | sandbox session DB 與 setting round-trip |
| `smoke_group_dispatcher.py` | `@alias` 解析與 mock 多 Agent 派發 |
| `smoke_incognito.py` | 無痕上下文與工具邊界 |
| `smoke_io_prison.py` | Alice／Bob 陰性路徑隔離只在 sandbox 驗證 |
| `smoke_ldh_handshake.py` | LDH 本地握手與 token contract |
| `smoke_mcp_lifecycle.py` | sandbox MCP 安裝／生命週期，不連外部 MCP |
| `smoke_memory_tools.py` | sandbox save→query→forget；embedding fake／offline |
| `smoke_mission_awareness.py` | sandbox mission awareness 注入 |
| `smoke_model_governance.py` | mock 模型治理、健康與 pruning |
| `smoke_provider_routing.py` | 純 provider route 解析，不發請求 |
| `smoke_pulse_rhythm.py` | sandbox autonomy quota 節律 |
| `smoke_router.py` | sandbox TestClient GET route 遍歷 |
| `smoke_skill_activation.py` | sandbox skill activation contract |
| `smoke_soul_recovery.py` | TemporaryDirectory 內的 bootstrap／recovery |
| `smoke_tool_coverage.py` | bounded safe invoke＋wired-only 分類；`research`／dispatch／stop 不實跑；domain error 必須 FAIL |

### 人工 live／可能計量 API（永久排除自動化）

| 入口 | 額度／副作用 | 自動狀態 |
|---|---|---|
| `tests/smoke/manual/live_chat_probe.py` | 固定 synthetic prompt，單一真模型 request，`max_tokens` 有界；本機寫入仍在 temp sandbox | `NOT RUN — HUMAN APPROVAL REQUIRED` |
| `tests/smoke/manual/memory_eval_probe.py` | smoke 11 情境、預計 21 requests，每個情境一個新的合成靈魂、每次一組新口令、裝上 UI 對話讀取器（ADR-0284）（continuity 2 情境、預計 3 requests，打開活動記錄器、事件只預放在沙盒）；替身嵌入名稱固定、門檻取產品預設（ADR-0280）；`--repeats` 1..5（先跑完一輪再跑下一輪、每一次新靈魂，輪數進核准範圍；報告帶每題 k/n、整批 Wilson 範圍與評估程式雜湊，兩次結果用 `python -m tests.memory_eval.stats` 比較）、價目表查不到的型號拒跑、靠家族名稱估價標 `pricing=family_estimate`（ADR-0284 第二部分）；requests 硬上限 1..200；請求裡的 tokens 1..1,024，送出前核對 Touka 實送值（Anthropic 照規則表、其他廠商照請求值）、工具天花板 8,192；每 request 送前檢查已花＋最壞花費 ≤ `--budget`（≤2）；主迴圈只送 1 次、零 fallback、摘要員／審查員不呼叫；只送合成資料；寫入全在 temp sandbox、結果 stdout（含每 request 用量），結束先放開 handle 再刪沙盒（ADR-0261、ADR-0268） | 同上 |
| `tests/smoke/manual/thinking_latency_probe.py` | 三個 Claude 5.5 型號 × 少想／模型預設 × 1..5 次，requests 硬上限 1..30；請求裡的 `max_tokens` 1..1,024、實送值另列（只有 Haiku／Sonnet 5.5 少想照請求值，其餘 8,192）、天花板 8,192；每 request 送前檢查已花＋最壞花費（該型號價、Haiku 5.5 取貴檔）≤ `--budget`（≤3）；`complete_once` 不重試、不換廠商；本機寫入只在 temp sandbox（ADR-0271） | `NOT RUN — HUMAN APPROVAL REQUIRED` |
| `tests/smoke/manual/embedding_threshold_probe.py` | 不呼叫 LLM 廠商、不收費；只連 Hugging Face 下載 `intfloat/multilingual-e5-base`（釘 commit、safetensors 9 檔 1,134,355,035 bytes、上限 1.2 GB），快取全在 `--cache-root`（`HF_HOME`）底下、跑完刪除；沒帶 `--human-approved-model-download` 只讀快取；不載入專案程式、不讀金鑰；資料 `tests/fixtures/embedding_threshold_pairs.json`（ADR-0265）。manifest 保守標 `metered_api=True` | 同上 |
| `scripts/run_scorecard.py` | 每個 scenario 呼叫真模型；request hard budget 1..64、零 retry／fallback（主迴圈只送 1 次，ADR-0261），拒絕 Gemini／web_search／docgen_xlsx；`--budget` 仍是 scenario 間 soft cap | 同上 |
| Debug Center scorecard | 正常 UI 有費用警示＋`confirmToast`；後端 endpoint 尚無 ADR-0214 capability／request hard budget，故只可人工操作、不得由自動化真 runner 呼叫 | 同上 |
| `scripts/record_tool_eval_baseline.py --model ...` | 12 cases；samples／turns 硬上限 4，最多 48 requests；每 request tokens 預設 128／硬上限 512；結果寫 `%TEMP%\touka_tool_eval_result_*`，不自動 promotion 或清除 | 同上 |
| `scripts/record_tool_eval_baseline.py --list-models` | 呼叫 provider `/models`；通常不耗 token，但仍使用外部 API／quota | 同上 |
| `scripts/run_tool_eval_episode.py` | 單一 case；requests 預設 3／硬上限 16、tokens 預設 128／硬上限 512、timeout 預設 180／硬上限 600 秒；主迴圈只送 1 次（ADR-0261）；Gemini／research-auto／非 DebugAgent 拒絕，寫入全在 temp sandbox | 同上 |
| Creation `ModelJudge`（兩個 eval gate 同時開啟） | 每次 score 一次真 LLM request | 自動環境強制關閉 |

`scripts/record_tool_eval_baseline.py --dry-run`、`--list-cases` 類本機列舉，以及
`scripts/measure_tool_token_cost.py` 的 schema token estimate 都不呼叫 provider，不消耗 API 額度。

### 人工同意規則

需要 live 診斷時，先停止並向人類揭露：exact command、script、provider、model、case／scenario／
retry 與最大 request 數、budget／token 上限、送出內容類型及本機寫入範圍。取得**該次或明列
bounded batch** 的同意後，才能在命令加 `--human-approved-live-api`。方案核准、已有 API key、
先前同意、YOLO 或 mock failure 都不是本次授權；未知是否計量時按會計量處理，禁止未核准的
fallback／retry。若未核准，唯一正確狀態是 `NOT RUN`，不得冒充 PASS 或自動 suite FAIL。
---

## 靜態守衛指令參考

```powershell
# 後端
python scripts/lint_ssot.py .                  # SSOT 主規則（IO/路徑/時間/環境/錯誤碼/日誌/raise...）
python scripts/guard_noqa_registry.py          # noqa 計數與 NOQA_REGISTRY.md 同步驗證（Guard #19）
python scripts/guard_dead_code.py              # 死碼偵測 vulture（Python）+ knip（TS）（Guard #20）
python scripts/guard_module_boundaries.py      # ToukaMemory→ToukaProxy 逆向 import 禁令（Guard #21）
python scripts/guard_endpoint_auth.py          # 全 FastAPI 端點認證 Depends 檢查（Guard #24）
python scripts/guard_native_npm.py             # 前端 native module 需編譯工具驗證（Guard #25）

# 前端（ToukaChat/）
npx tsx scripts/guard_frontend.ts             # API Parity（路徑 + /v1 前綴）
npx tsx scripts/guard_time.ts                 # 時間主權（禁 Date.now/new Date）
npx tsx scripts/guard_errors.ts               # 錯誤碼主權（禁硬編碼字串）
npx tsx scripts/guard_env.ts                  # 環境變數主權（禁 process.env/import.meta.env）
npx tsx scripts/guard_fetch.ts                # HTTP 客戶端主權（禁裸 fetch()）
npx tsx scripts/guard_localStorage.ts         # localStorage 主權（禁直接呼叫）
npx tsx scripts/guard_xAgentId.ts             # 身份主權（禁手動注入 X-Agent-ID Header）
npx tsx scripts/guard_eventSource.ts          # 串流主權（禁建立不帶 Token 的 EventSource）
npx tsx scripts/guard_console.ts              # Console 禁令（禁 console.log/warn/error）

# 一鍵全量（含 43 道關卡，推薦）
python scripts/guard_all.py
```

---

## 架構合規性說明

本測試體系同時執行 **「Touka 法律 (Legal Architecture)」**，確保代碼遵循架構守則：

| 守則 | 執法機制 |
|------|---------|
| 物理路徑禁令（TID251） | `lint_ssot.py` 靜態裁決 |
| 身分主權（IDENTITY_SSOT） | `test_agent_isolation.py`、`test_identity_isolation.py`、`guard_xAgentId.ts`、`guard_eventSource.ts` |
| 時間主權（Time SSOT） | `guard_time.ts`、`lint_ssot.py` |
| API 合約主權 | `test_api_schema_sync.py`、`guard_frontend.ts` |
| 環境變數主權 | `lint_ssot.py`、`guard_env.ts` |
| 錯誤碼主權 | `lint_ssot.py`（RAISE_STRING_BAN）、`guard_errors.ts` |
| HTTP 客戶端主權 | `guard_fetch.ts`（FETCH_SSOT，ADR-0024） |
| localStorage 主權 | `guard_localStorage.ts`（ADR-0026） |
| 日誌主權 | `lint_ssot.py`（PRINT_BAN / BASICCONFIG_BAN，ADR-0025） |
| 密碼安全 | `lint_ssot.py`（BCRYPT_IMPORT_SSOT，ADR-0028） |
| 資料庫主權 | `lint_ssot.py`（SQLITE3_IMPORT_BAN，ADR-0030） |
| 模組邊界 | `guard_module_boundaries.py`（ADR-0032） |
| 端點認證 | `guard_endpoint_auth.py`（Guard #24，ADR-0034） |
| noqa 登記簿同步 | `guard_noqa_registry.py`（Guard #19，ADR-0029） |
| Console 禁令 | `guard_console.ts`（Guard #15，ADR-0031） |
| 死碼 | `guard_dead_code.py`（Guard #20） |
| 自癒與降級 | `test_bootstrap.py`、`test_system_resilience.py` |
| 靈魂保護 | `test_soul_restoration.py`（Pruning 豁免、ID 歸一化） |
| 部署等價性 | `test_docker_deployment.py`、`guard_dependencies.py` |
| Native NPM Build | `test_native_npm_guard.py`（Guard #25） |
| 來回計算 SSOT | `test_sliding_window_service.py`（ADR-0036） |
| 創建主權（Creation Sovereignty） | `guard_creation_sovereignty.py`（Guard #29，ADR-0069）：路由 Owner/admin 把關＋禁繞引擎直寫＋門面純度 |
| 權限對照（Permissions Registry） | `guard_permissions_registry.py`（Guard #32，ADR-0088）：permissions.json 角色工具權限必對應真實 `tool_registry`，防改名/移除後漂移 |
| 依賴鎖檔 / 環境一致（Environment Parity） | `guard_environment_parity.py`（Guard #30，ADR-0071 amended by ADR-0117）：4個Python workspace members的base＋optional dependencies／dev group↔uv.lock逐項比對、installable member editable-source要求、Python版本一致與釘版政策（禁`>=`） |
| 測試自癒與真實 ID 匿名化 | 測試前後自清洗與真實 ID 匿名化、動態模型降級防禦（ADR-0073） |

> 執行時請確保已進入專案根目錄，腳本會自動處理 `PYTHONPATH` 設置。

---

## 六、前端 Vitest 測試 (`ToukaChat/tests/`)

前端測試採用 Vitest 架構，測試案例收攏於 `ToukaChat/tests/` 以及源碼同目錄的 `__tests__/`。執行命令：`cd ToukaChat && npm run test`。

### 核心單元測試

| 檔案 | 說明 |
|------|------|
| `unit/hooks/useLocalStorage.test.ts` | 驗證 `useLocalStorage` 傳回之 setter 函式參考在 render 間的穩定性 (M5) |
| `unit/hooks/settings/useUserSettings.test.ts` | 驗證 `useUserSettings` 資料層 Hook 不含 UI `confirm` 調用，只專注於 API 通訊 (M8) |
| `unit/hooks/useAgentSSE.token.test.ts` | 驗證 `useAgentSSE` 建立連接時會自動注入 accessToken 以進行 Query Token 降級驗證 (M9) |
| `unit/connections.localstorage.test.ts` | 驗證 `connections.ts` 內的所有 localStorage 讀取皆符合 SSOT 規範，無直接呼叫 |
| `unit/connections.fallback.test.ts` | 驗證 `fetchTouka` 的 404 自動補齊 `/v1` 降級遞迴重構邏輯 (M4) |
| `unit/connections.xagentid.test.ts` | 驗證 `connections.ts` 的 X-Agent-ID Header 動態判定與沙盒設定 |
| `unit/stream_tree.test.ts` | ADR-0080/0081 串流樹純函式：`buildStreamNodes`、`buildUserTurnTree`（user 回合先行持久化基底）、`appendAssistantNode`（resume 重建）、`applyChunkToTree`、`applyEventToTree`（皆含防污染邊界） |
| `unit/connections.resume.test.ts` | ADR-0081 `resumeChatStream`：starting_after URL、chunk/event/done 分發（與 fetchChatStream 共用 dispatchSseLine）、非 2xx onError、跨 chunk 斷行重組 |
| `unit/StreamRegistry.resume.test.tsx` | ADR-0081 重整接回：startStream 先 syncSession user 回合（順序斷言）、resumeActiveStreams 依 active 清單重建 entry＋重播重建樹＋onDone 持久化、已有 entry 不重複接回 |
| `unit/StreamRegistry.test.tsx` | ADR-0080 進行中串流 SSOT：startStream 建 entry、onChunk per-session 隔離不污染他 session、並發上限 5、stopStream abort+清除、onDone 寫回 session、onError toast（非 AbortError）/不 toast（AbortError） |
| `src/components/chat/__tests__/ChatArea.test.tsx` | ChatArea：既有互動回歸＋P11模型切換回呼隨翻譯函式更新，不改聊天或權限邏輯。 |
| `src/components/chat/__tests__/MessageItem.test.tsx` | MessageItem：既有訊息渲染回歸＋P11切換語言後Markdown程式碼複製按鈕使用新翻譯。 |
| `unit/hooks/useChatStream.test.ts` | ADR-0080／P11 useChatStream委派：既有session／新建session／stop／上限提示，以及固定provider與options後切換翻譯函式，限流提示使用新語言。 |
| `unit/hooks/useSessionActions.test.ts` | P11翻譯回呼：切換語言後家對話禁止刪除、過短對話不分割提示更新，且不刪除、不送出匯出或修改訊息。 |
| `unit/ChatProvider.backgroundStream.test.tsx` | ADR-0080 背景續跑端到端：切走不中止、per-session isSending、背景 chunk 不污染、切回看到累積、完成寫回 session；兩對話同時背景生成互不污染 |
| `unit/LeftSidebar.test.tsx` | 驗證 `LeftSidebar` 內部不讀取 localStorage，完全依賴外部傳入的 `isCollapsed` 決定是否隱藏 |
| `unit/SessionItem.test.tsx` | 模擬對話刪除狀態激活，並在 DropdownMenu 觸發關閉事件時，驗證 `onClearDeleteConfirm` 必須被呼叫；同時驗證新合約下的 `onSaveTitle` 與 `onDelete` 編譯相容性 |
| `unit/lib/tree_history.test.ts` | `migrateFlatToTree`／`getLinearMessages`；含環路防禦（parent/children/self-loop 成環皆安全終止、正常深鏈不被誤砍）|
| `unit/hooks/useMediaQuery.test.tsx` | `useMediaQuery` 改訂閱 MediaQueryList `change`（非 window `resize`）：初值取自 mql.matches、change 更新、卸載移除監聽、query 變更重新同步 |
| `unit/lib/mentions.test.ts` | `extractMentionPaths` 純函式：去尾隨句末標點（半形＋全形 CJK）、保留路徑內部 `.`/`/` 與中文、去重保序 |
| `unit/ToolStepTracker.test.tsx` | HAX G11：記憶/檔案庫工具顯示「記憶：<query/path>」徽章、非記憶工具不顯示 |
| `unit/ContextMeter.test.tsx` | HAX G12：context % 標示點擊開啟說明面板（揭露載入則數/上限/滑動視窗），空對話不顯示 |
| `unit/connections.deepResearch.test.ts` | ADR-0087：`deepResearch` 旗標於 openai/anthropic 兩協定皆 threading 至 body `deep_research`（以 onBeforeSend 攔截斷言） |
| `components/setup/__tests__/SetupWizard.test.tsx` ⭐ | ADR-0118 首裝精靈殼層：首步渲染、Next/Back 步進、第一步無 Back、× 觸發 `onClose`、可捲動 overlay + `m-auto` 卡片（高步驟仍可觸及）；ADR-0053 補記：`initialStepId` 從指定步驟開始（側欄「變更密碼」到管理員密碼）、找不到就從第一步；前進到「完成」時呼叫 `onStatusRefresh` 重讀狀態，其他步驟之間不重讀 |
| `src/components/setup/__tests__/SetupWizard.passwordRelogin.test.tsx` ⭐ | ADR-0053 補記（2026-10-23）精靈改完管理員密碼：真的 connections 配照後端規則的假伺服器（改密碼即撤銷所有舊卡）——改完顯示「請重新登入」、隱藏上一步與 ×、焦點在「前往登入」、記下接續第 3 步，沒有用失效的卡打後端；按「前往登入」才登出、登出中按鈕停用；改密碼的回應晚到、畫面已卸載時接續點照樣記下；改密碼失敗顯示原錯誤、不記接續點 |
| `src/components/setup/__tests__/SetupNoticesProvider.resume.test.tsx` ⭐ | ADR-0053 補記：重新登入後接續——同一位管理員且還有沒設定完的項目才開在記下的步驟並用掉接續點；都設好了不開並丟掉；別人的接續點不開不刪；非管理員不取；登入頁還沒有使用者時不取、狀態讀到才開；關掉後不再自己打開；前進到第 7 步會重讀狀態、顯示重讀後的結果 |
| `src/hooks/__tests__/useSetupResume.test.tsx` ⭐ | ADR-0053 補記：接續點存取——同一位使用者取得且取過就刪；別人的不取不刪；1 小時邊界、過期與時間在未來都整筆刪；能解析但格式不對（非物件、陣列、欄位型別錯、空編號、不存在的步驟）整筆刪；沒有紀錄時不寫不刪；登入頁只看不取；hook 在 user.id 出現或改變時取一次（登入、帶有效 session 重新整理都算），StrictMode 第二次讀不到不清掉已取得的步驟 |
| `auth/LoginSetupResumeNotice.test.tsx` ⭐ | ADR-0053 補記：登入頁有有效接續點時用 `role="status"` 說明「管理員密碼已更新」且不取走紀錄；沒有或過期時不顯示 |
| `src/components/sidebar/left/__tests__/SidebarSetupNotices.test.tsx` ⭐ | ADR-0053 補記（2026-10-23）左側邊欄的首次設定警告：出現條件對照表（只給管理員；臨時密碼兩張都出現；其他三種只出現設定警告；狀態沒讀到或全設好時不出現）；沒有警告或沒有提供者時完全不畫；密碼警告只有「變更密碼」、不能關；「變更密碼」打開精靈到管理員密碼步驟、「立即設定」從第一步，兩者都呼叫 `onAction`；「稍後」只隱藏設定警告，側欄重新掛上後仍隱藏；關掉精靈會重讀、問題處理好時警告消失；重讀中或重讀失敗保留上次的警告；提供者在有警告時也不在頁面上多畫東西（舊橫幅在最外層排成直欄的回歸） |
| `unit/LeftSidebar.setupNotices.test.tsx` ⭐ | ADR-0053 補記：兩個警告在左側邊欄裡、「新對話」按鈕上面；按警告的按鈕會呼叫 `onSetupAction`；側欄整欄可上下捲動、不橫向捲動，對話紀錄區有最小高度 |
| `src/components/layout/__tests__/MainHeader.setupWarning.test.tsx` ⭐ | ADR-0053 補記：有首次設定警告時，桌面側欄收合鈕上有紅點、按鈕名稱說明側欄裡有設定警告；沒有警告時沒有紅點、名稱照舊 |
| `src/app/__tests__/page.setup-notices.test.tsx` ⭐ | ADR-0053 補記（手機版）：左側欄在 Sheet 裡時，按警告按鈕會把選單收起來（精靈才不會被 Sheet 擋住）；選單沒打開且有警告時選單鈕上有紅點與說明名稱，沒有警告時都沒有；`app/layout.tsx` 在 `AuthGuard` 裡用 `SetupNoticesProvider` 包住頁面（源碼檢查） |
| `components/setup/steps/__tests__/Step1SystemHealth.test.tsx` ⭐ | ADR-0118 嵌入模型下載＋切換防呆（fail-loud）：模型未變更不打端點直接 `onNext`；變更→下載完成→切換成功→前進；既有記憶需確認顯示警告且不前進，按「仍要切換」以 `force` 切換；下載失敗（status error）顯示錯誤且不前進 |
| `components/setup/steps/__tests__/Step3ApiKeys.test.tsx` ⭐ | ADR-0118 精靈金鑰輸入（永不回填＋不吞失敗，配合 fetchTouka 不 throw）：輸入框一律留空（含已設 anthropic）、有輸入成功→`POST /v1/config/env` 後 `onNext`、套用失敗（`res.ok=false`）不前進並顯示 detail、網路錯誤不前進並記 SSOT 錯誤、無輸入直接 `onNext` 不 POST |
| `components/setup/__tests__/Step5DataPath.test.tsx` ⭐ | ADR-0118 雙欄位主機路徑（bug②）：資料目錄＋工作區根目錄兩欄位；皆空直接 `onNext` 不 POST、皆填成功→POST 帶 `data_path`＋`workspace_root`、僅填工作區則只帶 `workspace_root`、失敗（ToukaException 物件 detail）顯示 message 且不前進 |
| `src/components/settings/tabs/security/__tests__/RbacMatrix.test.tsx` ⭐ | 權限表示法 P3：原生工具／MCP／技能三軸皆有開放式 toggle；Owner `["*"] → [] → 明確清單 → ["*"]` 可逆；欄位隔離與不可變更新 |
| `src/components/dashboard/__tests__/HeartbeatWhitelist.test.tsx` ⭐ | ADR-0245：心跳白名單依後端有效能力列名單（靈魂經類型規則取得的也算）；授予／撤銷時覆寫以有效角色與能力為底，不把靈魂降級成 Base |
| `src/lib/__tests__/connections.auth-refresh.test.ts` ⭐ | ADR-0131：送出前 token 續期＋領卡防挾持——`isTokenExpired`（exp/skew/fail-safe）、過期先 `/auth/refresh` 再送、single-flight、auth 端點不遞迴、refresh 失敗保舊卡、已登入不採納 `X-New-Auth-Token`（未登入採納）、fetchChatStream ①②鏡像 |
| `src/lib/__tests__/connections.noSampling.test.ts`、`connections.apiError.test.ts` | ADR-0241：`fetchChatStream` 各分支不送 `temperature`（ollama 無 `options`）；非 200 丟帶碼的 `ToukaApiError` |
| `src/hooks/__tests__/useAgentBrainNotice.test.ts`、`src/lib/providers/__tests__/StreamRegistryProvider.brainNotice.test.tsx` | ADR-0241：`AGENT_BRAIN_NOT_SET` 跳通知、按鈕切到靈魂設定、其他錯誤照舊 |
| `src/components/settings/tabs/__tests__/AgentDataResetCard.test.tsx` | ADR-0113／ADR-0262：只列可重置的代理人；打字確認＋密碼前確認鈕停用、送出的 body 正確；打錯 ID 保持停用；`SYSTEM_BACKUP_FAILED` 時明講「快照沒做成、重置沒有執行」，其他錯誤照錯誤碼的訊息；ADR-0270 補記 +2（確認與完成畫面有「還有備份」提醒） |
| `src/components/common/__tests__/BackupRetentionNotice.test.tsx` | ADR-0270 補記 hist-bak a：兩語每句提醒都有、位置字樣（`.env`、`backups`、兩種快照檔名、`vault/core_memory_backups`）、不提 `WORKSPACE_ROOT` 與執行器；重置、清除連備份完成後、刪除、DebugAgent 各列哪些句子（DebugAgent 不提勾選、只有刪除寫「刪除前不會自動存快照」、清除完成後只講快照）。另 `AgentDataResetCard.scrub.test.tsx` V11（清除完整完成時改成只講快照的版本，沒完成時完整提醒）、`ScorecardPanel.test.tsx` +1；2026-10-27 補記 B05b：清除完成但有不在範圍的舊資料時用 `reset_scrubbed_partial` 版（照實說留在備份裡、列快照與升級備份、不叫人再勾選） |
| `src/components/layout/__tests__/SoulStudio.crud.test.tsx`、`SoulStudio.test.tsx` | ADR-0250 A1／A2；ADR-0262 +1（還有記憶時刪除被擋，toast 顯示先重置的引導、靈魂留在畫面上）；ADR-0270 補記 +2（刪除確認框與完成訊息提醒還有備份）：你指定的與目前在用的兩欄分開顯示，下拉的值是你指定的、只列一般模型；只改人格存檔送回你指定的（不是模型自己換的）；沒設時兩欄顯示翻譯過的未配置、佔位選項不能選；沒指定過時不顯示模型自己換過的說明；唯讀、編輯與建立表單都沒有取樣參數拉桿（安全設定保留，原本斷言建立表單有 Top K 拉桿的案例改寫）；建立表單的模型清單只列一般模型、預設第一個；ADR-0281 T2：唯讀頁只列勾了的工具、看不到的條目標「目前不在清單」，編輯與建立都用同一個勾選器、MCP 存 `伺服器/工具`、保留看不到的條目、沒有外掛 |
| `src/lib/__tests__/soul_tools.test.ts`、`src/components/studio/__tests__/SoulToolPicker.test.tsx`、`src/hooks/__tests__/useSoulToolCatalog.test.ts` | ADR-0281 T2：tools_stats 轉成勾選清單（MCP 的值是 `伺服器/工具`、內建名稱照舊、不列外掛與名稱含斜線的、配套對照表、缺欄位不出錯）；舊長名算勾了、取消時兩種寫法一起拿掉；看不到的條目保留、只有純名稱算對不到的舊短名；配套只跟著勾了的工具；勾選器勾 MCP 存 `伺服器/工具`、看不到的條目要自己按才拿掉、唯讀只列勾了的、配套標「隨 execute_command 一起開」且不能單獨取消、讀取中與讀不到時不標「不在清單」也不給改、空清單提醒（讀不到時也提醒）、`constructor` 這類原型名稱不崩潰；hook 換連線不給別的連線的清單、分得出讀完與讀不到 |
| `src/components/layout/__tests__/ModelStudio.test.tsx` | ADR-0250 A2（新檔；ADR-0254 H10 +1）：沒有 Temperature／Top P／Top K 拉桿與推理參數區塊；存檔照原值送回三個取樣欄位；還沒有自訂模型、或已存的沒有基礎引擎時用清單第一個一般模型，已存的照原值保留；基礎引擎下拉不列代理、human 與舊靈魂路由（和靈魂頁同一個 `isSelectableBaseModel`）；ADR-0254 H12 補記：改名回 500 不再對新 ID 存檔；改名成功、存檔失敗後重試不再送一次改名，沒存的修改還在，之後刪除刪的是新 ID；刪除失敗時模型留著，回 404 照樣拿掉；沒存過的新模型不送刪除 |
| `src/components/dashboard/__tests__/EnvSettings.test.tsx` | ADR-0254 H1（+4）：還原送出後先查 `GET /v1/system/restore/status`——失敗跳錯誤、不重整、回到備份清單；還在跑先不重整、5 秒後再查、伺服器重啟後才重整；成功重啟（`idle`）或查不到照舊重整、不跳錯誤（`it.each` 兩個）。其餘為既有的備份、排程與還原確認案例 |
| `src/components/settings/__tests__/searchIndex.test.ts` | 設定搜尋（ADR-0254 N1 +1）：每筆索引的分頁與翻譯 key 合法；本地化 label／中文與英文關鍵字命中、label 命中排在 keyword 前、多個詞要都命中；空查詢與沒有符合回空；非 admin 查不到 admin 專用設定；取樣參數條目已拿掉，搜溫度／Top P／Top K 找不到、兩語翻譯也刪掉 |
| `src/components/settings/tabs/__tests__/ModelsTab.test.tsx` | ADR-0250 A2：刪掉參數面板的案例（面板本身刪除，ADR-0111 Phase 2）；新增 `params_editable` 的原生模型也沒有參數鈕與滑桿、啟用與隱藏照常 |
| `src/lib/providers/__tests__/ModelProvider.test.tsx`、`ChatProvider.defaultModel.test.tsx` | ADR-0250 A2：`pickDefaultModel` 跳過代理、未啟用、隱藏、離線，回第一個可用的一般模型，都不可用回 undefined；ModelProvider 初值空字串、清單載入後才選、已選的不被蓋掉、沒有可用的維持空字串；ChatProvider 開新對話時選的是靈魂而只剩代理模型就不切換也不通知，有一般模型才換並提示，選的是一般模型不動 |
| `src/components/settings/tabs/security/__tests__/AutoReviewerSettings.test.tsx`、`src/hooks/settings/__tests__/useAutoReviewSettings.test.ts` ⭐ | ADR-0246 U2c-6 審查員選單：選項只來自 `/v1/autonomy/reviewer-models`、預設未設定、建議值不自動選、選擇寫回 `auto_review.model`（選「未設定」存空白）、已存模型沒金鑰時警告、清單空／載入失敗提示、格式不對不採用；說明文字兩語照核定原文且不含任何廠商名稱，也不再提無痕（U2c-9） |
| `src/lib/providers/__tests__/ChatProvider.authLevel.test.tsx`、`src/components/chat/__tests__/ToolApprovalCard.test.tsx`、`src/lib/__tests__/stream_tree.approval.test.ts`、`src/lib/__tests__/heartbeat_mode.test.ts` ⭐ | ADR-0246 U2c-6：非授權者選 Auto／YOLO 存檔後立即顯示後端實際存下的 Manual 並提示（存檔失敗或選 Manual 不重讀）；新對話還沒有 `agent_id` 時切模式只在選的是靈魂才一起帶上、選一般模型不綁、已綁靈魂的不改，先切模式再選靈魂不被拉回、沒有訊息的對話換模型不被拉回、有訊息的照舊還原；空對話換模型後送出前先重綁並顯示後端存下的模式、存檔失敗不送出（R-2c）；無痕對話模式選單鎖定顯示 Manual、不能切換並說明原因（`tests/unit/AuthSwitcher.test.tsx`；U2c-9 說明改成「無法追溯」兩語同步），無痕對話要設成 Auto／YOLO 時留在 Manual、不存檔並提示原因（U2c-9）；審批卡顯示審查理由與「只有你能核准」、事件欄位解算後保留、型別不對不顯示；心跳 safe→Auto、full→YOLO、看不懂（含 `constructor`）→Manual；另 HeartbeatScheduler、AddConnectionForm（直連標示）、SecurityTab、error_handler（1118～1120 翻譯）各補案例 |
| `src/components/chat/__tests__/ToolApprovalCard.test.tsx`、`src/lib/__tests__/stream_tree.approval.test.ts`（隔離標示）⭐ | ADR-0272：`isolation` 解析（沒帶或 null 照舊、看不懂的層級當 none、fallback 只在 none 保留、features 名稱清單或物件、解算後保留）；`landlock`／`executor` 不顯示標示；「隔離降級」中性開頭與依 `tcp`／`signal` 接的尾句（沒有 `tcp` 時不寫「核心太舊」：網路設定不安全也會沒有 `tcp`）；「未隔離」寫影響範圍；看不懂時批准停用；每個原因碼兩語都有說明（含 `net_tcp_fastopen`、`net_mptcp`，附原生 Linux 改主機 sysctl 的一句）、細節專屬說明優先、未知代碼退回「原因：{code}」；核准 none 卡送回 `isolation_ack`，其他卡與拒絕不帶 body |
| `src/components/chat/__tests__/IsolationNotice.test.tsx`、`src/components/sidebar/__tests__/IsolationBanner.test.tsx`、`SystemNoticeBar.test.tsx`、`src/lib/__tests__/SystemStatusContext.systemNotices.test.tsx`、`src/components/sidebar/left/__tests__/SidebarIdentity.systemWarning.test.tsx`、`unit/RightSidebar.isolationBanner.test.tsx`、`unit/RightSidebar.systemNotice.test.tsx`（C2-2）⭐ | ADR-0272（`stream_tree.approval.test.ts`、`unit/LeftSidebar.memoryWarning.test.tsx` 也各加了 C2-2 的案例）：卡片的主句、原因子句順序、結尾、原因行與補救（執行器降級、綁專案加網路設定不安全、讀不到連線設定、溢位、`interpreter_untrusted`），覆寫的服務依 `expected`；`stream_tree` 保留執行器去處與文案事實、型別不對的丟掉；缺漏或非物件的 MCP 計數不顯示橫幅；隔離橫幅在低於預期、埠沒擋、自癒額度用完時出現，`denied_burst` 不顯示；系統提示列與左側警示標記只看 `system_notices` |
| `src/hooks/__tests__/useChatStream.incognito.test.tsx` ⭐ | ADR-0131（L3）：無痕旗標＝清單 ∪ 誕生登記——session 被 refresh 洗掉時 `isIncognitoSession` 兜底仍送 `incognito: true`；一般 session 不誤標 |
| `src/lib/providers/__tests__/SessionListProvider.syncSession.test.tsx` ⭐ | ADR-0131（L4）：syncSession 失敗可見化——4xx `logSSOTError`＋toast（固定 id）、網路例外同、2xx 不吵、無痕不發 POST（ADR-0086 收口鎖）|
| `src/lib/providers/__tests__/SessionListProvider.refreshMerge.test.tsx` ⭐ | ADR-0131（⑤b）：refreshSessions merge——本地無痕不被整批覆蓋洗掉、updatedAt desc 排序、後端仍為 persisted 權威、同 id 以本地無痕為準、回傳值維持後端清單 |
| `src/components/settings/tabs/mcp/__tests__/McpFeatureExplorer.test.tsx` ⭐ | ADR-0203 P5：未支援 feature 不顯示；resource URI 原樣交後端 read 並渲染安全文字；prompt arguments 只送字串且 preview 固定顯示 untrusted 警告 |
| `src/hooks/settings/__tests__/useMcpOAuth.test.ts` ⭐ | ADR-0206 P8-A：exact server status、popup origin/source/shape/operation 驗證、popup blocker fail-loud、duplicate connect 取消舊 flow，且 browser storage 零殘留。 |
| `src/components/settings/tabs/mcp/__tests__/McpOAuthStatus.test.tsx` ⭐ | ADR-0206 P8-A：auth_required／connected UI、disconnect 與 popup blocker 可見錯誤。 |
| `src/components/settings/tabs/mcp/__tests__/McpOAuthSelection.test.tsx` ⭐ | ADR-0206 P8-A：只顯示後端投影的可信 issuer，回傳 exact 值。 |
| `src/hooks/settings/__tests__/useMcpWorkloadOAuth.test.ts` ⭐ | ADR-0207 P8-B：credential secret 只存在單次 PUT body、不進 hook state；owner delegation URL encode且 payload 不含 source grant identifiers；recovery 只經明示 POST retry。 |
| `src/components/settings/tabs/mcp/__tests__/McpWorkloadAdminPanel.test.tsx` ⭐ | ADR-0207 P8-B：client ID/secret 使用 password 欄位，成功或失敗都在 finally 清空。 |
| `src/components/settings/tabs/mcp/__tests__/McpOwnerDelegationPanel.test.tsx` ⭐ | ADR-0207 P8-B：owner 只提交 exact installation/tool/scopes/expiry，來源 profile/grant 由 server 推導。 |
| `src/components/settings/tabs/mcp/__tests__/McpServerForm.test.tsx` ⭐ | ADR-0207 P8-B：編輯/存檔保留 auth、workload authority、protocol、capability 與 security policy欄位，不被 transport 表單重建洗掉。 |
| `src/components/sidebar/__tests__/MemoryNoticeBar.test.tsx`、`unit/RightSidebar.memoryNotice.test.tsx` ⭐ | ADR-0253 記憶提示列：資訊級平靜顯示、不是 alert、不用 destructive 色；警告級是 alert、顏色和降級橫幅相同；沒有提示或看不懂的代碼不顯示；右側欄各分頁都顯示提示、資訊級不產生 alert |
| `src/lib/__tests__/SystemStatusContext.memoryNotices.test.tsx` ⭐ | ADR-0253：合法的 `memory_notices` 原樣帶出；沒有這個欄位當成沒有提示；不是陣列、缺代碼、空代碼、`null` 等格式不對的略過，狀態判斷不變；看不懂的等級當資訊級、不合法的 `count` 丟掉；記憶系統降級時提示照留 |
| `src/lib/__tests__/memoryNotices.test.ts`、`src/components/sidebar/left/__tests__/SidebarIdentity.memoryWarning.test.tsx`、`unit/LeftSidebar.memoryWarning.test.tsx` ⭐ | ADR-0253 左側警告標記：`hasMemoryWarning` 只在有警告級提示時為 true（只有資訊級、空陣列、undefined 都是 false）；`SidebarIdentity` 有警告時顯示帶 `aria-label` 與 tooltip 的標記，沒有時不顯示；`LeftSidebar` 依 `SystemStatusContext` 的提示決定要不要標記 |
| `src/hooks/__tests__/useLegacySync.test.ts` ⭐ | ADR-0253：後端回錯誤狀態、或 200 但 `status` 不是 success 時不顯示同步成功；只有真的成功才顯示一次成功；讀統計失敗時顯示錯誤、不默默略過 |
| `src/components/sidebar/__tests__/MemoryNoticeBar.schema.test.tsx` ⭐ | ADR-0256 記憶庫結構提示：5 個結構代碼都顯示成翻譯過的警告（`role="alert"`、和降級橫幅一樣醒目）；5 個代碼都有英文訊息 |
| `src/components/dashboard/__tests__/VaultDashboard.listError.test.tsx`、`VaultDashboard.test.tsx` ⭐ | ADR-0256 記憶寶庫列表讀取失敗（新檔）：顯示錯誤、不顯示成空的記憶寶庫，並叫側欄重抓記憶提示；權限不足照錯誤原文顯示、不叫人看側欄。既有的 `VaultDashboard.test.tsx` 只補 `useSystemStatus` 的替身 |
| `e2e/mcp-oauth.spec.ts` ⭐ | ADR-0206 P8-A Playwright：callback 當前 URL 無 state/code canary、local/session storage 與 console/message surface 無秘密、exact origin/source opaque message。 |

### 靜態守衛邏輯測試

| 檔案 | 說明 |
|------|------|
| `scripts/lib/__tests__/eventsource_guard_logic.test.ts` | 驗證 EventSource Token Query 靜態 AST 守衛的語法掃描與 noqa 排除邏輯 |
| `scripts/lib/__tests__/xagentid_guard_logic.test.ts` | 驗證 X-Agent-ID 身份主權守衛的 AST 檢索邏輯 |
| `scripts/lib/__tests__/env_guard.test.ts` | env 守衛邏輯：ADR-0250 B2 起暫存資料夾改用 `fs.mkdtempSync(os.tmpdir())`、只刪自己建的那個，新增一個確認位置在系統暫存資料夾底下；原有 4 個檢查的斷言不變 |
| `tests/guard/a11y_basics.test.ts` | 無障礙基本檢查：ADR-0250 A2 刪掉 `/create` 頁後，拿掉 layout metadata 清單與 main-content 清單裡的 create 兩項，其他路由的斷言不變 |
| `scripts/lib/theme_guard_logic.test.ts` | P11主題守衛characterization：移除`any`前後維持原違規攔截與合法CSS變數判定，不放寬守衛。 |

演化協定驗證另含 `test_evolution_protocol_over_real_isolation` 的 Linux 案例：Landlock／executor 各驗成功、工具錯誤、偽造標記、未啟動 MCP、跨檔案累計寫入配額。

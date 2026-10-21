# ADR-0245：模型溝通層第 2a 段（只留靈魂）

> ADR-0276 修訂對話綁定與 SNS 邊界：家定位／注入必驗同列 agent_id；人類 UI 換綁需兩側授權、家先取消，非人類不得換綁。SNS 使用可信執行身分與每靈魂頻道，跨目標限 adapter 唯一人類擁有者同時擁有的靈魂，群聊不寫單靈魂 session。

- 狀態：✅ 已接受（Owner 2026-10-12 核准模型層總計畫與 AGENTS 大綱）
- 日期：2026-10-12
- 決策者：Owner
- 影響範圍：聊天端點（`routers/proxy.py`）與上傳（`routers/chat_upload.py`）、`MonitorLayer`／`SoulLayer`／`CustomModelLayer`（靈魂目標授權，修補）、`authorization_gateway`（`can_drive_soul`／`require_soul_access`，修補）、自訂模型路由與 `custom_soul` 路徑（修補）、計分卡路由（修補）、前情摘要與舊串流寫回的 session 綁定（`SoulLayer`／`dispatcher`，修補）、`SkillLayer`、`models/identity.py` 的角色集合、`permission_service`（角色解析、讀到舊值略過）、`agent_service.holds_agent_role`、`identity_router` 的 X-Agent-ID 切換、`/identity` 路由、子代理（`subagent_tools`）、群聊（`group_router`、`dispatchers/group.py`、`group_settings`、SNS `@別名`）、三個 `.example` 設定與 `permission_defaults`、`endpoint_auth_allowlist`、`guard_role_plane` 鏡像、ToukaChat 身分對應／心跳白名單／群聊設定頁、AGENTS.md（身分、禁令 16、群聊）
- 修訂關係：本 ADR 修訂 [ADR-0134](0134-in-process-subagent-dispatch.md)（子代理改用 Subagent）、修訂 [ADR-0141](0141-role-plane-separation.md)（代理角色集合、X-Agent-ID 只切靈魂）、修訂 [ADR-0147](0147-soul-role-type-binding.md)（拿掉自訂模型的類型規則、覆寫只對三種 ID 生效）、修訂 [ADR-0154](0154-foreground-soul-identity-assumption.md)（修補：不擁有靈魂的非 admin 人類指定靈魂改為 403，不再只是不換身分）

## 背景

Owner 決定之後只讓靈魂有代理權限（DebugAgent、DispatchAgent 保留），群聊的一般模型與訪客移除。盤點時確認：

- **既有漏洞**：`/messages`、`/chat/completions`（含 `/v1`）在免登入清單內，沒帶憑證就以 Guest 情境進 pipeline。指定靈魂名字會套上它的人設、注入家對話，並用伺服器金鑰呼叫模型；外部無卡與「系統認領後的本機無卡」都成立。
- **Guest 被三種東西共用**：未登入的訪客、自訂模型（ADR-0147 的 `is_custom→Guest`）、in-process 子代理（ADR-0134）。
- **非靈魂取得代理權限的路徑**：管理員可在身分對應頁把任何 ID 設成任何角色；擁有者可用 X-Agent-ID 切進自訂模型；心跳白名單可把喚醒能力給任何 ID（含 ToukaGroup），而且把靈魂加進白名單時會寫一筆 `Base` 覆寫，把它從 Owner 降級；群聊可以直接點原始模型 ID 或 regex。

## 決策

1. **沒登入不能聊天**（禁令 20 安全列）
   - 兩個聊天端點改掛 ADR-0239 授權閘道的 `require_authenticated`（Depends）。沒有已驗證身分一律 401 `AUTH_INVALID_TOKEN`，在 pipeline、人設、家對話與上游呼叫之前就擋下。刪除 `_build_guest_context`；`endpoint_auth_allowlist.py` 拿掉這 4 筆（收緊）。
   - 首次安裝前的本機無卡請求照舊領卡（LDH）：帶 X-Agent-ID 時照那個 ID 發卡，沒帶時發 DebugAgent 卡；LDH 程式未改，禁令 60 的範圍不變。
   - SNS 程式內部路徑不經 HTTP 閘，行為不變；`SkillLayer` 遇到沒有身分的內部路徑不再捏造 `Guest` 名稱，沒有 `agent_role` 就沒有技能。
   - 上傳（`/chat/upload`）原本只拒 Guest，改成只放行人類、admin 代打與 Owner／Debugger；其餘代理身分（Base、Subagent、沒有角色）一律拒絕，不比原本寬。
   - SECURITY.md Known Resolved 加一列（HIGH），回歸測試 `test_unauthenticated_chat_security.py`。
2. **Subagent 取代 Guest**
   - `AGENT_ROLES` ＝ {Owner, Base, Subagent, Debugger, System}；Guest 從角色集合、`permissions.json.example`、`permission_defaults` 的 fallback 與 `guard_role_plane` 鏡像中移除（鏡像只照抄，不放寬）。
   - `Subagent` 只給 in-process 子代理（`__subagent_{hex}__`），工具清單逐項照抄原 Guest：13 項（research、read、list、find、grep、write、edit、insert、undo、delete、project_tools、vault_tools、ide_open_diff），拒絕 `update_my_soul`、`edit_soul`，不含 `dispatch_subagent`、沒有 MCP、繼承 Base。子代理的遞迴雙閘不變。
   - Owner 不再繼承 Guest；Owner 本來就是 `*`，有效答案不變。
   - `ASSIGNABLE_AGENT_ROLES` ＝ {Owner, Base, Debugger}：Subagent 與 System 只供內部，經 `POST /identity` 指派回 422。
   - `identity_map.json.example` 拿掉 `is_custom` 規則：自訂模型沒有規則，落到 Base（無工具）。
3. **只有靈魂、DebugAgent、DispatchAgent 有代理角色**
   - `permission_service` 的角色與能力解析，只對靈魂（`is_toukasoul`）與 `SYSTEM_AGENT_IDS`（DebugAgent、DispatchAgent）套用 `agent_overrides`、`tag_rules` 與 `default_role`。其他 ID（自訂模型、一般模型、群聊、任意字串）一律 Base、沒有能力；被略過、而原本會給角色或能力的設定項，每項只記一次 AUDIT 警告。
   - 靈魂判定用 `soul_service.is_toukasoul`（soul 目錄＋config 標記），不做 `ToukaSoul` 前綴糾偏，舊前綴 ID 不算靈魂。角色解析時，呼叫端帶的靈魂旗標還要用原始 ID 再確認一次：`identify_agent` 會把 `ToukaSoul<靈魂>` 糾偏後帶上旗標，所以前綴 ID 經 token 驗證、`get_identity`、`resolve_role_and_caps` 都是 Base、沒有能力（R-2a 修正）。沒帶旗標的路徑（例如 LDH）照舊不套靈魂規則。
   - X-Agent-ID 只能切進靈魂。人類（擁有者或管理員）切向自訂模型、群聊、DebugAgent、DispatchAgent 或任意 ID，一律 403 `AUTH_PERMISSION_DENIED`。管理員切進靈魂仍照 ADR-0141 §8-1 選項 A（保留 admin、不繼承軀體角色）；本機 Debugger 例外與 LDH 的程式未改。
   - `POST /identity` 的覆寫只收這三種 ID，其他 ID 回 422 `VALIDATION_ERROR`，body 帶 `rejected_agent_ids`。`GET /identity` 與 `/identity/agents` 只列這三種 ID。
   - ToukaChat：身分對應頁的角色選單只列可指派角色；心跳白名單依後端的有效能力列名單（靈魂經類型規則得到的喚醒能力也算），授予或撤銷時以有效角色與能力為底寫覆寫，不再把靈魂降級成 Base。心跳的能力閘（`can_awaken`）本身未改，非這三種 ID 拿不到 `heartbeat_awakening`，所以喚醒不了。
4. **群聊只限靈魂**
   - 群聊白名單只認 `ToukaSoul` 區；別名必須解析到 `agents_registry` 裡、而且經 `soul_service.is_toukasoul` 確認是靈魂的成員（ID 或註冊別名，不分大小寫）；白名單值不是靈魂時，不退回拿別名本身比對 registry。原始模型 ID 與 regex 不再展開，指向非靈魂的別名回錯誤訊息。
   - 只有最新一則 user 訊息的點名嚴格驗證；更早歷史裡解析不到的點名（例如已退役的別名）略過並記 debug log，曾點名過一般模型的舊群聊可以繼續（R-2a 修正）。
   - 群聊派發一律以靈魂身分執行（套人設與家對話、`is_toukasoul=True`），刪掉一般模型分支與它的 Guest 身分。SNS 的 `@別名` 走同一條路，只到靈魂。
   - `GroupWhitelist` 只剩 `ToukaSoul` 一區（`extra="forbid"`，送 `Native`／`Custom` 回 422）；`group_whitelist.json.example` 與前端群聊設定頁一併移除兩區與對應翻譯 key。
   - 1:1 直接和一般模型聊天不在本段範圍：照舊以人類身分執行，沒有工具也沒有技能。
5. **讀到 live 設定舊值時：略過＋警告，不改檔**（tracked live 設定由 Owner 手動維護）
   - `identity_map.json`：指派給 Guest 的規則一律當 Base（不往下落到其他規則，避免升級）；非這三種 ID 的覆寫與規則讀取時略過。
   - `permissions.json`：Guest 角色與 `inherits` 裡的 Guest 移除，不當 Subagent 用；檔內沒有 `Subagent` 時子代理沒有工具。live 權限設定檔已改成新版面（人類 10/12 同意），與 `.example` 相同。
   - `group_whitelist.json`：`Native`／`Custom` 等非 `ToukaSoul` 區讀到就略過。
   - `POST /identity` 存檔時，檔內原有、讀取時被略過的舊覆寫原樣保留，不替 Owner 刪設定。群聊設定頁存檔則照舊整份寫回，只寫 `ToukaSoul` 區（見剩餘風險）。

## 代價與剩餘風險

- live `identity_map.json` 仍留著舊值：非靈魂的舊覆寫畫面上看不到、但會被存檔保留，要清掉只能手動改檔。每次載入都會記 AUDIT 警告。live 權限設定檔已改成新版面（人類 10/12 同意）。
- 使用者自己的舊權限檔若還有 Guest、沒有 `Subagent`，子代理沒有工具（舊 Guest 只略過並記警告），要手動改成新版面。
- 自訂模型、一般模型不再能當代理人使用（沒有工具、不能被 X-Agent-ID 切入、不能進群聊）。管理員也不能再用 X-Agent-ID 切進 DebugAgent、DispatchAgent。
- 靈魂判定依 soul 目錄與 config 標記；資料不完整的靈魂會被當成非靈魂（Base）。
- 群聊設定頁存檔時只寫 `ToukaSoul` 區，live `group_whitelist.json` 裡的舊 `Native`／`Custom` 區會在 Owner 按下存檔那次消失（和 `POST /identity` 保留舊覆寫的做法不同；只在人按存檔時發生）。
- 舊版前端若還送 `Native`／`Custom` 會被 422 拒絕，載入新版即可。
- **原列已知殘留，已修**：本段第 1 項只擋了「未登入」；已登入的非擁有者（含經 `/auth/register` 自助註冊的帳號）仍能指定別人的靈魂（套人設、注入家對話與前情摘要、用伺服器金鑰呼叫模型），群聊 `@別名`、子代理 `sub_model` 同一根因。已由下方「修補：非擁有者不能指定別人的靈魂」一節修掉。
- 首裝前帶 X-Agent-ID＝靈魂名領到的 LDH 卡只綁 ID、效期 7 天；系統認領後這張卡會解析成該靈魂的 Owner（含兩個能力）。既有行為，另案處理。
- 首裝前 `Authorization: Basic …`、空的 `Bearer ` 等也被當成無卡而發 LDH 卡，和禁令 60「完全未附 Authorization」的字面不符。既有行為，另案處理。
- 全部以 sandbox／mock 驗證；沒有啟動 Touka、沒有讀寫真資料，live 設定實際被略過的情形要等下一段實驗才會看到。

## 驗證

- 新測試：`test_unauthenticated_chat_security.py` 8 個（修補前 6 個紅，證明沒登入能叫出靈魂人設與家對話）、`test_guest_role_retirement.py` 8 個（子代理工具與改名前逐項相同）、`test_soul_only_agent_roles.py` 17 個、`test_group_soul_only.py` 14 個、`HeartbeatWhitelist.test.tsx` 3 個（第 1 個先紅，證明靈魂被寫成 Base）。把 Guest 當低權限樣本的既有測試改指 Subagent 或 Base，不改斷言強度；改成更嚴的期望：`test_proxy_auth_flow` 的外部無卡改為 401。
- 突變證明：U2a-1 7 個、U2a-2 18 個、U2a-3 18 個（含前端 6 個）、U2a-4 11 個，改壞都會變紅、還原後無殘留。
- R-2a 修正（前綴 ID、群聊靈魂檢查、舊群聊歷史點名）：新增 6 個測試（前綴 ID 2、群聊 4），用合成靈魂 ID 的既有測試改成先宣告或建立靈魂、斷言不變；突變 6 個都會變紅。
- 全量 pytest、golden（零差異）、守衛與前端結果見 HANDOVER。本段沒有打任何 API。
- 修補（session 歸屬）：新增 `test_session_binding_security.py` 15 個（修補前 8 個紅：兩個端點、一般與 SNS 形式的 session ID，前情摘要被注入、回覆寫進對方 session 且綁定被改成 Bob）；`test_soul_layer_incognito` 讓 sess-1 綁在 Sumi，兩例只差在無痕閘，斷言不變。一般模型寫回補 4 個（修補前 2 個紅：normal 使用者帶別人的 session_id，回覆寫進對方 session），共 19 個。
- 修補（非擁有者指定靈魂）：新增 `test_soul_target_ownership_security.py` 30 個（修補前 14 個紅：HTTP 10 個回 200 且人設、家對話、上游都被觸及；子代理 1 個把別人的靈魂人設注入；群聊 3 個照樣派發）；兩個既有測試因新規則調整、不放寬：`test_monitor_authlevel_gate` 的一般使用者改為預期 403（另保留模式不被寫入的斷言），`test_auth_mode_authority_security` 點名另一個靈魂的那一例改由兩個都擁有的擁有者發出，斷言不變。再審（R-2a ① 第 2 次）的繞法：新增 `test_soul_target_bypass_security.py` 28 個（修補前 21＋3 個紅：再審第 2 次的子代理 `model=None` 兩例回 `MODEL_NOT_FOUND`、`ToukaSoul<靈魂>` 建立回 200；其餘：同名自訂模型與 `../soul/`、`..\soul\` 穿越都會套上別人的人設；建立、改名自訂模型照樣接受靈魂名稱與含 `..` 的 ID；ToukaChat 靈魂對話裡的子代理被 403、還清掉父 session 的中斷旗標；計分卡對別人的靈魂照樣開跑）；Guard #45 基準只新增子代理讀 `base_ctx` 的 `headers` 一項。

## 修補：非擁有者不能指定別人的靈魂（Owner 2026-10-12 19:04「一 甲」）

R-2a ① 指出：已登入的非擁有者（含自助註冊後立即登入的帳號）或別的代理 token，只要把 `model` 或 `X-Touka-Soul-ID` 指向別人的靈魂，MonitorLayer 認出靈魂後只是「不換身分」，SoulLayer 照樣套人設、注入家對話與前情摘要，再用伺服器金鑰呼叫模型。群聊 `@別名` 與子代理 `sub_model=<靈魂>` 是同一個根因（子代理已用測試確認：組裝時會套上別人的靈魂人設，家對話由 SoulLayer 同一段接著注入）。

新規則（授權閘道 `can_drive_soul`／`require_soul_access`，不是 Depends，由呼叫端在解析出目標後呼叫）：

- **人類呼叫者**：要擁有該靈魂（`owned_agents`）或是 admin，否則 403 `AUTH_PERMISSION_DENIED`。
- **代理呼叫者**：只能是靈魂本身或 `DebugAgent`，否則 403。admin 經 X-Agent-ID 代打（ADR-0141 §8-1 選項 A，保留 `human_role="admin"`）照舊算 admin（技術主管 10/12 裁定）；擁有者經 X-Agent-ID 切進自己的靈魂後，就是那個靈魂的代理身分，不能再指定別的靈魂。
- **兩道檢查**：MonitorLayer 解析完目標（標頭、模型名、session 連結、已驗證身分四個來源都算）之後、寫入 `is_toukasoul` 與前景代打（ADR-0154）之前先驗一次；SoulLayer 套人設、注入家對話之前，對它真的要注入的靈魂 ID 再驗一次（前景代打後 ctx 身分已是該靈魂）。中間的層改了 `agent_id` 也擋得住。擁有者或 admin 與靈魂交談的既有前景代打不變；心跳不經 pipeline，不受影響。
- **自訂模型**（再審第 1 點）：CustomModelLayer 遇到靈魂請求直接跳過，同名自訂模型不能把已授權的目標換掉。`custom_soul` 路徑必須留在 `custom/` 底下（禁令 17，`is_relative_to`）：`../soul/<靈魂>`（Windows 另有 `..\soul\<靈魂>`）不再讀到、也寫不進靈魂目錄或資料夾外。建立自訂模型與改名時，新 ID 和靈魂撞名回 409 `MODEL_ALREADY_EXISTS`（經 `agent_service.identify_agent` 正規化，`ToukaSoul<靈魂>` 也算；大小寫跟著檔案系統，Windows 不分），含 `/`、`\` 或 `..` 回 422 `VALIDATION_ERROR`；更新既有自訂模型不受影響。
- **群聊**：成員名單解析完後，每位成員都檢查；有一位不行整輪不派發（連可以的成員也不先講），以群聊既有的串流內錯誤訊息回覆（名單在串流內解析，HTTP 狀態已是 200，和其他群聊錯誤一致）。成員必須是靈魂（`resolve_soul` 的 `is_toukasoul` 檢查）已在 R-2a 修正時加上，本次只補釘出貨範本 HeartbeatTester 的測試。
- **子代理**：組裝時去掉從父請求繼承的 `x-touka-soul-id`、`x-touka-session-id`、`x-toukachat-session-id`（再審第 2 點：否則 ToukaChat 靈魂對話裡的子代理一律被認成父靈魂而 403，還會清掉父 session 的中斷旗標）。臨時身分 `__subagent_*__` 不是任何靈魂，拿靈魂當 `sub_model` 在組裝時就 403（子代理回 FAILED）；用一般模型照舊。沒指定 `sub_model`、執行器也沒注入模型時（3b-U4b-2 之前 Claude 路由不寫 ctx 的 model），改用父代理的大腦（`get_active_brain`），只取模型名、不帶父靈魂的人設與家對話（再審第 2 次）；ToukaChat 靈魂對話與心跳派生的子代理因此不再以 `__subagent_*__` 當模型名而 `MODEL_NOT_FOUND`。
- **計分卡**（再審第 3 點）：`/v1/debug/scorecard/run` 在 pipeline 裡以 DebugAgent 跑，所以路由先用真正的呼叫者驗：`model` 解析成靈魂（和 MonitorLayer 共用 `agent_service.identify_model_target`，`@名稱`、`ToukaSoul-` 前綴都算）時要 `require_soul_access`。選這個而不是直接拒絕靈魂模型：ADR-0101 沒有禁止拿靈魂跑計分卡，前端的模型建議清單也可能列出靈魂，擁有者與 admin 原本能跑自己的靈魂，照舊。
- **沒有身分的路徑**：只剩 SNS 這類程式內部直接呼叫 pipeline 的路徑（不經 HTTP 閘），照舊不檢查；HTTP 聊天端點由第 1 項的 `require_authenticated` 保證一定有身分。
- 錯誤碼沿用既有的 `AUTH_PERMISSION_DENIED`、`MODEL_ALREADY_EXISTS`、`VALIDATION_ERROR`，沒有新增錯誤碼，OpenAPI 不重新生成。

剩餘風險：

- **DebugAgent 例外＋首裝前的 LDH 卡**：首裝前本機無卡請求領到的 DebugAgent 卡效期 7 天（R-2a ① 問題 3，既有行為）。系統認領後這 7 天內，拿著這張卡的人仍可指定任何靈魂、對任何靈魂跑計分卡。
- 靈魂判定（`is_toukasoul`）本身不擋 `../soul/<靈魂>` 這類變形 ID；這種 ID 對不上任何人的 `owned_agents`，一般使用者會被 403，只有 admin 與 DebugAgent 走得過去（他們本來就能指定任何靈魂）。
- `/v1/models` 仍列出所有靈魂（不看擁有權），非擁有者在選單看得到別人的靈魂名稱，選了才會收到 403。
- SNS 等內部路徑不檢查，安全性依賴「HTTP 聊天端點一定有身分」；之後若新增不經 `require_authenticated` 的入口呼叫 pipeline，要自己帶身分。
- 已存在、和靈魂同名的舊自訂模型不會被改名或刪除；它們不再能蓋過靈魂（MonitorLayer 先認靈魂、CustomModelLayer 跳過靈魂請求），要清掉須 Owner 手動處理。
- 父代理沒有大腦設定時（例如人類身分直接跟一般 Claude 模型聊天），沒指定 `sub_model` 的子代理仍會路由失敗（既有問題，本次未擴大範圍）。
- 子代理的預設模型：主要情況已修。3b-U4b-2（ADR-0248，附註 35）讓 factory 的 Anthropic 分支也寫入 `context["model"]`，執行器注入的就是父這一輪實際用的模型，心跳 `model_override`、計分卡、admin 或 DebugAgent 驅動別的靈魂都對。剩父這一輪的 context 完全沒有模型的情形，才退回「執行中身分設定的大腦」（`get_active_brain`），那時仍可能和父實際用的模型不同。
- 原列「前情摘要與 assistant 回覆寫入不檢查 session 屬於誰」已修，見下方「修補：session 歸屬」一節。
- 自訂模型存檔失敗（`save_model` 回 False）時路由仍回 success（既有問題，本次未修）。
- 靈魂把自己當 `sub_model` 也會被拒（臨時身分不是它）；原本這樣用時請求的模型名是靈魂 ID，本來就路由不到模型。

## 修補：session 歸屬（Owner 2026-10-12 21:29「甲」）

R-2a ① 再審第 2 次問題 2：靈魂對話的前情摘要（`context_summary`）只用 session_id 查、不看 session 屬於誰；沒帶 `ui_persistence` 的舊串流路徑（`resumable_stream`）也只用 session_id 把回覆寫回。擁有 Bob 的人送「model／標頭 Bob＋別人 session 的 session_id」，就讀到對方的前情摘要，回覆還寫進對方的 session，連 session 綁的 agent 都被改成 Bob（測試實測）。SNS 的 session ID（`sns_{platform}_{channel}`）可預測。R-U2-SESSION 再審又找到兩條能**改寫綁定**的路徑（綁定一改，`session_bound` 就放行）：Owner 角色的代理對「沒有人類擁有者」的一般模型 session 被 `can_access_session` 放行（attack_a）；`connect_mcp_server`／`activate_skill` 寫 session 不檢查綁定、還用 `identity=` 改綁定（attack_b）。兩條都一併修掉。

- **規則**：SoulLayer 算出 `session_bound`，兩個條件都成立才是 True：session 綁的 agent（經 `agent_service.identify_agent` 正規化，`ToukaSoul` 前綴也算）等於這次要注入的靈魂；呼叫者能驅動那個靈魂（`can_drive_soul`；沒有身分只會是程式內部路徑）。
- **讀取**：`session_bound` 為 True 才注入前情摘要。
- **寫入**：舊串流路徑的靈魂回覆只在 `session_bound` 為 True 時寫回；一般模型對話寫回前照路由的 session 存取規則檢查（`authorization_gateway.can_access_session`，路由與 dispatcher 共用同一份）。`can_access_session` 的規則（R-U2-SESSION 修正）：admin／Debugger 全放行；人類是 session `owner_user_id` 本人放行；**非人類呼叫者（代理）只接受 session 綁的 agent 等於自己**，不走 `is_owner` 的「無擁有者」與 30 天分支；其餘人類走 `is_owner`（擁有 session 綁的 agent）。沒有權限寫回時，不以該 sid 建串流 buffer（改走一般串流）、不寫回、滑動視窗觸發也一起跳過。`_persist_assistant_turn` 的存取檢查預設開啟。
- **工具改綁定（R-U2-SESSION HIGH 2）**：`connect_mcp_server`／`activate_skill` 寫 session 前要求 session 綁的 agent（去 `ToukaSoul` 前綴後）等於執行中的 agent，否則回 `permission_denied`；upsert 不再傳 `identity=`，不把綁定改成執行中的靈魂。
- **set_home 改綁定（R-U2-SESSION 再審第 2、3 次 HIGH）**：`POST /ui/sessions/set_home` 原本只驗呼叫者能不能用那個 agent、不驗目標 session，擁有 Bob 的人能把別人的 session 設成 Bob 的家（之後每次跟 Bob 對話注入對方訊息；心跳首次喚醒把對方 session 綁到 Bob、`_session_bound` 被繞過）。第 2 次的修法誤把舊檢查換掉（改成只看綁定＋session 存取），導致沒有靈魂的使用者能把自己的 session 標成 `ToukaSoul<別人>`／`ToukaGroup` 再設家（owner_user_id 是自己，session 存取直接過）。最終規則**檢查只加不減**：（R1，復原舊檢查）呼叫者必須能用 `req.agent_id`（`can_access_session(identity, req.agent_id)`；`ToukaSoul<X>`／`ToukaGroup` 這類無人擁有的標籤在此被拒）；（R3）目標 session 存在，否則 404；（R4）綁定經 `identify_agent` 正規化後等於正規化的 `req.agent_id`；（R5）呼叫者能存取目標 session，否則 403。
- **不符合就略過、不回 403**：比照 ToolLayer 遇到 session 綁定不符時降成 Manual 的做法；聊天本身已通過靈魂目標檢查，session_id 只是附帶的上下文，略過即可保護對方資料，也不會打斷同一人換靈魂的對話。判斷不了（沒有 session、沒綁 agent、對不上）一律略過。
- **不變**：自己的一般模型對話照舊寫回；`ui_persistence` 路徑的寫回（路由已用 `_can_access_session` 驗擁有者）；無痕；子代理（沒有 session）；心跳（不經 pipeline）；SNS（請求不帶 session_id，本來就不注入前情摘要，回覆由 SNS 自己寫回）。

剩餘風險：

- 同一人在綁 A 的 session 改跟 B 聊時，B 拿不到那個 session 的前情摘要（判斷不了就保護）。
- 舊串流路徑裡沒綁 agent 的 session，不再寫回靈魂的回覆。
- ToolLayer 照 session 讀 `active_mcp_servers` 時不看綁定（只影響掛哪些 MCP、仍受角色授權，不在本次範圍）。
- MonitorLayer 清中斷旗標已移到 `require_soul_access` 之後——被 403 的靈魂請求不再清旗標（R-U2-SESSION 再審第 2 次 LOW）。殘留：一般模型對話帶別人 sid 仍會在授權前清到該 sid 的旗標（非靈魂、不經 soul-access 檢查；資料不外洩、buffer 不為未授權寫入建立、且需先知道對方 UUID／8 位隨機碼 sid）。
- 工具綁定守衛以去 `ToukaSoul` 前綴後字串比對，不走 `identify_agent` 的完整正規化（自訂模型等情形不適用，因綁定值為靈魂 id）。
- set_home 現在以正規化後的正式靈魂 id 為準，兩個邊角可接受（reviewer 判斷）：伺服器端 `agent_id` 為 NULL 的舊 session 無法設家（R4 正規化後 None≠req）；家對話裡已換過模型（綁定被改成另一個模型）時，無法再經 set_home 取消該家對話。前端正常流程送 `sess.agent_id`、與伺服器綁定一致，不受影響。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。四件事分開撤回，各自 revert 產品程式、測試與對應文件；本 ADR 改「⛔ 已撤回」（或註明撤回哪一項），ADR-0134／0141／0147 的反向註記同步改寫，CHANGELOG 加一列。

- 撤回 1＝重新開放沒登入的聊天，會把漏洞帶回來，不建議。
- 撤回 2 要一起還原 Guest 角色、`.example`、`permission_defaults`、`guard_role_plane` 鏡像與子代理角色；live 權限設定檔已改成新版面（人類 10/12 同意），要一起改回舊版面。
- 撤回 3、4 各自還原角色解析、X-Agent-ID、`/identity`、群聊與前端；live 設定裡被略過的舊值會重新生效。
- AGENTS.md 的身分、禁令 16、群聊段落要改回須經 Owner 同意。

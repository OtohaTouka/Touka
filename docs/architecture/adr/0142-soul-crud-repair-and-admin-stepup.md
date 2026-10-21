# ADR-0142: 靈魂 CRUD 修復（soul_io 受認可通道）＋admin 步升驗證的靈魂資料管理

## 狀態

✅ **已接受並實作完成（決策 2026-07-10；C1–C5 全數落地同日收官；擁有者逐批 commit）**。
本 ADR 記錄擁有者對
[soul-crud-repair-and-admin-stepup 提案](../proposals/soul-crud-repair-and-admin-stepup.md)
的核准（六拍板點 D1–D6 全照建議）與全部五批實作：C1 後端修復（soul_io 通道＋回滾）→
C2 前端修復（模型選單刷新＋錯誤對映）→ C3 功能後端（步升驗證＋DELETE）→ C4 功能前端
（編輯/刪除 UI＋密碼 modal）→ C5 文件收官。**終態**：建立/編輯/刪除靈魂全鏈路可用；
靈魂資料的人類面變更一律 admin 步升重驗密碼＋AUDIT；Soul Protection 原語零弱化
（防退化錨釘死）；孤兒資料可冪等清除、id 可重用。

## 日期

2026-07-10

## 背景

使用者回報建立新靈魂的四個症狀：soul.md 存不進（BUG1）、brain 設定存不進（BUG2）、
存檔後不能說話（BUG3）、手動補檔仍不能說話（BUG4）。同日五路並行診斷＋逐條對抗驗證
（12 CONFIRMED）鎖定根因：

- **BUG1/2/3 同源**：`create_soul`（`souls.py:53/56`）與 `update_soul`（`:172/175`）經
  `resource_manager.write_file_text/write_json` 寫入 `<app_data>/soul/<id>/`，而
  **Soul Protection 路徑監獄**（`resource_manager.is_protected_path`，對 soul/legacy 下
  一切寫刪無條件 raise `PermissionError`、無任何合法端點豁免）把系統自己的建立/編輯
  端點也擋死 → 未捕捉 500。`make_dir`（無保護檢查）先成功 → 留**空孤兒目錄**，同 id
  重試撞 `SOUL_ALREADY_EXISTS`＝「建立失敗後連清都清不掉」。
- **BUG3 前端半**：SoulStudio 建立成功後不刷新 ModelProvider → 新 `ToukaSoul<id>` 不
  出現在聊天模型選單＝仍選不到。
- **BUG4**：手動放檔須完整重現 SSOT 片段（config `is_toukasoul` 旗標／brain_settings
  精確鍵／可辨識供應商模型名／models+registry 註冊），缺任一即靜默失敗。
- **遮蔽原因**：souls 測試把 `write_*` monkeypatch 成 no-op＝真實落盤零涵蓋。

## 決策（六拍板點，擁有者核准「全照建議」）

- **D1 寫入機制＝soul_io 受認可通道**：souls 路由寫入改走 `soul_io.write_soul_file`／
  新增 `write_soul_json`（raw 原子寫＋自動 `.bak`＋`_safe_target` 監獄）。
  **Soul Protection 原語一行不動**——它照擋 agent/工具面等其他一切呼叫者；防退化錨
  測試釘死。棄 `allow_protected=` 參數案（弱化原語）。
- **D2 步升形式＝每請求內嵌密碼**（`admin_password`；`verify_password`＋AUDIT log）。
  棄 sudo token（無新 token 型別、無 TTL 狀態）。
- **D3 update_soul 契約**：人類呼叫者一律步升；agent 自我存取不變（C3 實作）。
- **D4 刪除範圍（v1）**：三目錄（soul/legacy/personal_drive）＋SSOT 註冊面
  （models.json／agents_registry／brain_settings／owned_agents／identity_map override）；
  sessions/記憶 vault 不動（既有 per-agent reset〔ADR-0113〕涵蓋）。冪等＝孤兒照清（C3）。
- **D5 DebugAgent 不可刪**（403，C3）。
- **D6 create 失敗回滾**：任何一步失敗 → best-effort 撤銷**本次新建**的目錄（既存
  personal_drive/legacy 資料不誤刪）＋冪等反註冊，同 id 可重試。

## C1 成果（後端修復，紅→綠）

- **真實落盤測試**（`tests/unit/test_soul_crud_real_writes.py`，11 條、走 conftest 沙箱）：
  初跑 7 紅（create/update 500、孤兒殘留、路徑逃逸現況 200）／4 綠錨。取代 no-op
  monkeypatch 遮罩（`test_idor_soul_sandbox_security` 拒絕面 spy 同步遷移至 soul_io）。
- **soul_io 擴充**：`write_soul_json`（dumps→同一通道）＋ `remove_soul_storage_dir`
  （回滾/日後刪除的特權清除原語：逐 base 過 `validate_no_symlink_escape`、拒 `../`/
  空 id/`.`、best-effort）。
- **souls.py**：create/update 寫入走 soul_io（`_persist_soul_files` fail-loud →
  新 `ErrorCode.SOUL_WRITE_FAILED`〔id 1407；1403 與 MODEL_ALREADY_EXISTS 撞號被
  Guard #18 攔下〕）；**D6 回滾**（逐步 `contextlib.suppress`——紅燈當場抓到「導致失敗
  的那步在 remove 側再爆會中斷其餘撤銷」的第一版缺陷）。
- **順手修掉兩個相鄰潛在 bug**（皆紅燈鎖定）：
  1. **路徑穿越**：`agent_id`/`soul_id` 含 `../` 會建到 `soul/` 之外**且繞過**只認
     soul/legacy 的保護（admin-only 面；`update_soul("..")` 實測寫到 app_data 根）
     → create/update 先於任何動作過 `validate_no_symlink_escape` 監獄。
  2. **fresh-install 註冊靜默跳過**：`if not registry: return` 對空 `{}` 直接略過＝
     全新安裝的新靈魂永遠註冊不進 registry → 改為僅 `None`（缺檔/損毀）略過。
- **防退化錨**：直呼 `resource_manager` 寫/刪 soul/legacy 路徑**仍必須** raise
  `PermissionError`（4 條錨測試；錨若紅＝有人弱化原語＝立即回退）。

## C2 成果（前端修復＋本 ADR 起草）

- **BUG3 前端半**：SoulStudio 建立成功後 `window.dispatchEvent("touka_refresh_models")`
  （ModelProvider 既有監聽）→ 新 `ToukaSoul<id>` 立即可選（vitest 紅→綠）。
- **錯誤 toast 對映修復（getErrorMessage 巢狀 detail bug）**：ToukaException 的實際
  wire 形狀是 FastAPI 再包一層——`{"detail":{"code","id","message","i18n"}}`；
  `error_handler.getErrorMessage` 只在**頂層**找 `id` → 全部後端結構化錯誤 toast 都
  變成 `JSON.stringify(detail)` 原始 JSON（使用者看到的正是這個）。修復＝巢內 id 也
  對映 SSOT/i18n＋未知碼優雅退路（巢內 `message`）；既有頂層 legacy 形狀回歸鎖保留。
  `SOUL_ALREADY_EXISTS` 現在如實顯示「靈魂已存在」而非 JSON。
- **i18n**：補 `error.soul.write_failed`（zh-TW「靈魂檔案寫入失敗」＋en），
  `already_exists`/`vault_error` 兩語系本已存在（先前只是永遠沒被走到）。

## C3 成果（功能後端：步升驗證＋DELETE，紅→綠）

- **步升 helper `_verify_admin_stepup`**（souls.py）：呼叫者須為 **is_human 的登入 admin**
  且重新提供密碼（`AuthService.verify_password`）；失敗 → 新 `AUTH_REAUTH_FAILED`
  （id 1111、403）＋AUDIT log（成功/失敗皆記操作者與對象，**不記密碼**）。只有真人有密碼可
  驗——assumed-admin 代打／agent（is_human=False）一律過不了（天然把機器擋在敏感變更外）。
- **`DELETE /v1/souls/{soul_id}`**（D2/D4/D5）：admin 步升 → 冪等清全 SSOT 面
  （`soul_io.purge_soul_storage`＝soul/legacy/personal_drive 三目錄逐 base 過監獄；
  ＋models.json／agents_registry／brain_settings／各使用者 owned_agents／identity_map
  override，皆 `contextlib.suppress` best-effort 冪等）；回 `{removed, skipped}`。**孤兒
  （空目錄／零註冊）照清、清完 id 可重用**＝直解使用者痛點。`DebugAgent` 硬擋 403（D5）。
  soul_id 路徑逃逸先於清除擋下（監獄）。
- **update_soul 掛步升（D3）**：人類呼叫者一律步升重驗密碼；**agent（is_human=False）走原本
  的角色＋擁有權閘、零改動**（自我存取/IDOR 拒絕的既有測試續綠即證）。`SoulUpdateRequest`
  加選用 `admin_password`；新 `SoulDeleteRequest`。
- **測試**（`tests/unit/test_soul_admin_stepup.py`，12 條）：刪除 happy-path＋孤兒＋全面清除
  （owned_agents/identity override）＋密碼錯/缺 403＋非 admin 403＋agent token 403＋
  DebugAgent 403＋路徑逃逸；update 人類步升＋密碼錯＋agent 自我存取回歸鎖。C1 的人類
  update 測試同步改帶密碼。
- **openapi/api.ts 重生**（新增 DELETE 端點＋兩 request 模型＋AUTH_REAUTH_FAILED）。

## C4 成果（功能前端：編輯模式＋刪除＋密碼確認 modal，紅→綠）

- **SoulStudio 詳情頁解鎖**（先前全唯讀）：新增 **Edit**（`Pencil`）與 **Delete**（`Trash2`）
  鈕；編輯模式下 persona textarea 可寫、brain 引擎改 `<select>`、temperature/topP/topK 改
  ParamSlider（草稿存 `editForm`、取消即棄）。
- **步升密碼確認 modal**（編輯儲存與刪除**共用**）：仿 `AgentDataResetCard` 的「破壞性 admin
  動作＋密碼重輸」樣式（password-only、Enter 送出、busy 防連點）；確認後帶 `admin_password`
  呼叫 `POST /v1/souls/{id}`（編輯）或 `DELETE /v1/souls/{id}`（刪除）。兩動作成功後皆
  `refreshAgents()`＋dispatch `touka_refresh_models`＋`loadSouls()`（刪除/改動即時反映到
  聊天模型選單與側欄）；刪除後清 `activeSoulId`。錯誤走 `getErrorMessage`（C2 修復後
  `AUTH_REAUTH_FAILED` 等如實顯示、非原始 JSON）。
- **i18n**：`error.auth.reauth_failed`＋15 個 `layout.soul_studio.*`（編輯/刪除/儲存/密碼
  modal/成敗 toast）兩語系同鍵；guard #33 綠。
- **測試**（`SoulStudio.crud.test.tsx`，4 條）：詳情頁露出 Edit/Delete；Delete→密碼 modal→
  `DELETE` 帶 `admin_password`；取消 modal 不發請求；Edit→persona 可寫→Save→密碼 modal→
  `POST` 帶 persona＋`admin_password`。既有 SoulStudio 6 測試續綠。

## C5 成果（文件收官）

本 ADR 定稿（狀態→✅）；ADR 索引／提案狀態同步收官；SECURITY.md 補步升面附註；
HANDOVER／memory 更新。**全提案終態驗收**：後端 pytest **3245**（C1 +11、C3 +12 淨）／
guard_all **41/41**／前端 vitest **984**（C2 +6、C4 +4）／ruff・lint_ssot 零新增／
openapi＋api.ts 與 wire 同步（C3 重生）。

## 後果

- **安全立場**：資料保護家規（機器不可自動化管理 `data/`）不變——保護原語零弱化、
  錨測試釘死；新增的寫/刪能力只存在於「admin 端點（＋C3 起新鮮密碼步升）」之後，
  agent/工具面永遠拿不到。
- **BUG4 的立場**：修好正規路徑＋（C3）提供刪除後，手動放檔不再必要；所需 SSOT 片段
  清單記於提案 §2（背景表），不另行支援。
- **驗收（C1+C2）**：後端 pytest 3233 passed／guard_all 41/41（#17 無 wire 變更、
  #18/#34 errors 同步）／ruff、lint_ssot 零新增；前端 vitest 980（+6）／eslint／tsc
  經 guard #27/#35；openapi/api.ts 無變更。

## 相關

- [提案：soul-crud-repair-and-admin-stepup](../proposals/soul-crud-repair-and-admin-stepup.md)
  （拍板點 D1–D6、批次 C1–C5、風險）；診斷（2026-07-10 五路工作流，12 CONFIRMED）
- [ADR-0113]（per-agent 破壞性重置——D4 刪除範圍與其分工）；[ADR-0141]（角色權限面
  分離——`agent_role="Owner"` 建構慣例）；SECURITY.md DA17/DA18（is_owner 閘與
  soul_io `_safe_target` 監獄的由來）

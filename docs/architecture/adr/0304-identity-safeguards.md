# ADR-0304：首裝卡在認領後失效，以及身分與防呆的三件修正

- 狀態：✅ 已實作。一頁計畫由維護者核准，實作計畫經兩輪審查。
- 日期：2026-11-07
- 決策者：維護者
- 相關：ADR-0008（優雅身分重新簽發）、ADR-0131（認證續期與 LDH 收斂）、ADR-0144（LDH 只限首裝）、ADR-0147（LDH 不帶類型旗標）、ADR-0287（破壞性操作互斥）。
- 影響範圍：`ToukaProxy/src/services/identity_service.py`、`src/services/identity_router.py`、`src/routers/auth.py`、`src/services/isolation/policy.py`、`src/services/tool_executor.py`、`src/routers/souls.py`、`src/routers/custom_models.py`、`src/routers/heartbeat.py`、`src/routers/mcp.py`、`src/main.py`。

## 背景

首次安裝前，本機沒帶 Authorization 的請求會拿到一張首裝卡（LDH），讓設定精靈能建立認證鏈。ADR-0144 讓系統認領之後不再發卡，但已經發出去的卡有效 7 天，驗卡時不看系統是否已認領。另外兩個同源的問題：

- 首裝卡發卡時刻意不帶類型旗標（ADR-0147），角色是 Base；但卡被重送時，驗卡用帶旗標的解析，`sub` 是既有靈魂 id 的卡會變成 Owner。
- 沒有 type 的卡，`sub` 剛好是使用者 id 時，驗卡會把它當成那位使用者。

這次一起修的還有三件小事：

- 檔案工具接受模型在參數裡自己填的專案編號，而且優先於對話綁定的專案；那條路只檢查專案根在工作區裡，不看專案是誰的。
- 還原備份期間仍能刪除靈魂、刪除或改名自訂模型。
- 三個端點把例外原文回給呼叫者，其中兩個不用登入。

## 決定

1. **首裝卡帶記號**：`issue_token(..., ldh=True)` 在酬載加 `src: "ldh"`（`LDH_CARD_SOURCE`），只有 LDH 發卡時用。不換簽章金鑰：同一把金鑰也用在人類 token、心跳與設定精靈。
2. **驗卡順序**（`verify_token`，通過既有的 type 檢查之後）：
   1. `sub` 是使用者：只收 `type == "access"`，否則拒絕（首裝卡沒有 type，所以也在這裡被拒）。
   2. 有首裝卡記號：系統已認領（`AuthService.any_claimed_user()`）就拒絕；否則回 `ldh_identity(sub)`，和發卡時同一個判斷（不帶類型旗標）。讀使用者清單出錯時拒絕。
   3. 其餘照舊（沒有記號、沒有 type 的一般代理人卡）。
3. **LDH 不發卡給使用者 id**：`X-Agent-ID` 是使用者 id 時不發卡，首裝卡永遠不是人類。
4. **`get_current_user` 只收 `type == "access"`**：人類的 access token 一律帶 type。
5. **檔案工具的專案只來自系統**：`isolation/policy.session_project_id(ctx, session_id)`（ctx 優先，否則 session 綁定的專案），執行器與終端、驗證指令共用；不讀模型給的參數。擁有權在對話綁定專案時檢查（`ui_sessions.save_session`），工具呼叫時不重做。
6. **還原中不能刪靈魂、不能刪除或改名自訂模型**：回 409 `SYSTEM_RESTORE_IN_PROGRESS`。
   - 刪靈魂：在身分檢查之後、驗密碼之前（和重置一樣）。
   - 改名：在破壞性操作鎖裡檢查。前面的檢查會讓出事件迴圈，還原可能在那時開始；拿到鎖之後還原端點就會拒絕。
   - 刪除自訂模型：緊接在取鎖之前，中間沒有 await。不放在 `_remove_model_dir`，因為更新失敗的回滾也走它。
7. **例外原文只進 log**：`/heartbeat/test` 回固定文字；`DEGRADED_REASON` 只留英文類別（`GET /` 會回出去，也進模型的系統提示）；`/mcp/hub/sync` 失敗回標準 500 `SYSTEM_ERROR`。

## 理由

- 用記號而不是「所有沒有 type 的卡都當首裝卡」：測試的固定裝置大量鑄沒有 type 的代理人卡，範圍會變大。
- 首裝卡的角色在發卡與驗卡用同一個函式，ADR-0147 的「不帶旗標」才不會在重送時失效。
- 專案編號由系統決定：工具說明本來就寫模型不能提供；終端與驗證指令早就這樣做。

## 後果與已知限制

- 升級前發的首裝卡沒有記號。`sub` 是使用者的會因為決定 2-1 失效；`sub` 是代理人的（例如 DebugAgent）到期前（發卡後 7 天）仍能用，不論系統何時認領。會碰到的是升級前 7 天內認領的安裝，以及升級時還沒認領、之後才認領的安裝。
- 子代理的 `session_id` 是 None，所以子代理的檔案工具拿不到專案（以前只有模型自己填編號時才拿得到；終端本來就拿不到）。
- 沒有 type 的人類 token 不再有效（現行版本不發這種 token）。
- 聊天請求本身帶的 `project_id`（`middleware/layers/context.py`）還沒檢查擁有權，記為候選（見 `docs/handover/proxy.md`）。
- 用編輯建立或轉換靈魂時沒有還原檢查，記為候選。

## 驗證

- `tests/unit/test_identity_safeguards_security.py`：首裝卡帶記號、認領後失效、認領前照常可用、重送角色不變、使用者 id 的卡不是人類、讀不到使用者清單時拒絕；檔案工具只拿到 session 的專案；還原中刪靈魂、刪除與改名自訂模型（含經 `/v1/models` 刪除、改名途中才開始還原）；三個端點不回例外原文。

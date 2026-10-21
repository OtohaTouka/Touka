# ADR-0131: 認證續期與 LDH 收斂——根除 DebugAgent 身分挾持（Auth Renewal & LDH Containment）

## 狀態

✅ **已接受（實作與驗證 2026-07-05；擁有者尚未 commit）**（採納條件收窄 by [ADR-0144](0144-auth-seam-hardening.md)：X-New-Auth-Token「僅未登入採納」另加 `!hasEverLoggedIn()`（touka_has_logged_in）閉殭屍身分縫；LDH 收斂機制本身不變）。修復擁有者回報「進入無痕對話後側欄對話紀錄被打亂/消失、模型變成 DebugAgent」的不定性缺陷簇；經源碼追蹤＋實機日誌證實根因為**認證身分無聲翻轉**（2,142 筆 `Local Graceful Downgrade: 'DebugAgent'` 實錘），無痕操作僅為放大器。新增**禁令 60**。

## 日期

2026-07-05

## 背景

**症狀**：進入無痕對話前後，左側欄對話紀錄隨機被打亂、消失，或模型選單變成 DebugAgent；行為不固定、難以重現。

**根因鏈（五環相扣）**：

1. **1 小時 access token 斷崖**：人類 access token 效期 1h（`auth_service.py` `now_sec + 3600`），而前端 `AuthContext` 只在掛載時 refresh 一次——無定期續期、無 401 重試。開頁超過 1 小時後，每個請求都帶過期卡。
2. **LDH「壞卡當無卡」**：`identity_router.get_optional_identity` 對本機信任來源的失效 token 不回 401，而是走 LDH 優雅降級：`target_id = header_agent_id or "DebugAgent"`——ToukaChat 從不送 `X-Agent-ID`，故一律降級成 **DebugAgent**，並經全域 middleware 在回應附 `X-New-Auth-Token`（**7 天**效期的 DebugAgent 卡）。
3. **前端無條件領卡**：`fetchTouka`／`fetchChatStream` 看到 `X-New-Auth-Token` 就覆蓋記憶體 token → 整個 app 被降級卡**挾持**成 DebugAgent 身分（掛載競態亦然：資料 Provider 首刷與 AuthContext refresh 賽跑，後寫者贏 → 不定性）。
4. **兩個身分宇宙翻轉**：`list_sessions` 依 `_can_access_session` 過濾——人類卡與 DebugAgent 卡看到的清單集合完全不同；`refreshSessions` 又是整批覆蓋 → 側欄整批換血（打亂/消失）。伺服器端 `_persist_assistant_turn` 以**請求當下身分** upsert（`final_agent_id = agent_id or existing`，新值蓋舊值）→ 對話被錯記到 DebugAgent 名下 → 側欄按 `agent_id` 分組顯示「DebugAgent」、`ChatProvider` 將 `setSelectedModel(sess.agent_id)` → **模型真的變成 DebugAgent**（後續發送被攔進模擬模式）。
5. **儲存失敗靜默**：`syncSession` 不檢 `res.ok`（fetch 對 4xx/5xx 不 reject）→ 授權失敗 403 被吞掉數月無人察覺（`ui_sessions.db` 長期空白）。

**無痕的角色**：無痕 session 依 [ADR-0086](0086-incognito-mode.md) 永不落盤（僅存前端記憶體），`refreshSessions` 整批覆蓋會把它洗出清單——除了 UX 反覆（切走即消失、串流完成又跳回），更嚴重的是發送時旗標從 `sessions` 陣列回查，被洗掉時查得 `undefined` → **無痕訊息以一般模式送出、後端零殘留閘門失守**（隱私缺口）。

## 決策

分三批（批1 認證鏈／批2 可見性與隱私／批3 merge 與後端收斂），全 TDD：

1. **送出前 token 續期（批1 ①）**：[`connections.ts`](../../../ToukaChat/src/lib/connections.ts) 新增 `isTokenExpired()`（解 JWT exp、30s skew、解析失敗 fail-safe 不觸發、時間走 time_utils SSOT）＋ `refreshAccessToken()`（**single-flight**、認證端點豁免防遞迴）。`fetchTouka` 與 `fetchChatStream`(proxy) 送出前檢查、過期先續期再送——**token 永不過期抵達後端 → LDH 對 UI 永不觸發**。採「送出前 exp 檢查」而非 401-retry：LDH 對過期卡本不回 401（靜默降級），401-retry 根本觸發不了；亦不受睡眠/凍結影響。
2. **領卡防挾持（批1 ②）**：`X-New-Auth-Token` **僅在未登入（記憶體無 token）時採納**；已登入一律忽略——縱深防禦，就算續期失敗也不被降級卡接管。未登入採納保留 LDH 首次建卡相容（首裝精靈等 pre-login）。
3. **授權地基（批1 ③）**：`users.json` 使用者升 admin（單人自架務實解；`owned_agents` 蓋不到裸模型 id 型 session——長期正解為 session 加 `owner_user_id`，比照 [ADR-0107](0107-user-self-service-data-clearing.md)，另案）。
4. **無痕旗標誕生登記（批2 ⑤a）**：[`ChatProvider`](../../../ToukaChat/src/lib/providers/ChatProvider.tsx) 以 ref Set 於 `handleNewSession` **誕生即登記**無痕 id，經 `isIncognitoSession` 傳入 [`useChatStream`](../../../ToukaChat/src/hooks/useChatStream.ts)；發送時旗標＝**清單 ∪ 誕生登記**——wipe 競態下旗標不再遺失、零殘留閘門不失守。hax_basics（G17）加接線鎖。
5. **儲存失敗可見化（批2 ④）**：[`SessionListProvider.syncSession`](../../../ToukaChat/src/lib/providers/SessionListProvider.tsx) 補 `res.ok` 檢查——失敗 `logSSOTError`＋`toast.error`（訊息走 errors.json SSOT／禁令 44；**固定 toast id** 防背景高頻同步轟炸）。
6. **refreshSessions merge（批3 ⑤b）**：整批覆蓋改 merge——後端清單仍為 persisted 權威（更新/刪除以後端為準），本地僅保留 incognito；`updatedAt` desc 排序；id 防禦性去重（同 id 以本地無痕為準）；回傳值維持後端清單（`handleSwitchSession` 語意不變）。
7. **LDH 收斂（第6刀，後端）**：[`identity_router.get_optional_identity`](../../../ToukaProxy/src/services/identity_router.py) 刪除「本地壞卡當無卡」分支——**帶失效 token 一律 401 `AUTH_INVALID_TOKEN`（不分本地/外部）、嚴禁降級發卡**；LDH 僅保留給「真正無卡」的本機請求（首裝精靈、開發工具首次領卡）。＝**禁令 60**。

## 後果

- **症狀根除**：token 不再過期抵達後端（1）＋就算抵達也 401 不降級（7）＋就算降級也不被採納（2）——三層互為備援；會話不再錯記 DebugAgent 名下、模型選單不再漂移、側欄不再兩宇宙翻轉。
- **行為翻轉（刻意）**：「本地壞卡→重發卡」退場——`smoke_ldh_handshake.py` 第 3 段、`test_identity_graceful_reissue.py`、`test_token_resilience.py` 斷言同步翻轉為 401＋不發卡；`test_lifecycle_atomicity.py` 改 override 正確依賴（`get_optional_identity`）注入錨定身分——生命週期原子性（Gate 1 過門後不受 token 過期影響）主張**不變**，只是不再靠 LDH 續命誤入。
- **無痕 UX**：無痕 session 不再因刷新從側欄消失/跳回；隱私旗標不依賴競態。
- **假設/邊界**：LDH 對無卡本機請求的發卡行為**保留**（含 `X-Agent-ID` 指名領卡）；`refreshAccessToken` 失敗保留舊卡（fail-loud 401，由使用者重登，不清卡以免誤觸 LDH）；merge 僅豁免 incognito（正常 session 若落盤失敗＝④ 的可見化職責，不掩蓋）。
- **驗收**：前端 vitest **971 passed/188 檔**（+27：auth-refresh 14／incognito 3／syncSession 4／refreshMerge 5／hax 接線鎖 1）；後端 pytest **2717 passed/4 skipped**（+2：robustness +1、graceful_reissue 重寫 2→3；resilience/atomicity 為翻轉修正不加數）；LDH 冒煙 4/4；guard_all **38/38**；NOQA `FETCH_SSOT` 5→6（`refreshAccessToken` 直呼 refresh 防遞迴）。
- **剩餘（另案）**：session `owner_user_id` 歸屬——**已由 [ADR-0132](0132-session-owner-attribution.md) 實作**（授權地基長期正解落地）；AuthContext 定期背景續期（現行送出前續期已覆蓋主要路徑）。

## 相關

- [ADR-0086](0086-incognito-mode.md)：無痕模式四閘門（本 ADR 補上旗標來源的競態缺口）
- [ADR-0033](0033-infosec-hardening-c1-m6.md)／[ADR-0034](0034-endpoint-auth-dependency-guard.md)：安全守衛框架（禁令 60 為其延伸）
- `SECURITY.md` L1–L4；`AGENTS.md` 禁令 60；`docs/architecture/PROXY_INTERFACE.md` 認證章節

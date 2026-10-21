# ADR-0153: 系統狀態記憶探針的認證競態——以認證卡把關＋認證就緒後重探

## 狀態

✅ 已接受（owner 回報 console `GET /v1/memory 401`；診斷為既有啟動競態，非 T 系列引入）

## 日期

2026-07-13

## 背景

Owner 回報 console 反覆 `GET http://localhost:8000/v1/memory 401`。日誌鐵證使用者**完全登入
正常**（`/v1/auth/me`、`/v1/memory/health`、`/v1/memory/list` 同一 token 皆 200），偏偏裸
`/v1/memory` 401。三個端點在 `observability.py` 的認證依賴**完全相同**（`get_verified_identity`）
——同後端邏輯卻一 401 兩 200，唯一可能是那個 `/v1/memory` 請求**送出時未帶 token**。

根因＝**啟動時序競態**：`SystemStatusContext` 於掛載時 `setTimeout(0)` 即探 `/v1/memory`
（讀 `system_degraded`／驅動 hybrid vs standalone 徽章），搶在 `AuthContext` 的 `refresh()`
還原 session token **之前**。`fetchTouka` 僅在 `_accessToken` 有值時才附 `Authorization`
（connections.ts:247），且其「401→續期重送」只在 `usedModuleToken` 為真時觸發（:295）——
故無卡的 `/v1/memory` 直接 401，被 `SystemStatusContext` 的 `catch {}` 吞掉。

**影響（輕微、非破壞）**：①console 噪音（每次頁面載入輸掉競態即一筆 401）；②`checkStatus`
只在掛載＋`touka_connections_updated` 跑，不在 auth 完成後補跑 → `mode` 卡在 `standalone`，
頁首／左側欄的「hybrid／記憶啟用」徽章整場顯示錯誤（記憶其實是活的）。無資料／功能損失。
Owner 之所以現在看到＝proxy 重啟（載 ADR-0152 新碼）致登出重登，全新頁面載入剛好輸掉競態。

## 決策

沿用既有 `touka_connections_updated`（window 事件觸發重健檢）的同一慣例，**事件驅動、不耦合
provider 巢狀**：

1. **記憶探針以認證卡把關**：`SystemStatusContext.checkStatus` 僅在 `getAccessToken()` 為真
   時才探 `/v1/memory`——auth 還原前無卡即**不送**，根除 401（`/v1/health` 公開端點照探、
   `isConnected` 不受影響）。
2. **認證就緒後補探**：`AuthContext` 於 `isAuthenticated` 變動（登入／開機還原成功／登出／
   session 判死）後 `dispatchEvent('touka_auth_changed')`；`SystemStatusContext` 監聽並重跑
   `checkStatus`——此時已持卡 → `/v1/memory` 200 → `mode` 正確為 `hybrid`。

## 實作

- `SystemStatusContext.tsx`：`import { getAccessToken }`；`/v1/memory` 探針包 `if
  (getAccessToken()) { … }`；掛載 effect 加 `touka_auth_changed` 監聽（與 connections 事件
  同一 `handleUpdate`）＋卸載反註冊。
- `AuthContext.tsx`：單一 `useEffect([isAuthenticated])` 於變動時 `dispatchEvent(new
  Event('touka_auth_changed'))`（`authSettledRef` 跳過掛載初值；`typeof window` 守 SSR）。
  一處涵蓋所有轉換——時序上 `_accessToken` 於各 `setIsAuthenticated` 前已由 `storeToken`／
  `clearAccessToken` 更新，故事件觸發的重探必讀到正確卡況。

## 後果

- **正面**：console 不再有 auth 還原前的 `/v1/memory` 401；hybrid／standalone 徽章在登入／
  登出後即時正確；登出後 `mode` 亦正確回落 standalone（無卡不可讀記憶）。
- **相容**：`fetchTouka`／後端／認證流全不動；純前端狀態探針的時序修正。既有 `/v1/memory`
  P5b 降級讀取行為在「已認證」情境完全不變。
- **限制（誠實界定）**：登入頁等未認證情境刻意不顯示記憶狀態（無卡不探）＝設計取捨；`mode`
  以「本使用者能否讀取記憶」為語意，非「伺服端記憶是否載入」。

## 後續（同族修復：左側欄會話清單自癒，2026-07-13）

同一 `touka_auth_changed` 自癒範式在落地時**只補了 `SystemStatusContext`，漏補其兄弟
`SessionListProvider`**。症狀：與某靈魂（Sumi）的家對話從左側欄消失。

- **根因**：`SessionListProvider` 掛載時 `refreshSessions()` 只探一次；`GET /v1/ui/sessions`
  若 `res.ok=false`／throw，`refreshSessions` **靜默回 `[]` 不動 `sessions`**（SessionListProvider.tsx:46-49），
  且**無 `touka_auth_changed` 重探監聽** → 空清單在該頁生命週期內不自癒，直到手動重整。
- **實機觸發**：admin（`75ec02b2`）於管理頁編輯使用者 → 觸發自身 token 撤銷
  （`system_status.log` 07-13 06:59:46／07:01:03「Tokens revoked」＋隨後 `LDH refused: cardless`
  ＋兩檔合計 7× `401`）→ 之後的 `/v1/ui/sessions` 吃 401 → 空側欄。**資料完好**
  （`ui_sessions.db` 該 row `agent_id='Sumi'`、`is_home=1`、6969B 訊息樹完整），純顯示層。
- **後端已排除為肇因**（對抗式覆核）：`/v1/ui/sessions` 查詢無過濾，`_can_access_session`
  對 admin/Debugger **無條件先放行**（ui_sessions.py:85，早於 `owner_user_id` 檢查）→ 兩個
  admin 帳號皆可見該 row → **非 owner_user_id 過濾、非帳號切換**所致。
- **修法**：`SessionListProvider` 掛載 effect 增設 `touka_auth_changed` 監聽 → 事件觸發即
  `refreshSessions()`；卸載反註冊。與 `SystemStatusContext` 同構。認證就緒（登入／開機還原）
  後列表自動補回。測試 `SessionListProvider.authSelfHeal.test.tsx`（掛載首探 401→事件→補回；
  卸載反註冊）。
- **未納入（另案，owner 排序）**：token 為何於使用者編輯後反覆撤銷（餘波追查）；前景對話身分
  裂腦（人類 UUID 洩為 agent 身分、`data/custom/{human_uuid}/drive` 幻影目錄，ADR-0147 撞名於
  runtime 現形）。

## 相關

- [ADR-0144](0144-auth-seam-hardening.md)（認證縫：session-expired handler／`_accessToken`
  記憶體卡／LDH 首裝限定——本案的 401 正因 LDH 對已認領使用者不發卡而顯露）
- [ADR-0147](0147-soul-role-type-binding.md)（靈魂→角色類型綁定；其警告的 user.id↔agent_id
  撞名於本次前景對話身分裂腦中於 runtime 現形，列為另案）
- 診斷：proxy 日誌三端點同 token 一 401 兩 200 → 定位為「探針早於 token 還原」的競態

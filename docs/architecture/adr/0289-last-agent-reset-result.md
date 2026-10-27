# ADR-0289：舊審查報告丙組：「結果不明」時查得到最後一次重置的結果

- 狀態：✅ 已實作（丙-5）。維護者 2026-10-25 核准修正計畫（「照建議：1～5 都選甲」，第 4 題選甲：只記在記憶體、不做輪詢）。
- 日期：2026-10-28
- 決策者：維護者
- 修訂 ADR-0270（「替代方案」裡「背景工作加輪詢：多一套狀態與端點」：仍不做背景工作與輪詢，但加一個查最後結果的狀態與端點，見下方）。
- 相關：ADR-0113（每靈魂重置）、ADR-0231（結果不明不寫失敗）、ADR-0287（破壞性操作鎖、清除旗標）、ADR-0239（授權閘道）、ADR-0207（Proxy 單一行程）、禁令 23、25。
- 影響範圍：ToukaProxy 新 `services/agent_reset/last_result.py`；`services/agent_reset/orchestrator.py`、`services/memory_scrub/orchestrator.py`、`routers/admin.py`、`models/agent_reset.py`；`openapi.json`；ToukaChat 新 `components/settings/tabs/AgentResetLastResult.tsx`，`AgentDataResetCard.tsx`、`hooks/useAgentDataReset.ts`、`lib/types.ts`、`types/api.ts`（產生檔）、i18n；tests 新 `test_agent_reset_last_result.py`、`AgentDataResetCard.last.test.tsx`，以及 `test_memory_scrub_orchestrator.py`、`AgentDataResetCard.test.tsx`、`AgentDataResetCard.scrub.test.tsx`；PROXY_INTERFACE、CHAT_INTERFACE、SSOT_MASTER_MAP、tests/README。

## 背景

10/15 的程式審查（Touka #89～#105）丙組第 4 題，在 dev 上核對後仍有效的：

- 重置（尤其勾選清除連備份）可能要幾分鐘。請求斷線、逾時或閘道回錯誤頁時，畫面只能顯示「結果不明」（ADR-0231），要使用者「稍後重新整理查看」。
- 但伺服器沒有任何地方能查這次的結果：重新整理只看得到代理人清單，看不到清了什麼、清除有沒有完成。請求取消後清除仍在工作執行緒裡跑完（ADR-0270），結果只寫進稽核日誌。
- ADR-0270 當時不做「背景工作加輪詢」，理由是多一套狀態與端點。這次只加「查最後一次」的一個狀態與一個端點，重置照舊在請求裡完成。

## 決定

1. **只記最後一次，只在記憶體**：新 `services/agent_reset/last_result.py` 留一份 `LastResetStatus`。
   - 和還原狀態（`routers/system.py` 的 `_last_restore`）同一種做法：每次整筆換掉參照，清除工作在工作執行緒裡寫也不用鎖。
   - 伺服器重啟後回 `idle`（ADR-0207 單一行程）。不寫檔、不進資料庫。
   - 失敗只記錯誤碼，不放路徑或例外原文（禁令 23）；不是 Touka 錯誤的一律 `SYSTEM_ERROR`。
2. **五種狀態**：
   - `idle`：本次啟動後沒有重置紀錄。
   - `running`：拿到破壞性操作鎖之後記。請求斷線、清除還在跑時也是 `running`，直到清除工作記下結果。
   - `done`：附完整報告，和重置端點回的同一份（含 `scrub`）。
   - `failed`：附錯誤碼（例如快照沒做成的 `SYSTEM_BACKUP_FAILED`、預檢的 `MEMORY_SCRUB_UNAVAILABLE`）。
   - `interrupted`：請求中途取消、還沒交給清除工作，可能只清了一部分。
   - 計畫只寫了 idle／running／done；沒有 failed 與 interrupted 時，出錯或取消後會一直顯示 running，所以補上。
3. **誰在什麼時候記**：
   - 重置協調器 `_execute_reset` 在 `destructive_op` 裡：拿到鎖就記 `running`；正常結束、還持有鎖時記 `done`。
   - 出錯或被取消：`last_result.recording` 記 `failed`／`interrupted`，前提是還沒交給清除工作。
   - 交給清除工作之後，由清除工作記：`run_after_reset` 多一個 `on_report`，在 `run_memory_scrub` 的 `finally` 寫完稽核之後呼叫（和 `audit_result` 同一處），清除的安全網路徑也呼叫。協調器的回呼把清除結果放進重置報告，再記 `done`。
   - 所有寫入都在破壞性操作鎖或清除旗標放開之前，所以下一次重置開始時（它要等這兩個都放開），上一次晚到的結果蓋不掉它。
   - 被 409 擋下的請求、還沒拿到鎖就被拒絕的（不能重置的目標、清除沒確認等），都不算一次重置，不改紀錄。
   - DebugAgent 的重置也走同一個協調器，一樣會記。
   - 報告不合回應格式時（端點回應同樣會失敗）記成 `failed`／`SYSTEM_ERROR`，不讓記錄弄壞重置。
4. **端點**：`GET /v1/admin/agents/reset/last`（根路徑對等），回 `LastResetStatus`。
   - 閘道是 `Depends(require_admin_human)`：只給人類 admin，admin 代打不算，和清除連備份同一道閘（報告裡有清除結果）。其他人回 403 `AUTH_ADMIN_REQUIRED`。
   - 唯讀，不改任何資料。
5. **畫面**：
   - 重置卡掛載時查一次。有紀錄時在卡上方寫明是哪個靈魂、什麼時候（還在跑寫開始時間，其餘寫結束時間，台北時間）和結果一句話；結束的那次可以按「查看結果」打開報告。
   - 「結果不明」畫面加「查看結果」按鈕：同一個靈魂已結束就換成那份報告，上面寫明「以下是某某在幾點結束的那次重置」；還在跑、沒有完成、中途中斷就照實說；伺服器沒有紀錄（重啟過）或是別的靈魂的，就說查不到這次的結果。
   - 「結果不明」的文字改成「請稍後按『查看結果』」。
   - 重置完成按「完成」後也重新查一次。
   - 不輪詢：要使用者自己按（第 4 題甲案）。

## 後果

- 「結果不明」之後不用重新整理、也不用看伺服器日誌，就能看到清了什麼。
- 已知限制：
  - 伺服器重啟後就查不到（甲案）。畫面會說「伺服器可能重新啟動過」。
  - 只記最後一次。請求根本沒送到伺服器時，查到的是更早的那次；畫面寫了靈魂與時間，由使用者判斷是不是這次。
  - 清除工作在 `run_memory_scrub` 的 `finally` 裡、寫結果之前出錯（例如寫稽核失敗），又剛好請求已經斷線時，紀錄會停在 `running` 直到伺服器重啟。
  - 系統還原會重啟伺服器，紀錄跟著消失。
- 撤回：revert 即恢復原樣；不改任何資料。本 ADR 檔保留，在狀態註記撤回。

## 驗證

- 先寫的測試在實作前全紅（新模組不存在）：
  - 後端 `test_agent_reset_last_result.py`：結束後記住同一份報告與時間、進行中是 running、出錯只留錯誤碼（不放例外原文）、取消是 interrupted、被 409 與被守衛擋下的不改紀錄、報告不合格式不弄壞重置、放鎖時已經記好、端點只給人類 admin。
  - `test_memory_scrub_orchestrator.py` o23～o26：請求取消後清除跑完才記 done（期間是 running）、清旗標之前就記好、正常清除記的報告和回應相同、交出前取消是 interrupted、安全網那份報告就是記下的那份。
  - Chat `AgentDataResetCard.last.test.tsx` V12～V19：掛載查一次並寫明靈魂與時間、idle 或查詢失敗不顯示、「結果不明」按查看結果換成報告且不跳通知、running／failed／interrupted 照實說、重啟過或別的靈魂說查不到、卡上的按鈕打開報告、完成後重新查。
- 突變檢查：後端與前端各個突變都讓測試變紅（數字見 PR）。

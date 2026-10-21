# 提案:三 Handler 漂移修復批(Handler 抽取重構的前置步驟 0)

## 狀態

🏁 **已收官（2026-07-13）**——owner 核准（拍板 P1=B/P2=含檔案讀取面/P3=納入/P4=照案）＋工具名對齊校驗後，C1–C6 全數落地：[ADR-0155](../adr/0155-handler-drift-fix-batch.md)。終態驗收 pytest **3469**／guard_all **41/41**／lint_ssot clean／ruff 基線持平／NOQA 零新增。

## 日期

2026-07-13(源於同日三 handler 解剖審計:openai 410 行/claude 507/gemini 498,約 60% 為同一狀態機三份拷貝、12+ 處逐位元相同;本批只修「拷貝間已漂移的 bug」,**不做抽取重構**——重構另案,修完本批才有乾淨基線)

## 背景與原則

同一關切點(防幻覺/記憶訊號/計時/快取)在三個 handler 各有一份拷貝,**每次修復只落在其中一份**,已累積六個經逐行覆核確認的漂移 bug。本批的三大原則:

- **TDD**:每個修復先落紅測(紅測本身即 bug 的再現證明)→ 修 → 綠;每個 commit 收在 `guard_all` 41/41＋pytest 全綠。
- **SOLID(邏輯下沉/SRP)**:判定邏輯統一下沉 `guard_service`(它本來就是防幻覺 SSOT,只是三 handler 沒有全部接上);handler 只供訊號、不重複實作。
- **SSOT**:「已知工具全集」「記憶使用訊號」「計時語意」各收斂為單一來源;**修復＝恢復禁令 59(c) 合規**(幻覺偵測器的已知工具集**必須涵蓋 MCP 工具**——現況 openai/gemini 兩路違反)。

**刻意不做**:任何 wire/schema 變更(openapi 零變動)、任何 handler 結構重排(抽取重構的範圍)、前端變更(純後端批)。

---

## 修復清單(D1–D6 核心＋D7/D8 選配)

### D1【高・功能性】Gemini 迴圈中途 re-cache 弄丟 system prompt＋工具宣告

- **症狀**:Gemini 長對話(contents>10)觸發迴圈中途重快取後,該輪起模型**沒有 system prompt、沒有工具宣告**(人格消失＋不會用工具)。
- **根因**(已實錘):初始快取設定時 `gemini_handler.py:152-157` 把 `system_instruction`/`tools` **從 `gemini_req` 刪除**;中途 re-cache `gemini_handler.py:371-372` 卻從同一個 `gemini_req.get(...)` 讀取 → 恆為 `None`;`gemini_cache_service.py:117-121` 只在 truthy 才寫入 payload → 新快取只含歷史;`:377` 再把 contents 塌縮成最後一則 → 全損。
- **紅測**:tests/proxy 既有 gemini handler mock 範式(假 httpx＋假 cache service)驅動 >10 contents 觸發 re-cache,**斷言第二次 `get_or_create_cache` 收到非 None 的 `system_instruction` 與 `tools`**(現況 None=紅)。
- **修法**:初始刪除前先把兩值存進 handler 區域變數(`preserved_system_instruction`/`preserved_tools`),re-cache 呼叫改傳保存值。單一保存點=快取輸入的 SSOT;不改 cache service 本體。

### D2【高・禁令 59(c) 合規】已知工具全集 SSOT 下沉(MCP 幻覺盲區)

- **症狀**:模型在 openai/gemini 路徑幻覺一個 **MCP 工具**的模擬日誌 → 偵測不到(假陰性);claude 路徑抓得到。
- **根因**:MCP 修正(`collect_known_tool_names`,含 `protocol_manager.get_all_tools()`)只落在 `claude_handler.py:32-59` 的**私有副本**;`openai_handler.py:341-345`/`gemini_handler.py:424-428` 仍只用三個靜態 registry。
- **紅測**:`guard_service` 層新測——`collect_known_tool_names(...)` 含 MCP 工具名(對 guard_service 現況=函式不存在=紅);handler 層特徵測——openai/gemini 路徑幻覺 MCP 工具名時產生警告。
- **修法**:`collect_known_tool_names` **下沉至 `guard_service`**(async,簽名照 claude 版,含 MCP 列舉失敗退回靜態集的防禦);三 handler 一律呼叫 `guard_service.collect_known_tool_names(...)`;claude 私有副本刪除。頂層匯入(禁令 33)。

### D3【中】guard_service 記憶/誠實檢查掛在已不存在的工具名上(死碼)＋警告文案教幻覺名

- **症狀**:openai/gemini 的記憶幻覺檢查**永遠不會被抑制**(該抑制時誤報)、誠實度檢查**永遠不知道模型有查證**;且 `guard_service.py:92` 警告文案叫模型去用 `query_memory`/`access_personal_drive`——**這些名字已非可呼叫的工具**(`save_memory`/`query_memory` 僅存為 ToukaMemory 內部函式,對映 `memory_tools` 的 action;`access_personal_drive` 全不存在)=防幻覺系統自己在教幻覺名。
- **根因**(已實錘):記憶工具大一統後對外只剩聚合工具 `memory_tools`(action enum=**save/query/forget/random/diagnostics**,`ToukaMemory/.../tools/schemas.py`),但 `guard_service.py:57` 閘 `"save_memory"`、`:69-70` 閘 `"query_memory"/"access_personal_drive"`;`tool_executor.py:266-267` 只把聚合名加進 `actually_used_tools` → 三個舊名永遠不在集合。
- **紅測**:①`actually_used_tools={"memory_tools.save"}`＋回應承諾記錄 → `check_memory_hallucination` 應回 False(現況 True=誤報=紅);②`{"memory_tools.query"}` 或 `{"read"}`(裸動詞)＋提及過去記憶 → `check_fact_without_tools` 應回 False(現況 True=紅);③警告文案不含 `query_memory`/`access_personal_drive` 字樣。
- **修法**(訊號生產單點＝SSOT;**工具名已對照 ADR-0119 重設計後現況校驗**,見文末「工具名對齊校驗」):
  - `tool_executor` 加入名字時,**若工具參數帶 `action`,同時加入限定名 `f"{name}.{action}"`**(如 `memory_tools.save`、`vault_tools.read`)——單一生產點(:266-267),三 handler 零改動即獲得訊號;**九裸動詞無 action 參數,依工具名本身計**(注意:裸動詞內部委派 `vault_tools` 是 Python 函式呼叫、不經 tool_executor,**不會**產生 `vault_tools.*` 訊號,故查證集必須顯式列裸動詞)。
  - `guard_service` 判定改讀:記憶承諾閘=`memory_tools.save`;查證閘=`memory_tools.query` ＋ **裸動詞 read-family `read`/`list`/`find`/`grep`**(重設計後的主廣告面,scope 含 personal/legacy/soul)＋ shim 相容 `vault_tools.read/list/find/grep`(——**拍板點 P2**)。
  - `:92` 文案改引用實存工具名(`memory_tools`(action=query) 與 `read`/`grep` 等檔案動詞)。

### D4【中】e_index 記憶訊號在 openai/gemini 恆為 0

- **症狀**:湧現指標的檢索分量在 2/3 供應商永遠是 0(string-sniff `"query_memory" in logs`——名字不存在、成功路徑 log 也不含工具名);claude 用結構化旗標=正常。
- **紅測**:openai/gemini 路徑上執行 `memory_tools(action=query)` 後,`calculate_emergence_index` 收到的檢索訊號 >0(現況 0=紅)。
- **修法**:三 handler 的檢索訊號統一改讀 **D3 的限定名 SSOT**(`"memory_tools.query" in actually_used_tools`);claude 的 block-walking 旗標收集(claude:183-184/263-269)一併退役改讀同一訊號——**三家同一來源,漂移根絕**。
- **誠實行為變化**:openai/gemini 的 e_index 將**上升**(檢索分量活了)。

### D5【中】`pure_gen_ms` 計時語意三家相反(遙測不可比)

- **症狀**:同一遙測欄位,openai/gemini(`if not final_response: t2=now` 每輪重置,openai:197-198)量到**近 0**;claude(t2 於迴圈前,claude:185)量到**整圈含工具執行**——兩種都不是「純生成」。e_index 消費 gen_time → 跨供應商不可比。
- **紅測**:以 `time_utils` 假時鐘(可 mock,時間 SSOT 的紅利)驅動兩輪工具循環:上游耗時 Y1+Y2、工具耗時 X → 斷言 `pure_gen_ms ≈ Y1+Y2`(不含 X)。
- **修法**(**拍板點 P1**,推薦 B):
  - **選項 A**:統一 claude 語意(整圈含工具)——最小改,但欄位名說謊。
  - **選項 B(推薦)**:`pure_gen_ms` = **各輪上游呼叫耗時累加**(每輪 `t_send→t_recv` 差值累計,不含工具執行)——字面誠實、三家可比、實作為每 handler 兩行累加。
  - **選項 C**:首輪回應延遲(TTFR)——語意改變最大,不推薦。
- **誠實行為變化**:數值大變(openai/gemini 近 0 → 真值);審計提醒**需確認無任何 UI/審計邏輯以舊值設閾**(實作時全庫 grep `pure_gen_ms` 消費點並記錄於 ADR)。

### D6【中】防幻覺偵測器分叉(claude inline 舊版:誤殺修復未跟上＋潛在 re.error)

- **症狀**:claude 路徑仍會**跨段落誤殺**(guard_service 已改同行匹配＋`re.escape`,claude:397-430 的 inline 副本沒跟上);且 inline 版不 escape 工具名——MCP 工具名含 regex 特殊字元時直接 `re.error`。
- **紅測**:①工具名含 `[`/`.` 等字元時 claude 路徑不炸(現況 re.error=紅);②guard_service 已修的跨段落誤殺案例在 claude 路徑同樣不誤殺。
- **修法**:claude 改呼叫 `guard_service.detect_tool_hallucinations`/`get_hallucination_warning_message`(D2 統一已知集後水到渠成);inline 實作與私有警告字串刪除。
- **誠實行為變化**:claude 路徑警告文案改為 guard_service 版本(字面略異)。

### D7【低・選配】Gemini 工具呼叫遙測失去關聯 id

`gemini:328` 傳 `call_id=None`(Gemini API 無 id)→ ToolCallEvent(ADR-0122)只有 Gemini 路徑無法逐呼叫關聯。修法:以輪次+序號合成穩定 id(`g{round}-{i}`)。紅測:斷言 Gemini 路徑 ToolCallEvent 帶非空自合成 id。——**拍板點 P3a:是否納入本批。**

### D8【低・選配】`is_toukasoul` context 預設三家不一致(latent)

openai:73/gemini:61 預設 `True`(fail-open)、claude:85 預設 `False`(fail-closed)。dispatcher 現況恆有設值=零實際影響,純防禦統一為 `False`(fail-closed)。紅測:不帶旗標的 ctx → 三 handler `allow_react` 判定一致。——**拍板點 P3b:是否納入本批。**

---

## 拍板點(owner 決策)

| # | 問題 | 建議 |
|---|---|---|
| P1 | D5 計時語意:A 整圈/B 累加純上游/C TTFR | **B**(欄位名誠實、三家可比) |
| P2 | D3 誠實閘的「查證」認定範圍:僅 `memory_tools.query`,或＋檔案讀取面(裸動詞 `read/list/find/grep`＋shim `vault_tools` 讀側) | **含檔案讀取面**(查檔案=查證;裸動詞為主、shim 為輔) |
| P3 | D7/D8 選配是否納入 | **納入**(皆小、順手根絕) |
| P4 | commit 切分照下方 C1–C6 | 照案 |

## Commit 切分(每個皆:紅測 → 修 → guard_all 41/41＋pytest 全綠)

```
C1  防幻覺 SSOT 統一(D2＋D6):collect_known_tool_names 下沉 guard_service、
    三 handler 接上、claude inline 副本刪除
C2  記憶/誠實訊號結構化(D3＋D4):tool_executor 限定名單點生產、guard_service
    閘改讀、警告文案修正、e_index 訊號三家統一(claude 旗標收集退役)
C3  Gemini re-cache 保存 system/tools(D1)
C4  計時語意統一(D5,按 P1 拍板)
C5  選配小項(D7＋D8,按 P3 拍板)
C6  文件收官:ADR(取下一號、README 索引)、HANDOVER 現況、tests/README 計數、
    NOQA registry(若有新增,禁令 12)
```

順序理由:C1/C2 是純 guard 層(風險最低、合規恢復最急);C3 獨立功能修;C4 動遙測值(放後、變化可獨立歸因);每個 commit 可獨立回退。

## 全域驗收

- `guard_all` **41/41**(尤其 #38 工具事件封套、#28 ADR 完整性)、後端 pytest 全綠(基線 3440＋新增紅轉綠測試)、ruff 零新增。
- **openapi 零變動**(不動任何 wire schema)、vitest 不動(純後端)。
- 行為變化誠實清單(上列 D2/D4/D5/D6 各條)寫入 ADR「後果」節,實作時逐條實測記錄。

## 風險與緩解

1. **e_index/pure_gen_ms 數值變動**:若任何 UI/審計以舊值設閾會受影響 → 實作時全庫 grep 消費點,發現閾值即停手回報。
2. **claude 警告文案變動**:若有測試釘死舊文案 → 測試隨修(文案 SSOT 歸 guard_service)。
3. **修復與重構糾纏**:本批嚴禁順手抽取共用迴圈——那是下一提案;本批的每個 diff 都必須小到能單獨 review。
4. **Gemini re-cache 修後行為**:長對話快取內容變大(包含 system/tools)→ 快取成本略升,換正確性;TTL/淘汰邏輯不動。

## 與後續抽取重構的關係

本批收官後,三 handler 對齊到「同一行為的三份拷貝」= 抽取重構(單一 ReAct 核心＋Provider Protocol,審計 synthesis 已給七至九個 session 的絞殺者計畫)獲得**行為保持的乾淨基線**;屆時特徵化測試不會把本批的 bug 一起凍結成「契約」。

## 工具名對齊校驗(2026-07-13,對照 ADR-0119 原生工具重設計後現況)

| 計畫引用 | 現況核實 | 判定 |
|---|---|---|
| `memory_tools` action=`save`/`query` | `ToukaMemory/.../tools/schemas.py` action enum=save/query/forget/random/diagnostics;dispatch `tools/__init__.py:89-97` | ✅ 正確 |
| `save_memory`/`query_memory` | 僅存為 ToukaMemory **內部函式**(對映 action save/query),非工具名;`MEMORY_INTERFACE.md:59` 的 `save_memory()` 指內部函式=無誤 | ✅ 只有 guard_service 把它們當工具名比對是錯的(=本批 D3) |
| `access_personal_drive` | 全系統不存在(僅殘於 guard_service 判定與文案) | ✅ D3 一併清除 |
| 檔案查閱工具 | 主廣告面=九裸動詞(`src/tools/file_verbs.py`,read-family scope=project/personal/legacy/soul,**內部委派 vault_tools 但 tool_executor 只記裸動詞名**);`vault_tools` 為聚合 shim 仍在架上(builtin/__init__.py:100) | ⚠️ 初版計畫只列 `vault_tools.*`,已修正為「裸動詞為主＋shim 為輔」(見 D3/P2) |

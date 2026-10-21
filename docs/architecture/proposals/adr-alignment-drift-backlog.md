# ADR 對齊漂移修訂 Backlog（Phase 2）

> **狀態**：🏁🏁 文件修訂全收官（R1–R4 全數落地，2026-07-22）——64 條受害者 ADR 三處視型態補反向註記＋ADR-0173 機器執法納閘（Guard #28 第 4 檢查、活體 0 違規）。純文件零行為碼、未 commit。**僅餘候選真實缺口 G-1/G-2/G-3＝碼/行為層，超出本 backlog（補註記）範疇，待 owner 另評修碼。**
> **前置**：ADR 對齊回顧線 Phase 1（0001–0168，除已退役 0048/0063/0072＝165 篇 100% 掃完）
> **總帳（逐塊逐條 file:line 證據）**：scratchpad `adr_align_DRIFTS.md`（session 內），本文件為其可執行摘要。

---

## 方法與結果

- **方法**：每 Accepted ADR 派 analyze 比對現行碼分類 `aligned/partial/stale/superseded_unmarked`；`high/med` 漂移派獨立 verify 代理對抗覆核（盡力推翻）。workflow `wf_adr_align_chunk.js`（analyze=sonnet / verify=opus 已校準）。
- **結果**：**91 aligned（55%）/ 70 partial / 0 stale / 4 superseded_unmarked**；**82 條確認漂移（42 MED＋40 low）** ＋ ~49 條 low 面值（未覆核）；**7 條推翻**（假陽性，見附錄 A，不需修訂）。
- **健康度佳**：`0 stale`＝無任何「核心決策整個錯、卻沒人知道」的案例。漂移全集中在 `partial` 的**點狀過時**，非系統性腐化。

---

## 橫貫發現（本審查的核心結論）

**「修訂追蹤是單向的」**：後續重構 ADR 幾乎都有**前向連結**（0117→0016、0143→0089、0156→0127…），但**被改的舊 ADR 未回頭加「已被 X 修訂/取代」狀態註記**，仍標 `✅ 已接受`。直接打開舊 ADR 的讀者會誤解現行系統。

**關鍵佐證：所有超級 hub 本尊都是乾淨的（aligned）**——0141/0143/0146/0156 自己前向連結齊全、連兩塊 hub 集中區 0 MED。**⇒ 治本只需「逐受害者補反向註記」＋「機器執法雙向一致」，完全不需動 hub。**

**規範已存在、只是缺執法**：專案「做得對」時是雙標的——
- ADR-0077 退役 Skill Hub 時，在 0063/0072 的**狀態行＋README＋SECURITY.md 三處都標**了 `🗑️ 已取代（Superseded by ADR-0077）`（模範）。
- ADR-0035 的 README 列有 `✅ 已接受（ADR-0092 修訂：則數→token%、雙觸發）`（模範）。
- 問題是**不一致，不是不能**——同樣是 0077，卻漏標了 0061。

---

## 修訂慣例（每受害者 ADR 三處同步）

依專案既有模範，每條受害者 ADR 的反向註記須落在下列位置（視漂移類型取用）：

| 位置 | 格式（模範來源） | 用於 |
| :-- | :-- | :-- |
| **① ADR 本體狀態行** | `✅ 已接受（部分修訂 by [ADR-XXXX](...)：<一句>）`；整取代用 `🗑️ 已取代（Superseded by [ADR-XXXX](...)）— 日期：<原因>。保留為歷史紀錄。`（模範：0063 狀態行） | 全部 |
| **② README 索引狀態欄** | `✅ 已接受（ADR-XXXX 修訂：<短語>）`（模範：0035 README 第 43 行） | 全部 |
| **③ ADR 關聯/相關段** | `- [ADR-XXXX](...) — <標題>（**修訂本 ADR 的 <哪一點>**）`（模範：0118 關聯段） | Class A / superseded |

> **原則**：superseded_unmarked 與「機制已刪但 ADR 仍列活體」必須用 ①＋②＋③ 全標（讀者最易被誤導）；純檔案指標的 Class B 可只補 ①＋②。**不改 hub、不改行為碼**——本 backlog 全為文件修訂（除「候選真實缺口」另評）。

---

## 消化批次與優先序

優先序 = **讀者被誤導的風險 × 語意嚴重度**。批 R1–R2 為 MED、批 R3 為 low（可一次一族），批 R4 為機器執法治本，另有「候選真實缺口」獨立評估。

| 批 | 主題 | 條數 | 優先 |
| :-- | :-- | :-- | :-- |
| **R1** | superseded_unmarked ＋ 機制已刪仍列活體（最易誤導） | 6 ADR | 🔴 最高 |
| **R2** | 安全/行為語意反轉（Class A MED，含安全邊界） | ~14 ADR | 🔴 高 |
| **R3** | amendment hub 一次一族（大宗 low 檔案指標 + 中量 MED） | 4 hub | 🟡 中（低風險高數量）🏁 |
| **R4** | Guard #28 橋（把單向追蹤結晶成機器執法） | 1 守衛 | 🟢 治本 🏁 |
| **候選真實缺口** | 非文件問題，另評（不屬本 backlog 修訂範圍） | 3 項 | ⚠️ 另案 |

---

## 批 R1 — superseded_unmarked ＋「機制已刪仍列活體」（🔴 最高，6 ADR）🏁

**🏁 已完成（2026-07-22）**：6 條全補反向註記，三處同步（① ADR 本體狀態行 `🗑️`/修訂註 ② README 索引狀態欄 ③ 相關段反向連結）；Guard #28 綠（交叉連結全解析、索引齊全）。**純文件、零行為碼變更。未 commit（owner 自行）。**

這批的共通特徵：**核心 Decision 已被整個取代或機制已被刪除，ADR 卻仍標 `✅ 已接受`、讀起來像機制還在**。三處全標（含 ③ 相關段）。

| ID | ADR | 漂移（核心 Decision 現況） | 取代者 | 建議狀態行 |
| :-- | :-- | :-- | :-- | :-- |
| R1-1 | **0035** 滑動視窗 | 則數→token%／PostHeartbeat→雙觸發／ACID 2→3 步（README 已標、本體未標） | 0092 | 本體補「部分修訂 by 0092」（README 已為模範） |
| R1-2 | **0071** 依賴鎖檔 | 整套 `requirements.in`＋`uv pip compile`＋`lock-input-sha256` 檔案**全刪**、torch「永遠特例」反轉進 uv.lock | 0117 | `🗑️ 部分取代 by 0117`（機制已不存在） |
| R1-3 | **0089** read-only auto-allow | `read_only_tools` 欄位/config 鍵/Guard#32 驗證**全換** `tool_risk`（語意保留、機制盡改） | 0143 | 本體＋README 補「機制汰換 by 0143」 |
| R1-4 | **0120** 指令非同步審批 | 「不阻塞 ReAct＋前端零改動」被反轉成 Gate-2 阻塞 broker（原 approval_pending 流正常運作下**不可達**） | 0143 | `🗑️ 部分取代 by 0143`（含行為回退，須醒目） |
| R1-5 | **0135** 停止綁看板 | `mission_stop_gate.py`＋`board_allows_stop`**整檔刪除**，ADR 決策#3／README:144 仍列為活體 | 0146 | `🗑️ 決策#3 已取代 by 0146（機制刪除）` |
| R1-6 | **0139** harness_context | Guard#40「看板未清不可停」釘死對指向**已刪機制**（0146 已改釘 consume_round）＋審批句非同步→同步（0143） | 0146/0143 | 本體補雙修訂註（0146 停止閘／0143 審批句） |

---

## 批 R2 — 安全/行為語意反轉（🔴 高，Class A MED，~14 ADR）🏁

**🏁 已完成（2026-07-22）**：20 條全補反向註記，三處同步；Guard #28 綠。**例外 0040＝守衛訊息碼修正**（非反向註記）：`guard_eventSource.ts` runner 的 3 條 console 訊息原教「EventSource URL 須含 token 參數」＝ADR-0040 要消滅的反模式，已改為「全面禁用 EventSource、改 Fetch+Header、token 絕不進 URL」（執法邏輯與 per-violation 訊息本就正確、無測試斷言舊字串）。**其餘純文件、零行為碼變更。未 commit（owner 自行）。**

Decision 描述的**機制/安全邊界已被後續 ADR 反轉**，讀者信舊 ADR 會誤解現行安全行為。三處全標。

### 安全相關（最優先）
- **0008 LDH（3 條）**：①壞卡當無卡→fail-loud 401（0131）②預設 Guest→DebugAgent（0147）③無條件降級→縮首裝/未認領（0144）。**本線首個高價值標的**（認證行為反轉）。
- **0053 臨時密碼揭露反轉**：`token_hex`+印 console→`token_urlsafe`+寫一次性檔+只印路徑（0118/DA21，避免輪替 log 留有效 admin 憑證）。
- **0060 dispatcher RBAC 豁免反轉**：「常駐注入免 RBAC」→`usable_skill_ids` 過 `can_use_skill` 條件注入（0148，Base/Guest 空清單無 dispatcher）。
- **0018 / 0021 ReadOnly 白名單反轉**：「ReadOnly 允許 ls/pwd」→現對 safe/dangerous 皆 DENY（0143，只 read_only-class 工具放行）。README:152 已記、ADR 未標。
- **0134 子代理「唯讀」誤述**：後果邊界稱子代理 Guest「唯讀」→Guest 實有 write/edit/delete（**自始不準**＋安全邊界誤述）；另 `enable_autonomy` 硬編→繼承（0146）。

### 非安全機制反轉
- **0011** 脈衝串流：非串流 content 清空優化「僅 stream=False 且非多輪」→現無條件跑且 populate、無 ReAct 閘。
- **0012** 自主預算（2）：nudge 續圈整段移除（0146）／5 slots→50 工具輪保險絲。
- **0022** I/O DIP：「零匯入」→legacy_vault/config_utils 直 import（0116 白名單化、狀態註只涵蓋 _FallbackResourceProxy）。
- **0032** DIP 白名單：「自動降級為本地實作」→get_resource_proxy 已 fail-loud（0116）。
- **0038** heartbeat 自癒：單檔→目錄掛載（0150）。
- **0040** guard 訊息**與規則相反**：規則禁 EventSource、訊息卻教「URL 含 token」＝指導開發者做反模式（**修復指引 actively wrong**，此條建議連同訊息文字一併修）。
- **0046（2）**：§2 DISPATCH_TOKEN Bearer→handshake/session_token（0047/0050，§3 有註 §2 沒有＝部分註記）／Decision#1 ToukaMemory/server.py 已被 0116 刪。
- **0051** 重啟即失取捨→0138 補耐久層跨重啟。
- **0052** resolve_env_file_path→0151 加 TOUKA_ENV_FILE 先查覆寫。
- **0066（2）**：frontmatter 扁平頂層→巢狀 metadata（過 quick_validate）／Phase P3 auto-trigger 未落地（實為 Pull activate_skill）。
- **0079** docgen 輸出隔離：共用 temp→per-agent vault jail（0115）。
- **0092** 狀態標頭自我矛盾：狀態行「B0-E 規劃中」vs 自己 Action Items 全 [x]（**分階段標記落地時未推進**，此條只需更新自身狀態行）。
- **0123** validation protocol：二元→三分（0143 H4 明言舊措辭「fail-open 時代的謊言」）。
- **0140** heartbeat 目錄（0150）＋.env 錨（0151）。

---

## 批 R3 — amendment hub 一次一族（🟡 中，低風險高數量）🏁

**🏁 已完成（2026-07-22）**：**38 條**補反向註記，三處視型態同步（① ADR 本體狀態行／② README 索引狀態欄；純檔案指標 Class B 只補 ①＋②）；Guard #28 綠（172 份、0001..0172 連續、無斷鏈、所有新交叉連結解析、索引齊全）。**純文件、零行為碼變更。未 commit（owner 自行）。** 分佈：
> - **ADR-0156 react-core hub（9）**：0078/0100/0111/0127/0128/0129/0130/0146/0152＝claude/gemini/openai handler 薄殼化後的檔案指標（→adapters／react_core）；README:136-139 與 0156 line 52（已寫 ClaudeAdapter）的自我不一致隨 0127-0130 反向註記一併收斂。0129 另註明 Guard #21 層界引用為自始不準（非漂移、不在本註記範圍）。
> - **ADR-0117 uv workspace hub（5，+0071 已在 R1）**：0016/0049/0070/0082/0108＝requirements.*／pyzipper／qdrant/filelock 依賴宣告載體遷 pyproject/單一 uv.lock。
> - **ADR-0143 hitl hub（8）＋ADR-0146 nudge hub（4）＝已於 R1/R2 全數覆蓋**：0089/0120/0139（R1）＋0012/0018/0021/0123/0134/0135（R1/R2）皆已補反向；唯 **0125（dangerous+background emergent 未測）＝候選真實缺口 G-3**、非補註記可解，另案。
> - **散見單發 Class B low（24）**：0006/0017/0026/0027/0029/0030/0031/0034/0041/0043/0044/0045/0055/0056/0061/0067/0073/0080/0081/0083/0095/0106/0131/0144。含安全提示（0056 hashed_password mock 勿重引入被 DA22 移除的欄位）、子系統退役（0043/0061→0077）、限制解除（0080→0081）、身分語意（0045→0141、0081→0132）、檔案指標（0027→0156）、死碼/命名草圖/時點性宣稱自修正（0041/0067/0106/0026/0029/0030/0034）等。0073 無狀態段＝改在標題後加 blockquote 修訂註（→0103）。

單一後續 ADR 改多個舊 ADR。**同一治本批次逐 hub 掃過去，補反向即收斂 ~27 條**。多為純檔案指標 low（改 ①＋② 即可）。

| Hub | 受害者數 | 受害者 | 性質 |
| :-- | :-- | :-- | :-- |
| **ADR-0156** react-core 抽取 | **9** | 0078/0100/0111/0127/0128/0129/0130/0146/0152 | 全 low 檔案指標（`claude_handler`→`claude_adapter`）＋**README:136-139 自我不一致**（同檔 line 52 已寫 ClaudeAdapter） |
| **ADR-0143** hitl 統一 | 8 | 0012/0018/0021/0089/0120/0123/0125/0139 | 含行為反轉（多條已在 R1/R2） |
| **ADR-0117** uv workspace | 6 | 0016/0049/0070/0071/0082/0108 | 鎖檔機制全遷 pyproject/uv.lock |
| **ADR-0146** nudge 退役 | 4 | 0012/0134/0135/0139 | 含整檔刪除（0135，已在 R1） |

> **0156 特別單純**：9 條全是「檔案指標從 handler 改指 adapter」＋修 README:136-139 與 line 52 的自我不一致，幾乎是純文字批次。建議 R3 從 0156 起手。

### 其餘單發 Class B low（散見，②＋①）
0006（措辭）/0017（run_server.py 刪）/0026（whitelist 清單）/0027（gemini_handler→adapter）/0029（guard 序號）/0030（「架構違規仍存在」假宣稱）/0031（noqa）/0034（terminal-stream 已改真認證＋偵測規則#2 從未實作）/0041（logout 死碼）/0043（installHubSkill 刪 by 0077）/0044（artifact_ref 部分註記）/0045（identity.role→is_privileged）/0055（測試 SSOT 搬移）/0056（**hashed_password mock：讀者照做會重引入被 DA22 移除的安全欄位**，此條雖 low 但建議提示）/0061（Skill Hub 退役未回標 by 0077）/0067（pass_env 死碼）/0073（自清機制 vestigial by 0103）/0080（已知限制被 0081 解除）/0082（requirements SSOT by 0117）/0083（表單邊框雙模 by 0105）/0095（建模 config 4→6＋Guard#34 加 errors.json 檢查）/0100（Claude 轉換移 adapter）/0106（image_validation 模組名從未存在）/0108（passphrase 8→12＋requirements 退役）/0131（X-New-Auth-Token 加 hasEverLoggedIn by 0144）/0144（watermark 加 tokens_revoked_at by 0160）。

---

## 批 R4 — Guard #28 橋：把單向追蹤結晶成機器執法（🟢 治本）🏁

**🏁 已完成（2026-07-22）＝ADR-0173（Guard #28 第 4 檢查）**：擴充 `scripts/guard_adr_integrity.py` 加「修訂雙向一致」檢查（同守衛檔內第 4 項，`guard_all` 總數不變仍 44）。
> - **反轉性動詞 SSOT**（`_AMEND_VERBS`）：修訂／取代／反轉／汰換／廢棄／supersede／amend。**刻意不含**延伸／延續／建基於／抽取／細分（擴充或純檔案位移非決策反轉，反向註記選配、不納閘＝避免大量低價值假陽性）。
> - **方向判別**：只認「動詞在前、ADR 編號緊接、中間無 by、非冒號標籤」的正向宣告；「ADR-Y 動詞…」（編號在前）／「… by ADR-Y」（被動）／「動詞：ADR-Y」（冒號標籤）三種反向註記格式天然排除，故掃全 ADR 內文＋README 列不會把 R1–R3 補的反向註記誤判為正向 cite。
> - **反向註記驗證**：正向對 (Y→X) 須在 X 狀態區或 README 索引列回指 Y；否則 FAIL。逃生口 `AMENDMENT_BACKLINK_GRANDFATHER`（目前空集）。
> - **紅→綠實證**：合成單元測試（動詞在前匹配／編號在前・by・冒號三式不匹配／弱動詞不納入／README 列偵測／缺反向註記紅→補上綠→grandfather 抑制）＋**活體**掃出 6 個正向對（0050↦0048／0092↦0035／0105↦0083／0117↦0071／0118↦0053／0151↦0118）**全數已有反向註記＝0 違規**＝R1–R3 治本完整的機器證明，直接納閘無需 grandfather。
> - 驗收：`guard_adr_integrity` 173 份 0001..0173 連續＋4 檢查全綠；`test_guard_adr_integrity` 25 passed；ruff clean。**順手修 R2 遺留迴歸**（`guard_eventSource.ts` 訊息字串含字面 `// noqa: EVENTSOURCE_SSOT` 被 Guard #19 掃成未登記 noqa→改為不帶 `noqa:` 前綴的規則名，Guard #19 復綠）。**未 commit（owner 自行）。**
> - ⚠️ **既有失敗非本案**：`guard_all` 唯 **Guard #23（npm audit）**FAIL＝既有依賴 CVE（@hono/node-server 路徑穿越／body-parser DoS／brace-expansion 等），與 Phase 2 零關係（`npm audit fix --force` 破壞 shadcn＝另案）。

**目標**：防止未來再出現單向修訂追蹤。擴充 `scripts/guard_adr_integrity.py`（Guard #28，現只查引用/連續/索引結構）加一條**雙向一致檢查**：

> 若 ADR-Y 的內文/相關段 cite「修訂/取代/延伸 ADR-X」（正規化關鍵詞：修訂／取代／延伸／supersed／amends），則 ADR-X 的**狀態行或 README 索引列必須有對應反向註記**（提到 Y）。否則 FAIL。

- **SSOT**：正向 cite 關鍵詞集合＋反向註記關鍵詞集合（可比照 lint_ssot 規則風格）。
- **紅→綠**：先跑一次收集所有現存單向對（即本 backlog R1–R3 清單）＝紅；R1–R3 補完後＝綠；此後任何新 ADR 只前向不回標即 FAIL。
- **落地順序**：**必須在 R1–R3 文件修訂完成後才納閘**（否則守衛一建即紅），或先以 grandfather 白名單納入（比照 ADR-0170 GRANDFATHER 模式）逐步清零。
- **測試**：合成一對「Y cite X 但 X 無反向」→守衛紅；補上→綠；活體對真實 ADR 樹跑清白。

---

## ⚠️ 候選真實缺口（非文件問題，另評——不屬本 backlog 修訂範圍）

以下三條**超出「ADR 過時」範疇**，是碼/行為層的候選缺口，須 owner 另評是否修碼（非補註記能解）：

| # | ADR | 缺口 | 風險 |
| :-- | :-- | :-- | :-- |
| G-1 | **0065** | live `SKILL.md` Step 1 仍指示內部 agent 呼叫**不存在的** `dispatch_*` MCP 工具（proxy MCP client stdio-only、dispatch_* 在 ToukaProxy/src 零匹配、內部實走原生 mission_tools）；若 agent 照做會呼叫失敗 | 中（活體指令誤導 agent） |
| G-2 | **0110** | GDPR 匯出 conversations 只濾 `agent_id ∈ owned_agents`，**漏掉使用者 `owner_user_id` 的「裸模型」對話**（gemini-2.5-flash 等）；0132 自標「另案」未做 | 中（隱私合規：使用者「擁有」的對話漏出匯出範圍） |
| G-3 | **0125** | `dangerous(CONFIRM)+background=True` 組合現已能執行（0143 preapprove 後落 background 分支），但 ADR 列為「未做」**且無任何測試覆蓋** | 低-中（emergent capability 未驗證） |

---

## 附錄 A — 推翻的假陽性（7 條，不需修訂）

opus 對抗覆核推翻，證明**原措辭本就精準或 analyzer 誤讀**，非事後漂移：
- **0028** passlib：原始措辭本就不精準（非事後漂移）。
- **0031** DIP 歸屬：analyzer 誤讀（ADR 本就說 DIP 守衛非 console 守衛）。
- **0039** Background 句：該 fallback predates ADR 同 commit、後續只加固（碼朝宣稱靠攏非漂離）。
- **0044** Cross-identity：analyzer 只讀到 mission_manager.py:74 就停，實際仍解真 DispatchAgent 身分。
- **0089** A.2 範圍：ADR 明示「後續（A.2）」延遲項，被他 ADR 撿起實作不使原陳述失準（判準排除）。
- **0122** sse_line「三 handler」：歷史整併敘述非活體不變式（SSOT 仍在 src/utils/sse.py）。
- **0129** Guard#21 引用：自始不準的作者疏漏非事後漂移（Guard#21 從不涵蓋 adapters/services 邊界）。

---

## 附錄 B — 5 類漂移型態（分類參照）

1. **單向修訂追蹤（最大宗）**：後續 ADR 前向連結、舊 ADR 未回標。Class A（Decision 反轉，MED）/ Class B（時點性引用過時，low）。
2. **superseded_unmarked（4）**：核心 Decision 被整取代仍標「已接受」（0035/0071/0089/0120）。
3. **同一 ADR 部分註記**：0046/0044 §3 有註 §2 沒有＝比全未標更誤導。
4. **狀態標頭自我矛盾**：0092 分階段狀態行 vs 自己 Action Items 打架。
5. **文件主動誤導活體**：0065 live SKILL.md 教呼叫不存在工具＝候選真實缺口（→ G-1）。

---

*建立：2026-07-22（Phase 1 收官同日）。living doc：R1–R4 逐批完成時回填 🏁。逐塊逐條 file:line 證據見 session scratchpad `adr_align_DRIFTS.md`。*

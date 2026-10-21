# ADR-0175: 早期守衛缺口 批 LIVE——三條生產真實缺陷修復（系統技能可刪／reset 誤刪上傳／互審成本閘失效）

## 狀態

✅ 已接受（早期 ADR 守衛涵蓋 backlog 批 LIVE：E35/E65/E76，owner 2026-07-23 拍板「繼續處理①3 條 LIVE 缺口」）

## 日期

2026-07-23

## 背景

早期 ADR 守衛涵蓋審查（0001–0139）掃描收官後確認 **3 條 LIVE 生產缺陷**——與批 A（ADR-0172，生產正確、只補測試）不同，這三條是**生產真的壞**：ADR 承諾的行為在今日生產碼不成立，且各自的既有測試因取樣偏差恆綠。三條皆 MED、皆需修碼：

- **E35（ADR-0066，塊7）**：`skill_service.delete_skill` 讀 `fm.get("is_system")`（**頂層鍵**）判定系統技能保護，但生產寫入路徑 `build_frontmatter` 把擴充欄位收進巢狀 `metadata:` 子欄位（Anthropic quick_validate 相容，ADR-0066 Phase 4）→ **新制系統技能的刪除保護完全失效**。同檔其餘讀取點（`render_skill_registry` 等 5 處）皆已用 `read_touka_field`（metadata 優先＋top-level fallback），唯獨 delete 閘漏改＝家族⑧不對稱姊妹。假綠成因：唯一保護測試 `test_delete_system_skill_protected` 用 legacy 頂層 `is_system: true` 格式。
- **E65（ADR-0113，塊13）**：per-agent／DebugAgent reset 的 `purge_outputs` 對整個 `outputs/` `remove_dir`，但聊天上傳（`get_uploads_path`）**刻意巢狀於 `outputs/uploads/`**（供 bash/personal-drive 相對路徑存取）→ **每次重置靜默刪除使用者上傳檔**。上傳是「使用者資產」非 agent 產出，reset 語意（清 agent 執行期衍生內容）不涵蓋它。假綠成因：既有測試只放 agent 產出檔、從不放上傳。
- **E76（ADR-0137，塊17）**：互審成本閘 `cost_ceiling_exceeded`（設計決策 4：「cap 有數字即強制查、無視 `cap.enabled`＝**不可關**」）在 `review_loop.py:156` 有真分支，但 **`run_review_round` 全 repo 唯一生產呼叫端 `mission_tools`（action=request_review）從不傳 `config=`** → config 恆 None → limit 恆 None → **成本天花板對所有生產流量永不觸發**，不論 owner 設多少 `max_total_cost_usd`；連帶 `review.max_attempts` 收緊通道（`review_loop.py:208`）也是死格。假綠成因：test_review_budget 直呼純函式手組 config；test_review_loop 的 `_run` helper 同樣不傳 config；唯一工具接線測試 monkeypatch 掉整支 `run_review_round`。

## 決策與實作

三處生產修（各一個聚焦變更）＋4 支釘死測試；全部以紅→綠變異驗證（stash 生產修、留測試 → 4 測全紅 → 還原全綠）。

### E35 · `skill_service.py` delete 閘改走 `read_touka_field`

`delete_skill` 的 `fm.get("is_system")` → `skill_parser.read_touka_field(fm, "is_system")`（與同檔其餘讀取點同判準；legacy 頂層格式因 fallback 仍受保護）。釘死（`test_skill_service_dir.py` +2 測）：①巢狀 `metadata.is_system: true` 從磁碟載入 → 刪除必拒、目錄保留；②生產寫入 round-trip（`update_skill(is_system=True)` → `delete_skill`）必拒。

### E65 · `agent_reset/primitives.py` purge/remaining 一致排除 `outputs/uploads`

新增 `_PRESERVED_OUTPUT_DIRS = ("uploads",)`：`purge_outputs` 改逐子項刪除、跳過保留目錄；**`_outputs_file_count`（purge 計數＋`remaining_outputs` 自檢共用）同步排除**——只清不計必須一致，否則 orchestrator 完整性自檢會把保留的上傳誤判為漏清（false-nonclean）。無保留項（無上傳）時連空殼目錄一併移除＝既有「目錄消失」契約（`test_custom_agent_identity_survives_reset`）零變動。`RETAINED_STORES` 補列 `outputs/uploads`＝重置報告透明列示（ADR-0104 R3 同款誠實原則；既有測試為子集斷言、additive 安全）。釘死（`test_reset_primitives.py` +1 測）：上傳檔 purge 後存活、不入 purge 計數、remaining 自檢為 0、agent 產出照清。

### E76 · `mission_tools.py` 對 `run_review_round` 補 `config=autonomy_service.config`

頂層匯入 `autonomy_service`（`builtin/system.py`／`evolution.py` 既有慣例），`request_review` 呼叫補傳 `config=autonomy_service.config`——`cap.max_total_cost_usd`（成本閘）與 `review.max_attempts`（收緊往返）自此接通；cap 形狀與 `is_over_budget` 同源（autonomy config SSOT），差別僅互審路徑無視 `enabled`（不可關）。釘死（`test_review_loop.py` +1 測）：**不 monkeypatch `run_review_round`**，stub autonomy `cap={enabled: False, max_total_cost_usd: 0.01}`＋`get_total_cost_usd=0.02` → `request_review` 必回 BLOCKED；載重斷言是 note「成本天花板」（成本閘先於蒐證——漏傳 config 時本板無證據會落軟任務分支、status 同為 BLOCKED，note 斷言使該變異仍紅）；`enabled: False` 同時鎖住「無視 enabled」語意於工具端到端。

### 變異驗證（紅→綠，皆已還原）

| 缺口 | 變異（＝修前生產碼） | 結果 |
|------|---------------------|------|
| E35 | 還原 `fm.get("is_system")` 頂層讀 | 2 測紅（巢狀＋round-trip 皆刪除成功） |
| E65 | 還原整樹 `remove_dir` | 1 測紅（計數 2≠1 且上傳被刪） |
| E76 | 還原不傳 `config=` | 1 測紅（落軟任務分支、note 無「成本天花板」） |

## 後果

- **三條 LIVE 缺陷歸零**：真實系統技能不可再被刪、重置不再吞使用者上傳、互審成本天花板真的會觸發（owner 設數字即生效、不可關）——掃描總帳中「生產真的壞」一欄清空。
- **變更面極小**：生產修各為單點（一行判準／一段逐子項刪除／一個 kwarg），行為語意不變者（legacy 技能、無上傳 reset、cap 無數字）皆零變動；受影響 7 測試檔 86 passed、`guard_all` 44 道全綠。
- **E65 語意澄清入碼**：`outputs/uploads`＝使用者資產自此為顯式常數＋RETAINED 列示，未來新增保留目錄有單一落點。
- **範圍界定**：backlog 餘項（②E39/E66 HIGH 補測、③claude_adapter fixture 群、④E77、⑤家族③/⑦批）屬另批，待 owner 拍板。

## 關聯

- 提案：`docs/architecture/proposals/early-adr-guard-coverage-gap-backlog.md`（E35/E65/E76 回填 🏁；3 LIVE 全數收官）
- 前作：ADR-0169／0170／0171（三高槓桿）／ADR-0172（批 A HIGH 行為級測試）＝同一 backlog 的前四批
- 守護：ADR-0066（技能目錄格式，metadata 巢狀欄位）／ADR-0104＋ADR-0113（DebugAgent／per-agent 重置）／ADR-0137（證據接地互審，決策 4 成本閘）
- 相關：ADR-0146（autonomy cap 配置形狀 SSOT）

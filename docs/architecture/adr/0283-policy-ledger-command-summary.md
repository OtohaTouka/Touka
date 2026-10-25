# ADR-0283：政策稽核帳本只記終端指令的程式名稱和長度

- 狀態：✅ 已接受（維護者 2026-10-25 核准：「照建議：1 甲、2 甲」）
- 日期：2026-10-25
- 相關：ADR-0104（重置靈魂時刻意保留 `decisions.csv`）、ADR-0086（無痕模式）、ADR-0108／ADR-0109（備份包含 `stats/`）、禁令 18、禁令 23
- 影響範圍：新 `ToukaProxy/src/utils/audit_resource.py`；`services/audit_service.py` 的 `_sync_log_write`；新 `tests/unit/test_audit_resource_security.py`；SECURITY、SSOT_MASTER_MAP、tests/README

## 背景

- `PolicyAuditService` 每次權限判斷都在 `stats/decisions.csv` 寫一列，`resource` 欄原樣寫入 `PolicyContext.resource`。
- 終端指令的兩個判斷點（`tool_executor.py` Gate 2、`permission_service.get_execution_strategy`）把**整條指令**放進 `resource`。指令帶了金鑰時（`curl -H "Authorization: Bearer …"`、`OPENAI_API_KEY=… python x.py`、`--password …`），金鑰就明文留在這個檔。
- 這個檔跨靈魂共用，重置靈魂時刻意保留（ADR-0104），備份也會打包整個 `stats/`。沒有網頁或 API 直接讀它，所以風險是金鑰在硬碟與備份裡多留一份。
- 2026-10-23 修過同類問題：終端指令升級成 CONFIRM 時，日誌改成只記長度（SECURITY 那節把帳本列為後續待處理）。
- 計畫審查時評估過「遮掉認得出的金鑰寫法、其餘照記」：要認的寫法太多（PowerShell 的 `$env:KEY="…"`、網址的 `?key=…`、JSON 的 `"api_key":`、HTTP 標頭的各種名稱），名稱判斷又會誤遮 `--max-tokens` 這種正常參數，仍會漏網。完整指令本來就留在對話紀錄裡，帳本只要看得出「哪個程式、被允許還是擋下」。

## 決定

1. **終端指令只記摘要**：`terminal_execute` 那一列的 resource 改成 `[command program=<名稱或?> chars=<原文字元數>]`，參數和值一律不記。
   - 程式名稱的取法：以空白（含 tab、換行）切段。開頭的 POSIX 環境變數設定 `NAME=值` 跳過：NAME 必須是 `[A-Za-z_][A-Za-z0-9_]*`，值只收英數與 `_./:@%+,-`。設定寫法不合，或值出現其他字元，就停下記 `?`，因為值可能延伸到後面幾段。
   - 第一個不是設定的段落，整段只能是 `[A-Za-z0-9_./\\:+-]`，不能含 `=`。取最後一個 `/` 或 `\` 之後的部分，必須符合 `[A-Za-z0-9][A-Za-z0-9._+-]{0,63}`，否則記 `?`。所以 `$env:…`、`[Environment]::…`、`&`、引號開頭的路徑都記 `?`；`set NAME=…`、`env`、`sudo` 這類記第一個程式名稱。
   - 程式名稱再過一次 `redact_secrets_from_content`，命中（例如誤把 `sk-…` 當指令跑）也記 `?`。
   - 比對一律 `re.fullmatch`，字元集寫明確的 ASCII 範圍，不用 `\w`。
   - 任何例外都回 `[command unavailable]`，不讓原文落地。
2. **其他列**：resource 符合工具名稱形狀 `[A-Za-z0-9_.:/-]{1,200}` 時，過 `redact_secrets_from_content` 後照記；不符合就記 `[resource chars=N]`；例外記 `[resource unavailable]`。
3. **只改寫進檔案的那一份**：`_sync_log_write` 以區域值寫入，`PolicyContext` 與 `evaluate_policy` 不變。黑名單、底線與模式決策仍看完整原文。欄位與表頭不變，舊檔照常往下寫。
4. **SSOT 分工**：新模組 `utils/audit_resource.py` 只決定帳本 resource 欄的寫法，不另寫金鑰樣式；認出金鑰樣式的 SSOT 仍是 `utils/secrets.py`。依禁令 18 寫成可獨立測試的純函式。
5. **守門**：`test_audit_resource_security.py` 用 AST 列出 `ToukaProxy/src` 每個 `PolicyContext(...)` 呼叫（含 `import … as` 別名）與它的 `action=` 原始碼，比對固定清單；另外擋下任何 `model_copy(update=…)` 改 `resource` 或 `action`。新增或改動建構點時測試會紅，提醒先看 resource 放了什麼。
6. **舊紀錄不自動改**：已寫進帳本的舊列維持原樣（維護者選擇）。真的有金鑰時，有效的做法是到廠商那邊換掉那把金鑰；備份、對話紀錄與 payload 日誌裡也有，刪掉 `decisions.csv` 清不到，還會失去 ADR-0104 刻意保留的稽核紀錄。

## 後果

- 畫面上沒有差別。帳本裡新的終端指令列變短；要看代理人跑了哪條指令，看對話紀錄。
- `reason` 欄不變：內容只有固定句子、`hard_floor.FLOOR_LABELS` 與 `check_match` 回傳的設定檔黑名單樣式，不含指令原文。測試對整列所有欄位斷言不含假金鑰，日後有人讓 reason 回顯指令會被抓到。
- 已知限制：
  - 指令仍留在其他副本：對話紀錄、`logs/proxy_payload.log`（送給模型廠商的對話本身，無痕不寫，只限三元組角色讀）、任務看板的驗證指令與互審蒐證摘要、終端面板記憶體裡最近 200 筆事件（任何登入的人都能從終端紀錄 API 讀，不分代理人；之後另外看）。
  - 無痕對話的權限判斷仍記一列，只剩程式名稱和長度，與「零殘留」原則不完全一致；這是既有行為，本 ADR 不改。
  - CSV 其他欄位（例如 reason 裡的黑名單樣式）被試算表當公式的問題不處理；新的 resource 摘要開頭是 `[`，不會被當公式。
- 撤回的路：revert 本 ADR 的變更即恢復原樣記錄，權限判斷不受影響。

# ADR-0291：刪除湧現指數（E-Index）

- 狀態：✅ 已實作。維護者 2026-10-28 決定把湧現指數從前後端完全刪除，舊資料不動；一頁計畫由維護者核准。
- 日期：2026-10-28
- 決策者：維護者
- 修訂 [ADR-0244](0244-model-layer-stage-1b.md) 兩條：「E-Index 歷史每次 run 只記一筆」整條刪除；「稽核 CSV 欄位不變」改成新檔沒有 `e_index` 欄、舊檔沿用自己的表頭（見決定 3）。
- 相關：ADR-0152、ADR-0155（C2 記憶訊號、C4 `pure_gen_ms`）、ADR-0281（訊號改看「執行過」）。這幾份提到 e_index 的段落從此只是歷史紀錄，其餘決策都不變。
- 影響範圍：ToukaProxy `services/telemetry.py`、`handlers/react_core.py`、`routers/stats.py`、`services/completion.py`、`main.py`，刪除 `utils/metrics.py`；ToukaChat `hooks/useSystemMonitoring.ts`、`components/sidebar/SystemMonitoringView.tsx`、`components/sidebar/monitoring/MemoryStatus.tsx`；`openapi.json`、`api.ts`；34 份 characterization golden；對應測試與文件。

## 背景

- E-Index 的公式是「（有沒有查記憶：5 或 0）× 0.7 × 上游耗時 ÷ 最後一則訊息的香農熵」，每次 run 算一次，存在三個地方：
  - SSE `touka_status.telemetry` 與最終回應附的 telemetry 裡的 `e_index`；
  - `token_usage.json` 每個供應商底下的 `e_index_history`（最多 50 筆）；
  - 稽核 CSV（`usage_audit.csv`）的第 9 欄。
- `/stats/emergence`（含 `/v1`）讀稽核 CSV 最後兩列，回傳數值與趨勢；ToukaChat 右側邊欄「系統監控 → Memory Status」顯示成一條進度條。
- 沒有其他功能用到它：成本上限只讀 `total_cost_usd`，心跳、記憶服務、提示注入都不讀。
- 寫和讀對不上：寫入的是各代理人自己的 `stats/<agent_id>/usage_audit.csv`，端點讀的是全域的 `stats/usage_audit.csv`，所以畫面大多只顯示預設的 0、Stable。
- `utils/metrics.py` 另有一份公式不同的計算，全庫沒有人匯入。

## 決定

1. **後端刪除**：
   - `telemetry.py` 的 `calculate_shannon_entropy`、`calculate_emergence_index`；整檔刪 `utils/metrics.py`。
   - `react_core` 的計算、telemetry 的 `e_index`、run 結束時只為了記 E-Index 的那次空用量入帳，以及只寫不讀的 `all_thinking_and_tool_logs`（以前給 E-Index 字串比對用）。
   - `accumulate_token_usage` 拿掉 `e_index` 參數；旁路呼叫（`complete_once`）跟著不再傳。
   - `/stats/emergence` 與 `/v1/stats/emergence`；根路由 `features` 清單的 `emergence`。
2. **前端刪除**：`MemoryStatus` 的指數條、`SystemMonitoringView` 的傳值、`useSystemMonitoring` 的請求與欄位（其餘結果的索引往前移一格）。
3. **舊資料不動**：
   - `token_usage.json`：新的供應商項目不再建 `e_index_history`；舊檔已有的照留，程式不再寫、也不清。現有讀者都只拿特定欄位，殘留無害。
   - 稽核 CSV：新檔的表頭是 12 欄，沒有 `e_index`。`append_csv_row` 只在檔案不存在時寫表頭，所以既有的檔沿用自己的表頭：寫入前用 `read_file_prefix` 只讀開頭 4 KB 取得表頭，表頭有 `e_index` 時在那個位置補空字串，後面的欄才對得上。不改檔名、不遷移舊檔。
4. **逐輪入帳與稽核 CSV 每次 run 一列照舊**（ADR-0244 其餘決定不變）。

## 後果

- 對外回應：telemetry 少了 `e_index`，`/stats/emergence` 回 404。前端沒讀這些欄位；外部客戶端若有依賴，要自己拿掉。
- `/stats` 的 `usage_history` 照原樣回傳 CSV 列：舊檔的新列 `e_index` 是空字串，新檔沒有這個鍵。
- 更早的稽核檔（2026-06 加 `rate_source` 之前建立）表頭本來就比資料列少一欄，這次不處理；這類檔也有 `e_index` 欄，新列一樣補空字串，錯位情況和以前相同，不會更糟。
- 記憶查詢訊號 `memory_tools.query`（`MEMORY_QUERY_SIGNAL`）查證守衛還在用，保留。
- 撤回：revert 即可，舊的 CSV 列與 `e_index_history` 都還在。注意：這一版建立的新稽核檔是 12 欄表頭，revert 後舊程式會寫進 13 欄的列；撤回前先把這些檔改名，讓舊程式重建。本 ADR 檔保留，在狀態註記撤回。

## 替代方案

- **保留 `e_index` 欄、一律寫 0 或空值**：表頭永遠不變，最簡單；但沒有完全刪除，新檔也留著一欄沒有意義的欄位。不採用。
- **換新檔名，或遷移舊檔**：前者讓同一個代理人的稽核帳分成兩份；後者要改寫使用者的資料。不採用。
- **不讀表頭、直接寫 12 欄**：舊檔會整列錯位（`input_tokens` 落到 `e_index` 欄）。不採用。
- **清掉舊的 `e_index_history`**：要改寫使用者的資料，殘留又無害。不採用。

## 驗證

- 先寫的測試在實作前全紅：
  - `tests/unit/test_emergence_index_removed.py`：兩個端點回 404；計算函式、`utils/metrics.py`、`e_index` 參數都不在；舊 `e_index_history` 原樣、新供應商項目沒有這個鍵；新稽核檔 12 欄；舊稽核檔表頭與舊列不變，新列 `e_index` 是空字串、其他欄對得上。
  - `tests/unit/test_per_round_accounting.py`：三輪含記憶查詢的 run 沒有 `e_index_history`、telemetry 只剩 `request_id`／`total_ms`／`local_ms`／`pure_gen_ms`；稽核 CSV 改用 12 欄表頭。
  - ToukaChat：`useSystemMonitoring` 不再請求 `/v1/stats/emergence`、結果索引前移後 token 用量與記憶數量仍對；`MemoryStatus` 不再顯示指數。
- 34 份 golden 依 harness 規則刪檔重錄；逐檔核對和舊版相比只少了 `e_index`（68 段）。
- 刪掉的舊測試：`/stats/emergence` 兩個 smoke、記憶偵測裡「較早回合查記憶 → e_index > 0」、記憶訊號 SSOT 裡 e_index 檢索訊號的靜態檢查。

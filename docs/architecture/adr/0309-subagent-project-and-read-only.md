# ADR-0309：子代理看得到對話綁定的專案，也可以限制成只能看

- 狀態：✅ 已實作。一頁計畫由維護者核准，經兩輪審查。
- 日期：2026-11-10
- 決策者：維護者
- 相關：[ADR-0134](0134-in-process-subagent-dispatch.md)（子代理派發）、[ADR-0245](0245-model-layer-stage-2a.md)（Subagent 角色）、[ADR-0269](0269-advertised-tool-list-enforcement.md)（只執行列出的工具）、[ADR-0304](0304-identity-safeguards.md)（專案編號不收模型填的值）
- 影響範圍：`ToukaProxy/src/tools/subagent_tools.py`、`src/models/safety.py`、`src/middleware/layers/tool.py`、`src/services/tool_executor.py`、`src/tools/contracts.py`、`src/tools/builtin/schemas.py`、`scripts/native_tool_contract_baseline.json`

## 背景

靈魂派出的子代理沒有 session（`session_id=None`），檔案工具拿不到對話綁定的專案，只會回「No project selected」。要讓靈魂派子代理寫程式、再派另一個審查，子代理得看得到專案，審查者也要能限制成不能改。

## 決定

1. **專案只來自對話的綁定**：派出時用 `session_project_id` 算一次，寫進子代理的 ctx，組裝與執行用同一份（`SubagentScope`）。跑到一半改綁不影響。模型填的編號無效（ADR-0304）。
2. **權限照對話的模式走**：子代理一律算無人在場。Manual 寫入被拒；Auto 可寫，刪除被拒（審查員不審無人在場的工具呼叫）；YOLO 可寫可刪。
3. **工作區、受保護位置、連結的檢查都會跑**：子代理身分不算管理員或 Debugger，也沒有擁有者，所以比靈魂自己嚴。
4. **只能看放在決策表**：`read_only_run` 進 `PolicyContext.environment`，廣告與執行兩個入口都帶。`_evaluate_rules` 在模式決策表之前，非 `read_only` 類一律 DENY，YOLO 放不寬，硬地板也不會把 DENY 放寬。只看 `tool_risk` 分類，不另列工具名單；沒分類的工具走 `default`（dangerous），等於預設拒絕。
5. **`read_only` 由靈魂在派工時給**：`dispatch_subagent` 沒有輸入合約，只有沒給、`null` 或明確的 `false` 算可寫，其他值一律當只能看。群組的每個子任務各算各的。
6. **提示不放真實路徑**：不用 `request.project_id`（`get_project_context` 會寫真實路徑和「You have CRUD permissions」）。子代理的第一則訊息加固定句；路徑解析得到才說有專案（心跳建的家 `project_id` 是 `"default"`）。
7. **給靈魂的說明放在工具描述**：`<harness_context>` 已到 800 token 上限，不加長。

## 後果

- Auto／YOLO 且綁專案的對話，子代理會真的改專案。它被誘導時能改的範圍就是這個專案。
- 按停止只停靈魂，已派出的子代理最長還會跑 10 分鐘（既有限制）。子代理能跑指令之前一定要處理。
- 「只能看」只保證不改檔案：讀得到專案裡所有檔（含專案自己的 `.env`），`research` 也還能用。
- 舊名 `project_tools`、`vault_tools` 要後繼工具全在清單才放行，只能看的子代理連讀也不能用舊名，要用 `read`、`list`、`find`、`grep`。
- `undo` 的鍵只有專案，可寫的子代理能還原靈魂的編輯。
- 同時派多個子代理改同一個檔案，後寫的會蓋掉先寫的，要靠靈魂分工避開。
- 子代理只拿得到靈魂勾選的工具；靈魂沒勾檔案工具，子代理也用不了。

## 驗證

- `tests/unit/test_subagent_project_access.py`：資料庫裡真的有對話與專案，真的派子代理、組工具清單、跑工具與權限判定，只有模型回應是假的。涵蓋讀得到綁定的專案、沒綁或模型自己填都讀不到、跑到一半改綁、工作區外、受保護位置、連結、三種模式的寫入與刪除、只能看的工具清單與決策表、群組各算各的、提示句。
- Docker 上的路徑翻譯，測試碰不到，實際部署到 Docker 時才走得到。
- 前置檢查（2026-11-10，維護者的 Docker Desktop，用完即刪的容器，限制同執行器）：`uv` 裝 pytest 跑通約 2 秒，`npm` 裝 vitest 跑通約 33 秒，共佔 15 MB＋41 MB（下載快取另外約 145 MB）。

## 回復

還原影響範圍列的檔案與測試，本 ADR 改「⛔ 已撤回」，CHANGELOG 加一列。

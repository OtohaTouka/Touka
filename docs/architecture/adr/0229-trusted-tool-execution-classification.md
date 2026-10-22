# ADR-0229：可信工具執行分類與批准例外

日期：2026-09-16。狀態：維護者已核准計畫；F01 已實作，B1 階段已經審查接受，整體 B6 驗收中。

〔[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md) 修訂：自帶批准工具剩 `request_skill_install`、`request_mcp_creation` 兩個；`evolution_implement_and_verify` 改為一般分類。〕

## 問題與範圍

既有 ToolExecutor 以模型參數是否包含 `command` 決定 `terminal_execute` 及 headless 例外。
這會讓普通寫入越過應有的人工批准；工具 grant、七工具 typed validation 並不能替代該次批准。
本次只修批准分類、同一 invocation 的 handler 綁定與相關守衛；保留 ADR-0228 的公開參數契約。
不調整真實角色設定、風險表、外部 MCP/plugin 權限或工具業務寫入能力。

## 決定

- `src/tools/execution_policy.py` 定義純 `ToolExecutionClass` 與 frozen `ExecutionBinding`。
  builtin 註冊完成時一次建立 `MappingProxyType`；保存真實 callable 身分及 canonical 名稱。
- 終端類只有實際註冊的 `execute_command`、`bash`（canonical 為 `execute_command`）。
  三個自帶批准工具為 `request_skill_install`、`evolution_implement_and_verify`、`request_mcp_creation`（ADR-0254 起剩前後兩個）。
  它們沿既有內部人類介面作批准，不能由模型自行批准。
- 普通、Memory、plugin、外部 MCP 的 `command` 不影響分類。未知 handler 不取得例外；
  冒用已登錄例外名稱但 callable 不符時，回 `permission_denied`／`execution_classification_mismatch`。
  普通動態 handler 仍受普通 PaC/HITL，不擴張成本次全量 typed 遷移。
- 順序為 verified identity → 原 RoleGrants/MCP grant → 可信分類與 PaC → HITL →
  選定公開輸入驗證 → 可信 context 注入 → 已解析 handler。grant 仍按呼叫名稱核對，canonical alias 不擴大 grant。
- 在等待批准前保存實際勝出來源與 callable；builtin 優先於 Memory，plugin 次之。
  等待期間 registry 被替換，也只執行該次已批准的 callable。MCP 同一 invocation 使用原已解析 server/tool。
  真終端 command 仍受既有黑白名單與 terminal 內部安全檢查。

## 守衛與驗證

Guard45 不 import 產品。新增有限 immutable binding AST 解析、canonical/alias/registry reference 核對、
拒絕重新賦值與不支援的動態形狀；只更新兩個新增 import、五筆分類 metadata 與新 pure 模組的 consumer 形狀。
既有 149 個函式的基線不全量重錄，不承諾推導任意動態 Python。

F01 真 policy＋executor＋sandbox `verb_write`／`project_tools`／`core_write` 覆蓋有無 `command`、
Manual/SafeAuto 需批准、pending 零寫入、批准一次、拒絕零寫入。SafeAuto 寫入負例明示使用合法 dangerous
風險設定；不聲稱產品預設 safe write 必須拒絕。真終端別名、人類批准、黑名單/grant 拒絕、
三個自帶批准真 handler、handler 替換及外部 command 不升權均有對應案例。
既有 recorder 測試採明示 synthetic binding，只驗 executor 路由；不冒稱真 handler 證據。

測試前置另修 `tests/conftest.py`：兩份權限 seed 只從 tracked `.json.example` 複製到 sandbox 的產品檔名。
缺範本明確失敗，不查私有設定是否存在、不讀私有內容、不退回權限 fallback。
獨立 CLI 僅載入 fixture AST 至 owned 合成樹的原 RED 保留；這證明存在舊私有 copy 路徑，
不證明歷史執行曾讀到私有資料。回歸中的單行 `S102` 例外只用於同一受控 AST 接縫。

原件存在本機，未入版控。
F01 原 RED 為 12F/16P；最後純格式收尾後相關回歸 238P／1 個既有 warning，11 檔 Ruff／format、SSOT 與 Guard19／28／45 通過；精確範圍記錄在未入版控的收據檔。
B1時尚待的B6現已完成來源驗證：full 9862P／9skip／5warnings，425.71秒、43fresh guards＋2audit REUSED_NOT_FRESH、24smoke及Chat1241P。來源實作與驗證已通過最終審查（審查原件未入版控）；自動化以sandbox/mock為目標、歷史I/O NOT_PROVEN。

## 相容性與回復

保留既有 terminal UI、broker 事件、內部 self-approval 與錯誤 envelope；普通額外 `command` 不再變更授權規則。
已知例外名的非標準 callable 必須在可信註冊端被明確設計與審查，不能依名稱自動取得特權。
來源回復只反套本批相對「HEAD＋前次 44 檔」的精確差異，不使用 HEAD 還原覆蓋前次成果。
沒有 commit、release、正式映像或 live 變動，無現場資料回復操作。

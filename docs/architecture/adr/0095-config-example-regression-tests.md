# ADR-0095: 設定範本回歸測試（config drift 防線，Guard #34）

**狀態 (Status):** ✅ 已接受（範圍擴充：_MODELED 建模 config 4→6（加 identity_map.json〔ADR-0147〕/group_whitelist.json）＋Guard #34 增 errors.json 結構/parity 檢查（2026-07-02）；config drift 防線機制本身不變、僅列舉範圍過時）
**日期 (Date):** 2026-06-15
**決策者 (Deciders):** 專案擁有者 + 主程

## 上下文 (Context)

設定漂移稽核（config-drift-audit）挖出一批 inert／stale／drift 問題，並暴露**根因**：既有
33 道守衛與 1700+ 測試都聚焦在「執行中的程式行為」，而漂移大多住在「**不被執行的範本檔**
（`.example`）、被 gitignore 的 live 設定、與沒有行為的死設定」——這層從未納入 CI。

具體逃過檢查的類型：① 範本帶模型沒有的過時鍵（autonomy `.example` 的 `pulse_cooldown_seconds`／
`enable_heartbeat`）；② `server_config.json.example` 種出連不上的 MCP server（`touka_memory.server`、
`research_server.py` 皆不存在）；③ 範本與 schema 不同步。本 ADR 為「回頭補防線」的第一道。

## 決策方案 (Decision)

新增 `tests/unit/test_config_examples.py`，把「設定範本品質」納入 CI，並登記為 **Guard #34**
（`guard_all` 以 `pytest tests/unit/test_config_examples.py` 執行——快速 pre-commit 閘）。五項檢查：

1. **合法 JSON**：所有 `config/*.json.example` 可被解析。
2. **schema 驗證**：有 pydantic 模型的設定（`auth_policies`/`autonomy`/`permissions`/`sliding_window`），其 `.example` 通過 `model_validate`（型別/必填正確）。
3. **無過時鍵**：上述 `.example` 不得含「模型未定義的鍵」（meta 鍵 `version`/`author`/`description` 除外）——擋類型①。
4. **MCP 種子可解析**：`server_config.json.example` 的 `python` 啟動指令須指向真實存在的檔案／可匯入模組——擋類型②。
5. **env 平價（防線 #2）**：`ToukaProxy/.env.example` 文件化的每個 env 鍵須 ∈ `Settings` 欄位（或明列白名單 `_ENV_EXAMPLE_NON_SETTINGS`：`MEMORY_SYSTEM_SRC_DIR`、`TEST_*`）——擋「範本宣傳一個 `Settings` 沒有、會被 `extra="ignore"` 靜默丟棄的 env」（如曾經的 `RELOAD_MODELS_ON_REQUEST`／`FORCE_AGENT_ID`）。僅檢 `ToukaProxy/.env.example`（唯一有 pydantic `Settings` 的 env）；根/ToukaMemory/前端 env 無對應模型故不納入。

**實作為 pytest 測試而非獨立 guard 腳本**：檢查需 import 後端 pydantic 模型＋讀設定檔，
pytest 環境（conftest 設定 PYTHONPATH／處理 bootstrap）可乾淨執行，免去獨立腳本得自行
hack sys.path 與觸發 bootstrap 副作用的風險；再由 `guard_all` 以子程序執行該測試取得「守衛」可見性。

## 後果與影響 (Consequences)

- 範本漂移（過時鍵、malformed、壞 MCP 種子）會在 CI/pre-commit 當場被擋；guard 33 → **34**。
- **誠實記錄的限制**：本防線**不檢查**「模型有、範本卻省略的『有預設值』欄位」（如 `summary_model`）。
  帶預設的欄位省略是合法常態，強制列舉會大量誤報，故此類仍靠人工/審查（與稽核當時的判斷一致：
  inert/missing-default 是公認難以靜態守衛的角落）。
- **不涵蓋 `.example` ↔ live 比對**：live 設定 gitignored、CI 全新 checkout 上不存在，無法比對——
  此為結構性限制，非疏漏。
- 後續可選防線（本 ADR 未做）：全新安裝端到端冒煙（已部分由 `test_env_bootstrap` ＋本測試的種子檢查涵蓋）、
  `.env.example` ↔ `Settings` 平價守衛。

## Action Items
1. [x] `tests/unit/test_config_examples.py`：五項檢查（valid JSON／model_validate／no stale keys／MCP 種子可解析／`.env.example`↔`Settings` 平價＝防線 #2）。
2. [x] `guard_all.py` 登記 **Guard #34**（執行該測試）＋ docstring 33→34。
3. [x] ADR-0095 ＋ `adr/README`；`README_HANDOVER`／`CHANGELOG`／`tests/README` 同步（guard 34、pytest 計數）。

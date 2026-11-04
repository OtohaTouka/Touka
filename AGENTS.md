> 本檔是給 AI 程式代理人（例如 Codex、Claude Code）與貢獻者的專案規則；個人的溝通偏好不放在這裡。

## DATA_PROTECTION（資料保護）
嚴禁開發者或代理人以任何形式自動化修改、覆寫或「管理」位於 data/下的資料，僅限使用者手動維護和擁有者更改。

開發者與代理人沒有必要時不讀取 data/；不小心讀到或搜尋到不算違規，照實回報即可。

**既有 Owner 產品授權界線**：以上限制開發代理直接修改／管理實體資料；Owner 已授權 Touka 正常 startup/runtime（含其受管初始化、migration、session、marker 與記憶寫入），屬產品行為。此區分不授權開發代理直接操作 data/ 或 soul/，也不授權任意 live probe；所有自動化測試仍一律 sandbox/mock，人工 probe 仍依下列狹義例外與當次授權。P11 歷史發布委託不構成永久 Git／部署授權。

**受管測試狹義例外（Owner 2026-08-14；ADR-0214）**：本例外只適用於經產品受管介面執行的測試／人工 probe；開發者與代理人仍不得直接修改、清理或管理 `data/`。

- 測試可以讀取實體資料，但必須能證明完整路徑為**物理唯讀**；初始化、migration、index／cache／lock／log 建立皆算寫入。不能證明時，必須改用 sandbox／mock，或停下詢問 Owner。
- 實體寫入只允許精確的 `DebugAgent` 邏輯範圍，且只限人工啟動的 live probe：由 `AgentIdentity("DebugAgent")` jail 解析出的專屬路徑、帶強制 `agent_id="DebugAgent"` predicate 的共享資料列，以及精確 `memory_DebugAgent` collection。全域 config／setting、schema／migration、registry、其他 Agent 與任何 `soul/` 寫入都不在例外內。
- `pytest`、Vitest、CI、guard 與 `run_all_smoke_tests.py` 的全部自動化測試一律使用 sandbox／mock；不得套用實體讀取或 `DebugAgent` 寫入例外。
- `DebugAgent`-scoped 表示可拋棄的受管測試資料，不等於 filesystem sandbox，也不授權代理人自行檢查或清理既有資料。

## 程式碼修改規則
- 在修改任何 Python 檔案後，必須檢查**型別標註（Type Hinting）**的匯入：
- 確認 List, Dict, Any, Optional, Union 等是否已從 typing 正確匯入。
- 若系統環境有安裝 ruff、ESLint 或其他語法檢查工具，交付前必須執行一次靜態檢查，確保沒有 undefined name（未定義名稱）報錯。
- 嚴禁在交付代碼後，讓 Docker 容器因為語法或匯入錯誤而陷入重啟循環（Restart loop）。

## SOLID 重構守則
- **前端元件拆分 (SRP & OCP)**: 嚴禁建立「上帝元件」(God Components，如超過 20 個 states 的單一檔案)。React 視圖應拆分為多個領域專屬的小元件，並透過設定檔或註冊表 (Registry) 達成 OCP，新增分頁或功能時不應修改核心佈局代碼。
- **資料層抽離 (DIP)**: UI 不應直接與 API 綁定，應將 `fetch`、`localStorage` 操作封裝成領域專屬的 Custom Hooks (如 `useXxxSettings`)。
- **依賴反轉 (DIP)**: 在後端優先使用 `typing.Protocol` 定義合約。高層邏輯不應直接依賴具體實作，應依賴抽象介面。
- **匯入規範 (Import SSOT)**: 嚴禁在方法或函數內部使用區域匯入 (Local Imports)。所有依賴必須在模組頂部定義，以確保測試時可精確進行 Patch 與 Mock。

## SSOT原則
- **禁止使用硬編碼**: 嚴禁在代碼中出現硬編碼的磁碟代號 (如 `D:/`, `C:/`) 或容器絕對路徑 (如 `/app/data`)。
- **動態解析優先**: 所有路徑必須透過 `get_app_data_dir()`、`ResourceManager` 或基於 `Path(__file__)` 的動態探測獲取。
- **查詢與諮詢**: 如果沒有確定的 SSOT 來源，可以查詢 SSOT 文件，或是詢問使用者。

## TDD 與 測試主權 (TDD & Test Sovereignty)
- **測試優先**: 增加功能或修改錯誤時，必須先建立/執行測試。
- **測試主權**: Agent 在修改核心代碼後，具備執行測試並確保 **100% Green** 的義務。
    - **後端 (ToukaProxy/Memory)**: 使用 `pytest tests/`。
    - **前端 (ToukaChat)**: 使用 `npm run test` 或 `vitest`。
- **沙盒隔離**: 所有自動化測試必須遵循 `conftest.py`、Vitest Mocks 或 standalone smoke sandbox，禁止繞過沙盒直接讀寫實體 `data/`。人工 live probe 僅能使用本檔 `DATA_PROTECTION` 的受管狹義例外，且仍須逐次取得人類同意。

## 以上不可以更改，如需更改，必須徵求使用者同意。
---
## 🔄 文件協議：任務前讀 (a) → 任務中查 (b) → 任務後更新 (c)

每個任務的三個階段都有對應的文件動作（對齊標準開發循環）：
- **(a) 任務前讀**：見下方「任務前文件閱讀協議」——動手前先讀對的文件。
- **(b) 任務中查**：實作過程中遇到 SSOT / 介面 / 架構疑問，**立即查對應文件或詢問使用者，嚴禁自行假設**；文件有衝突或不清楚時當場提出。
- **(c) 任務後更新**：見下方「任務後文件更新協議」——改動完成後同步更新文件，並由守衛執法。

**ToukaDev 流程問題紀錄**（只適用於使用 ToukaDev 開發方法論 Skill 的維護者；外部貢獻者不需要。這是開發方法論的 Skill，不是產品的 `config/skills/`）
- 流程或規則出問題時（照做或沒照做而出重大錯誤或差點出事、問題到合併後或由人類才發現、人類表示流程造成困擾或對同一件事第二次糾正、某個步驟花了成本卻沒有作用），記錄到主要專案資料夾的本機 `.toukadev/lessons/`，格式依 improvement-loop Skill；紀錄不是授權，依紀錄修改規則只能經人類啟動並逐條核准的改進任務進行。

---

## 📋 任務前文件閱讀協議 (a — Pre-Task Reading Protocol)

接到任務後，先按類型確認需要閱讀的文件：

| 任務類型 | 必讀文件 |
|---------|---------|
| **所有任務** | 本檔 **§18 開發禁令**（禁令 1–68，全文或一行索引）＋ `HANDOVER.md`（當前交班現況） |
| **開工前、交付前、審查時** | 跑 `python scripts/applicable_rules.py`（計畫階段加 `--paths`，可以給資料夾），讀它列出的規則全文與交班檔 |
| 新增/修改端點或認證 | `docs/architecture/PROXY_INTERFACE.md` TL;DR 區塊 |
| 安全弱點修復 | `SECURITY.md`、`docs/architecture/LEGAL_ARCH.md` |
| 記憶/工具/I/O | `docs/architecture/MEMORY_INTERFACE.md` TL;DR |
| 心跳/排程 | `docs/architecture/HEARTBEAT_INTERFACE.md` TL;DR |
| 前端（Chat） | `docs/architecture/CHAT_INTERFACE.md` TL;DR |
| SSOT 疑問 | `docs/architecture/SSOT_MASTER_MAP.md` |
| 架構決策疑問 | `docs/architecture/adr/README.md`（影響範圍欄） |

> 介面文件的 TL;DR 區塊在每份文件的第一段，閱讀 3~5 行即可獲得關鍵約束。

## 📝 任務後文件更新協議 (c — Post-Task Update Protocol)

依變動類型，完成任務後必須更新以下文件：

| 變動類型 | 必須更新 |
|---------|---------|
| 新增 `# noqa: RULE_ID` | `docs/architecture/NOQA_REGISTRY.md` 對應統計數字（Guard #19 自動驗證） |
| 修復安全弱點 | `SECURITY.md` Known Resolved Vulnerabilities 表 |
| 新增/修改 FastAPI 端點 | `openapi.json`（`python scripts/update_openapi.py`） |
| 架構層變動（新 SSOT、新守衛等） | 在 `docs/architecture/adr/` 新建 ADR，並更新 `adr/README.md` |
| 配給 ADR 編號（在程式/測試/文件引用 `ADR-NNNN`） | **ADR 編號主權**：必須**同步**建立 `docs/architecture/adr/NNNN-*.md` 並補 `adr/README.md` 索引列；編號連續無跳號，**嚴禁**引用尚未建檔的編號（Guard #28 `guard_adr_integrity.py` 自動執法） |
| API 行為變化（影響前端整合） | 對應 `*_INTERFACE.md` 的相關章節 |
| 重大功能（影響禁令或核心守則） | `AGENTS.md` 對應章節與 `HANDOVER.md` 對應章節；若是歷史記錄則寫入 `docs/CHANGELOG.md` |
| 新增測試檔 | `tests/README.md` 加一列（檔名、說明；不寫案例數，ADR-0278） |

> ⚠️ 直譯器（3.12 venv）與 Windows 本機前置條件（Node／npm 版本、前端依賴、已知環境跳過）：全文見 docs/dev-environment.md。

**每次任務完成後必須執行（先啟用 3.12 venv；佈建見 docs/dev-environment.md——`uv sync` 必帶 `--all-packages`）：**
```powershell
.\.venv\Scripts\Activate.ps1
python scripts/guard_all.py   # 全綠（目前 44 guards；#42／#43 已退役）
python -m pytest tests/       # 全綠
```

**修改 ToukaChat 前端時額外執行：**
```bash
cd ToukaChat && npm run test  # vitest auth tests
```

**改到 ToukaChat 或會影響畫面的後端行為時再跑實際驗證（真前端、真後端、假模型；全在沙盒、不花額度；ADR-0295）：**
```bash
cd ToukaChat && npm run test:e2e:real  # 用到埠 4901～4904；截圖在系統暫存的 touka-e2e-real/shots
```

**修改 ToukaIDE 時額外執行（compile／package 必須串行，避免共寫 `dist/`）：**
```powershell
cd ToukaIDE
npm ci --ignore-scripts
npm test
npm run typecheck
npm run compile
npm run package
```

## 🚧 開發禁令清單

### 18. 法規即代碼 (Legal Architecture, LaC) ⭐
*   **靜態守則執行 (Automated Mandate Enforcement)**：系統將抽象的架構守則轉化為前端 ESLint 與後端 Ruff 法律。ESLint/Ruff 在 Touka 中不僅是風格檢查，更具備「主權法官」地位。
> 開發禁令清單的說明與資訊安全主題摘要全文：docs/rules/README.md。編號是穩定識別碼，只往後追加、不得重編，也不得繞過或弱化任何一條。
> 每條一行：標〔全系統〕〔退役〕的就是全文；有「全文：」的全文在 docs/rules/，摘要以全文為準；標〔模組〕的適用範圍看 `scripts/applicable_rules.py`。
> Codex 只讀本檔前 32 KiB：禁令清單之後的全系統小節（§3、§4 時間、§5、§7、§11、§12、§15）要另外讀完本檔。

*   **禁令 1**〔模組：memory、proxy〕：記憶通訊維持同行程整合，除非使用者明確要求，不得改成 MCP。全文：docs/rules/memory.md
*   **禁令 2**〔模組：proxy〕：soul_service 不得暴露路徑，走 resource_manager。全文：docs/rules/proxy.md
*   **禁令 3**〔模組：memory〕：改 Memory 工具要同步 tools/__init__.py 導出清單。全文：docs/rules/memory.md
*   **禁令 4**〔全系統〕：嚴禁繞過 `AuthService` 使用原生 `bcrypt` 或 `passlib`。必須維持 `SHA256 -> Bcrypt` 的預雜湊鏈以相容長密碼。
*   **禁令 5**〔模組：proxy、memory〕：工具執行器與工具都要傳 auth_level，不得忽略。全文：docs/rules/proxy.md
*   **禁令 6**〔模組：memory〕：Memory 的 I/O 走 get_resource_proxy，不直接匯入。全文：docs/rules/memory.md
*   **禁令 7**〔全系統〕：**嚴禁**在業務代碼中直接調用 `print()`，必須使用 `logger = logging.getLogger(__name__)`。腳本/CLI 工具（`scripts/` 目錄）除外。（PRINT_BAN 規則）
*   **禁令 8**〔模組：proxy、memory、heartbeat、dispatch、deploy、tests、ide〕：basicConfig 限 logging_config（lint 會擋；noqa、白名單能躲過檢查，但仍算違規）。全文：docs/rules/proxy.md
*   **禁令 9**〔模組：chat〕：前端不直接碰 localStorage，用 useLocalStorage。全文：docs/rules/chat.md
*   **禁令 10**〔全系統〕：**嚴禁**在業務代碼中用 `raise ValueError("字串")` / `raise RuntimeError("字串")` 帶硬編碼字串，必須改用 `raise ToukaException(ErrorCode.XXX)`。ToukaMemory 等跨服務邊界可加 `# noqa: RAISE_STRING_BAN`。
*   **禁令 11**〔模組：proxy、memory、heartbeat、dispatch、deploy、tests、ide〕：bcrypt、sqlite3 只准授權檔匯入（lint 會擋；noqa、檔名能躲過檢查，但仍算違規）。全文：docs/rules/proxy.md
*   **禁令 12**〔全系統〕：**嚴禁**在新增 `# noqa: RULE_ID` 後不同步更新 `NOQA_REGISTRY.md` 的統計表。`guard_noqa_registry.py`（Guard #19）會自動驗證，不一致時 CI 失敗。
*   **禁令 13**〔模組：chat〕：前端生產程式不得直接 console，用 logSSOTError。全文：docs/rules/chat.md
*   **禁令 14**〔模組：memory、deploy〕：ToukaMemory 不得匯入 ToukaProxy 內部，只走依賴反轉介面。全文：docs/rules/memory.md
*   **禁令 15（密鑰主權）**〔全系統〕：**嚴禁**在 `config.py` 或任何位置寫入公開已知的硬編碼密鑰預設值（如 `"touka_*_secret_2026"`）。歷來公開過的值列在 `config.PUBLIC_SECRET_VALUES`（`ToukaProxy/src/config.py`）。`Pydantic Settings` 的 `TOUKA_AUTH_SECRET` / `DISPATCH_TOKEN` / `HEARTBEAT_TOKEN` 預設**必須**為空字串；持久密鑰於**首次需要時隨機生成並持久化**——`AuthSecretManager`（TOUKA_AUTH_SECRET）、`DispatchSupervisor`（DISPATCH_TOKEN）與 `config._resolve_heartbeat_token()`（HEARTBEAT_TOKEN，proxy 消費端於 config 載入尾聲；因 consumer 以 `from src.config import HEARTBEAT_TOKEN` 綁常數，故須在載入尾聲完成）皆經 `config.persist_generated_secret()` 寫 `.env`；heartbeat 客戶端另有 `StorageService`（config.json）為備援解析。皆受 **`TOUKA_BOOTSTRAP_ENABLED` 閘**保護（測試/沙盒 False→不寫、退回臨時），跨重啟穩定、免精靈。⚠️ docker：heartbeat 容器**勿**用 `environment: HEARTBEAT_TOKEN=${...}` 覆寫（解析期鎖成空、壓過 env_file）——改由 `env_file: .env` 於容器建立期讀 proxy 已生成值（Guard `test_heartbeat_token_not_overridden_in_compose` 執法）。（ADR-0033 第 1 道守衛）
*   **禁令 16（身份主權）**〔全系統〕：沒登入一律拒絕，任何標頭都不能變成身分。**嚴禁**在認證失敗或沒帶憑證時讀取 `X-Agent-ID` Header（或任何客戶端控制標頭）作為 `agent_id` 來源，也**嚴禁**改用訪客或其他替代身分放行。聊天端點（`proxy.py` 的 `/messages`、`/chat/completions`，含 `/v1`）必須掛授權閘道 `require_authenticated`，沒有已驗證身分即 401、不得進 pipeline。首次安裝前的本機領卡（LDH）是唯一例外，範圍依禁令 60，不得放寬。跨子系統信任邊界（SNS Webhook）的事件物件必須有 `is_verified` 欄位。（ADR-0033 第 2 道守衛；ADR-0245）
*   **禁令 17（結構化邊界）**〔全系統〕：路徑檢查**嚴禁**使用 `str.startswith()` —— 必須用 `Path.is_relative_to()`（避免 sibling-directory bypass）。動態生成 Python/Shell 程式碼**必須**用 `json.dumps()` 對任何外部輸入轉義。從磁碟讀取的設定檔**必須**透過 Pydantic `model_validate()` 並含 `@field_validator`。（ADR-0033 第 3 道守衛）
*   **禁令 18（跨切面安全守衛）**〔全系統〕：新增的跨切面安全邏輯（CSRF、Rate Limiting、內容消毒、簽章驗證等）**必須**抽出為純函式至 `ToukaProxy/src/utils/` 或 `ToukaMemory/.../utils/`，可獨立單元測試；端點層只呼叫不重複實作。資源上限**必須**以模組級命名常數定義（如 `MAX_PENDING_APPROVALS`），禁止 magic number。（ADR-0033 第 4 道守衛）
*   **禁令 19**〔模組：proxy〕：改 get_clean_env 要同改黑名單與移除清單，高危變數無條件移除。全文：docs/rules/proxy.md
*   **禁令 20（安全文件同步）**〔全系統〕：每修復一個新的安全弱點，**必須**同時更新 `SECURITY.md` 的 Known Resolved Vulnerabilities 表、相關 `*_INTERFACE.md`（若涉及 API 行為變化）、`SSOT_MASTER_MAP § 3-K`（若新增 SSOT 元件）。每個 CVE 編號對應一個 `test_*_security.py` 或 `test_batch*.py` 模組（TDD-Red 先行）。
*   **禁令 21（並發鎖定主權）**〔全系統〕：對 JSON 文件（`users.json` 等）進行 read-modify-write 操作**必須**透過 `_atomic_users_write(mutate_fn)` 或等效的鎖定原語，**嚴禁**直接呼叫 `_write_users()` 或其他裸寫入方法。`user_service.py` 的所有使用者修改方法**必須**透過 `AuthService._atomic_users_write()` 執行（N4/N11 TOCTOU 修復）。
*   **禁令 22（HTTP 重定向防護）**〔全系統〕：所有使用 `httpx.AsyncClient` 或 `requests.Session` 的業務代碼**必須**明確設定 `follow_redirects=False`，除非有明確的白名單理由。預設允許重定向可能被攻擊者利用為 SSRF 重定向鏈（N8 修復）。
*   **禁令 23（敏感資訊消毒）**〔全系統〕：`_connection_errors` 等存儲連線失敗原因的 dict，存入前**必須**先呼叫 `_sanitize_connection_error()` 消毒。嚴禁將含有 `Bearer`、`sk-*`、`key=`、`password=` 等模式的原始 exception 字串存入任何可經 API 存取的結構（N7/N9 修復）。
*   **禁令 24（SQL 結構安全）**〔全系統〕：SQL 查詢的結構（包含 `IN` 子句的 placeholder 數量）**必須**在開發時固定為字面量，嚴禁透過 f-string 或字串拼接動態組裝。若查詢的 IN 子句數量確定（如永遠為 2 個），直接寫 `IN (?, ?)`（N12 修復）。
*   **禁令 25（端點認證守衛強制）**〔全系統〕：所有新建或修改的 FastAPI route handler，若非在 `endpoint_auth_allowlist.py` 豁免清單中，**必須**在函式簽名中加入至少一個認證 Depends（`get_verified_identity`、`_require_*_role()` 或 `verify_heartbeat_token` 等）。**嚴禁**新增項目至 `GRANDFATHER` 清單（清單永久維持空）——違規時 Guard #24 在 `guard_all.py` 中自動攔截（ADR-0034）。
*   **禁令 26**〔模組：proxy、heartbeat〕：awaken 驗 token 只准 verify_heartbeat_token。全文：docs/rules/proxy.md
*   **禁令 27（來回計算 SSOT）**〔全系統〕：全系統計算「對話則數」**必須**呼叫 `ToukaProxy/src/utils/history_manager.py` 的 `count_rounds_from_linear()`（後端）或 `ToukaChat/src/lib/export-utils.ts` 的 `countRounds()`（前端）。嚴禁在業務邏輯中自行實作 user 計數或節點計數作為「則數」替代品。（ADR-0036）
*   **禁令 28**〔模組：proxy〕：滑動視窗只能回合結束後、兩個授權點觸發，且經每對話鎖。全文：docs/rules/proxy.md
*   **禁令 29**〔模組：memory、dispatch、proxy〕：DispatchAgent 看板走 MissionManager 與能力校驗。全文：docs/rules/memory.md
*   **禁令 30**〔模組：memory、dispatch、proxy〕：看板狀態走 FSM，不得跳過 DOING，DONE 後不能再改。全文：docs/rules/memory.md
*   **禁令 31**〔模組：memory、dispatch、proxy〕：FAILED 狀態只有監督能力或特權身分能重設。全文：docs/rules/memory.md
*   **禁令 32**〔模組：memory、dispatch、chat、proxy〕：artifact_ref 與 artifact_refs 分開存，都要防路徑穿越。全文：docs/rules/memory.md
*   **禁令 33**〔模組：dispatch〕：DISPATCH_TOKEN 為空時 Dispatch 拒絕全部請求。全文：docs/rules/dispatch.md
*   **禁令 34 (禁止恢復 FORCE_AGENT_ID)**〔全系統〕：**嚴禁**在任何服務中恢復 `FORCE_AGENT_ID` 環境變數機制或任何形式的「憑證缺失時自動注入能力」邏輯。所有身份解析**必須**來自 `identity_map.json`（SSOT），存取控制不得依賴外部環境變數強制覆蓋。（ADR-0046 C1 修復）
*   **禁令 35**〔模組：memory〕：任務轉 BLOCKED 必填 blocker_detail，缺了就拒絕。全文：docs/rules/memory.md
*   **禁令 36**〔模組：memory〕：Task 模型保持 validate_assignment=True。全文：docs/rules/memory.md
*   **禁令 37**〔模組：proxy、memory、heartbeat、dispatch、deploy〕：服務層與路由層只用 AsyncFileLock（lint 會擋；noqa 能躲過檢查，但仍算違規）。全文：docs/rules/proxy.md
*   **禁令 38 (環境感知 .env 互斥載入)**〔全系統〕：嚴禁破壞或繞過 `resolve_env_file_path` 與 `resolve_heartbeat_env_file_path` 的環境感知互斥載入設計。整合執行時必須強制只讀取根目錄 `.env`，分開獨立執行時必須強制只讀取子專案目錄下的 `.env`，不得混淆加載或相互踩踏覆蓋。**讀寫 `.env` 一律經 `resolve_env_file_path()`（SSOT），嚴禁硬編碼相對 `.env` 路徑**（root-cause-A：`routers/config.py` 曾以裸 `".env"` 寫入，docker 下落在容器暫存層而非 compose 讀的宿主檔）。部署可設 **`TOUKA_ENV_FILE`**（最高優先，凌駕整合/獨立偵測）指向「持久化且被真正載入」的 .env——docker compose 已改設 `TOUKA_ENV_FILE=/app/host_root/.env`，並以穩定掛載 `./:/app/host_root`（恆為 compose 目錄、含 `env_file` 載入的同一份 `.env`）**與 `WORKSPACE_ROOT` 解耦**（舊 `/app/workspace/.env` 綁 `${WORKSPACE_ROOT}`，owner 設非預期值〔如 `D:/`〕時三 token 會寫進幽靈檔）；`.env` 須為目錄掛載內普通檔（`set_key` 的 temp+rename 覆寫需之），另有 `verify_env_persistence_integrity` 幽靈檔/生效探針守衛。（ADR-0052 amended by ADR-0151）
*   **禁令 39**〔模組：proxy〕：fetch_gemini_models 用正規式比主版本 ≥2.0，不寫死名單。全文：docs/rules/proxy.md
*   **禁令 40 (ToukaDispatch Token 隔離)**〔全系統〕：**嚴禁**將 `DISPATCH_TOKEN`（全域主金鑰）傳遞給任何外部 AI 工具或超出 Touka 進程邊界的任何通訊通道。外部 AI 工具只能收到由 `dispatch_initialize_mission` 生成的 `mission_token`（一次性，與 `mission_id`／generation 綁定，首次成功 handshake 即消耗，新 mission 建立時舊 credential/session 原子失效）。Token 必須透過**環境變數**傳遞（`DISPATCH_TOKEN=<mission_token>`），嚴禁放進 prompt 明文或命令列參數（避免 `ps` 可見）。`DISPATCH_URL` 必須是完整 modern `/mcp` endpoint；application session 不可跨 `/mcp`／`/sse` transport 使用。外部 session 不得呼叫 `dispatch_initialize_mission`，違反時系統立即拒絕。（ADR-0064 + ADR-0201）
*   **禁令 41（物理路徑禁令 / TID251）**〔全系統〕：**嚴禁**在代碼中寫死任何絕對路徑字串。所有路徑必須透過 `ResourceManager` 解析。**由 `lint_ssot.py` 的 `PATH_LITERAL_SSOT` 規則與 ruff TID251（`ruff.toml` 的路徑 API 禁用，Guard #44）靜態攔截。**（ADR-0004）
*   **禁令 42（環境變數主權 / Env SSOT）**〔全系統〕：業務邏輯**嚴禁**直接調用 `os.environ`、`os.chdir`、`sys.path.append` 等敏感 API；子程序一律經 `services/execution_broker.py`（`spawn_exec`／`run_sync`，ADR-0239），`os.system`／`subprocess.run`／`subprocess.Popen` 由 ruff TID251（Guard #44）攔截，`tests/lint/test_security_kernel_ratchet.py` 另禁止在 broker 以外直接啟動子程序；前端**嚴禁**直接調用 `process.env` 或 `import.meta.env`，所有配置必須統一經由 `src.config`（後端）或 `src/lib/config.ts`（前端）存取。特權白名單僅限 `config.py`、`main.py`、`bootstrap.py`、`terminal.py`、`conftest.py`。**由 `lint_ssot.py`（後端）與 `guard_env.ts`（前端）進行靜態稽核。**（ADR-0015）
*   **禁令 43（時間獲取禁令 / TIME_BAN）**〔全系統〕：**嚴禁**調用 `datetime.now()`、`time.time()` 或 `datetime.today()`，必須統一使用 `time_utils`（後端）/ `getSystemEpoch()`（前端），內部一律以 UNIX Epoch 為基準。**由 `lint_ssot.py`（AST 級）與 `guard_time.ts` 強制攔截。**（ADR-0013）
*   **禁令 44（錯誤碼主權 / Error Code Sovereignty）**〔全系統〕：嚴禁在 Exception 或 UI 提示中使用硬編碼字串——後端**嚴禁** `HTTPException(detail="...")`（補禁令 10 的 raise 字串禁令）；前端**嚴禁** `toast.error("...")` / `console.error("...")` 硬編碼內容。`errors.json` 為跨端唯一錯誤定義來源，自動生成雙端常數。**由 `lint_ssot.py`（後端）與 `guard_errors.ts`（前端）攔截。**（ADR-0014）
*   **禁令 45**〔模組：chat〕：前端 HTTP 一律走 fetchTouka，不得直接 fetch()。全文：docs/rules/chat.md
*   **禁令 46（死碼禁令 / Dead Code Guard）**〔全系統〕：**嚴禁**未使用的函式、export 或檔案長期存在。**後端由 `vulture`、前端由 `knip` 偵測，整合至 `guard_all.py` 第 20 道關卡。**
*   **禁令 47**〔模組：deploy〕：成員依賴與 Dockerfile 要同步，不得手動白名單 COPY。全文：docs/rules/deploy.md
*   **禁令 48**〔模組：proxy、chat〕：refresh Cookie 設 path=/，前端帶 credentials。全文：docs/rules/proxy.md
*   **禁令 49**〔模組：chat〕：前端視覺樣式出自 tokens.ts 或系統定義，不用任意值。全文：docs/rules/chat.md
*   **禁令 50**〔模組：chat〕：前端不得直接用未封裝的底層 UI 元件（如 Radix）。全文：docs/rules/chat.md
*   **禁令 51**〔模組：chat〕：前端不得手動重複宣告領域模型，型別來自後端生成合約。全文：docs/rules/chat.md
*   **禁令 52**〔模組：proxy、deploy〕：MCP、Skill 創建只走創建引擎；端點要角色把關，CREATE 不覆寫。全文：docs/rules/proxy.md
*   **禁令 53**〔模組：deploy〕：Python 依賴以單一 uv.lock 為準，不手改、不用 >=、版本全對齊。全文：docs/rules/deploy.md
*   **禁令 54（Skill Hub 同步來源主權）— 🗑️ 已退役（2026-06-07）**〔退役〕：persona Skill Hub 子系統已移除；此編號只保留退役墓碑，不重新啟用舊 Hub 規則。技能探索與安裝由禁令 55／ADR-0074 承接；舊規則見 ADR-0072。
*   **禁令 55**〔模組：proxy、chat、deploy〕：真技能只裝 catalog 內已審且釘 commit 的來源，不收自填 URL。全文：docs/rules/proxy.md
*   **禁令 56**〔模組：chat〕：側欄折疊狀態只由 page.tsx 持有，LeftSidebar 不自存。全文：docs/rules/chat.md
*   **禁令 57**〔模組：proxy〕：聊天側剪裁要非阻塞、排除無痕與子對話、經鎖，失敗只記 log。全文：docs/rules/proxy.md
*   **禁令 58**〔模組：chat〕：前端使用者可見字串要走 t()，翻譯目錄是唯一來源。全文：docs/rules/chat.md
*   **禁令 59**〔模組：proxy、memory〕：工具失敗要結構化且可見，過濾要說明，政策未載入預設 DENY。全文：docs/rules/proxy.md
*   **禁令 60**〔模組：proxy、chat〕：LDH 只限完全沒帶 Authorization 的本機請求，壞卡一律 401。全文：docs/rules/proxy.md
*   **禁令 61（測試資料與計量 API 主權）**〔全系統〕：任何會或可能向外部供應商發送請求並消耗 token、credit、quota 或費用的 test／eval／診斷 probe，**嚴禁**納入 pytest、Vitest、`run_all_smoke_tests.py`、CI 或 guard。若 mock 無法判定而必須 live 診斷，代理人必須先停止，揭露精確命令、script、provider、model、case／retry 數、budget 或 token 上限、送出內容類型與本機寫入範圍，取得該次或明列 bounded batch 的人類同意後才可執行。方案核准、既有 API key、環境變數、YOLO、錯誤發生或先前同意都不構成本次核准；未知是否計量時按「會計量」處理，禁止未核准的 fallback／retry。（ADR-0214）
*   **禁令 62（MCP rollout qualification 與 release evidence 主權）— 🗑️ 已退役（2026-10-08，ADR-0238）**〔退役〕：發版資格驗證、release evidence、seal／receipt 與 owner-local 發行機制已整體移除；此編號只保留墓碑，不重新啟用。映像發行改由 `.github/workflows/ghcr-publish.yml` 直接建置推送。
*   **禁令 63（臨時查驗沙盒限定 / Ad-hoc Probe Sandbox）**〔全系統〕：臨時查驗（`python -c`、臨時腳本、REPL 匯入等）**嚴禁**在測試沙盒外載入專案程式（`src`、`memory_server`、ToukaHeartbeat、ToukaDispatch；含透過 `scripts/`、`tests/` 模組間接匯入）——**載入時的初始化也算寫入**（例如 bootstrap 由 `.example` 自癒寫出設定、首次需要時生成密鑰並寫入 `.env`、建立 index／cache／lock／log）。需要看值或驗證行為時，**必須**改寫成沙盒內的測試（`conftest.py` 沙盒或 standalone smoke sandbox）。經受管介面、取得當次同意的人工 live probe 不算臨時查驗，另依 `DATA_PROTECTION`。（ADR-0243）
*   **禁令 64**〔模組：proxy、chat、ide〕：底線寫死在程式，審查員與 YOLO 都不能推翻，審查失敗不放行。全文：docs/rules/proxy.md
*   **禁令 65**〔模組：proxy、chat〕：只有擁有者或管理員能插話，插話不能核准也不能改模式。全文：docs/rules/proxy.md
*   **禁令 66**〔模組：isolation、proxy、deploy、chat、tests〕：代理人撰寫或指定的程式一律經隔離 runner 啟動，不可用就不執行。全文：docs/rules/isolation.md
*   **禁令 67（Hook / ADR-0292）**〔模組：hooks、proxy、tests〕：①hook 只能擋下或要人確認：**嚴禁**採用 `allow`、`updatedInput` 一類放行或改寫的欄位，hook 也**不能**核准審批卡、推翻底線或改寫已核准的參數；PreToolUse 排在政策判定之後，hook 的「要人確認」不送審查員、無人在場直接拒絕，Auto 與 YOLO 都不能代替人。②`hooks.json` 只經 `PUT /v1/hooks` 改（人類 admin、revision 比對、CSRF Origin），**嚴禁**放進設定匯出／匯入；代理人用終端讀寫屬底線 F2（http 標頭可能有權杖；檔案動詞與 MCP 不經底線，靠④的專案根限制）。③command hook **嚴禁**經 shell：只以程式＋參數清單經 `execution_broker.spawn_exec` 啟動，事件內容只從標準輸入給，環境不含金鑰；http hook 不跟隨轉址、有逾時與大小上限，標頭照字面送出、不替換環境變數。④參數裡的路徑必須在設定資料夾的 `hooks/`，程式在別處時**嚴禁**指到代理人寫得到的地方（`services/hooks/paths.py`），存檔與每次執行前都檢查，讀不到狀態一律拒絕；一般使用者的專案根**嚴禁**放在 Touka 保護的位置（`isolation/profiles.project_root_reason`），代理人的專案根路上有連結時專案檔案工具一律拒絕（`profiles.has_link`）；代理人的檔案動詞開檔與走訪**嚴禁**跟隨連結（`memory_server/utils/no_follow.py`、`ResourceManager` 的 `*_no_follow` 方法），list、find、grep 不進入連結。⑤回給模型的 hook 輸出一律當資料（`<hook_output>` 區塊或 `hook_output` 鍵，先中和區塊 tag）。⑥hook 自己失敗或逾時不擋動作、但要記在執行紀錄；無痕只記時間、事件與結果。⑦UserPromptSubmit 擋下的內容**嚴禁**換條路進到模型或對話：插話進佇列前也要檢查，SNS 外人訊息被擋下時不寫進對話；hook 執行期間使用者停止時**不得**再送模型或執行工具；工具 hook 也要比對舊工具名稱對應的新名稱。由 `test_hook_paths_security.py`、`test_project_root_protected_security.py`、`test_file_ops_no_follow_security.py`、`test_hook_runners_security.py`、`test_hook_wiring_security.py`、`test_hooks_router_security.py`、`test_interjection_endpoint_security.py`、`test_sns_gateway.py` 執法。（ADR-0292）
*   **禁令 68**〔模組：isolation、memory、proxy、tests〕：代理人資料夾路徑只用 ZonePath、經 RM 區內方法，不跟隨連結。全文：docs/rules/isolation.md

## 🛡️ Touka系統安全與核心守則 (Core Mandates)

搬走的小節只留摘要，以全文為準。

### 1. 脈衝驅動架構 (Pulse-Driven Architecture)
〔模組：heartbeat、proxy〕Heartbeat 管排程，Proxy 只接 /awaken，不得加回排程。全文：docs/rules/heartbeat.md

### 2. 身分憑證化與能力的 SSOT 解耦 (Identity & JWT Authentication) ⭐
〔模組：proxy、memory、heartbeat、dispatch、isolation、chat〕JWT、AgentIdentity、RBAC、執行模式、底線與隔離層級。全文：docs/rules/proxy.md

### 3. 來回計算主權 (Round Count SSOT) ⭐
*   **定義**：1 round = 1 個完整的 user+assistant pair。跳過 system/nudge；末尾未配對 user 不計入。
*   **唯一 SSOT**：見禁令 27。
*   **前端對等**：見禁令 27。
*   **執法**：三處計算（`async_db.py` round_count、`legacy_store.py` message_count、`export-utils.ts` keepCount）已統一，不得各自為政。（ADR-0036）
*   **插話**：插話存在 AI 回覆裡，不算回合。（ADR-0249）

### 4. 時間主權 (Time SSOT) ⭐
*   **基準定址**: 全系統時間獲取操作必須通過 `src.utils.time_utils` (後端) 或 `src/lib/time_utils.ts` (前端) 的專屬窗口。
*   **底層標準**：系統內部邏輯運算與資料庫標記一律遵守 **UNIX Epoch Time (int/ms 或 float/s)**。
*   **執法機制**：由 `lint_ssot.py` (後端) 與 `guard_time.ts` (前端) 進行靜態檢查，嚴禁調用原生 `Date` 或 `datetime` API。
*   **呈現標準**：僅在 UI、日誌輸出或 Prompt 注入時轉換為台北時間格式。嚴禁在業務邏輯中處理時區偏移。

### 4. 訊息呈現與 UX 韌性 (UX Continuity) ⭐
〔模組：chat〕前端保留換行；確認型選單處理 onSelect，關閉時重置狀態。全文：docs/rules/chat.md

### 4. 角色交替原則 (Role Sequence)
〔模組：proxy〕多脈衝時 Assistant 回覆之間必須注入 User 提示。全文：docs/rules/proxy.md

### 5. 靈魂保護機制 (Soul Protection)
*   核心靈魂檔案受嚴格保護。禁止透過自動化工具篡改 `data/soul/{id}/` 下的設定。

### 6. SOLID 原則與 Middleware 結構
〔模組：proxy〕脈衝走 Middleware Pipeline，Context 由上層注入。全文：docs/rules/proxy.md

> 📋 歷史更新日誌請見 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

### 7. 資料結構與主權 (Data Structure & Sovereignty)
*   **路徑主權隔離 (Path Sovereignty Isolation) ⭐**：系統實施 **「預設監獄」** 原則。
    - **強制物件依賴**：所有 Vault 操作必須透過 `ResourceManager` 並傳遞 `AgentIdentity` 物件取得路徑。
    - **全能 I/O 代理 (Full I/O Proxy)** ⭐：所有物理檔案操作（讀取、二進位寫入、追加、CSV 管理）必須透過 `ResourceManager` 執行。嚴禁調用 Python 內建 `open()` 或 `os` 模組進行直接操作。
    - **物理路徑禁令 (Physical Path Ban) ⭐**：見禁令 41。
        - **動態錨點**：系統根目錄必須透過 `config.py` 的 `DEFAULT_WORKSPACE_ROOT` 或 `config_utils.py` 的 `get_workspace_root()` 動態獲取。
        - **環境適應**：代碼必須同時相容 Windows 與 Docker 環境，路徑拼接統一使用 `pathlib.Path`。
    - **戰術性豁免 (Tactical Suppression) ⭐**：在開發核心基礎建設時，若需合法繞過 SSOT 靜態攔截（如 I/O 監獄），嚴禁使用 AST 規避手段（如 `getattr`）。必須使用標準的單行註解 `# noqa: [RULE_ID]` 進行精確豁免，以保持代碼的 Python 慣例與可讀性。
〔模組：memory、proxy〕其餘子項（資料位置、身分糾偏、配額、資產路由、記憶庫）。全文：docs/rules/memory.md

### 8. 大一統工具鏈 (The Grand Unification of Tools) ⭐
〔模組：proxy、memory、dispatch、ide、deploy〕工具合約與授權、記憶整合、MCP modern-only、只用本輪列出的工具。全文：docs/rules/proxy.md

### 9. 跨環境通訊適配 (Cross-Platform Adapting) ⭐
〔模組：proxy、ide〕Agent 透過統一 Bridge 適配器與 Web、IDE 通訊。全文：docs/rules/proxy.md

### 10. 脈衝自主演化 (Pulse-Based Self-Evolution)

### 11. 系統穩定性與韌性 (Resilience & Self-Healing) ⭐
*   **配置自癒義務**: 系統支援自動引導 (Bootstrap)，遺失配置將由 `.example` 復原。Agent 嚴禁嘗試刪除這些自癒基準檔。
*   **併發原子性**: 所有檔案與資料庫操作均受異步佇列與物理鎖定保護，Agent 應優先使用標準 `vault_tools` 以確保資料完整性。
*   **路徑執法 (Path Enforcement)**：禁止直接存取 `soul_service.base_dir` 等屬性，所有路徑存取必須透過 `resource_manager` 解析。
*   **失效可觀察性與當機復原 (Failure Observability) — ADR-0078** ⭐：失效不沉默——工具/網路/API 錯誤與系統降級皆在介面或 agent 回覆中明示（降級時側欄常駐 `DegradedBanner`）；檔案寫入採**原子置換**（`tempfile`+`fsync`+`os.replace`）防寫到一半損毀、壞檔有 corrupt 告警；背景多脈衝迴圈當機後可於 `/v1/stats` 的 `interrupted_runs` 看見「上次 N 個未完成」並一鍵重新觸發（`POST /v1/heartbeat/retrigger`，強制 Auto、僅擁有者）。
〔模組：proxy、chat、memory、heartbeat、dispatch、ide〕其餘子項（ADR-0231 可靠性契約、設定檔讀不到不放寬、優雅降級）。全文：docs/rules/proxy.md

### 12. 群聊別名與身分解耦 (Group Alias & Identity Decoupling) ⭐
*   **介面合約主權與自動化同步 (Contract as Code)** ⭐：全系統對外 API 必須使用 `src/models/` 下定義的 Pydantic 模型。
    - **強型別驗證**：嚴禁處理未驗證的 `dict`。
    - **端到端同步**：後端模型為 SSOT。透過 `openapi.json` 自動同步至前端 TypeScript 型別 (`api.ts`)。**實施 `test_api_schema_sync.py` (後端) 與 `guard_frontend.ts` (前端) 確保模型與物理合約 100% 同步。**
    - **合約唯一性保護**：嚴禁在前端手動重複定義已存在於 API 合約中的領域型別，必須透過 `src/lib/types.ts` 的 Alias 進行 SSOT 引用。
〔模組：proxy、chat〕其餘子項（群聊別名、ID 糾偏）。全文：docs/rules/proxy.md

### 13. 自主預算大一統與優先級 (Unified Autonomy Budget) ⭐
〔模組：proxy、heartbeat、tests〕輪數由 AutonomyService 判定：請求指定＞全域設定＞保底 5。全文：docs/rules/proxy.md

### 13.5 工作分派中介身分與安全邊界 (AI Agent Dispatch & Safety Boundaries) ⭐
〔模組：dispatch、memory、proxy〕DispatchAgent、任務 FSM、token 委派、Skill 標準。全文：docs/rules/dispatch.md

### 14. 策略即程式碼 (Policy as Code, PaC) ⭐
〔模組：proxy〕安全策略模型強型別、決策放模型層、基於 PolicyContext。全文：docs/rules/proxy.md

### 15. 大一統路由治理 (Unified Router Governance) ⭐
*   **路由對等性 (Prefix Parity)**：系統核心路由統一支援 `/v1` 前綴與根路徑對等訪問。開發者必須將新路由器註冊於 `main.py` 的 `CORE_ROUTERS` 清單中。
*   **標準化裝飾器**：所有路由器必須使用 `APIRouter(prefix="/...", tags=["..."])` 定義，並在端點裝飾器中使用相對路徑，以確保掛載時的路徑精確性。
*   **無斜線韌性 (Trailing Slash Resilience)**：系統預設啟用 `redirect_slashes`，但代碼層級應優先使用無斜線結尾的定義，確保 RESTful 介面的一致性與 CORS 穩定性。

### 17. 測試基導大一統與虛擬診斷員 (Unified Testing & Virtual Debugger) ⭐
〔模組：tests、proxy〕測試收攏 tests/、DebugAgent 是診斷身分、smoke 全隔離。全文：docs/rules/tests.md

# Touka 系統測試與品質保證指南

本文件定義了 Touka 生態系（含 ToukaProxy 與 ToukaChat）的測試規範與目錄結構準則。

## 核心原則：測試主權 (Test Sovereignty)
1.  **測試優先**：任何功能更動或 Bug 修復必須伴隨對應的測試案例。
2.  **100% Green**：提交代碼前，開發者或 Agent 有義務確保全域測試通過。
3.  **大一統管理**：邏輯類與架構類的測試應盡可能集中存放，方便全域稽核。

## 本機驗證方式

執行優先使用專案既有的 `tests/`、Vitest、`scripts/guard_all.py`、型別檢查與建置入口。
需要補強時，先在專案內新增或修正測試、守衛、`conftest.py`、sandbox 或 mocks，保留行為斷言與檢查強度。

- **重用標準工具鏈**：啟用專案 Python 3.12 `.venv`，讓 guard 的子程序也使用它；
  Node/npm 依專案宣告驗證。先重用版本與 lock 相符的既有 runtime、安裝與 caches。
  必要的 `node_modules` 準備及既有產生器輸出屬標準本機驗證準備，不要求另建整份專案副本；
  舊版本安裝不能冒充新 lock。需同步 Python 時遵守 `uv sync --all-packages`，不得裸 `uv sync`。
- **只隔離必要資源**：測試資料、設定、憑證、快取與暫存產物沿用既有 sandbox／mock；
  原生驗證仍不得讀取、列舉、修改或清理實體 `data/`。若某步確實無法以現有介面安全隔離，
  先指出具體缺口，再採該步所需的最小隔離，不把整套外部環境作為預設前提。
- **pytest 暫存根故障**：保留原始fixture setup錯誤；可沿用pytest正式 `PYTEST_DEBUG_TEMPROOT`，先由既有 `validate_explicit_basetemp` 驗證新根為repo外且無redirect，再建立全新owned根。每次pytest（含nested呼叫）各自使用numbered leaf；不以全域固定 `--basetemp` 共用可刪除葉節點，不更動舊根ACL或重建環境；方法不放寬conftest資料sandbox。
- **保留有實際用途的隔離**：Guard #16 的 per-member venv、依賴快取與 Dockerfile parity
  用來驗證成員依賴完整性，仍須保留；ADR-0222 的 audit 暫存輸入與 credential-free child
  環境、ADR-0214 的 smoke sandbox 同樣保留。
- **分別記錄結果**：測試、守衛、建置與 network audit 各自回報 PASS／FAIL／NOT RUN；
  本機驗證不構成 audit、真 Docker、計量 API、commit、push 或 publish 授權。
  普通測試收集不得為了探測工具而啟動真 Docker；需要補強時在既有 mocks／整合測試入口處理。
  `guard_all.py --non-audit` 會把 Guard #22／#23 回報為 NOT RUN，不得改稱完整守衛全綠。

---

## 前端測試規範 (ToukaChat)

我們採用 **「混合式目錄結構」**，兼顧開發效率與架構整潔。

### 1. 目錄分配
| 類別 | 存放位置 | 說明 |
| :--- | :--- | :--- |
| **邏輯類 (Logic)** | `tests/unit/` | 包含 Hooks、Library、Context 等純邏輯運算。 |
| **元件類 (UI)** | `src/**/__tests__/` | 鄰近組件存放，便於維護 UI 樣式與組件交互。 |
| **守衛類 (Guard)** | `tests/guard/` | 靜態分析腳本、API Parity 檢查、時間主權 (Time SSOT) 等架構守衛測試。 |

### 2. 執行指令
```bash
# 執行所有測試
npm run test

# 僅執行集中化的單元測試
npx vitest run tests/unit

# 執行架構守衛 (Architecture Guards)
python scripts/guard_all.py            # 全域架構與 SSOT 守衛 (包含 API, Time, Design)
python scripts/guard_dependencies.py   # 依賴主權與 Docker 物理一致性守衛
```

---

## VS Code 擴充測試與打包（ToukaIDE）

ToukaIDE 的 build/test runner 固定使用 Node.js 24.22.1 LTS／npm 11.19.1、Vitest、
TypeScript 與 esbuild；extension host API 下限仍是 Node.js 20／VS Code 1.90，故
`@types/node` 保持 20.x。MCP P4-B 驗收不可只跑 compile；以下四步必須分開執行，
且 compile／package 不可並行寫同一個 `dist/`：

```bash
cd ToukaIDE
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run typecheck
npm run compile
npm run package
```

Node/npm 驗證不得依賴 implicit audit、fund 或 update notifier。ToukaChat Docker build 另以
`NEXT_TELEMETRY_DISABLED=1` 關閉 Next telemetry，production runner 以
`NO_UPDATE_CHECK=1` 關閉 `serve` 更新檢查；正式 `npm audit` 仍是獨立 Guard #23／授權邊界。

`src/__tests__/mcpHttpBridge.test.ts` 使用官方 Node v2 client 真連 ephemeral loopback
port，涵蓋 modern／退役legacy入口404、Bearer、多 client 與取消；AuthLevel、modal HITL、
discovery／package contract 及 workspace realpath jail 由鄰近測試保護。完整協定與
安全邊界見 ADR-0202。

---

## 後端測試規範 (ToukaProxy / Memory)

後端嚴格執行 **「全量集中化」**，所有測試必須位於根目錄 `tests/`。

### 1. 關鍵測試類別
*   **自動 Smoke Tests**：以逐程序 temp write-root sandbox＋provider mock 快速驗證核心 ReAct 與
    通訊契約；credential-shaped env 被移除並停用模型下載，受測路徑為零計量 API，不以真實
    provider connectivity 作 Green 前提。這不是 OS-level egress jail（ADR-0214）。
*   **Integration Tests**：驗證身分驗證 (JWT)、LDH 領卡機制與資料主權隔離。
*   **資源代理測試** ⭐（ADR-0116：整合模式單一部署）：
    - `test_memory_resource_proxy.py`：驗證 `get_resource_proxy()` 回傳 ToukaProxy 真實 `ResourceManager`、取不到即 **fail-loud**、以及模組級快取（3 個測試案例；已退役獨立 MCP 模式與 `_FallbackResourceProxy`）
    - `test_documentation_contracts.py`：驗證 current-facing 文件不再宣稱已退役的 standalone／fallback 模式、核心文件保留 integration-only 契約錨點，且 AGENTS.md 禁令範圍完整（3 個測試案例）

### 2. 執行指令
```bash
# 執行所有後端測試
pytest tests/

# 僅執行資源代理與文件契約測試
pytest tests/unit/test_memory_resource_proxy.py tests/unit/test_documentation_contracts.py -v
```

### 3. 人工 live probe 與 API 額度

真實 Anthropic／Gemini／OpenAI-compatible 模型、scorecard、tool-eval episode／baseline 與 Creation
ModelJudge 都不得進 pytest、master smoke、CI 或 guard。完整入口、估算上限與零 API 對照表以
[`tests/README.md`](../../tests/README.md#人工-live可能計量-api永久排除自動化) 為準。

Mock 無法判定 provider auth／connectivity／routing 時，先停止並向人類揭露 exact command、
provider、model、cases／retries、最大 requests、budget／tokens、送出內容與本機寫入範圍；取得
該次有界同意後才可加 `--human-approved-live-api`。方案核准、已有 key、錯誤發生或先前同意都
不算執行授權。沒有同意時記為 `NOT RUN — HUMAN APPROVAL REQUIRED`，不得冒充 PASS／FAIL。

ADR-0214 改造的 CLI 會在 provider-capable import 前檢查旗標，並於下層 transport 驗 exact scope、
request budget 與 token cap。Debug Center 則是既有人工 UI 入口：目前有費用確認 toast，但後端
endpoint 尚未消費同一 capability／hard budget，不得由自動化真 runner 呼叫或宣稱已受 CLI gate
保護。

---

## 給未來開發者的建議 (For AI & Human)
*   **增加新測試時**：請先判斷該測試屬於「邏輯」還是「UI」。若不確定，優先放在 `tests/unit/` 下。
*   **匯入路徑**：前端請統一使用 `@/` 別名（Alias）來引用 `src` 下的代碼，避免使用易碎的相對路徑。
*   **Mock 準則**：在測試 `useChatStream` 等複雜 Hook 時，務必 Mock `useDebug` 與 `useModels`，以確保測試環境的純淨。
*   **架構守衛豁免 (SSOT Suppression)**：若測試腳本或 Fixtures 必須執行原生 I/O 或修改環境變數，請使用行內註解 `# noqa: [RULE_ID]` (例如 `# noqa: IO_PRISON_VIOLATION`)，嚴禁使用 `getattr` 等破壞可讀性的 AST 繞過方式。

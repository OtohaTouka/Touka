# ToukaChat (燈火控制台)

ToukaChat 是 Touka AI 系統的前端控制面板。它不僅是 Touka 生態系的一部分，更是一個 **強大的通用 AI 客戶端**。

---

## 🌟 核心特色：多後端與多協議支援

ToukaChat 具備強大的適配能力，可以連接多種後端服務：
- **ToukaProxy**：完整體驗語義記憶、自主心跳與工具調用。
- **Ollama**：直接連線本地運行的 Llama 3, Mistral 等模型。
- **官方 API**：支援直連 **OpenAI** 與 **Anthropic**。
- **中轉服務**：支援 **OpenRouter**、**LiteLLM** 等相容 OpenAI 協定的後端。

支援雙通訊協議：
- **OpenAI 協議** (`/v1/chat/completions`)
- **Anthropic 協議** (`/v1/messages`)

---

## 🚀 核心功能 (Feature Set)

- **樹狀對話歷史 (Tree History)**：隨時開啟對話分支，AI 會記住完整的脈絡並支援分支切換。
- **提及系統 (@Mentions)**：在對話中直接引用檔案、終端機內容或整個程式碼庫。
- **Touka Artifacts (視覺化協作)**：自動渲染 AI 輸出的 HTML/Tailwind、Mermaid 流程圖、Markdown 等。
- **自主執行與修復**：具備 ReAct 迴圈與終端機錯誤自動修復能力。
- **模型工坊 (Model Studio)**：建立 Level 2 自定義模型，具備 ID 歸一化與配置自動合併功能。
- **權限模式 (AuthSwitcher)**：提供 `Manual`、`Auto`、`YOLO` 三種模式（YOLO 需二次確認，無痕對話一律 Manual），安全設定可選 Auto 審查員模型（ADR-0246）。
- **使用者管理面板 (Admin Management)**：管理員專屬介面，支援使用者的 CRUD、密碼重設與代理人 (Agent) 配給。
- **個人檔案主權 (Profile Hub)**：支援使用者自定義頭像、簡介與顯示名稱，全鏈路身分感應。
- **全鏈路認證與身分鎖定 (Auth & Identity Lock)**：整合 JWT 自動握手、SHA256-Bcrypt 安全盾與主權路徑顯示。
- **法規即代碼 (Legal Architecture, LaC)**：實施「法規級」ESLint 與 `guard_env.ts` 監督，確保設計、時間、錯誤碼與環境變數的單一事實來源 (SSOT)。
- **無障礙基線 (WCAG 2.2 AA)** ⭐ *ADR-0083*：全鍵盤操作（skip link／會話切換／Esc）、螢幕閱讀器串流宣告（「正在產生回覆／回覆已完成」）、六主題對比度由守衛測試實算執法、`prefers-reduced-motion` 支援。詳見 [docs/chat/accessibility.md](../docs/chat/accessibility.md)。
- **滑動視窗設定 (Sliding Window Settings)**：設定分頁「滑動視窗」，可開關自動剪斷功能並調整保留來回數與觸發上限（需 admin 儲存）。
- **對話剪斷（來回計算）**：`splitSessionMessages` 的 `keepCount=30` 現以**來回**（user+assistant pair）為單位，與全系統計算口徑統一（ADR-0036）。
- **工作分派任務看板 (AI Agent Dispatch Board)**：展示與同步分派給 `DispatchAgent` 的任務狀態、驗證指令、法律規則、測試輸出日誌與變更的多檔案產出物列表，建立與外部 AI 工具協作開發的視覺化閉環。介面適配了有限狀態機 (FSM) 狀態轉換限制與 `FAILED` 狀態特權重設權限防禦，且將產出物欄位徹底拆分為單一產物 `artifact_ref` (str) 與多產物列表 `artifact_refs` (list[str])，杜絕物理覆蓋與踩踏，保障前端型別安全。
- **技能工坊與分發 (Skill Studio & Distribution)** ⭐：技能設定分頁支援 Anthropic 標準 `SKILL.md` 技能的檢視、啟用與管理；可一鍵將技能匯出為 `.skill`（ZIP）封包，或透過 `SkillInstallModal` 由 URL 安裝外部技能（受後端 SSRF 白名單與 zip-bomb 防護），實現技能的雙向分發（ADR-0066）。
- **側邊欄折疊與 TS 合約重構**：折疊狀態提升至 page 層級（useLocalStorage）實現 SSOT 單一 facts 來源；消除掛載級聯更新（set-state-in-effect）警告；SessionItem 刪除確認於選單關閉時即時重置（onOpenChange）；回呼合約精簡（(id) => void），徹底移除了 `as unknown as` 強轉型別（ADR-0075）。
- **失效可觀察性與當機復原 (Resilience UX) — ADR-0078** ⭐：記憶系統降級時側欄常駐紅色 `DegradedBanner`（`role="alert"`、跨分頁可見）；系統監控頁顯示「⚠️ 上次有 N 個未完成的背景任務」並提供「重新觸發」按鈕（`POST /v1/heartbeat/retrigger`，強制 Auto、僅擁有者）；串流/連線錯誤明確 toast 提示（守 ERROR_SSOT）。
- **首次設定精靈：失敗即現與雙目錄／嵌入模型管理 (Setup Wizard) — ADR-0118** ⭐（2026-07-02）：
  - **失敗即現 (Fail-loud)**：`SetupWizard.tsx` 不再吞掉錯誤；`Step1SystemHealth.tsx` 與 `Step3ApiKeys.tsx` 各自負責套用請求、檢查 `res.ok`、顯示錯誤，且**僅在成功時前進**（與 Step2／Step5 對齊）。注意 `fetchTouka` 對 4xx/5xx **不會拋出**，呼叫端必須自行檢查 `res.ok`（擴充 ADR-0053）。
  - **Step5 雙主機目錄配置**：同時設定 `SANCTUARY_DATA_PATH`（資料目錄 → 容器 `/app/data`）與 `WORKSPACE_ROOT`（工作區根目錄 → `/app/workspace`），兩者皆以**主機路徑**寫入 `.env` 供 docker-compose bind-mount；設定主機路徑後需 `docker compose up` 重新掛載方能生效（Docker 固有行為）。新增 `SetupStatus.workspace_root_is_default` 欄位。
  - **Step1 嵌入模型下載與切換**：可下載嵌入模型至 HF 快取（背景任務＋輪詢狀態、逐檔位元組進度、快取命中略過；純累加、絕不刪除既有模型）；切換模型時若偵測到既有記憶且模型不同，會**要求使用者確認**（`force` 旗標）方可寫入 `EMBEDDING_MODEL`（免重啟即時生效，向量重嵌入為離線維護、不自動觸發）。

---

## 已發布產品安裝

使用 [根目錄安裝指南](../README_QUICKSTART.md) 的三映像整合流程，Chat 是其中的 Web UI；不需要先安裝本機 Node/npm 或新增 Chat installer。已發布映像、source 開發伺服器與任意外部後端連線是不同條件，不能用 source 測試替代映像資格。

## 🛠️ 開發與測試 (Development & Testing)

ToukaChat 遵循嚴格的 TDD 與 SOLID 原則。

建置與驗證工具鏈固定為 **Node.js 24.22.1 LTS／npm 11.19.1**；版本由
`package.json` 的 `engines`、`packageManager` 與 `devEngines` 共同執法。

### 執行測試
```bash
# 執行全域測試
npm run test

# 執行架構守衛 (SSOT 檢查)
npm run guard:all

# Docker 容器建置（開發模式，含 node-pty 原生模組編譯）
docker compose build touka-chat
```

> **注意**：`ToukaChat/Dockerfile` 使用固定官方 Node 24.22.1 工具映像；production 的
> Node 另以相同版本的原始碼套用已核 OpenSSL 修補並建置、驗證。開發 builder 保留
> `python3 make g++`。全域 lifecycle 維持停用，只有 dev terminal 需要的
> `node-pty` 會以明示 `npm rebuild node-pty` 在 Linux 容器中編譯。

詳細測試規範請參閱 [測試指南](../docs/guides/testing_guide.md)。

---

## 🏗️ 技術棧 (Tech Stack)

- **Frontend**: Next.js 16, React 19, TailwindCSS, Shadcn UI.
- **State Management**: React Context + Custom Hooks.
- **Contract Sync**: OpenAPI -> TypeScript (automated).

---

## 📦 Source 開發環境

從 repo 的 `ToukaChat` 目錄執行，先依 [CONTRIBUTING](../CONTRIBUTING.md) 完成 root Python workspace（OpenAPI 來源／驗證需要）：

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run gen:artifact-runtime
npm test
npm run build
```

需要開發伺服器時另執行 `npm run dev`（包含既有 sync-api／artifact runtime 生成）。安裝 lifecycle 關閉，不能依賴 postinstall；明示生成完成後再跑 tests/build。原生 terminal 所需的 node-pty 以既有開發建置程序處理，不開放所有 lifecycle，也不在未知平台自行安裝全域編譯器。

Committed `.npmrc` 另固定 `audit=false`、`fund=false`、`update-notifier=false`。Docker build
會停用 Next telemetry；production `serve` 也停用自動更新檢查，讓非必要網路行為保持顯式。

---

## 🛡️ 前端安全

| 修復 | 說明 |
|------|------|
| **M5**（Header 注入） | `sanitizeHeaderValue()` 剝除 `\r \n \x00`；已套用至 `X-Touka-Soul-ID` |
| **P2**（Mermaid XSS） | `securityLevel:'strict'` + `DOMPurify.sanitize(svg)` |
| **P4**（Token localStorage） | Access Token 改存模組級記憶體變數，不再寫入 localStorage |
| **P10**（postMessage origin） | `PlanTracker` 加入 `event.origin !== window.location.origin` 驗證 |
| **P12**（iframe CDN SRI） | `srcdoc` 移除外部 CDN script，改用 inline CSS reset |
| **DA10**（Web 產出物 iframe 外洩） | `WebRenderer` sandbox 收緊為僅 `allow-scripts`（移除 `allow-forms`/`allow-downloads`）＋ `srcdoc` 注入 CSP（`connect-src 'none'`/`form-action 'none'`/`default-src 'none'`）封死 beacon/釣魚/下載；測試見 `tests/unit/ArtifactView.test.tsx` |
| **DA22**（admin 端點敏感欄位外洩） | 後端 `/admin/users*` 改回 `UserOut`，前端 `lib/types.User` 隨之指向 `components["schemas"]["UserOut"]`、`api.ts` 重生——完整 `User`（含 bcrypt 雜湊＋refresh JTI）退出前端型別合約 |

完整安全修復清單見根目錄 [SECURITY.md](../SECURITY.md)。

---
*安裝指引更新：2026-09-14；版本權威為 package.json 與鎖檔。*

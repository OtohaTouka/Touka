# 🌫️ Touka System

> **A local-first AI agent operating system with persistent memory, autonomous heartbeat, and multi-platform integration.**

[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://python.org)
[![Node.js 20+](https://img.shields.io/badge/Node.js-20+-green.svg)](https://nodejs.org)

---

## 🌌 關於本專案

**Touka** 是一個以「本機部署優先（local-first）」為設計理念的 AI 代理人作業系統。你的對話、記憶與設定完全保存在你自己的機器上，不依賴任何雲端服務。

本專案是受 [openclaw](https://openclaw.ai/) 與 [hermes-agent](https://hermes-agent.nousresearch.com/) 啟發的實驗性模仿作品，旨在探索「具備長期記憶的個人 AI 助理」的可能性。每一行程式碼均由人類開發者透過高階指令（Orchestration）引導 AI 協作完成。

---

## 🏗️ 系統架構

Touka 由四個獨立子系統組成：

| 子系統 | 角色 | 埠號 |
|--------|------|------|
| **[ToukaProxy](./ToukaProxy)** | 核心閘道：身分驗證、工具調度、多輪 ReAct 循環 | 8000 |
| **[ToukaChat](./ToukaChat)** | 前端控制面板：Next.js 16 現代化 UI | 3001 |
| **[ToukaMemory](./ToukaMemory)** | 記憶層：長期事實庫（Qdrant）+ 個人檔案庫 | 整合模式 |
| **[ToukaHeartbeat](./ToukaHeartbeat)** | 智慧節律器：自主排程與脈衝驅動 | 8082 |

---

## ✨ 核心功能

- **長期語義記憶**：跨會話向量檢索，Agent 記得幾週前的對話細節
- **自主心跳排程**：ToukaHeartbeat 定時喚醒 Agent 執行背景任務，不需人工觸發
- **多平台訊息整合**：支援 Telegram、Discord、LINE Bot 等訊息平台接入
- **本機資料主權**：所有對話、記憶、設定均存於本機，不上雲
- **三種權限模式**：Manual / Auto / YOLO，HITL 人工確認機制；Auto 由審查員把關，刪改資料、讀金鑰等底線寫在程式裡、只有人能核准
- **多模型支援**：Claude、Gemini、GPT、Grok 及 Ollama 本地模型
- **MCP 生態系整合**：支援 Model Context Protocol 第三方工具
- **Anthropic Agent Skills 標準**：技能採目錄式 `SKILL.md` 開放標準，可打包為 `.skill` 雙向分發，並依白名單自動召喚外部 AI 協作（ADR-0066 / ADR-0067）
- **滑動視窗歷史管理**：自動歸檔長對話，維持 context 效能
- **43 道靜態守衛**：`guard_all.py` 自動檢查架構規範、CVE、API 合約一致性
- **原生創建引擎 (Creation Engine)**：MCP 與 Skill 共用的創建流水線（VALIDATE → SMOKE 沙箱試跑 → COMMIT → EVAL 後置評測），支援從零 scaffold 決定性生成 Python SDK v2 `MCPServer`。Owner/admin 可經 `/v1/creation/*` 路由或 agent 對話創建產物；CREATE 不覆寫既有 id，受 Creation Sovereignty Guard #29 保護（ADR-0069／0199）。
- **湧現指標 (E-Index)**：計算意識成長與人格穩定性。
- **終端機修復**：自動生成與執行 Exit Code 修復腳本。
- **背景任務**：非同步執行長時間多脈衝 ReAct 循環。
- **動態 MCP 調度**：會話級別的工具按需掛載（不是行程隔離：MCP 伺服器仍和 Touka 同權限執行，見 SECURITY.md）。
- **群聊分發員 (ToukaGroup)**：讓多個靈魂在同一場對話輪流發言，支援自定義「點名別名」與靈魂 ID 的解耦映射；成員只限靈魂（ADR-0245）。
- **自主演化流水線**：具備「發現 -> 獲取 -> 創造」的全鏈路進化能力。
- **自主任務看板 (Mission Board)**：作為 Agent 的「非對話式外部記憶」。支援任務樹拆解、進度持久化與 Artifacts (產出物) 關聯，解決長生命週期任務中的專注力流失問題。
- **AI Agent 工作分派系統 (AI Agent Dispatch System)**：以 `DispatchAgent` 為中介的任務分派看板系統。允許已存 Agent 通過特權標籤代理操作其看板，並支援外部 AI 工具通過 MCP 看板工具（`mission_tools`）存取，更新任務進度、變更檔案與測試結果日誌，實現視覺化協作開發。
- **背景任務感知 (Awareness)**：系統自動在每一輪脈衝背景注入 `<mission_awareness>` 標籤，使 Agent 具備「自我進度認知」，實現真正意義上的自主工作閉環。

---

## 🛡️ 安全架構

Touka 歷經多輪系統性安全審計與深度對抗式複審（涵蓋身分驗證繞過、路徑穿越、指令注入、SSRF、競態條件、供應鏈風險等），逐項以 TDD 修復並由架構守衛持續執法。**完整的逐輪弱點清單與修復摘要見 [SECURITY.md](SECURITY.md)**（隨每輪審計持續更新）。

主要安全機制：

- **JWT 認證**：所有 API 端點強制驗證（Guard #24 端點認證守衛掃描執法）
- **本機動態握手（LDH）**：本機開發免 token 自動降級；雲端部署可啟用 `CLOUD_MODE=true` 關閉 LDH
- **Refresh Token jti 輪換**：重用舊 token 立即撤銷全部 session
- **速率限制**：登入 / 註冊 / 刷新 token 均有 IP 滑動視窗防護
- **非 root Docker 容器**：三個服務均以 `appuser` 身分執行
- **Security Header 中間件**：nosniff / X-Frame-Options / Referrer-Policy
- **請求體大小限制**：預設 10MB，防記憶體耗盡攻擊

詳見 [SECURITY.md](SECURITY.md)。

---

## 🚀 安裝與啟動

一般使用者使用 GHCR 上的三個 Linux amd64 映像，依 [README_QUICKSTART.md](README_QUICKSTART.md) 準備目錄與 `.env`，再分階段啟動。需要 Docker Compose 2.24.0+；四個服務埠限本機 loopback，沒有零設定、ARM64 或公網部署承諾。初始密碼與金鑰由產品 bootstrap／UI 管理。

開發者改走 [CONTRIBUTING.md](CONTRIBUTING.md) 的受鎖環境。根 `docker-compose.yml` 是 source 開發組態，含原始碼掛載，可直接 `docker compose up --build`；安裝預建映像請使用 `docker-compose.ghcr.yml`。Memory 整合在 Proxy 中，Dispatch 由 Proxy supervisor 啟動，無須額外安裝 Memory／Dispatch。

## 📁 目錄結構

```
Touka/
├── ToukaProxy/          # 核心 API 閘道（FastAPI / Python）
├── ToukaChat/           # 前端介面（Next.js）
├── ToukaMemory/         # 記憶系統（Qdrant + SQLite）
├── ToukaHeartbeat/      # 排程器（FastAPI / Python）
├── ToukaIDE/            # VS Code 擴充功能
├── docs/                # 系統架構文件
├── tests/               # 後端測試（pytest）
├── scripts/             # 守衛與維護腳本
├── nginx/               # nginx 設定範本
├── docker-compose.yml         # source 開發組態
├── docker-compose.ghcr.yml    # 本機預建映像（pull-not-build）
├── docker-compose.cloud.yml   # 雲端 VPS 覆寫組態（未驗證）
├── docker-compose.searxng.yml # 選用：自架 SearXNG 隱私搜尋後端
├── .env.example         # 環境變數範本
├── LICENSE              # MIT
├── NOTICE               # 第三方授權說明
└── CONTRIBUTING.md      # 貢獻指南
```

---

## 🔧 開發（接手開發者）

完整步驟見 [CONTRIBUTING.md](CONTRIBUTING.md)。Python 由 `.python-version`／`uv.lock` 鎖定 3.12；Chat／IDE 的 `package.json` 固定 Node.js 24.21.0／npm 11.19.1。

```bash
uv sync --locked --all-packages
cd ToukaChat
npm ci --ignore-scripts --no-audit --no-fund
npm run gen:artifact-runtime
```

`npm` lifecycle 預設停用，不能依賴 postinstall 自動生成。測試前啟用 root venv，將其 Scripts/bin 放到 PATH 首位，再由 repo root 執行原生 `python -B -m pytest tests/` 與 `python -B scripts/guard_all.py`；Chat 另跑 `npm test`。所有自動化驗證用 sandbox/mock，不讀寫真 data。

---

## 📦 第三方授權說明

本專案使用以下 LGPL 授權的套件：

- [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) — LGPL v3（動態連結，不影響本專案 MIT 授權）

其他依賴多為 MIT、BSD、Apache-2.0、ISC、PSF-2.0、Unlicense 等寬鬆授權，另有例如 MPL-2.0（certifi、tqdm）與 CC-BY-4.0（caniuse-lite 的瀏覽器相容性資料）。這些授權都允許和本專案一起使用與散布。需要署名的第三方內容列在 [NOTICE](NOTICE)；ToukaIDE 打包進擴充套件的 npm 套件列在 [ToukaIDE/THIRD_PARTY_NOTICES.txt](ToukaIDE/THIRD_PARTY_NOTICES.txt)。

GHCR 上的容器映像以 Debian 為基底，內含的 Debian 套件（例如 bash、glibc，部分為 GPL 授權）保有各自的授權，原始碼取得方式見 NOTICE 的「Container images」一節。

---

## 📜 授權

本專案以 **MIT License** 開源。詳見 [LICENSE](LICENSE)。

Copyright (c) 2026 Otoha

# ADR-0238：退役發版資格機制與容器安全管線

- 狀態：✅ 已接受（維護者 2026-10-08 決定）
- 更正（[ADR-0254](0254-iso-c1-small-fixes-and-execution-gaps.md)）：決策 5 寫 CI 直接執行 Ruff，但從 `4a0bf452` 起 workflow 沒有 Ruff 步驟；ADR-0254 起 `contracts` job 跑鎖定版 ruff `--select F`（四個產品子專案）並新增 `toukaide` job，完整 ruff 只在本機。
- 日期：2026-10-08
- 決策者：維護者
- 影響範圍：`scripts/` 發版工具、GitHub workflows、`ToukaReleaseAuthority`、`infra/release-authority`、三個產品 Dockerfile、`ToukaChat/container-security`、`ToukaChat/node-security`、MCP rollout evidence（Proxy／Dispatch／IDE）、Guard #42／#43、禁令 62、交班與證據文件
- 取代：ADR-0210、ADR-0211、ADR-0212、ADR-0213、ADR-0215、ADR-0216、ADR-0217、ADR-0225、ADR-0226、ADR-0227、ADR-0233、ADR-0234、ADR-0235、ADR-0236；ADR-0220 與 ADR-0221 的發行／冷切換部分

## 背景

Touka 的產品 runtime 與資料都在使用者自己的本機，所有 client 也在同一台機器（ADR-0220、ADR-0221）。為了證明每一次發版「確實由同一份 bytes 建置、驗證、發布、部署」，專案累積了一整套資格驗證機制：

- `scripts/local_release*`、`local_cold_cutover.py` 等約 7.9 萬行發版腳本，以及約 10 萬行對應測試。
- 9 支 GitHub release workflow、Azure release authority 套件與 IaC。
- Dockerfile 內自行重編 Debian 套件（perl／ncurses patch、openssl-u3、GPL 對應原始碼 tarball）、自組 npm、從原始碼自編打補丁的 Node，且 Node 透過空的 `FROM scratch` stage 由發版工具注入——一般 `docker build` 無法建置。
- 產品程式內的 MCP rollout evidence producer／ledger／oracle／verifier，與 ToukaIDE 的 Python 衍生 TypeScript 契約。
- 大量交班、證據與失敗紀錄文件。

這套機制要求保留的本機原件（LOCAL_ONLY）已不存在，歷史 activation 也無法補造；完整 pytest 因此長期跑不出結果，產品開發被資格流程卡住。投入與產品規模（四個 Python 服務約 8.1 萬行）明顯失衡。

## 決策

1. **整體移除發版資格機制**：刪除上述發版腳本、對應測試、release workflows、`ToukaReleaseAuthority`、`infra/release-authority`、Guard #42（Release Workflow Gate）與 Guard #43（ghcr Compose Pull-Purity）。守衛與禁令編號為穩定識別碼，不重排；禁令 62 保留退役墓碑。
2. **最小 GHCR 發行**：以 `.github/workflows/ghcr-publish.yml` 直接建置並推送 `touka-proxy`、`touka-heartbeat`、`touka-chat`。只接受手動觸發：一律發布 `sha-<commit>`，填入 `version` 時另發布該版號與 `latest`。不用 tag 觸發，避免與 ToukaIDE Marketplace 的 `v*` tag 衝突。不再有 qualification、seal、receipt 或 readback 程序。
3. **標準化 Dockerfile**：Node／npm 直接取自官方 node 映像（digest 釘版）；Debian 套件以 `apt-get upgrade` 取官方安全更新。移除自編 Node、自組 npm、Debian 套件重編與對應原始碼 tarball。保留 Chat 的 braces 同版本防禦回補（ADR-0237）。
4. **移除產品內 MCP rollout evidence**：刪除 Proxy／Dispatch 的 rollout producer、ledger、oracle、verifier、authority 與 release probe，以及 ToukaIDE 的 rollout 契約綁定。MCP modern-only 協定行為（ADR-0200～0205、ADR-0221）不變。
5. **CI 簡化**：原 CI 透過發版腳本執行檢查；改為直接執行 Ruff、lint_ssot、原生工具契約守衛、權限註冊守衛、pytest 與前端測試。job 名稱沿用 `contracts`／`frontend`。
6. **文件**：刪除 `docs/handover/`、`docs/evidence/`、`docs/history/`、`site/`（mkdocs 產出，改列 `.gitignore`）、`scratch/` 與發版流程提案。`HANDOVER.md` 只描述現況。

## 後果

- 產品可再用一般 `docker compose build` 建置；發版不再需要維護者本機的 runner、獨立 daemon 或受管 controller。
- 失去「同一 bytes 從建置到部署」的可驗證鏈，以及 Debian／Node 層級早於官方套件的自行修補；改依官方映像與 Debian 安全更新。供應鏈風險改由既有 Python／Node CVE 守衛（Guard #22／#23）與官方更新承擔。
- 既有 GHCR 上的舊映像（如 `modern-5c48a5bd51e6-q34854176569-a1`）仍可使用；新發行使用手動填入的版號。
- 依賴弱點稽核守衛（ADR-0222、ADR-0223）、braces 回補（ADR-0237）與所有產品安全修補維持不變。

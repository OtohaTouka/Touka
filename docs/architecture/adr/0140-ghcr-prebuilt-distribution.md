# ADR-0140: ghcr 預建映像發佈（pull-not-build 分發路徑）

> **現況**：部署細節：heartbeat 改目錄掛載（ADR-0150）、.env 錨定 compose 目錄（ADR-0151）。

## 狀態

✅ **已接受（實作 2026-07-08；尚未推首個 tag）**。

> 🔧 **兩處部署細節已被後續 ADR 更新**：①**heartbeat config 由「單檔 `./heartbeat-config.json` 播種」改為 [ADR-0150](0150-heartbeat-config-dir-mount.md) 的目錄掛載**（`${HEARTBEAT_CONFIG_DIR}:/app/config`、`touka-init` `mkdir`+遷移舊單檔+播種）；②**.env 錨 `TOUKA_ENV_FILE` 由綁 `WORKSPACE_ROOT` 改為 [ADR-0151](0151-env-persistence-anchor-decouple.md) 的 `./:/app/host_root` 解耦**（`TOUKA_ENV_FILE=/app/host_root/.env`，避免寫進幽靈 .env）。pull-not-build 核心架構＋CI 兩階段閘（trivy secret 硬閘＋fresh-volume 啟動閘）仍有效。

## 日期

2026-07-08

## 背景

分發計畫既定：ghcr.io **公開預建映像**（Open WebUI 式「拉了就跑」）、映像**無密設計**
（禁令 47：`COPY . .`＋黑名單）。盤點確認程式面已就緒——uv 單一鎖檔建置（ADR-0117）、
非 root、設定自癒（`core/bootstrap` 通用復原 `.env`／`config/*.json` 自 `.example`）、
HEARTBEAT_TOKEN 自生、HF 快取 named volume；心跳映像已烤 `awaken.py`＋範本（compose
掛載僅 dev 覆蓋）；chat 生產映像為純靜態（`next build`＋serve，sync-api 僅 dev）。
缺口＝**發佈管線與 pull 使用者路徑**：無 `.github/`、compose 全 `build:` 且含 repo
檔案掛載（純 pull 使用者沒有 repo）。

## 決策

1. **pull 版 compose**（[`docker-compose.ghcr.yml`](../../../docker-compose.ghcr.yml)）：
   三服務改 `image: ghcr.io/${TOUKA_IMAGE_OWNER:-otohatouka}/touka-*:${TOUKA_VERSION:-latest}`
   （可釘版）。與開發版的刻意差異——①移除全部源碼 dev 掛載（映像內容執行）；
   ②proxy 活體設定改 **named volume `proxy-config`**：首跑 Docker 自映像播種
   （`*.json.example`＋skills/），活體 JSON 由 bootstrap 自癒、跨重建持久；③heartbeat
   `config.json` 由 init 從**映像內範本**播種到宿主 `./heartbeat-config.json`（init 改用
   heartbeat 映像＝少拉一個映像＋能拿到範本；已入 `.gitignore` 防開發機誤 commit）；
   ④chat 移除 openapi.json 掛載；⑤`.env` 改**選用**（`required: false`，需 Compose
   v2.24+）——零設定首跑，精靈寫回 `./​.env`（`TOUKA_ENV_FILE=/app/workspace/.env` 沿用）。
2. **前端 baked URL 拍板**：公開映像於發佈 build 內嵌 `localhost` 預設
   （`NEXT_PUBLIC_*` 為靜態匯出 build-time 值）；非 localhost 部署走**設定→連線**
   執行期覆寫（`getActiveProxyUrl()` 先讀使用者連線再退內嵌值）——文件明載，
   runtime config 注入＝日後選配。
3. **CI 發佈管線**（`release-images.yml`）：
   `v*` tag 觸發（或手動＝僅 sha 標籤）。兩階段——**gate**：amd64 建置→ trivy
   `--scanners secret` **層內無密硬閘**→ **fresh-volume 啟動閘**（以剛建映像實跑
   pull compose：等 proxy healthy、curl 三服務、驗證 heartbeat 範本播種——CI runner
   全新＝等價驗證使用者首次 pull 起跑，含 `.env` 缺席自癒路徑）；**publish**（gate
   過才跑）：三映像 ×（amd64＋arm64）、semver＋latest 標籤、OCI labels（source／
   licenses／description → ghcr 套件頁連回 repo）、GHA layer cache。
4. **文件**：README_QUICKSTART 新增「方式 A：預建映像」（curl compose→up、零設定
   首跑、釘版／升級／備份＝整個資料夾）＋**部署邊界警語**（本地優先設計：LDH 同網段
   自動發卡、CORS 區網開放——**勿直接暴露公網**，遠端存取須反代＋自備認證）。

## 後果

- **使用者不需 clone／build**：一個資料夾＋一份 compose（＋選用 .env）即完整系統；
  備份單位＝該資料夾（data／.env／heartbeat-config.json）＋兩個 named volumes。
- **誠實邊界**：①管線**未實跑**——首次觸發需維護者推 `v*` tag（並到 ghcr 套件頁把
  三個 package 設 public，首推預設 private）；②trivy **弱點**掃描刻意不設硬閘（基底
  CVE 非本 repo 可控），密鑰掃描才是零容忍；③映像簽章／attestation（cosign／SLSA）
  ＝既定選配、本 ADR 不含；④repo 開源是另一件事——公開映像≈公開程式碼內容物，
  repo 轉公開前仍需 git 歷史秘密稽核；⑤`down -v` 會刪 proxy-config／hf-cache
  （文件已警示）；⑥proxy-config named volume 的映像播種只發生在**空 volume 首跑**，
  升級映像不會刷新已播種內容（skills 更新靠 bootstrap 自癒與應用層機制）。
- **驗收**：`docker compose -f docker-compose.ghcr.yml config` 通過；workflow YAML
  結構驗證通過；guard_all 40/40＋pytest＋vitest 全綠（本 ADR 零 Python／前端碼改動）。

## 相關

- [ADR-0082](0082-dispatch-in-proxy-consolidation.md)（proxy 單一映像＋禁令 47 黑名單）
- [ADR-0117](0117-uv-workspace-single-lock-migration.md)（uv workspace 單鎖檔建置）
- [ADR-0095](0095-config-example-regression-tests.md)（設定範本迴歸測試；Guard #34 與 bootstrap 自癒互補）
- [ADR-0150](0150-heartbeat-config-dir-mount.md)（**更新本 ADR heartbeat config 播種：單檔→目錄掛載**）／[ADR-0151](0151-env-persistence-anchor-decouple.md)（**更新本 ADR .env 錨：WORKSPACE_ROOT→compose 目錄解耦**）

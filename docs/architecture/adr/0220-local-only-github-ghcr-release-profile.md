> **結案基準（2026-09-14 原驗收）**：S50 rollback baseline `b540106f5e7b5467162164a27e1c75229c3fdae3`／S55 modern `927ce096f9fe297d6aca329b6cc8359993da1278` 的 selected local profile 已經審查接受，狀態為 **MODERN_ACTIVE**。原資格、原始映像發布、兩 consumer、compatibility／cleanup 與 actual activation 已接受，見結案文件（未入版控）。以下先前 pending／當前／S36／S39 敘述是歷史，不代表目前來源或新授權；原技術閘、未選 profiles、liblzma deferred 與 rollback 材料保存限制不變。新來源仍須各自 fresh release gates；本次來源整合不部署。

# ADR-0220：本機產品邊界與 GitHub／GHCR 發行剖面

> **2026-09-12 歷史 P11 快照**：modern-only M1–M4 本機來源驗證已完成，最終精確候選交付最終審查；不等於映像 qualification、發布或 live cutover。已合併 S36=`3fe08de1587121de370a501aa7cbe4569de30963` 的安全診斷保留於候選與隔離 baseline consumer。Q36 run34626118610/a1 在 entry Android cleanup DEADLINE／WAIT 失敗，build/runtime 未執行、artifacts 0；與 Q35 的容量成功／runtime UNKNOWN 分開。兩次相同方向容量失敗後不改 timeout 或盲重跑，未查得可驗證的新外部條件。最新 P11 依維護者的一次性委託經審查接受；M4／精確 commit／draft PR 可先完成，modern main merge／hosted／發布／切換仍待實際 immutable rollback seal。詳見[當前交班](../../../HANDOVER.md)。

> **2026-09-08 歷史程序**：維護者於 2026-09-08 一次性授權 P11 不再逐次人工核准；network、Git 與發行程序改依委託經審查處理，source identity、consumer、rollback、build-once、qualification 與 readback 條件仍有效。以下較早候選狀態／FAIL 依當時範圍保留；整體 implementation 仍 pending，未選 Azure／provider-v3／8 IDE host lanes 保持 dormant。

- 狀態：✅ 維護者決策已接受；selected local profile 於 2026-09-14 經審查接受，進入 MODERN_ACTIVE；未選 profiles 維持 NOT_SELECTED。
- 日期：2026-09-04
- 決策者：維護者
- 影響範圍：Touka 公開發行邊界、GitHub Actions、GHCR、Docker 映像、
  ToukaReleaseAuthority dormant baseline、Marketplace 與 P11 完成宣稱
- 相關決策：[ADR-0215](0215-mcp-trusted-rollout-verifier-and-release-qualification.md)、
  [ADR-0217](0217-public-release-authority-v3-bootstrap.md)、
  [ADR-0218](0218-toukaide-first-marketplace-release.md)、
  [ADR-0221](0221-modern-only-local-cold-cutover.md)

## 背景

ADR-0217 建立了包含 Azure authority、receipt v3 aggregate、兩個 OCI native lanes、
八個 IDE host lanes與無 caller reusable workflows的離線高保證發行候選；ADR-0218
則已把 ToukaIDE `0.2.0` 收斂為單機 Windows 的 Marketplace 人工流程。維護者
進一步確認：Touka 不採雲端 runtime，後續發行工作只處理 Docker、GitHub Actions與
GitHub Container Registry。既有高保證候選不得因這個範圍收斂而被誤寫為已通過。

## 決策

### 1. Touka runtime 與資料面維持本機

- Touka runtime、使用者資料、Agent 執行與產品 control plane只在使用者自己的本機運作。
- GitHub Actions只作隔離建置與驗證，GHCR只作容器映像散布；兩者都不是 Touka
  runtime、Agent 或使用者資料的託管環境。
- 發行輸入不得包含 `data/`、credential、使用者個人資料、repo 外候選或本機暫存證據。

### 2. 現行外部目的地

目前產品只保留三類公開目的地：

1. GitHub：原始碼與文件；P11 的 exact stage／commit／push／PR 依維護者的一次性委託經審查處理。
2. Visual Studio Marketplace：ToukaIDE。維護者已回報官方 Marketplace 審核通過，
   且既有 `0.2.0` upload／READBACK-R1 人工流程完成；此回報不證明 remote bytes與
   local VSIX 全等，也不產生 receipt v3。
3. GHCR：proxy、heartbeat、chat 的 Docker images；首輪目標平台為 `linux/amd64`。

Marketplace既有版本不因 GHCR 工作重跑高保證八 lane矩陣。未來每個新 ToukaIDE
版本仍須對實際支援的 Windows＋VS Code Stable完成最小安裝、啟動、modern MCP與
認證 fail-closed smoke。

本 ADR 範圍包含 dependency audit、既有 ToukaChat Next.js／SNS token 修復、modern-only 切換與 Docker／GitHub Actions／GHCR。既有修復及未变輸入證據保留，明示 REUSED_NOT_FRESH，不重做已完成修復；改變的 IDE lock／來源需新驗證。modern-only 不安排舊版調查或遷移，保留最小 artifact rollback 與現代協定安全。額外 Marketplace 發行、Azure／未選 provider／八 IDE host lanes 不納入。

本輪方法以[測試指南](../../guides/testing_guide.md)為準，
具體接續見原生驗證計畫（未入版控）。
舊計畫的工作區零寫入、整份外部projection及專用launcher不再強制；本ADR的產品資料、
正式發版隔離、audit及外部動作邊界保持有效。

### 3. 高保證雲端鏈不屬目前產品剖面

下列 ADR-0217 項目均為 **NOT SELECTED／dormant**，不是 PASS：

- Azure Functions production broker；
- Azure Blob、Key Vault 與 Bicep IaC；
- GitHub OIDC provider activation與 single-use authority grant；
- receipt v3 aggregate、兩個 OCI lanes、三個 OCI indexes與八個 IDE host lanes；
- private runner、Open VSX與跨目的地 aggregate transaction。

既有程式與 IaC 可繼續留在 repo 作歷史／候選基線，但 production必須維持
`PROVIDER_UNCONFIGURED`，無 caller workflow必須維持 inert。這些元件未經驗證，
也不再是現行 GitHub／GHCR 發行剖面的完成前提；若維護者未來重新選擇高保證鏈，
必須另提計畫並經審查，並恢復 ADR-0217 的全部 activation blockers。

### 4. GitHub Actions／GHCR 的待實作發行閘

目前只接受下列目標契約；第2項的Next.js remediation已有經計畫審查的候選，其餘不得宣稱已實作或已執行：

1. 依 exact commit 與 immutable base-image digest建置一次 `linux/amd64` images；
2. ToukaChat remediation候選把`next`與`eslint-config-next`精確釘為`16.3.4`；Next上游
   `sharp ^0.35.4`使root override退場，lock解析`sharp 0.35.4`。結構化版本、official-registry、
   SHA-512與離線重鎖重現已Green；歷史repo外sealed cache／node_modules曾通過preflight，
   不是後續驗證必建的環境。追加13份測試型別修正後focused 62 passed、lint零error、root TypeScript與Next build PASS；
   前輪完整Vitest 1159／230 files是最後test-only標註前的歷史結果，本輪未重跑。
   上述為歷史執行結果；除錯升級的修復計畫 R2 未完成驗收，現改由專案原生入口接續，資格驗證未完成；pip-audit／npm audit另取network gate，
   SNS token HIGH另案待修，不得把計畫審查後的候選寫成完成或clean；
3. 對同一份原始 build bytes執行測試、secret／vulnerability／configuration scan、
   dependency audit、SBOM與 provenance；
4. GitHub Actions第三方 action釘完整 commit SHA，權限採 least privilege；
5. publish job只能推送已驗證的原映像，不得重新 build或由 qualifier重封裝；
6. 由維護者當次核准後啟動 publish，GHCR receipt必須回綁 exact digest；
7. 從 GHCR依 digest拉回，在 fresh volume上驗證容器啟動、modern MCP版本、
   認證與協定級 health probe；
8. 明定 partial publish、rollback、retry與 tag promotion政策。

`pip-audit`、`npm audit`、Docker daemon、GitHub Actions live run、GHCR publish與remote readback
目前均為 **NOT RUN**。此前Node／npm下載、registry精確重鎖與cache-seed已分別依維護者核准執行；
2026-09-06先前批次僅重用既有隔離環境，未install、更新依賴或連網；此為歷史紀錄，
不要求新方式另建同類環境。新方式已依維護者授權完成原生offline `npm ci`，重用既有cache，
未下載或改動manifest／lock；每個後續外部動作仍受fresh gate約束。

### 5. 完成宣稱

- ADR-0215～0217 的已提交 implementation／offline bootstrap仍是歷史事實。
- ADR-0217 provider activation在目前剖面是 NOT SELECTED，而不是已通過。
- Marketplace人工結果不替代 Docker／GHCR qualification。
- 在本 ADR 的待實作發行閘及 ADR-0221 modern-only cutover真正完成前，P11總體仍為
  **IN PROGRESS／未完成**；不得因文件接受本決策而勾選完成。

## 安全與資料邊界

- 自動測試只能使用 sandbox／mock；不得讀取、列舉、修改、清理或掛載 `data/`。
- Audit、registry、Docker、GitHub Actions、push與 publish需要各自的精確命令、
  傳輸、寫入、retry與 budget揭露及維護者的當次核准。
- GitHub/GHCR發行不授權建立任何雲端 Touka runtime、同步使用者資料或啟動 Agent。

## 實作狀態

2026-09-06 維護者已授權改用專案既有測試、守衛、sandbox／mock接續；必要時可在專案內補強，
不再以修復外部launcher為完成前提。除錯升級修復計畫 R2 的TS2307與diff-check FAIL保留；原生驗證已接續，
本輪結果以原生驗證計畫為準，最終審查與P11總體仍未完成。以下為進入除錯升級前的歷史快照，不能作新方式的驗證結果。

本 ADR 記錄維護者已接受的目標剖面與經計畫審查的45-path候選；2026-09-06 root TypeScript與
Next build PASS，非audit守衛#1–#16 PASS。#17被repo外Popen包裝連續三次於收集前阻塞，
依停止線進入除錯升級；其餘25道非audit守衛及完整Python suite NOT RUN，全部FAIL保留，
R:已撤銷，最終審查未執行，P11總體未完成。Workflow重構、
Docker驗證、dependency audit、GHCR credentials、live GitHub Actions、publish、readback、
stage、commit與push均尚未因本ADR執行。後續每一批必須依維護流程另取計畫審查、最終審查
及對應外部動作核准。

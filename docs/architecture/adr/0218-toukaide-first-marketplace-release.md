# ADR-0218：ToukaIDE Marketplace 發行邊界

## 現行0.3.0發行批次（2026-09-15）

維護者要求更新MCP計畫並處理ToukaIDE新版發行；本輪技術審查
不宣稱嚴格獨立。本輪版本0.3.0／tag v0.3.0／ID touka-ai.toukaide，
反映已完成modern-only來源及legacy入口退役；實際source／VSIX／Windows smoke／Marketplace發行與回讀仍分別驗收。
工具鏈對齊現有package SSOT：Node24.19.0／npm11.19.1／vsce3.9.2，原builder的npm11.17.0漂移本輪修正。
精確source/tag、root LF LICENSE／MIT、create-only單一VSIX／canonical manifest、strictUTF8與shared10-member合約保持。
authority的0.2.0版本身份仍屬未選provider-v3 profile，不隨IDE升版或啟用。

本輪一次授權取代下方歷史的逐步確認程序；必要技術證據不省略。Marketplace登入仍由維護者完成，
僅使用既有帳號／方案，不讀credential/PAT。原工具不支援native UI時，必要Windows GUI／network觀察由維護者見證；
CLI安裝/version不等於GUI smoke，必要證據未取得不得發布。完整現行步驟見[發行指南](../../guides/toukaide-marketplace-release.md)。
0.2.0與全部拒絕候選／原失敗保留；新版本不覆寫舊tag/產物、不更新維護者日常使用的profile，也不改Touka runtime/data。
選定本機P11與GHCR後續已完成，見ADR-0220／0227；Azure／Open VSX／八個IDEhost lanes與provider-v3仍NOT SELECTED。

## 以下為0.2.0原始決策與見證歷史

下面全部狀態、版本、工具chain與逐步授權只代表當時；不代表目前待辦或新版已通過。

# ADR-0218：ToukaIDE 優先的 Marketplace 人工發行邊界

- 狀態：✅ implementation committed；ADR-0219安全修復與本機lightweight `v0.2.0`同指`94aa17976b7262bdda54df98dc1254dae072590c`；Marketplace upload／readback已由維護者人工見證，官方審核通過為維護者回報，remote byte identity未證
- 日期：2026-08-27（2026-09-04 狀態同步）
- 決策者：維護者
- 影響範圍：ToukaIDE npm lock、Marketplace candidate builder、VSIX evidence manifest、Webview發行安全、IDE發行文件與測試
- 相關決策：[ADR-0217](0217-public-release-authority-v3-bootstrap.md)、[ADR-0219](0219-toukaide-webview-plaintext-security-boundary.md)、[ADR-0220](0220-local-only-github-ghcr-release-profile.md)

> **2026-09-04 狀態修訂**：維護者回報 Visual Studio Marketplace 官方審核已通過；
> 這是外部審核狀態，不證明已上架 VSIX 與本機 candidate 的 remote-byte identity，也不替代
> 後續版本的 Windows＋VS Code Stable 最小 smoke。ADR-0220 將八個 IDE host lanes 與 receipt v3
> aggregate 列為 `NOT SELECTED`／dormant；目前選定的下一發行面是 Docker／GitHub Actions／GHCR，
> 仍為 `implementation pending`。

## 背景

ADR-0217 把 OCI、八個 IDE host lanes、Azure authority、reusable workflows 與 receipt v3 放在同一個
離線 bootstrap 中。該設計可以保留為未來的高保證自動化候選，但它不是目前發布 VS Code extension 的
必要條件。Touka 現階段是個人本機使用的單機 Agent 系統；公開面只規劃 GitHub 原始碼／文件、GHCR
映像與 Visual Studio Marketplace 的 ToukaIDE。

維護者決定先以最小、可人工核對的流程發行 ToukaIDE。GHCR 打包、容器驗證與試跑是後續獨立階段；
Azure runtime、Open VSX、private runner、OIDC 自動發布與跨平台 extension-host matrix 都不在本階段。

## 決策

### 1. 本階段只有一個產品與一個外部目的地

- 產品：ToukaIDE `0.2.0`，Git tag `v0.2.0`，extension ID `touka-ai.toukaide`，MIT。
- 外部目的地：Visual Studio Marketplace。
- 發布者由人類在 Marketplace 網頁登入並手動上傳 exact VSIX；AI 不接觸登入、PAT 或 publisher credential。
- Open VSX 不建立 namespace、不登入、不上傳。GitHub source push 與 GHCR image release 都要另走 fresh gate。

### 2. ADR-0217 的 aggregate activation 保持 dormant

既有 ToukaReleaseAuthority、Azure adapter／IaC、receipt v3 與兩個無 caller reusable workflows均保持
inert，不刪除也不啟用。它們不是本次 Marketplace 人工上傳的 prerequisite，也不能因 ToukaIDE 發布成功
而被宣稱已驗證。ADR-0217 所述八個 IDE host lanes與Azure authority只適用於未來另案採用該自動化鏈時。

### 3. 建置工具鏈與套件必須精確

- build runtime固定 Node.js `24.19.0`／npm `11.17.0`。
- `@vscode/vsce` 是 `ToukaIDE/package.json` 與 lockfile 的直接 exact pin `3.9.2`。
- 不加入 `@vscode/test-electron`；本階段的 Windows VS Code smoke由人類在隔離 profile執行。
- lockfile只能由 exact npm 11.17.0經核准的 official registry transmission重生，不得手工拼接 transitive entries。

### 4. 一次建置、外部 create-only 候選

`ToukaIDE/scripts/build_marketplace_candidate.py` 是 Marketplace candidate composition入口：

1. 只檢查 root `LICENSE` 與 `ToukaIDE` release inputs，要求 clean；不讀取 `data/`。
2. 要求 `v0.2.0` 解析到當前完整 HEAD SHA。
3. 驗證 Node、npm CLI package與repo-local `@vscode/vsce` exact版本；禁止 `npx` fallback。
4. 先在 source tree執行既有 production bundle，再重驗 source identity。
5. 只複製 package projection到repo外暫存；移除 staged `vscode:prepublish`，避免 `vsce` 第二次建置。
6. Root `LICENSE` 由`.gitattributes`固定LF checkout，從root注入hash-bound MIT bytes；不建立tracked `ToukaIDE/LICENSE`。
7. 以 Node直接執行已驗證的npm `npm-cli.js`與repo-local `vsce` JavaScript entry；不執行批次檔或shell字串。Child stdout／stderr一律先以bytes擷取，再strict UTF-8解碼；locale不可決定解碼，非法bytes必須以不回顯原始輸出的generic錯誤fail-loud。
8. 真`vsce 3.9.2` raw archive inventory必須先與release authority的exact 10-member SSOT同集合且同數量；其中vsce canonicalized README／LICENSE成員名稱也屬合約。通過後才由deterministic verifier正規化並輸出一份universal VSIX與canonical manifest。

最終 output directory必須位於repo外、原本不存在，且只包含：

- `toukaide-0.2.0.vsix`
- `toukaide-0.2.0.manifest.json`

manifest schema固定為`touka.ide.marketplace-candidate.v1`，記錄source commit、release tag、version、
publisher、license、extension ID、archive SHA-256與逐member size／SHA-256；不含時間、host path或credential。

### 5. Candidate、smoke、upload與readback是獨立 gates

- 最終審查、exact stage、commit與tag各自需要人類核准。
- Candidate build只在核准commit已clean且`v0.2.0`已錨定後執行。先前三次失敗流程依序揭露root LICENSE checkout CRLF、Windows locale解碼與真vsce raw inventory漂移；live-parity修復後建立的`f9e78c3`候選雖通過composition驗證，但manual-smoke preflight發現ADR-0219 Webview安全阻礙，因此該目錄與manifest只能保留為拒絕證據、不得發布或覆寫。修復後必須使用新的commit、tag anchor與create-only output directory。
- ADR-0219修復提交後，新的create-only候選以`94aa17976b7262bdda54df98dc1254dae072590c`／`v0.2.0`
  為錨，local exact VSIX SHA-256為
  `688d69d0b6bb71bcbd3f7d689d838cd34c4999265c606adb507d735d4a678a62`；舊`f9e78c3934d5`
  候選仍固定`DO NOT PUBLISH`。
- Windows VS Code smoke使用獨立user-data／extensions目錄與空白workspace，不連接或檢查既有Touka個人資料。
- Windows smoke另須驗證封裝內Webview無遠端script請求，惡意HTML／事件屬性與Markdown標記只顯示為文字，且原始換行保留。
- 人類只上傳manifest SHA-256對應的exact VSIX；不得在upload階段rebuild。
- 發布後另做public listing、install與version readback，記錄commit、tag、local SHA與Marketplace結果。

任一步失敗或結果模糊都停止，不自動retry、repack、覆寫`0.2.0`或改走其他registry。是否撤回、修復後建立
新版本或接受residual，由人類另行決定。

## 安全與操作邊界

- Builder child environment移除常見npm／GitHub／VSCE bearer變數，npm固定offline，subprocess使用固定argv、
  resolved executable／package-local entry與`shell=False`。
- Raw archive成員名稱視為不可信輸入；inventory mismatch只輸出排序、ASCII escaped、逐項截斷且固定筆數上限的診斷，不得把任意長路徑或控制字元原樣寫入log。
- Webview必須符合ADR-0219：只載入VSIX內script/style，所有動態內容只走`textContent`，0.2.0不解析Markdown／HTML。
- 自動測試只用pytest temp sandbox與fake runner，不登入Marketplace、不啟動VS Code、不碰`data/`。
- Docker、GHCR、audit、Azure、GitHub Actions、Open VSX與push未因本流程獲得授權；已發生的stage、commit、
  tag、candidate、smoke、upload與readback均各自經維護者當次核准，互不構成其他動作的授權。
- npm registry transmission只授權exact依賴與lock/node_modules重建；它不授權VSIX upload或任何publication。

## 目前人工發行證據

- 維護者人工見證本機候選的隔離Windows／VS Code smoke全部符合，並在Marketplace網頁完成單次手動upload。
- READBACK-R1在全新隔離profile從Marketplace安裝`touka-ai.toukaide@0.2.0`，其後版本、啟動、側欄／
  commands、惡意HTML／Markdown純文字顯示與換行的人工smoke均由維護者回報符合。
- 此紀錄沒有Marketplace回傳的artifact digest或其他可證明遠端bytes與上述local VSIX全等的證據；因此只能
  標示為維護者／human attestation，不能標示為exact-byte readback。
- 本流程不建立或驗證receipt v3，不啟動ToukaReleaseAuthority production provider，也不解鎖P11-C、
  provider-backed qualification或sunset decision；Git push亦未發生。

## 後果

優點是把真正必要的VS Code extension發行流程縮到可理解、可人工核對的最小集合，避免雲端authority與
容器發版阻塞ToukaIDE。代價是本階段只有Windows人工smoke證據，沒有Linux/macOS／其他CPU的真host
證據，也沒有自動發布與credential-less OIDC鏈；文件與發布紀錄必須如實標示這些residual。
0.2.0另以純文字顯示換取零runtime CDN與較小DOM攻擊面；未來恢復Markdown須另案審核，不得在發版階段臨時加入renderer。

## 後續階段

ToukaIDE Marketplace人工流程完成後已停止。GHCR的三個產品映像打包、掃描、雙平台／試跑、publish與readback
必須重新提出計畫並經審查；不得沿用本ADR或Marketplace登入的授權。Azure與ADR-0217 aggregate activation只有
在維護者未來明確選擇自動化高保證release chain時才重新評估。

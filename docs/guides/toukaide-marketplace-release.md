# ToukaIDE Visual Studio Marketplace 發行指南

現行 Marketplace 公開版本 **0.3.0**／tag **v0.3.0**／ID **touka-ai.toukaide**／MIT，一份universal VSIX。
依ADR-0218／0219，使用既有Marketplace產品，不涉及Open VSX或runtime映像更新。
每次發行須有Owner當次授權，不構成永久Git／發行權限；既有版本與tag不覆寫。

## 1. 來源與提交前驗證

exact Node.js **24.21.0**／npm **11.19.1**／repo-local `@vscode/vsce@3.9.2`；先核對實際CLI與package／lock／devEngines。
從ToukaIDE目錄串行執行：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run typecheck
npm run compile
npm run package
```

原Python3.12 venv PATH置前，原生`python -B -m pytest`；完成focused VSIX／builder／文件tests、
適用完整回歸、guards、smoke與runtime/full依賴audit。
`webviewSecurity.test.ts`驗證純文字與換行、無遠端腳本／inline權限；modern transport／Bearer／peer-close／legacy404
需原測試通過。這些source測試不代替真實VS Code smoke。

原真vsce的opt-in parity使用exact Node及pytest synthetic staging，不建立產品候選且不得skip：

```powershell
$env:TOUKA_NODE_24_EXECUTABLE = '<Node-24.21.0的node.exe>'
python -B -m pytest tests/integration/test_toukaide_marketplace_vsce_parity.py -q
```

真raw archive的member清單必須與 `ToukaIDE/scripts/deterministic_vsix.py` 的 `EXPECTED_MEMBERS` 全等。
完整候選經審查後，只stage核准清單。核對遠端／本機v0.3.0尚不存在後，commit與tag錨定同一正式來源；
舊v0.2.0及其原件保留。`@vscode/test-electron`、npx及臨時下載packer不在此流程。

## 2. 建立一次正式候選

root LICENSE與ToukaIDE release inputs須clean，`v0.3.0^{commit}`必須等於HEAD。
`.gitattributes`須保證root LICENSE LF，且raw bytes符合 `deterministic_vsix.py` 固定的LICENSE SHA-256。依lock準備node_modules；
輸出目錄位於repo外、真實parent下、尚不存在。執行：

```powershell
python -B ToukaIDE/scripts/build_marketplace_candidate.py `
  --repo-root . `
  --output-dir <repo外的新目錄> `
  --node <Node-24.21.0的node.exe> `
  --npm-cli <npm-11.19.1的bin/npm-cli.js>
```

原builder自行執行一次正式production bundle，再驗source identity；staging移除prepublish避免vsce重建。
輸出只含`toukaide-0.3.0.vsix`與`toukaide-0.3.0.manifest.json`。manifest schema為
`touka.ide.marketplace-candidate.v1`，核對source_commit、v0.3.0、touka-ai.toukaide、0.3.0、MIT、
exact10members各自size/SHA256及archive_sha256。不得拿別的來源、重包或自填verdict代替。

## 3. 隔離Windows Stable驗證

使用新user-data、extensions、TEMP/TMP及空白workspace；不改日常VS Code profile，不連真Touka資料。
設定`touka.proxyUrl`／`touka.apiKey`空字串、`touka.enableInlineCompletion=false`、`extensions.autoUpdate="off"`。啟動會listen loopback3005並在TEMP寫discovery，
故先以metadata確認port可用；不停止不明程序、不覆寫原discovery。CLI安裝exactVSIX並回讀ID/version，只能證明安裝。

真正GUI需驗：extension正常啟動、Start Touka／側欄與主要commands載入；輸入
`<img src=x onerror="alert(1)">**bold**`，以 Shift+Enter 換行後按送出，檢查實際對話泡泡逐字顯示，
沒有圖片／彈窗／Markdown解析且換行保留；僅看輸入框不算通過。proxy 未設定錯誤為此隔離測試的預期結果；
Webview不請求jsDelivr或其他遠端script。若可靠network觀察不可取得，記NOT_ATTESTED，不推定PASS。
原工具無native UI能力時由Owner執行精確受限啟動與見證；代理不自製UI控制繞過。所有必要smoke證據取得前不得上傳。

## 4. Marketplace上傳與回讀

只用既有touka-ai publisher帳號及方案；登入由Owner自行完成，不抽取PAT／session token。
核對產品ID/version與public README，指定manifest原SHA256對應的exactVSIX上傳一次，upload時不bundle或repack。
結果不明／timeout／版本衝突先停止變更並回讀遠端狀態，不盲重送或換registry。

發布後核對public listing/version，再由官方下載入口取得遠端VSIX並比對local archive SHA256與原10-member內容。
先保存HTTP headers／bounded body；有Content-Encoding時依宣告有界解碼，分別記transport與archive SHA，不能把gzip wire bytes當成VSIX。
若outer ZIP表示法不同，分別記錄outer-byte差異與完整member paths/sizes/SHA256；未證明全等不稱exact-byte readback。
以新的隔離profile從Marketplace安裝新版，再驗版本及必要Windows smoke。只驗本機Windows Stable，不外推其他host。

## 5. 當前狀態與回復

`0.3.0` 已在[Marketplace](https://marketplace.visualstudio.com/items?itemName=touka-ai.toukaide)公開。[PR56](https://github.com/OtohaTouka/Touka/pull/56)
把來源 `58e63e4361a947b4f22a0d025d9dc2ab0d9661d5` 合併為 main／dev 的 `ff711dc622ee76274289af993d5530a85af780a0`；
原 builder 在該 clean source／local `v0.3.0` 建置一次。後續文件 commit 不改這個 build source，也不重建VSIX。

- Archive：`toukaide-0.3.0.vsix`，1829063 bytes，SHA-256 `1e2e6cc444d7f4102d49cd70cc006a39ca56b477ff047985dcd18ff94ae40266`。
- Manifest：`toukaide-0.3.0.manifest.json`，1663 bytes，SHA-256 `0e63c00f623b0fab47e79e39b3c5d426c16628bd0d4dfc2e510158195c29bee8`。
- Windows Stable 1.137.0／x64 隔離 profile 的發布前與發布後 GUI／Webview network 檢查均由 Owner 人工回報通過；
  Marketplace 下載的 VSIX 解碼後與原 members 全等，package.json 只多官方 `__metadata`。

**`v*` tag 注意**：`v0.3.0` 只建立於本機、未推送遠端。runtime 映像的 `.github/workflows/ghcr-publish.yml`
只接受手動觸發，推送 IDE 的 `v*` tag 不會發布 runtime 映像。

Source可普通revert；未發布候選保留原VSIX/manifest；已發布版不可覆寫，必要修復需新的版本決定。
不清理Owner資料或舊發布。

## 歷史 0.2.0（不作新版PASS）

- 核准commit與本機lightweight tag：`94aa17976b7262bdda54df98dc1254dae072590c`／`v0.2.0`；
  Local exact VSIX SHA-256 `688d69d0b6bb71bcbd3f7d689d838cd34c4999265c606adb507d735d4a678a62`。
- Owner回報隔離VS Code smoke與Marketplace安裝回讀均符合，並在Marketplace網頁手動upload。
  Repo內沒有遠端bytes與local VSIX全等的證據，只能視為Owner人工見證，不能稱exact-byte readback。
- 舊`f9e78c3934d5`候選持續固定`DO NOT PUBLISH`。

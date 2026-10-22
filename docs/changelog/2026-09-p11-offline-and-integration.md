# 更新歷史：2026 年 9 月 P11：離線、原生整合與初期 hosted

> 本檔保存原 CHANGELOG 的歷史紀錄，保留當時的 PASS／FAIL／UNKNOWN／NOT_RUN／HOLD／REUSED_NOT_FRESH 與限制；舊「current」、批准與命令不代表現在可執行。唯一現況見 [HANDOVER](../../HANDOVER.md)，有效規範見 [AGENTS](../../AGENTS.md)；返回 [更新摘要](../CHANGELOG.md)／[歷史索引](README.md)。

> 本檔是開源前的歷史紀錄；文中的 PR 編號、commit 與 run 編號指的是私人歷史。依各紀錄明示日期與階段分類，檔內維持原文相對順序。

---

<a id="entry-0068"></a>

- **2026-09-08：PR #4 後的 Q4 掃描資料與封存預算修復**。main `1f321275c14aa14ffd25ed05d0aba730af41f168` 與來源`39c6366f62df86da62e07e1b9d89774b5f1c8a85`同tree；run34233634263／a1為`LOCAL_RELEASE_SCAN_DATA_FILE_SIZE`，實際file／size UNKNOWN、0 artifacts、publication skipped。官方有限TAR header各觀測ordinary DB1371025408B、Java DB1520816128B，不冒充exact Q4或完整layer hash；兩類payload改獨立2 GiB，metadata16 KiB／checks非空8192 entries及64 MiB總量不變。完整串流hash加入逐塊deadline／capacity與growth檢查，三capture入口受控；封閉seal模式統一原job／phase預算，成功退出前檢查並finally重置，真正cleanup保留。R1 35F／131P→166P、R2 5F／168P→173P及2項SLF001靜態FAIL保留；最終173 focused、49 adjacent與Ruff／format通過，scan82／CI91（共新增52），無新NOQA。完整6078 passed／10 skipped／5 warnings及Guard #1／#42／#44通過；未變smoke24、Node593、audit91及其餘守衛依適用性重用，ADR #28／doc ratchet另驗，不宣稱fresh44或新advisory查詢。原Q1～Q4及未知欄位保留；修復後hosted、三映像／modern-only／完整rollback／P11仍未完成。


<a id="entry-0069"></a>

- **2026-09-08：PR #3 後的 Docker 工具 ENV 順序與 mock 修復**。PR #3 合併為 `a8b18e2cbc88819141393ce956f23cac6c077530`（tree等於來源`2995b36c240dc607b6e9b164b9bee1c1e44cb12e`）；Q3 run34203760152／a1為`LOCAL_RELEASE_TOOL_BEHAVIOR`，實際不符工具／欄位UNKNOWN、0 artifacts、publication skipped。依Moby caller-first語義修正mock取得22F／53P，再修產品預期Env順序，保留完整列表與其他安全比對；新增閉集工具／欄位診斷，無原始值。新增19例後75 focused通過，完整原生6025 passed／10 skipped／5 warnings與Guard #1／#42／#44通過。其後test-only掛載mock的readonly子字串問題以1例RED→逗號option解析修復，最終76 focused通過（原56＋19＋1），產品bytes未變，完整回歸原count不改寫。Ruff／format通過，既有NOQA warning與排版FAIL保留，無新增NOQA；其餘39guard／smoke24及Node593／audit91適用證據重用，非fresh native44或current advisory。Q1／Q2／Q3歷史FAIL不覆寫，也未證明Q3當時就是Trivy Env。此候選未重跑hosted，P11／容器發布／modern-only／完整rollback仍待完成。


<a id="entry-0070"></a>

- **2026-09-08：PR #2 合併後的 Q2 命令診斷、Skopeo pin 與 modern companion**。main `ed3f2e99be210bbf2081ea99f7e52a79cc7b6411` 的 tree 與已驗收 `39a920b64d177015a63a48790e3857171144171d` 相同。run **34194185788／attempt 1** 為 `LOCAL_RELEASE_CI_COMMAND_FAILED`，內部 phase UNKNOWN、0 artifacts、publication skipped，不推定 zero builds。命令診斷新增10例（CI56）；原 Skopeo root 404／MANIFEST_UNKNOWN 保留，僅更新同一官方 stable root／amd64 digest，metadata 原始鏈已驗且聚焦226 passed／1 warning、Ruff／format通過；config label 1.22.2 不代表已執行 CLI。新增6例真 Proxy→Node loopback modern companion，驗證工具／認證與雙請求取消隔離，兩行固定子程序 S603 登錄。九檔來源重驗取得6006 passed／10 skipped／5 warnings與canonical Guard #1 PASS；其餘41道non-audit、smoke24、未變來源靜態／MkDocs116、Node593與audit91按適用輸入重用，非fresh native42／44。首輪Guard #1 ENV違規與52份FAIL保留；改用get_full_env的R1漏傳SystemRoot、esbuild134／6cases NOT RUN保留，R2 casefold修正後6 focused與SSOT／Ruff／format通過，無新增ENV NOQA。未重跑 hosted，P11／三映像／modern-only／完整rollback 未完成；原 Q1／Q2 FAIL 不覆寫。


<a id="entry-0071"></a>

- **2026-09-08：PR #1 合併與首個 hosted HTTP 入口修復**。已驗收來源 `88487508bc76a56046f7d62d985d264aa4263d3c` 經一次精確 push／PR 合併為 `b225a0ea3a3c70ca09d8b6710f8438fc3d561131`，官方回讀確認兩者檔案樹相同；先前兩次推送遭自動審核拒絕的紀錄保留。hosted run **34188101199／attempt 1** 在 `LOCAL_RELEASE_HOSTED_HTTP` 失敗，0 artifacts／0 builds，publication skipped。原 hosted log 未保留HTTP status；本機以相同 Python `http.client` 匿名請求的無UA403／補UA私人資源404對照定位缺少 GitHub `User-Agent`，不將本機status冒充原run觀測或權限證明；四檔修復共享 headers 並保留安全 phase/status 診斷，行為 RED 後 **138 focused passed／1 既有 audioop warning**，經審查接受；CI46／hosted92（新增12），0新增NOQA。七檔完整原生回歸取得 **5990 passed／10 skipped／5 warnings**、42 non-audit、smoke24、精確4檔Ruff／format及sandbox MkDocs116 warnings無增減，技術結果經審查接受；Ruff exit0的2項自訂NOQA語法warning如實保留，舊stderr無這2項紀錄，未新增NOQA。91份audit與593份Node適用輸入不變，接受新42＋既有2的組合覆蓋；原生INCOMPLETE／#22、#23 NOT RUN不改稱fresh44。修復後hosted資格仍待完成，原FAIL保留。維護者確認所有 client 都在同一台機器上使用、沒有保留舊版相容性的需求；具體client版本／流量未量測，modern companion／exact removal／完整rollback gate不變。IDE八檔本機回復封包掃描零發現並通過ZIP回讀，持久保存及真host smoke仍待完成。


<a id="entry-0072"></a>

- **2026-09-08：selected local release本機整合驗收**。基線101-path提交 `0376ec0523ae02925d4f13ff68cc87693f11664b` 後完成CI／builder／hosted／runtime／scan／admission／registry接縫。44-path R1為5978 passed／10 skipped／5 warnings、42 non-audit、smoke24、Ruff／format與MkDocs116 warnings全通過；新42＋適用輸入未變的既有2道audit的組合覆蓋經審查接受，不冒稱fresh44。首輪5967 passed／3 failed、Guard #1 FAIL與全部早期FAIL保留。真hosted tiny／三映像／publication未執行，未知consumer／modern／rollback仍未完成；source push兩次遭自動審核拒絕、實際0次push，未改用其他通道。


<a id="entry-0073"></a>

- **2026-09-08：101-path原生整合的累積最終審查經審查接受**。LC-A／audit-retention與依賴／SNS來源切片整合後，完整Python **5496 passed／9 skipped／4 warnings**、default **44/44 PASS**、六scope fresh audit **0 finding**（Python110/110與152/152均0skip；Node四scope各severity0）、smoke24、Ruff34paths與sandbox MkDocs通過。MkDocs warning multiset保持116；136 evidence＋101candidate＋2075source＋9runtime共2321hash／size核對，Node593inputs一致，重用原八項完整結果。原full pytest的4395 passed／3 skipped／1107相同WinError5 setup errors保留；R1修正經審查接受，用pytest正式 `PYTEST_DEBUG_TEMPROOT` 與既有路徑guard建立fresh owned root，原case先1pass再full GREEN，不改ACL／舊根／產品或另建環境。9skip及Qdrant Temp lock殘留如實記錄；P11、container qualification、consumer／rollback與GHCR發行仍pending。證據未入版控；本批未stage。


<a id="entry-0074"></a>

- **2026-09-08：P11 coverage、SNS 與 Python／Node 依賴修復切片經審查接受**。ADR-0223 的完整 query／CPU advisory 身分／first-party coverage 與 error-path 121 focused 通過；原六 scope findings 保留。Python 三個 build/test 工具定向更新後，162 focused、既有 root venv／五份 Guard16 cache、Guard30／pip check 通過，fresh runtime 110/110 與 build-test 152/152 均 0 skip／0 finding；四份修復來源與 148 份 evidence 核對接受。原 Windows mapped-file lock 寫入 FAIL 不覆寫；六 manifest＋原 lock 的暫存解析與 atomic replace 不建完整外部環境。
  SNS 13-path 修復移除 raw-secret storage，新增 28 cases、最終 40 focused；Node 27 lock rows 更新（Chat 18／IDE 9，僅新增 `@humanfs/types`）與 46 focused 接受。完整 Chat 1187 passed／233 files、TypeScript／Next 16.3.4 production PASS，lint 0 errors／74 warnings（缺逐筆原 baseline）；IDE 119 passed／19 files、typecheck／compile／package PASS。原 Chat full 1185／2 舊預期 FAIL 保留。未部署，舊 browser clients 須載入新版才能清除 SNS key；Node 四 fresh scopes、完整累積最終審查／44 guards 與三映像發版仍未完成。


<a id="entry-0075"></a>

- **2026-09-08：P11 LC-A 本機發行合約／inert bootstrap 開始實作**。依維護者的九點委託，經審查接受精確七份 source/test/workflow；[ADR-0225](../architecture/adr/0225-local-release-profile-and-evidence.md) 與索引同步建立。先取得 OCI／來源／權限行為 RED **44 failed／2 passed**，早先 namespace collection failure 另存且不計 RED。追加四項對抗 RED 保留；最終新 60＋既有 29 共 **89 passed／1 warning**，Ruff、Guard #42、exact-file SSOT 通過，切片已通過最終審查。USTAR-only preflight 尚待真 BuildKit exporter 驗證；新 workflow 保持 implementation pending 非零，舊 provider gates 不變。LC-A 不包含真 build、qualification、publish、readback 或 Git 寫入。


<a id="entry-0076"></a>

- **2026-09-08：P11 Python R2 更新 Torch CPU 與 runtime setuptools，完成本機回歸**。經維護者人工核准依賴修復計畫 R2 與其網路存取，兩個 embedding 成員精確宣告 Torch **2.13.0**，單次 uv lock 及既有 root sync 安裝 **2.13.0+cpu／setuptools83.0.0**；168 rows 無新增套件、GPU rows／edges或無關升級，原三項 R1 修復維持。原五類 cp312 wheel 完整，新版額外的官方 Windows ARM64 wheel 只記為 artifact；不宣稱該平台相容性。原檢查器排他集合造成的 FAIL 保留，補正反測試 **1 failed／1 passed → 2 passed**後改為要求既有覆蓋，所有來源／SHA256／CPU／版本／marker 檢查保持，未重跑解析或手改 lock。
  R2 有效 RED **4 failed／27 passed**（三項舊版真 CPU encode 基準通過）；新版聚焦 **84 passed**、完整 native Python3.12 **5342 passed／9 skipped／3 既有 warnings**、sandbox smoke **24/24 PASS**；五份原 Guard16 cache 更新／匯入 **5/5 PASS**。兩個測試模組共 **33 cases**，含真實本機 BoW→Dense→Normalize、零向量、VectorStore encode/cache及 CPU 圖正反邊界；無模型下載或新完整環境。Ruff通過；non-audit守衛、MkDocs與完整70-path候選的最終綁定證據未入版控。全部網路僅7個核准top-level uv程序，零重試；原A失敗與audit不覆寫。原R1已通過最終審查，R2候選仍待新的最終審查；新audit／coverage與SNS另案、stage／commit／發布未完成，P11整體未完成。


<a id="entry-0077"></a>

- **2026-09-07：P11 Python R1拆分更新成功，runtime setuptools仍待修復**。維護者人工核准依賴修復計畫 R1 與其網路存取，在原8檔範圍定向更新aiohttp **3.14.3**、cryptography **50.0.0**、h2 **4.4.1**；168 lock rows沒有新增或無關升級，既有root venv已安裝三個新版本。ReleaseAuthority獨立build-system使用setuptools **83.0.0**並由installed WHEEL Generator確認；Torch **2.12.1+cpu**與runtime setuptools **81.0.0**保持原值，PYSEC-2026-3447的runtime範圍仍Known Open。先RED **6 failed／4 passed**，新測試10例全綠，聚焦 **113 passed**（AES-GCM／TLS／簽章正反回歸等），完整native Python **5319 passed／9 skipped／3 warnings**，smoke **24/24**、non-audit guards **42/42 PASS**；Guard16既有五份cache刷新 **5/5 PASS**，Ruff check／format通過。網路僅7個核准top-level uv程序、零整批retry，未另建整套外部產品環境。原A衝突FAIL與audit證據保留，新增測試／清單、安全狀態及交班同步；本批完整67-path候選待新的最終審查，精確輸出與最終驗收證據未入版控。新audit／coverage缺口／SNS另案仍未完成；stage、commit及發布未執行，P11總體未完成。


<a id="entry-0078"></a>

- **2026-09-07：P11 Python套件定向更新遇到可重現的依賴約束衝突**。經維護者人工核准依賴修復計畫 A 與其網路存取，先完成7個有效RED與4個合成正反GREEN，Ruff check／format通過。單次uv lock（2.73秒、exit 1）證明`torch 2.12.1+cpu`要求`setuptools<82`，與83.0.0修復目標互斥；依核准停止條件不重試。未執行root sync或member cache刷新，uv.lock及安裝版本清單未變。失敗來源、原始輸出與receipts完整保留（證據未入版控），本次未完成來源／測試改動已精確回復，未把RED測試留在現行suite。先修三套件與獨立build backend的R1替代計畫待核准；Torch／runtime setuptools與coverage缺口仍未解決，P11未完成。前批63-path報告修復已通過人工最終審查，本次新狀態文件不沿用舊候選核准，未stage／commit／audit或發布。


<a id="entry-0079"></a>

- **2026-09-07：P11首批Python audit取得真實結果，接續離線報告修復**。維護者以人工最終審查通過63-path R1，並核准一次audit網路存取；版本probe、offline export、runtime audit共3個child。111筆dependency rows中109筆已查核、2筆skipped；aiohttp／cryptography／h2／setuptools回報7筆原始findings（去重6筆，UNRATED）。Guard #22因合法skip row沒有`vulns`而exit 1；停止後續Python build-test及Node scopes，零retry／依賴變更。147份原始證據與FAIL另行封存（證據未入版控），不因後續修復覆寫。報告修復計畫已經維護者人工核准，限定7個既有檔案與原生sandbox／mock；合法skip完整呈現findings與原因後仍INCOMPLETE阻擋；有效RED 4 failed／44 passed，GREEN 50 passed（新增19例），完整Python **5309 passed／9 skipped／3 warnings**、non-audit **42/42**、smoke **24/24 PASS**，Ruff check／format通過且無新NOQA。舊JSON離線replay保留7 findings＋2 skips，原證據不變；pytest完成後外層cp950列印錯誤另存，不重跑掩蓋。修改後候選待新的最終審查。實際依賴與coverage缺口見根目錄`SECURITY.md`的Known Open，判讀契約見[ADR-0222](../architecture/adr/0222-dependency-audit-gate-hardening.md)；本批不升級套件或重跑network audit，P11仍未完成。


<a id="entry-0080"></a>

- **2026-09-06～07：P11 audit 前置接續完成**。既有Guard #22／#23補create-only原始bytes證據、明確npm零retry、Python scope cache／timeout與Windows必要SystemRoot；新增16個audit回歸。smoke跨磁碟負例沿用sandbox並加2例。完整Python **5290 passed／9 skipped／3 warnings**、smoke **24/24**、non-audit **42/42 PASS**；兩個CLI環境讀取採精確登記，不放寬scanner。啟動／驗證入口與首輪guard失敗全部保留。63-path完整候選與待核准network範圍見接續計畫（未入版控），未audit、最終審查、stage／commit或發布。


<a id="entry-0081"></a>

- **2026-09-06：維護者改採 P11 Next 原生驗證優先**。沿用專案pytest／Vitest／guards與既有sandbox，允許必要tests／guards／mocks修正；先前repoWritePaths=[]、完整source/node_modules外部投影與launcher限定不再套用新工作。新增aggregate non-audit／dependency-cache-only入口，Docker探測延至明確opt-in執行期，Guard16使用既有smoke sandbox並保留member依賴隔離；三scanner在metadata／下探前排除data及連結，靜態測試負向fixture移至tmp_path。原工作區離線npm ci重用既有cache，未改manifest／lock或下載；Python **5272 passed／9 skipped／3 warnings**，Vitest **1159 passed／230 files**，root TypeScript／Next16.3.4 build、non-audit **42/42 PASS**。舊的除錯計畫與R1／R2失敗及未執行證據保留，七份暫存文件以sidecar註記取代舊指示（皆未入版控）；完整驗證與依賴安裝狀態見接續計畫（未入版控）。#22／#23與audit NOT RUN，最終審查未執行，不宣稱P11完成。



<a id="entry-0082"></a>

-   **2026-09-06**: **P11-NEXT-TEST-TYPES-REMEDIATION-A：Next build已解除；Guard #17 launcher轉除錯交班⭐**
    - 本輪追加13份既有測試＋8份文件，保留32-path候選並形成45-path人工審查候選；只補fixture／mock型別，原62個案例及全部行為斷言保留，未改產品來源、generated API、檢查設定或依賴。原47則TypeScript診斷作RED；首輪62 passed後另見SkillsTab工具description缺欄（9則），授權內補齊後 **62 passed／13 files、lint 0 errors／1既有warning、root TypeScript零診斷、Next.js 16.3.4 production build PASS**。
    - 上一輪artifact generator、完整lint（0 errors／78 warnings）與產品型別檢查已PASS；完整Vitest 1159／230 files發生於上一輪最後test-only標註前，本輪依核准未重跑全量或cache-seed。
    - 非audit守衛 **#1–#16 PASS**；#16空sandbox設定以既有tracked examples hydration解除。#17三次均在收集前被repo外Popen包裝阻塞（class繼承／generic下標／Windows ver遭allowlist拒絕），未發生產品測試斷言失敗；依同方向三次即停止的規則轉入根因分析，不做第四次。#17未通過、其餘25道非audit守衛與完整Python suite **NOT RUN**；#22／#23與audit NOT RUN、44-guard INCOMPLETE、最終審查未執行，P11總體仍未完成。
    - 全部舊／新FAIL另行保留（證據未入版控）；臨時磁碟代號映射已撤銷。HEAD仍為`03697f1b9c9c5a9e5bed8c91e329a0e181f7449f`、index空；未install、更新依賴、連網、audit或Git寫入，未存取`data/`。SNS raw-token localStorage Known Open HIGH另案待修，沒有外洩證據，不能宣稱security clean。

<a id="entry-0083"></a>

-   **2026-09-06**: **P11-NEXT-LINT-REMEDIATION-A 32-path 累積候選（計畫審查階段，人工審查）；資格驗證未完成⭐**
    - 本輪19-path（6來源／5測試／8文件）合併既有21-path。5個翻譯callback補`t`、SNS fetch補穩定setter依賴、主題守衛以型別縮窄移除`any`，不更改規則或新增依賴。5份測試新增9個案例：5個翻譯回歸＋4個主題characterization。有效RED **23 passed／5 failed**，修正後focused **28 passed／7 files**。
    - 既有sealed npm cache／node_modules及5 Guard caches preflight匹配；重用repo外projection與臨時磁碟代號映射，本輪未install、重鎖或連網。artifact generator、ESLint **0 errors／78 warnings**、`tsconfig.guard`產品型別檢查PASS；完整Vitest **1159 passed／230 files**發生在最後兩處test-only型別標註修正前，保留該證據邊界。
    - Next build編譯成功、TypeScript FAIL：初次54 diagnostics／15份測試檔；僅修授權範圍內MessageItem role與useChatStream mock型別，focused再次**28 passed**，再次build剩**47 diagnostics／13份範圍外、未修改的既有測試檔**。未擅自擴大19-path寫入範圍；本輪完整42道驗收及完整Python suite **NOT RUN**，計畫審查的資格驗證未完成、最終審查未執行，P11總體未完成。
    - 舊R3／R4、本輪bootstrap與兩次build FAIL皆保留；repo外的`Final`及`BuildResume` outcome均`FAIL_STOPPED`（證據未入版控），臨時磁碟代號映射已撤銷。HEAD／空index未變，未存取`data/`；audit、stage、commit、push、publish與release均NOT RUN。
    - SNS raw token可能經設定hook寫入localStorage，列為**Known Open HIGH**，未有外洩證據，維護者指定另案修復；本輪未改token生命週期。MCP後續仍依安全待辦／dependency audit、modern-only、Docker／GitHub Actions／GHCR推進，未選的雲端高保證項目不新增為完成前提。


<a id="entry-0084"></a>

-   **2026-09-04**: **P11-NEXT-SECURITY-REMEDIATION-A 21-path 累積候選（計畫審查階段，人工審查）⭐**
    - 原15-path audit foundation納入本候選，再加入ToukaChat `package.json`／`package-lock.json`、`test_cve_2026_75604_nextjs_security.py`、P8-B供應鏈回歸、ADR-0220及public-release guide共6條路徑。`next`與`eslint-config-next`精確釘`16.3.4`；Next上游`sharp ^0.35.4`滿足ADR-0174退場條件，移除root `sharp` override並解析`sharp 0.35.4`；React／ReactDOM維持`19.2.3`，無`react-server-dom`。
    - 官方Node `24.19.0`／npm `11.17.0`在repo外、ignore-scripts／no-audit投影完成精確重鎖；42個lock entry變動全屬Next／ESLint／Sharp因果鏈，1322 rows的official-registry URL＋SHA-512完整性通過。716180-byte lock SHA-256為`dd9aeffb78a0345e7bca56d15e9613c461f0d43d793642347a6bbf6f0c3818b6`，同cache強制offline重算後雜湊不變。
    - 有效TDD RED **3 failed**；lock回填後Next／供應鏈組 **18 passed／1 warning**，本批五個Python test modules合計 **71 passed／1 warning**，Ruff lint／format Green；`PYTHONHASHSEED=20260904`完整Python 3.12為 **5230 passed／9 skipped／3 warnings**（203.09s）。完整suite結束時repo外sandbox的Qdrant `.lock`經三次bounded cleanup仍占用，留給OS temp回收，未強制刪除。cache只有metadata、無tarball，現有`node_modules`仍是舊Next／ESLint／Sharp，故Vitest、lint、typecheck、build與local non-audit 42/42不得假跑或借舊安裝宣稱通過，等待fresh repo外 cache-seed核准。
    - `pip-audit`、`npm audit`、Guard #22／#23均 **NOT RUN**，完整44-guard **INCOMPLETE**；最終審查、stage、commit、Docker、GitHub Actions、GHCR、modern-only、push與publish均未執行。未讀取、列舉、修改或清理`data/`。


<a id="entry-0085"></a>

-   **2026-09-04**: **P11-DEP-AUDIT-FOUNDATION-A 離線實作候選（人工審查）⭐**
    - Guard #22改為Python runtime／build-test兩個lock projection，移除自動安裝；export只落repository-external temp，`uv`與pip-audit均以絕對執行檔、argument array、`shell=False`及credential-free最小env啟動。空／壞JSON、非0/1 exit、exit與payload矛盾、未知severity與tool缺席全數fail-closed。新Guard #23以同一aggregate精確覆蓋ToukaChat／ToukaIDE各runtime／full scope，Node直接啟動`npm-cli.js`，要求tracked public-registry `.npmrc`與official-registry lock entries，禁止fix／force／retry／install／re-lock。
    - TDD初始RED **34 failed／10 passed**；絕對runtime與最小environment加固另得RED **15 failed／25 passed**；parser cross-consistency加固RED **2 failed／5 passed／1 warning**；manifest／nested-lock加固RED **2 failed／19 deselected／1 warning**。最終focused **53 passed／1 warning**；候選6個Python檔Ruff check／format Green；固定seed `20260904`完整Python 3.12 **5226 passed／9 skipped／3 warnings**（207.05s）。完整suite結束時repo外sandbox的Qdrant `.lock`經三次bounded cleanup仍占用，留給OS temp回收，未強制刪除。
    - `uv export --offline --frozen`兩個scope均成功且未啟動audit：runtime為7434 bytes／SHA-256 `12789A96C2FD363E1DEEDB5655437A41CFF9A255C8AE913BFCBD84E22F59CB8B`，build-test為10029 bytes／SHA-256 `DC56687ECCAC7CF68275945CD3F6433F62D4E1F66A1D64A5341D317E4F1FAD78`。Guard #16先重算`uv.lock`＋五個member pyproject SHA-256，5/5均為current cache；以禁止`uv sync`與cache rebuild的wrapper完成5/5 import／Dockerfile parity，沒有安裝、更新或下載。當次local non-audit 42/42尚未完成：runtime preflight預期Node **24.19.0**／npm **11.17.0**，ambient Node為**26.2.0**；現存bundled Node 24.19.0不含npm、system npm為11.13.0，先前portable npm 11.17.0已被OS temp清理。因此未啟動需要exact Node/npm的canonical remaining batch；另獨立完成cache-only Guard #16及protected-root mirror中的Guard #1／#19／#28，未採fallback或改版。
    - 此為foundation形成時的邊界：當時`pip-audit`、`npm audit`、Next.js advisory查核及所有network audit均 **NOT RUN**，完整44-guard維持 **INCOMPLETE**，且未修改任何pyproject／lock／package manifest。該15-path foundation其後已納入上方21-path P11-NEXT累積候選；未單獨進入最終審查、stage或commit。


<a id="entry-0086"></a>

-   **2026-09-04**: **P11 offline audit commit closeout 與文件事實漂移修正候選⭐**
    - `P11-OFFLINE-FINAL-AUDIT-A 已完成並提交`：exact commit `0dc0681a4c1ba9c6c1649e87905f1ce921bba4c7`，parent `dad19dc2df019ae5b2fe05dd3da876fac188c7a1`。該提交只封存本機 offline audit 與文件 ratchet 證據，不代表 P11-C 或 P11 總體完成；P11 總體仍為**未完成**。
    - Guard #22 pip-audit／#23 npm audit仍為 **NOT RUN**，完整 44-guard 維持 **INCOMPLETE**。維護者已選定 Touka runtime／資料 local-only，後續依 dependency audit／ToukaChat Next.js remediation、modern-only cold cutover、Docker／GitHub Actions／GHCR 的順序處理；這些均為已選方向但 **implementation pending**，尚未取得實作、資格驗證或發佈證據。
    - 官方 Marketplace 審核通過僅為**維護者回報**；既有人工 upload／readback 不等於 Marketplace remote-byte identity，也不補足 GHCR 或 P11 發行證據。
    - 本輪文件事實漂移 ratchet 的起始 RED 為 **3 failed／3 passed**；最小文件修正後 focused GREEN 為 **6 passed／1 warning**。Random seed `4145842591` 的完整 Python 3.12 為 **5187 passed／9 skipped／3 warnings**；結束時暫存 sandbox 的 Qdrant `.lock` 仍被占用，三次 bounded cleanup 後留給 OS temp 回收，未強制刪除。MkDocs repo 外 build 與不含 `data/` 的 repo 外 ADR guard（221 份、0001..0221）均 Green。完整 44-guard、audit 與其他外部資格驗證仍未執行，本條不得解讀為最終審查、提交或發佈完成。


<a id="entry-0087"></a>

-   **2026-09-03**: **MCP P11-OFFLINE-FINAL-AUDIT-A 候選（計畫審查階段，人工審查）⭐**
    - 本機終局盤點只找到一項可修漂移：`AGENTS.md`任務前閱讀表宣告禁令1–61，但§18已連續定義到禁令62。新增`test_mcp_2026_p11_documentation.py`第三個case，鎖定禁令編號必須從1連續且表列最高號等於實際最高號；首個parser草稿未涵蓋半形括號而造成的失敗不列有效RED，修正test harness後取得**1 failed／1 warning**（`61 == 62`）有效RED，最小Green只把宣告改為`1–62`。
    - 驗證：19個P11 Python測試檔 **396 passed／1 skipped／1 warning**；固定seed `20260903`完整Python 3.12 **5184 passed／9 skipped／3 warnings**；ToukaIDE exact 10個MCP測試檔 **79 passed**且typecheck Green；Python→TypeScript generator `--check`、release workflow guard與release-control workflow guard均Green。
    - Local non-audit guards **42/42 PASS**。#1／#19／#28在全新、只含Git追蹤檔且不存在`data/`的repo外鏡像執行；#16只使用5/5 current dependency cache並禁止rebuild；#18 repo外create-only生成與tracked `error_codes.ts`逐byte全等；#20 vulture＋knip不啟用cache；#21 import-linter使用`--no-cache`。Guard #22 pip-audit／#23 npm audit仍 **NOT RUN**，完整44-guard保持 **INCOMPLETE**。
    - 稽核期間三個基建型非Green均排除於產品證據：mirror-only #27因Git忽略的`artifactRuntime.ts`未被archive收錄而找不到module，來源checkout `--noEmit`隨後Green且porcelain不變；#17／#34首次把`--basetemp`放在鏡像內，被既有policy於collection前以`PYTEST_BASETEMP_REPO_LOCAL`拒絕，改用鏡像外sibling後分別**1 passed／1 warning**與**19 passed／1 warning**。沒有重跑任何產品失敗。
    - 計畫核准的精確寫入範圍只有`AGENTS.md`、P11文件ratchet、migration proposal、`HANDOVER.md`、本檔與`tests/README.md`；沒有product／API／schema／ADR變更。未讀取、列舉、修改或清理`data/`，未啟動獨立審查、連網、provider／Azure、Docker／GHA／registry、stage、commit、retag、push、publish或remote readback。
    - 本機readiness不等於P11完成：provider-backed qualification、8個IDE host lanes／3個OCI indexes、remote-byte receipt v3、live release、Dispatch／IDE legacy usage=0滿一個release及維護者的sunset核定仍未取得。P11-C與P11總體維持**BLOCKED／未完成**；P9 Tasks維持獨立HOLD。本候選等待人工最終審查。


<a id="entry-0088"></a>

-   **2026-09-02**: **MCP P11-DOC-A current-facing文件與發行狀態收斂已完成並提交（`c7600caa`）⭐**
    - 計畫審查與最終審查皆由維護者人工通過。本輪新增2-case文件ratchet，掃描19個current-facing指南面：現行文件只能描述ADR-0116 integration-only、ToukaProxy同行程注入真實`ResourceManager`與缺失fail-loud，不得再宣稱ToukaMemory standalone MCP或`_FallbackResourceProxy`自動降級；歷史ADR／proposal／handover不套禁詞，以保留真實時序。
    - TDD有效RED為 **2 failed**；最小文件修正後focused **2 passed**。最終擴充ratchet覆蓋後，與`test_memory_resource_proxy.py`／P5相鄰回歸共 **39 passed／1 warning**，Ruff check／format Green。完整Python 3.12在最後一輪僅測試路徑擴充前為 **5183 passed／9 skipped／4 warnings**；其中producer fresh-process reader有一筆Windows child-output UTF-8 decode thread warning，exit仍為0，保留residual且不retry掩蓋。
    - MkDocs以全新repo外輸出build成功。SHA-256重驗的Node **24.19.0**／npm **11.17.0**離線完成local non-audit guards **42/42 PASS**：#1／#19／#28採protected-root-safe明列root，#16只驗證5/5 current cache與Dockerfile parity、未呼叫rebuild，#18 repo外create-only生成並與tracked TypeScript逐byte全等。#16 equivalent首個wrapper因未把`scripts/`加入import path而在cache檢查前停止；修正wrapper後一次通過，沒有cache寫入或下載。Guard #22 pip-audit／#23 npm audit仍 **NOT RUN**，故完整44-guard保持 **INCOMPLETE**。
    - ToukaProxy dual-era exact-version修復的exact 8-path normalized seal已提交為`d25b8a624242cd4ef675091ecc39e6f3370adff3`（parent `38f35d997fd4d385cd55c25b8c99ac0a7c3c01d9`）。本機lightweight `v0.2.0`仍指`94aa17976b7262bdda54df98dc1254dae072590c`；該P3 seal未retag、push或publish。
    - ADR-0217 offline inert bootstrap已提交為`778b0e58794db49571dcb0fc0bff0e80a3b6c18f`。ToukaReleaseAuthority production provider、receipt v3 aggregate、Azure、reusable workflow caller、live registry qualification與sunset authority仍未啟動，P11-C與P11總體狀態維持**BLOCKED／未完成**。
    - ToukaIDE Webview純文字安全修復已提交為`94aa17976b7262bdda54df98dc1254dae072590c`；新local exact VSIX SHA-256為`688d69d0b6bb71bcbd3f7d689d838cd34c4999265c606adb507d735d4a678a62`。維護者已人工見證隔離smoke、Marketplace手動upload，以及READBACK-R1從Marketplace安裝`touka-ai.toukaide@0.2.0`後的隔離人工smoke均符合；舊`f9e78c3934d5`候選持續固定**DO NOT PUBLISH**。
    - 上述Marketplace紀錄只是維護者的人工見證：未取得可證明Marketplace遠端套件bytes與local VSIX全等的digest／下載證據，也不構成receipt v3、public-release authority activation、GHCR或Git push證據。P11-DOC-A exact 28-path normalized seal已提交為`c7600caac971200e68aea7d78b7ba0b09815aa65`（parent `d25b8a624242cd4ef675091ecc39e6f3370adff3`）；這只完成19個current-facing guidance paths的文件切片，P11-C與P11總體仍維持**BLOCKED／未完成**。未讀取、列舉、修改或清理`data/`，未執行獨立審查、retag、push、publish、Docker、live provider probe或外部傳輸。


<a id="entry-0089"></a>

-   **2026-09-01**: **ToukaProxy dual-era exact-version 發版閘修復候選（ADR-0200）⭐**
    - Proxy 容器 release probe 原本只確認 modern `server/discover`、legacy `initialize` 與 `tools/list`，卻不自行拒絕錯版；workflow 直接依 CLI exit code 放行，故協商版本漂移但工具完整時可能假綠。修復改從鎖定 MCP SDK 2.0.0 的公開 version registry 取得 SSOT，要求 modern exact `2026-07-28`／legacy exact `2025-11-25`；mismatch 回報 era／expected／actual 並非零阻止 publish。
    - modern 首次測試因 test double constructor `TypeError`，不列為有效RED；只修 harness 後，modern／legacy 兩條 negative oracle 各以 `DID NOT RAISE` 取得有效RED。最小 production 修復後兩條negative＋既有happy path **3 passed**，完整P3檔 **22 passed**，Ruff check／format與release workflow guard Green。
    - 固定seed `20260901`完整Python 3.12為 **5181 passed／9 skipped／1 warning**（228.12s）；ADR-0214自動smoke **24/24 PASS**，manual live provider probe維持NOT RUN。MkDocs以全新repo外輸出build成功，只有既有nav／link警告；local non-audit guards **42/42 PASS**，#22 pip-audit／#23 npm audit仍**NOT RUN**，故完整44-guard保持**INCOMPLETE**。
    - branch HEAD／candidate base為`38f35d997fd4d385cd55c25b8c99ac0a7c3c01d9`，本機lightweight `v0.2.0`仍指parent `94aa17976b7262bdda54df98dc1254dae072590c`。exact 8-path候選；維護者僅以人工審查核准計畫，最終審查待進行。未修改workflow、Dispatch、依賴鎖或發版設定；未讀取、列舉、修改或清理`data/`，未執行audit、Docker、獨立審查、stage、commit、retag、push、publish或外部傳輸。


<a id="entry-0090"></a>

-   **2026-09-01**: **ToukaDispatch legacy exact-version 發版閘修復已完成並提交（ADR-0201；`38f35d99`）⭐**
    - 發版 probe 原本會精確拒絕錯誤 modern `2026-07-28`，但 legacy `/sse` 只要工具集合完整便接受任意已知協定版本，可能讓錯版映像假綠。新增 legacy `2025-11-25` exact-version fail-closed；modern／legacy 版本均改從鎖定 MCP SDK 2.0.0 的公開 version registry 取得，移除 probe 內重複硬編碼。
    - TDD 先以已知舊版 `2025-06-18` 取得有效RED **1 failed**（預期 `RuntimeError` 未發生），最小修復後 `tests/unit/test_mcp_2026_p4a_dispatch.py` **12 passed**、真本機 modern／legacy transport integration **1 passed**、release workflow marker **1 passed**，Ruff check／format與release guard Green。未修改 workflow、ToukaProxy 通用 probe、依賴鎖或發版設定。
    - 固定seed `20260901`的完整Python 3.12為 **5179 passed／9 skipped／1 warning**（226.14s）；manifest-driven自動smoke **24/24 PASS**，manual live provider probe維持NOT RUN。一次誤用`pytest tests/smoke/`因smoke檔名刻意不符合pytest收集規則，在收集前非零且無測試摘要；未重跑該錯誤入口，改依ADR-0214正式runner完成驗證。MkDocs使用repo外輸出build成功，只有既有nav／link警告。
    - SHA-256重驗的Node **24.19.0**／npm **11.17.0**以全新repo外cache、offline／no-audit完成local non-audit guards **42/42 PASS**：#1／#19／#28採protected-root-safe明列root，#16只執行5/5 current-cache import與Dockerfile parity且未呼叫rebuild，#18 repo外生成後逐byte相等。Guard #22 pip-audit／#23 npm audit仍**NOT RUN**，故完整44-guard保持**INCOMPLETE**。
    - 最終審查由維護者人工完成；exact 8-path seal 已提交為 `38f35d997fd4d385cd55c25b8c99ac0a7c3c01d9`，parent 與本機 lightweight `v0.2.0` 仍為 `94aa17976b7262bdda54df98dc1254dae072590c`。未讀取、列舉、修改或清理 `data/`；未執行 audit、Docker、獨立審查、retag、push、publish 或外部傳輸。


<!-- End of archived source entries. -->

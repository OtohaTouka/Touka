# ADR-0222：依賴弱點稽核閘 fail-closed 強化

> **2026-10-21 空輸出重跑一次**：Guard #22 的 pip-audit 若 stdout 只有空白（不看 exit code），同一指令重跑一次，第二次的證據放在 `audit-<scope>-query-retry`，第一次的保留。兩次都空、或啟動失敗、逾時（這兩種不重跑）都是 `AuditNotRunError`：印「掃描沒跑成」，仍非零、不轉 clean，和有漏洞、證據無效分開。有內容的輸出不重跑。所以每個 Python scope 最多兩個 audit 程序；Guard #23 仍不重跑。下面「原六scope/no-install/零retry」與 Network gate 段「零retry、最多六個top-level audit processes」裡的零 retry 不再描述現行 Guard #22（後者是當時那次核准的範圍）；其餘照舊。

> **整合 audit retention（2026-09-08）**：`guard_all.py --audit-evidence-root <新絕對repo外根>` 在default44下由aggregator create-only建立根，再把python/node leaves傳給兩個既有guard。parent必須存在，root不得已存在；non-audit模式不建根且22/23維持NOT RUN。兩來源切片已經審查接受，聚合器23cases、四模組119passed；這些mocked測試不代表liveaudit。原六scope/no-install/零retry/原始evidence契約不變。

> **2026-09-08 現行接續**：coverage 設計依 [ADR-0223](0223-dependency-audit-coverage-and-source-identity.md)，要求完整 query pairing、跨平台 markers、CPU 官方來源與 first-party 身分；舊批次只取 host marker 的查核方式不再是現行完整覆蓋契約，coverage/error-path 實作切片已接受；default44已完成六個fresh scopes：Python runtime110/110、build-test152/152均0 skip／finding，Node四scope各severity皆0；101-path原生整合已通過最終審查，P11發版仍待完成。維護者 2026-09-08 的一次性授權取代 P11 逐次人工核准的 network 程序，接續計畫只提供回查指標，不自行授權。以下狀態及 FAIL／NOT RUN 屬原批次快照，來源、資料保護與 fail-closed 實質要求保留。

- 狀態：🚧 63-path R1原生候選已通過維護者人工最終審查；首批Python runtime audit有findings及coverage gaps，依停止條件結束；離線報告修復驗證完成，修改後候選待新的最終審查，P11未完成
- 日期：2026-09-04
- 決策者：維護者
- 影響範圍：Guard #22、Guard #23、uv workspace、ToukaChat、ToukaIDE、
  dependency-audit network gate
- 相關決策：[ADR-0117](0117-uv-workspace-single-lock-migration.md)、
  [ADR-0174](0174-frontend-npm-cve-remediation.md)、
  [ADR-0220](0220-local-only-github-ghcr-release-profile.md)

## 背景

ADR-0220 選定 Docker、GitHub Actions 與 GHCR 作為目前公開映像發行面，因此產品
runtime 依賴之外，會在 build／test／package 階段執行的 Python 與 Node 工具鏈也屬
供應鏈信任邊界。現行兩道 audit guard 有可重現缺口：

1. Guard #22 的 parser 沒保留 severity，卻把 advisory ID 傳給 severity classifier；
   每個無 severity finding 都落入 warning，原保守 fallback 永遠不可達。
2. Guard #22 會把空白、非 JSON 或錯誤 stderr 解析成零 finding，且 tool 缺失時會自行
   `pip install`；lock export 還寫入 repo 內 `scripts/.cache`。
3. Guard #23 只在 ToukaChat 執行 `npm audit --omit=dev`，漏掉 Chat build/dev graph與
   ToukaIDE runtime/full graph。
4. Windows若經 `npm.cmd`、PowerShell與字串 shell層層轉送，argv、引號與錯誤來源不再
   可精確歸因；同時 inherited environment可能把 credential或 registry override帶入 child。

上述缺口使「audit clean」可能只是 parser／transport／scope 假綠，不能作 release gate。

## 決策

### 1. Guard #22：兩個 frozen Python scope

`scripts/guard_python_cve.py` 固定從單一 `uv.lock` 匯出：

- `runtime`：`uv export --frozen --all-packages --no-dev`；
- `build-test`：`uv export --frozen --all-packages --all-groups`。

兩份輸入只建立於 OS 提供、解析後確定在 repo 外的 fresh temporary directory。
`uv` child固定 `UV_OFFLINE=1`，不得解析、更新或下載。Audit固定使用已鎖入環境的
`python -m pip_audit`、`--no-deps --disable-pip --vulnerability-service pypi`；tool缺失是
setup blocker，禁止自動安裝或修復。

pip-audit stdout必須是 schema可辨識的 JSON object。Exit code只能是0或1；非預期碼、
timeout、空輸出、invalid JSON、錯誤形 payload，或exit 1卻沒有finding都屬 `ERROR`，
不得轉成 clean。只有權威 `INFO／LOW／MEDIUM／MODERATE` 可作 warning；`HIGH／CRITICAL`、
缺 severity及未知 severity一律 blocking。

#### 2026-09-07：合法 skipped row 與覆蓋不完整

pip-audit 的未查核依賴可只有 `name` 與非空字串 `skip_reason`，沒有 `version`／`vulns`。
解析結果必須分別保留已查核依賴的 findings 與未查核項目；合法 skip 不屬 parser error，
也不能被默默忽略。缺失、空白或錯型的 skip reason、同時聲稱 skipped 與已查核的矛盾 row，
仍須拒絕。沒有合法 skip reason 的 row 仍必須符合原本的 `vulns` schema。

任一 skipped 使本批為 **INCOMPLETE／非零**，即使 child exit 0 且沒有 finding 也不能 clean。
先報告已取得的 warnings、blocking findings 與 skipped 名稱／原因，再停止後續 scope；
不得以首個 skip 的 parser error 遮住後面的 findings。原 severity、exit 與 finding 一致性、
嚴格 UTF-8／JSON 與先保存 raw bytes 的契約不變；local editable 不自動豁免，Torch `+cpu`
也不得靜默映射為 PyPI base version。

### 2. Guard #23：兩個專案乘兩個 Node scope

新增 `scripts/guard_node_cve.py`，固定且只掃：

| 專案 | runtime | full/build |
|---|---|---|
| ToukaChat | `--omit=dev` | 不省略 dev |
| ToukaIDE | `--omit=dev` | 不省略 dev |

每個專案必須同時存在可解析的 `package.json`與`package-lock.json`；lock中明列的
`resolved` URL只接受 `https://registry.npmjs.org/`，tracked ToukaChat `.npmrc`也必須與
既定 public-registry policy精確一致，否則在 network前停止。

Windows執行不經 `npm.cmd`或PowerShell：先解析絕對`node`與同一 runtime的
`npm-cli.js`，再以argument array、`shell=False`直接執行。Child只收到repo外
HOME／profile／TEMP／cache／log／npm config等最小環境；不繼承PATH、credential、proxy、
Node option或其他process environment；Windows僅保留必要的`SystemRoot`，以免原生
`_overlapped`初始化出現WinError 10106，並以真child回歸驗證。CLI再固定public registry、strict TLS、空 auth、
null proxy、`--package-lock-only`與`--ignore-scripts`。禁止`audit fix`、`--force`、install、
retry或任何lock mutation。

npm stdout必須是`auditReportVersion=2`且 severity counts自洽的 JSON。High／critical
blocking；info／low／moderate只報 warning。Exit code與JSON finding矛盾、tool error、
timeout或parser error都屬`ERROR`並阻斷clean宣稱。

### 3. 聚合與完成語意

`guard_all.py`維持44道守衛：#22呼叫Python aggregate，#23呼叫Node aggregate。兩者都是
network guard；offline 42/42只證明其他守衛，不得擴張為44/44或dependency clean。

本 ADR建立可靠的執行與判讀基礎；2026-09-07首次真audit結果見下節與`SECURITY.md`。
以下是2026-09-06的歷史狀態：其15-path
foundation其後納入21-path，於2026-09-06經lint／測試型別修正再納入P11-NEXT 45-path cumulative candidate。
Next `16.3.4`／Sharp `0.35.4` remediation已有官方公告查核、精確重鎖與結構化回歸；歷史sealed
cache／node_modules曾通過preflight，root TypeScript與Next build曾PASS，但除錯升級修復計畫 R2 仍未完成驗收。
2026-09-06 維護者已改採專案既有測試與守衛優先，方法見
[測試指南](../../guides/testing_guide.md)，順序見
原生驗證計畫（未入版控）。
舊的工作區零寫入、整份外部projection與專用launcher不再是前提；本ADR第1／2節的audit暫存
輸入與credential隔離是稽核本身的邊界，仍須保留。原生驗證已接續，本輪結果以該計畫為準，最終審查未完成。
`pip-audit`、`npm audit`及Guard #22／#23仍為 **NOT RUN**；
SNS token localStorage HIGH為另案Known Open，不因lint或結構化版本檢查通過而消失。

## Network gate

2026-09-06 接續補齊：兩支既有 guard 提供 `--evidence-dir`，在新的 repo 外目錄保存
raw stdout／stderr bytes、scope 輸入、受控設定與 argv／exit／timeout／hash receipts；
`scripts/dependency_audit_evidence.py` 僅共用證據保存，不取代各 guard 的 subprocess／parser。
先保存再做嚴格 UTF-8／JSON 解析；預設不指定時仍使用會清理的既有暫存模式。
Node 固定 `--fetch-retries=0`；Python 明設每 scope cache、15秒 socket timeout與禁止
child bytecode 寫入。這些均不授權連網，精確執行與限制見
本批接續計畫（未入版控）。

六個 audit processes 不等於六個 HTTP requests，另有版本檢查與離線 export；
npm finding可能觸發額外同 registry metadata GET，PyPI client仍有上游redirect行為。
不得把最低child environment、固定起始URL或零retry說成OS egress／redirect目的地證明。

Offline candidate通過最終審查後，另提`AUDIT-NETWORK-P11-A`當次核准，至少綁定：

- final script與三份lockfile hash、HEAD與空index；
- Python 3.12、uv、pip-audit、Node與npm的exact runtime identity；
- PyPI vulnerability service與`registry.npmjs.org`目的地；
- repo外 TEMP／cache／raw stdout／stderr／receipt的create-only路徑與seal；
- 每程序timeout、零retry及最多六個top-level audit processes（Python 2＋Node 4）；
- 會傳輸package name、version、dependency relationship、IP與client metadata；不傳source、
  credential、environment value或`data/`。

任一finding只產生evidence與新的exact remediation計畫（須經審查），不授權安裝、升級、override、
re-lock或`audit fix`。任一seal drift、transport／parser error或目的地歧義立即停止。

### 2026-09-07：首批已執行，保留原始 FAIL

維護者核准 P11 audit 的人工最終審查與 `AUDIT-NETWORK-P11-A`，
綁定63-path R1 request SHA-256
`24167f7a58924b480fc4f5074e5e9acb5a80adfa576c93eb1fd806d60faabc48`。
只執行版本檢查、離線runtime export及一次runtime audit，共3個child。pip-audit完成並回傳exit 1：
111筆dependency rows中109筆已查核、2筆skipped；4個套件回報7筆findings，去重後6個
package／version／ID組合。pip-audit 2.10.1 JSON沒有severity，依既有政策標為UNRATED並阻擋。

Guard #22因合法skip row沒有`vulns`而回傳`pip-audit dependency has no vulns list`；
這是guard解析相容性問題，與原始finding、查核覆蓋不足分開記錄，不是transport／timeout failure。
Python build-test與Node四個scope均 **NOT RUN**，沒有retry、install、relock或dependency mutation。
原63檔候選及12份audit inputs／runtime綁定無漂移；原始FAIL與147份證據保存在本機暫存目錄（未入版控），
以`AUDIT_EVIDENCE_MANIFEST.json`及
`FINAL_AUDIT_RECEIPT.json`封存，不由後續parser修復改寫成PASS。

本輪 `P11-AUDIT-REPORT-FIX-A`（人工核准）僅核准7個既有檔案的離線報告修復、
測試與文件。沿用專案Python 3.12 venv、subprocess mocks與sandbox，不另建環境。
實際套件弱點及兩筆coverage gaps列於根目錄`SECURITY.md`的Known Open；
套件更新、coverage來源及新audit批次仍須具體方案，完整44與P11維持INCOMPLETE。

## TDD 與驗證邊界

### 2026-09-07：七檔離線報告修復

- 有效RED為 **4 failed／44 passed**：合法skip遮住同scope findings與INCOMPLETE，
  以及矛盾skip row被舊parser接受。補齊exit一致性案例後GREEN **50 passed**，較基線新增19例。
- 既有原始JSON的離線parser replay完整保留 **7筆findings＋2筆skipped**，全部findings仍阻擋；
  這不是重新audit，也不重新產生guard PASS，原始stdout SHA-256不變。
- 標準Python 3.12完整suite **5309 passed／9 skipped／3 warnings**（228.73秒，seed 20260907）；
  non-audit **42/42 PASS**、自動smoke **24/24 PASS**、兩個Python檔Ruff check／format通過，零新增NOQA。
- 原生pytest exit 0及raw receipts已保存；外層cp950列印UTF-8日誌失敗另記為顯示錯誤，
  不當作測試失敗或重跑理由。既有Qdrant sandbox lock殘留交由OS Temp回收，未強制清理。
- 改動只涉及核准的7個既有檔案；舊63-path其他56檔、lock／runtime及所有原audit證據保持不變。
  最新證據保存在本機暫存目錄（未入版控）。
  修改後候選待新的最終審查，stage／commit／新network audit／套件更新及發布均未執行。

### 2026-09-06 cumulative candidate歷史快照（進入除錯升級前）

以下保留當時PASS／FAIL／NOT RUN，不代表除錯升級各輪或新原生方式的結果；最新狀態見上述接續計畫。

- 本輪追加13份既有測試＋8份文件，保留32-path候選並形成45-path人工審查候選；只補fixture／mock型別，原62個案例及全部行為斷言保留，未改產品來源、generated API、檢查設定或依賴。原47則TypeScript診斷作RED；首輪62 passed後另見SkillsTab工具description缺欄（9則），授權內補齊後 **62 passed／13 files、lint 0 errors／1既有warning、root TypeScript零診斷、Next.js 16.3.4 production build PASS**。
- 上一輪artifact generator、完整lint（0 errors／78 warnings）與產品型別檢查已PASS；完整Vitest 1159／230 files發生於上一輪最後test-only標註前，本輪依核准未重跑全量或cache-seed。
- 非audit守衛 **#1–#16 PASS**；#16空sandbox設定以既有tracked examples hydration解除。#17三次均在收集前被repo外Popen包裝阻塞（class繼承／generic下標／Windows ver遭allowlist拒絕），未發生產品測試斷言失敗；依同方向三次停止線進入除錯升級，不做第四次。#17未通過、其餘25道非audit守衛與完整Python suite **NOT RUN**；#22／#23與audit NOT RUN、44-guard INCOMPLETE、最終審查未執行，P11總體仍未完成。
- 全部舊／新FAIL保留於repo外（未入版控）；exact R:已撤銷。HEAD仍為`03697f1b9c9c5a9e5bed8c91e329a0e181f7449f`、index空；未install、更新依賴、連網、audit或Git寫入，`.gitignore`排除且未存取`data/`。SNS raw-token localStorage Known Open HIGH另案待修，沒有外洩證據，不能宣稱security clean。

### 2026-09-04 foundation與21-path歷史快照

以下是當時結果，不能冒充2026-09-06新候選的全量驗證；當時runtime／cache阻礙已不再是現況。

- 初始RED：**34 failed／10 passed**，命中Python假綠、自動安裝、repo內export、Node runner
  缺席及Guard #23舊接線。
- Credential-free direct-runtime amendment RED：**15 failed／25 passed**，命中非絕對uv、
  inherited child environment及`npm.cmd` parser邊界。
- Parser cross-consistency amendment RED：**2 failed／5 passed／1 warning**，命中Python
  exit 0夾帶finding，以及npm metadata severity count與finding物件矛盾。
- Manifest／nested-lock amendment RED：**2 failed／19 deselected／1 warning**，命中不可解析
  `package.json`與`packages`以外巢狀非官方`resolved` URL仍會進入subprocess。
- Focused GREEN：**53 passed／1 warning**；候選6個Python檔的Ruff check／format Green。
- 固定seed `20260904`的完整Python 3.12：**5226 passed／9 skipped／3 warnings**
  （207.05s）。結束時一個repo外測試sandbox的Qdrant `.lock`經三次bounded cleanup仍被
  占用，保留給OS temp回收，未強制刪除。
- 納入Next.js修復後的21-path cumulative candidate以`PYTHONHASHSEED=20260904`重跑完整
  Python 3.12：**5230 passed／9 skipped／3 warnings**（203.09s）；結束時同類repo外
  sandbox Qdrant `.lock`經三次bounded cleanup仍占用，仍只留給OS temp回收。
- `uv export --offline --frozen`兩個scope均成功且未啟動audit：runtime輸出7434 bytes，
  SHA-256 `12789A96C2FD363E1DEEDB5655437A41CFF9A255C8AE913BFCBD84E22F59CB8B`；
  build-test輸出10029 bytes，SHA-256
  `DC56687ECCAC7CF68275945CD3F6433F62D4E1F66A1D64A5341D317E4F1FAD78`。
- Guard #16先以SHA-256證明5/5 dependency cache均與目前`uv.lock`＋member
  `pyproject.toml`一致，再以禁止`uv sync`／cache rebuild的wrapper完成5/5 import與
  Dockerfile parity；沒有安裝、更新或下載。
- 當次local non-audit 42/42 **尚未完成**：frontend runtime preflight預期Node
  `24.19.0`／npm `11.17.0`，ambient Node實為`26.2.0`；現存bundled Node雖為
  `24.19.0`但不含npm，system npm為`11.13.0`，而先前的portable npm `11.17.0`
  已被OS temp清理，因此未啟動需要exact Node/npm的canonical remaining batch；另獨立完成
  cache-only Guard #16及protected-root mirror中的Guard #1／#19／#28。沒有fallback、安裝、
  下載或改版。
- `pip-audit`、`npm audit`與network audit均 **NOT RUN**，不屬offline驗證；完整44道守衛
  維持 **INCOMPLETE**。

## 後果與回滾

正面結果是scope、process、parser error與vulnerability finding成為不同且可驗證的狀態，
不能再以空輸出或漏掃取得clean。代價是full/build graph可能讓先前被忽略的dev advisory
成為blocking evidence；這是GitHub Actions／GHCR build trust boundary的刻意選擇。

在最終審查與提交前，既有45-path候選基線及後續授權差異保持unstaged。回復須針對已封存的本輪差異，
保留其他既有修改與全部FAIL證據，不自動捨棄累積候選；不得以放寬severity、恢復stderr fallback、
自動安裝或移除IDE/dev scope作修復。Next.js、lint與追加13份測試型別均已取得計畫審查核准，cache-seed也曾
另取核准；它們不授權audit、產品範圍擴修或發布。Network findings、SNS token、modern-only、
Docker／GitHub Actions／GHCR仍各自需要新的計畫審查。

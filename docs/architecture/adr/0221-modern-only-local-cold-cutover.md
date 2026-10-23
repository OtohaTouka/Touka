> **2026-09-14 modern 升級完成；2026-09-15 回讀驗收**：新版 source `5c48a5bd51e61081bdeb58493fea1343bc4a10cd` 已完成自己的 qualification／publication、Release 388511097 與 S55→新版受管升級，經審查接受，狀態為 MODERN_ACTIVE，詳見 [ADR-0227](0227-modern-to-modern-local-upgrade.md)。兩代各自 consumer、完整來源分類、fresh sandbox roundtrip、restart 退休、實際 health／protocol 及事後 metadata 均成立；S55 停止保留，回復材料／命令接受、實際 rollback NOT_RUN。原 dual-era 初次切換保持；下方 S50/S55 與「尚未／pending／本次不部署」是歷史，不代表本次狀態，也不授權未來發版。

> **結案基準（2026-09-14 原驗收）**：S50 rollback baseline `b540106f5e7b5467162164a27e1c75229c3fdae3`／S55 modern `927ce096f9fe297d6aca329b6cc8359993da1278` 的 selected local profile 已經審查接受，狀態為 **MODERN_ACTIVE**。原資格、原始映像發布、兩 consumer、compatibility／cleanup 與 actual activation 已接受，見結案文件（未入版控）。以下先前 pending／當前／S36／S39 敘述是歷史，不代表目前來源或新授權；原技術閘、未選 profiles、liblzma deferred 與 rollback 材料保存限制不變。新來源仍須各自 fresh release gates；本次來源整合不部署。

> **2026-09-14 Q53：Windows環境、lease身分與就緒等待。** 以 actual S52 c790e0368eaf9aa8a519fa19ae18f96ca436f5d3 為基準，Q53 exact6 修正 caller、permissions 測試及四份文件。Windows 僅傳遞既有 PROGRAMFILES，保留大小寫不敏感、POSIX排除與missing不猜值；Docker預設Compose discovery的唯一變數A/B由rc125變成rc0。ExistingPathLease先驗HostIdentity及attributes整數型別，volume／128-bit file ID仍必須相同，只容許attributes xor為0或ARCHIVE32；其他位元含REPARSE仍拒絕，不重新採納identity。六個HOST固定失敗碼可見，任意例外文字仍不外洩。
>
> HB／Chat健康確認以原operation剩餘及最多150秒限制inspect、exec與sleep。只GET健康端點，只有封閉NOT_READY的CONNECTION_REFUSED／TIMEOUT可有界等待；HTTP狀態、缺token、schema、未知或額外probe欄位立即失敗，原/status的正常其他欄位仍允許。原native非零／stderr／逾時仍立即失敗，marker/write/read不改。HB child5秒是socket與monotonic checkpoint軟預算，不能宣稱逐header/body硬5秒；host exec由原生runner以min(30,remaining)硬限，連同整段150秒與operation剩餘收束。
>
> 修訂後原雙代SANDBOX_INTEGRATION實際PASS：39.047秒、264 native commands、7 containers／1 bridge／1 fresh HF volume，原命令rc0／stderr空，native／產品／fixture cleanup PASS，15pins未漂移；modern建立inactive schedule，baseline正常讀回。兩代三產品正常啟動、UID/GID1001及NSS、原協定檢查均走原流程。此驗證使用已發布S50/S52六映像，source_compatibility={}，不具新source formal qualification或production authority。兩次完整sandbox原FAIL及窄init診斷均保留；第二次HB rc2實際分支未匯出，readiness source缺口已修，不將競態寫成歷史唯一根因。
>
> S50 baseline與S52 modern原bundles／published seals／two-consumer已接受，GitHub Release immutable=false，不宣稱server鎖定；liblzma仍RISK_ACCEPTED_DEFERRED、未修復。production原live在前置階段FAIL且原服務未切換，本次sandbox不碰真data/.env，modern尚未active、P11未完成。新caller仍嚴守driver==sealed_driver，需最終審查、exact6 Git／PR、新source qualification／publication／兩seal與正式live；不可將舊S52build資格改綁新source。正式CLI應由正式執行帳號建立fresh exact-commit checkout，核完整tree／bytes及plain Git ownership；不移植測試safe.directory、改ACL/global Git或放寬env過濾。

以下保留先前的正文與checkpoint（用語已改為對外寫法），舊狀態僅代表當時。

> **2026-09-14：Windows caller 的精確環境及身分表示。** Docker Windows plugin discovery使用ProgramFiles，原受限子程序環境應僅在Windows傳遞既有PROGRAMFILES；不猜造missing值，不因其缺少就預先拒絕全部Docker命令。原敏感與失敗契約保持。
>
> Windows FILE_ATTRIBUTE_ARCHIVE（32）屬可由正常metadata寫入切換的非物件身分位元。原lease驗型別後仍要求volume／128-bit file ID相等，attributes xor只容許0或32，DIRECTORY／REPARSE和其他位元變化仍拒絕，原identity不可重設。實際合成init觀察到16→48、同一volume/file ID，精確負對照覆盖未知attrs及錯型；六HOST固定reason保持可見，任意例外文字不輸出。[Microsoft attributes](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants)、[FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)。
>
> 此為S52之後的新caller候選，driver==sealed_driver不放寬。原S50/S52兩份published映像可用於明示SANDBOX_INTEGRATION；原protocol／normal startup／cleanup真執行仍不能替代正式新source qualification或production live。正式原CLI需正常執行帳號的exact-commit checkout，不能以測試safe.directory覆寫或放寬env過濾作權宜authority。GitHub immutable=false與liblzma deferred保持，完成定義仍為modern active；目前新完整sandbox尚無終局，P11未完成。

以下保留原文（用語已改為對外寫法）。

> **2026-09-13：Docker metadata 表示修正。** actual S51 a7c9aa7e3212c3725b490dbe239e5778c7a51ce8 上的八檔修正：切換 caller 使用原 consumer 的 index_sha256，分別驗 classic config-ID 與 containerd target/platform Descriptor 身分；保留 exact RepoDigest、linux/amd64、RootFS、User、Entrypoint/Cmd/WorkingDir 與未知值拒絕。metadata 投影保留 missing/null 差異，self.images 保留實際 target 身分，sealed_driver authority不變。
>
> runtime、sandbox caller、live caller 三個 volume 接點只正規化 Docker CLI 28.0.4 的精確空 DriverConfig:{}；省略與空dict視為未指定driver設定。null、錯型、非空 Name/Options、其他volume選項、NoCopy型別/值及bind夾入driver均仍拒絕，原raw不改。官方28.0.4 create-only四卷觀測重現舊拒絕並完成cleanup；保存陣列對新mount block離線接受。此為本機表示差異證據，原hosted實際Mounts未匯出，不能斷言歷史唯一根因。
>
> S50 baseline Release387888604、原rollback seal及原single consumer已接受。PR47曾merge為S51；S51 qualification34757126757/a1在init inspect前CONTAINER_MOUNTS失敗，cleanup/capacity PASS，無modern bundle/發布。這份新來源仍須最終審查、精確Git交付與新modern qualification／publication／seal；不能把caller與sealed_driver解綁或改稱S51含本修正。Two-consumer、六個真映像相容性、live冷切換尚未驗收；P11終態須modern active。維護者允許產品正常啟動運作寫入，開發代理不得直接管理真data/.env，自動化仍sandbox。

以下保留原正文及歷史前綴（用語已改為對外寫法）；舊狀態只代表當時。

> **2026-09-13 Q42接合S50來源。** actualS50=b540106f5e7b5467162164a27e1c75229c3fdae3（PR48/H50 b65cf659f37956f6aeaed26f3349946fcb8014f8）接合Q42 Hmodern 6f16f1dc0993476bccb3291435c1a2c207802a8b，候選仍83＝81regular＋2delete／3new。6overlap中4docs只合併空ancestor新增前綴；所有非衝突正文與两方歷史保留。Q50 evidence/diagnostic及其tests四檔是S50 baseline直接繼承，不重列候選差異。
>
> 一般copy與modern latest promotion共用_copy_original：原tar immutable，先原graph驗證再建立fresh-owned OCI layout，不重播tar owner/mode；Skopeo讀readonly layout，global tmpdir落owned scratch，preserve-digests／0retry／精確owned cleanup與closed container_state均保留。start原錯不被本機receipt/觀測失敗覆蓋。modern profile/tag/latest/expected-old與seal原格式、self.profile.dispatch_probe及consumer Git blob closure維持；GET/copy不是atomic CAS，不新增此保證。
>
> PR47維持draft，原Hmodern歷史保留；最終審查後核對完整tree並以parents=[Hmodern,S50]的merge commit接回原branch，普通fast-forward push，無rebase/force。S50 baseline qualification run34751646105/a1／job103709102120仍在追蹤，本文件尚未取得終局；publication／actual immutable rollback seal不預造。modern merge/hosted/發布/live須等待actual published seal與原驗收。兩真seal原bundle消費、兩digest正常startup相容性roundtrip、真live切換尚未執行；P11成功終態是modern active。維護者允許Touka正常運作寫入，開發代理不得直接管理真data/.env；所有自動化維持sandbox/mock。kernel identity UNKNOWN與liblzma既有deferred均未宣稱解除。

以下保留為歷史與既有正文；舊『當前』、pending、source/count僅代表當時，不覆蓋本輪事實。

> **2026-09-13 Q42／S49最終來源決定。** modern-only client／Dispatch／IDE、permission-only initializer、selected Compose bind與external volume／NoCopy、Q44四步offline helper、Q45 lifecycle、Q46普通bridge／published-loopback、Q47封閉probe診斷、Q48 child import根與Q49 publication diagnostics全部保留。sealed consumer closure從各自actual Git blobs建立隔離capsule，包含動態release_probe_contract；各自profile的dispatch probe名稱不混用。新增caller提供verify／sandbox-activate／live-activate／live-rollback，私有credential透過既有回呼與敏感stdin接線，HTTP child絕對deadline／process tree／RO-RW／volume／exact ID接線已驗。
>
> live orchestration保留現場exact IDs與原running set，既有config bind以selected Compose受限投影保存；兩代writer互斥、lease涵蓋init及所有mount activation／確認。init已開始或結果未知時metadata可能改變；未確認停止保留INIT_RECOVERY_REQUIRED／UNCONFIRMED，不反向chown或冒稱原data／ACL恢復。primary不被failure-budget／cleanup／lease覆蓋，canonical PASS activation receipt只在lease成功釋放後寫入。rollback重新透過正常Compose讀.env，先baseline Proxy healthy再create Heartbeat，避免舊token；不讀或直接改.env補償。P11成功終態是modern active，fallback baseline不是P11完成。
>
> 維護者已確認資料限制針對開發代理直接操作，允許Touka正常startup／init／migration／session／marker／記憶寫入；因此舊LIVE_STARTUP_PROFILE_UNAVAILABLE的內容寫入疑義已解除，未新增全域readonly產品功能。所有自動測試仍sandbox/mock。Windows lease／selectedCompose CLI／live CLI原生機制已驗，但其中consumer／seal／產品protocol／health注入均明示NOT_RUN_FIXTURE_INJECTED，不是兩份真seal的admitted E2E。實際baseline＋modern published seals／原bundle consumer E2E、兩digest正常startup相容性roundtrip（含Proxy UID/GID/NSS、三產品、active:false schedule持續性且不awaken）、真live activation尚未執行。container kernel identity維持UNKNOWN。liblzma exact5.8.1-1+deb13u1仍RISK_ACCEPTED_DEFERRED，沒有修復或新repro。
>
> 基底actual S49／PR46：5a89e631fbc0630b123cf52b81b9c5d8f5ba2b3f，此Q42 source未stage／commit／發布。S49 qualification run34742841637/a1成功、原1,144,870,406 B ZIP與19件經審查驗收；publication run34746361248/a1於07:57:04Z FAIL WRITE_UNCERTAIN，Proxy writer start rc1、tool exit未取得、三產品後回讀ABSENT，沒有Release／published rollback seal。原hosted底層錯誤仍UNKNOWN；Q50本機A readonly /var/tmp與B untar chown失敗是兩項實測機制缺口，原始證據未入版控，修訂獨立進行，不混入本83。
>
> full-r2 9012P／10skip／0F／0E及原生機制僅支持source／sandbox候選；actual seal後才進modern merge與後续產品驗收。下文S36／S39與舊限制為歷史，不作當前來源／授權。

# ADR-0221：MCP modern-only 本機冷切換與 legacy transport 退役

> **2026-09-12 歷史決定與待辦快照**：維護者再次確認沒有任何舊版 agent、data 或 tools 會使用；不再安排 legacy 使用調查、遷移或相容維護，不因此宣稱未知 client 版本或 traffic 已查明。依 P11 一次性委託，具體候選、Git 與發布經審查接受，不另設維護者逐次核准。modern M1–M4 本機來源驗證已有完整證據；實際映像／發布／冷切換尚待完成。現行 dual-era baseline 必須綁 S39 原 consumer/schema/readset 與實際 immutable artifact；隔離 S36 僅留歷史；immutable rollback seal 是 modern main merge／hosted／publication／live cutover 前置，來源構造、TDD、完整本機驗證與最終審查後 exact commit／draft PR 可以先完成。歷史 FAIL 保留，未選 provider lanes 不啟用。

- 狀態：✅ 維護者決策已接受；selected local profile 於 2026-09-14 經審查接受，進入 MODERN_ACTIVE；未選 profiles 維持 NOT_SELECTED。
- 日期：2026-09-04
- 決策者：維護者
- 影響範圍：ToukaProxy MCP client、ToukaDispatch、ToukaIDE、release probes、
  rollout／sunset governance、回滾與 current-facing 文件
- 相關決策：[ADR-0201](0201-toukadispatch-streamable-http-and-request-auth.md)、
  [ADR-0202](0202-toukaide-node-v2-streamable-http.md)、
  [ADR-0215](0215-mcp-trusted-rollout-verifier-and-release-qualification.md)、
  [ADR-0220](0220-local-only-github-ghcr-release-profile.md)

## 背景

ADR-0201／0202 先以 modern `/mcp` 為主路徑並保留隔離的 legacy HTTP+SSE
相容窗，ADR-0211～0215再建立 telemetry、trusted evidence與 provider-backed sunset
邊界。維護者最新確認所有 client 都只在本機使用，而且沒有任何舊版 agent、資料或
工具需要相容。此決定允許關閉相容路徑，仍不代表已量測 usage／版本，也不代表
新映像已發布或本機 runtime 已切換。

## 決策

### 1. 目標狀態

- Touka 自有 MCP server只提供 Streamable HTTP `/mcp`，並精確驗證 modern
  `2026-07-28`協定版本。
- ToukaProxy不再提供 `protocol_mode=legacy`或以 legacy transport作自動降級；
  真送 `server/discover` 後完整驗證再 adopt；包括 `-32601` 在內的協商、validation、
  capability、version 錯誤一律 fail-loud，不重開 legacy transport。
- ToukaDispatch退役 `/sse`＋`/sse/messages/`；ToukaIDE退役
  `/mcp/sse`＋`/mcp/messages`及其 discovery投影。
- Release probe、workflow guard與 current-facing文件最終只驗證 modern path。
- 單一 `/mcp` request 中合法的 **modern request-scoped SSE response 必須保留**；
  它不是 legacy HTTP+SSE endpoint。

### 2. 來源候選與已凍結 baseline 分開

Q40 隔離候選以 S39 整合已提交 H37 modern-only 行為，不能套用到已安裝 runtime。
正式 baseline 使用 S39 原 dual-era source/consumer/schema/readset 及實際 run/artifact，
完成 qualification／publication 與 immutable seal；舊 S36 consumer 接合不轉移成 S39 PASS。
Q40 modern parser 不代替舊格式 consumer；兩者 phase、probe schema、seal 與來源 bytes 不得混用。

### 3. 本機冷切換的強制前置

本機 cold cutover只有在同一 exact removal candidate滿足下列全部條件時才可執行：

1. 完成只含 Git tracked／generated 契約面的精確移除清單；不直接讀取、列舉或推論
   真 `data/` 內容，也不擴成無關的全庫 legacy 字串清理。
2. 維護者的無舊版使用需求決定已提供；不另索取確認，不把 caller self-attestation、
   環境變數或設定缺席當成已量測的版本／traffic 證據。
3. Proxy、Dispatch、IDE與 `linux/amd64` container各自通過 modern exact-version、
   discover／tools-list／代表性 call、拒絕舊 initialize、空白／錯誤 Bearer、Host／Origin、
   兩個 modern 身分與 mission/session/replay/generation／peer-close isolation smoke。
4. 以 exact commit、lock、image digest與設定 schema封存最後一份 dual-era immutable
   rollback seal；rollback不得依賴把新版 runtime切回已刪除的 legacy mode。
5. Removal manifest須精確列出 endpoint、package、config、probe、test與文件 write set，
   依既有委託經審查驗收；保留 request-scoped SSE 及 principal/config-generation 隔離。
6. 映像、modern companion、identity seal 或來源漂移時停止依賴該證據的發布／切換；
   獨立 source/TDD/M4 可以繼續，不能偽造成功、盲重跑或擴大風險例外。

缺少實際 qualification／rollback seal 時明確保留 BLOCKED 與實際缺口；未選 provider、
Azure、八個 IDE host lanes 或額外 Marketplace 發行不作替代路徑。

### 4. 安全理由

成功切換後可移除舊 SSE session、messages route、dual protocol parser與 downgrade面，
縮小認證與維護攻擊面。維護者已排除舊版使用需求，modern auth/version 證據與 immutable
rollback 仍是必要條件；request-scoped SSE、失敗關閉與現有資料隔離不能隨 legacy 移除。

### 5. 完成宣稱

- modern-only 來源與真 sandbox transport／companion 元件已實作驗證；H37 M4 已有來源驗收；Q40 union 完整驗證以本輪 receipts 為準。
- 實際 modern 映像 qualification／發布／匿名digest協定驗證及 live cold cutover 為
  **NOT RUN／pending**，不把本機測試當已部署或 Marketplace 安裝證據。
- 本 ADR完全落地前，P11 sunset與P11總體維持 **IN PROGRESS／未完成**。

## 實作與回滾邊界

後續 implementation必須以 TDD同批處理 Proxy、Dispatch、IDE、release probes／guards、
API／generated contracts與 current-facing文件。不得直接修改或管理 `data/`。維護者已允許
受管唯讀掛載與必要 ownership/mode 初始化；既有 initializer 的 mkdir/copy/seed 與
startup migration／background Agent/provider 不因此全部獲准。唯讀驗證與正式 Agent
可寫記憶的 runtime 設定要分開；自動化 pytest／CI／guards 一律 sandbox。

最小回復必須重新綁 S39 IDE source/lock/schema/bundle 實際 bytes（非 VSIX）；旧 S35 bytes只屬歷史，並需取得
immutable baseline 三映像 seal；失敗後以原 digest／原設定契約回復，不使用已刪除的
legacy mode 開關。必要回復依同一 P11 委託經審查處理，不另設維護者逐次核准。

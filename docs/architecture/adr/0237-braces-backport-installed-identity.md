# ADR-0237：braces 同版本防禦回補與安裝內容身分

- 日期：2026-10-06。
- 狀態：計畫經審查接受；候選進行中，A核心30案有限通過，整合及正式驗收未完成。
- 影響：Chat／IDE建置依賴、安裝／工具入口、Guard23、維護者本機的 audit reader／host。
- 前置：[ADR-0236](0236-security-candidate-source-and-installed-identity.md)；本決策只補braces專案依賴，不重定義npm CLI bundle或維護者本機的發行框架。

## 問題

braces維持3.0.3時，lock與npm audit不能單憑版本辨識本機內容回補；copied PASS也不能證明每個實際安裝副本已修補。維護者選擇固定PR72且不重現漏洞，須分開保存來源適用性、普通API驗證、安裝bytes與原audit結果。

## 決策

1. 以官方3.0.3與PR72 base `e53730e6f935498326c72d768889ac194eedc0e0`、head `28d440b5dd449dbf1fe6f3506cf94ecca4d02660` 固定五個production檔的必要depth差異。`parse.js`的PR base另有quote／comma變更，不能整檔帶入。保留version、lock URL／SRI、LICENSE；helper旁置manifest／patch列完整before／after inventory。
2. 單一Node helper只對明示owned installation執行apply／readonly verify。full重驗全部lock braces nodes、parents及完整package檔案；runtime先核dev分類，再驗實際omit-dev topology absence。未知／部分修補、額外副本、link／reparse／hardlink、缺件或未知內容拒絕，不以stamp修補或放行。
3. installed proof固定 `touka.braces-backport.installed-bytes.v1`：sha256:前綴、sorted nodes/files arrays，每檔有bytes、digest與canonical body_base64。純Python reader從trusted source重建並重hash，不執行receipt內程式；完整stdout含LF上限1MiB。外部source authority與host時間／原件持有仍由原流程提供。
4. `node-lock-and-braces-backport-v1` 必須明示；legacy `lock-only-v1` 的43件契約保留，新profile僅加Chat／IDE各4件verify originals＝51。current host要求外部profile、12input readset與outer argv一致；不能由自報profile推導authority或用舊receipt搭新consumer。
5. 原audit parser tuple、raw high與exit不變。只有固定GHSA完整via graph及trusted patched bytes可另列coverage，raw_blocking／covered_blocking／unresolved_blocking分欄；runtime high、critical、其他advisory、未知圖及缺原件仍阻擋。Guard23與現行入口明示新profile，不自動放寬舊reader。
6. controlled install於第一個開發工具前apply／verify，既有工具入口先readonly verify；offline canonical／actual沿同一sequence（full六步、runtime兩步）。不增lifecycle hook、不改resolver／audit fix、不另造通用launcher。

## 驗證與限制

測試原件納入四個官方MIT小tarball（braces3.0.3、fill-range7.1.1、to-regex-range5.0.1、is-number7.0.0，共30,904bytes），目標是Git-only checkout可重播；它們是公開依賴fixture，不是PoC。正式預設patched，以動態ROOT固定archive/hash與PATH Node取得輸入，缺inputs／Node須FAIL，不靠額外env才啟用；operator的unpatched RED覆蓋仍保留。A10最終30 PASS／0 skip、完整PRE=POST（Python3.12.12／Windows Node26.2.0）。正常Vite `.vite`／`.vite-temp` cache只做有界metadata核對；links／hardlinks、隱藏package與dangling node_modules link仍拒絕，不把斷鏈視為absence。

普通短pattern／小AST及maxDepth=1或2驗新API契約；未修版正常結束但不符合新選項可形成RED。禁止深層、循環AST、資源耗盡刺激與漏洞重現。維護者不申請Daybreak；此範圍不是平台解除聲明，原具體拒絕不可繞過。歷史29案身份UNKNOWN保留，沒有新增EXCLUDE。

A只有上述Windows Node26有限結果，B/C只有交接紀錄（未入版控）所列有限來源／mock結果。正式Linux Node24.21.0、六fresh audits、完整品質、build、P3 qualification、P4 publish／readback與P5 cutover仍NOT RUN。G13 PARTIAL／full P5 OPEN及ADR-0236既有分期保持；不宣稱Known Resolved、部署完成或runtime無漏洞。

# ADR-0223：依賴稽核覆蓋與來源身分

- 狀態：✅ coverage／error-path與101-path原生整合已獲 SUPERVISOR_ACCEPTANCE；P11／發版仍未完成
- 日期：2026-09-08
- 決策者：Owner 直接委託下的主代理代審
- 影響範圍：Guard #22、Python lock projection、advisory query coverage、Torch CPU 來源 SSOT、first-party editable 身分、sandbox tests
- 相關決策：[ADR-0117](0117-uv-workspace-single-lock-migration.md)、[ADR-0222](0222-dependency-audit-gate-hardening.md)

## 背景

首批 Python runtime audit 留下 Torch CPU 與 first-party editable skip；合法 skip 的報告解析已補齊，但 skip 仍代表未完成覆蓋。只檢查回傳列、只採本機 marker 或把 `+cpu` 去掉後直接當作同一來源，都不能證明鎖定相依已完整查核。R2 套件修復及原生相容性檢查可接續，不等於新 audit 已通過。

本決策的審查來源是本對話 2026-09-08 起首「請以 SUPERVISOR_DELEGATED（主代理代審）模式」的使用者九點直接委託；本 ADR 只記錄契約，不創造 network、Git 或 release 權限。現行範圍與證據見接續計畫。

## 決策

1. 將覆蓋判定抽至 `scripts/dependency_audit_coverage.py`，由 Guard #22 呼叫；依賴版本仍以 frozen `uv.lock`／workspace manifest 為準，不引入第二份 lock。
2. 先建立 runtime 與 build-test 各 scope 的完整預期 query 集合，再對照實際回傳的正規化 package name／exact version。缺列、多列、重複列、版本錯配、未解釋 skip、工具或格式失敗均不得成為零 finding／PASS；原始輸出先由既有 evidence helper 保存。
3. 覆蓋每個 frozen scope 原始 export 的全部 marker 分支，不依執行 host 剪枝；不能因 Windows host 不使用某列或自行縮減「支援平台」而略過鎖定依賴。這是依賴查核範圍，不代表任何平台 runtime qualification；無法確定適用性時回報 coverage gap。
4. `docs/security/torch-cpu-advisory-identity.json` 是 Torch CPU 發行物對應 advisory 身分的唯一來源紀錄。映射必須綁定 exact CPU 版本、官方發行物／metadata 來源與 hash，以及可核對的 upstream advisory package／version 依據。只有映射內容與本次 lock 證據一致才可查詢；單憑去掉 `+cpu`、名稱相近或未驗證自述不可通過。來源證據不足或漂移時維持 INCOMPLETE；此檔不是弱點豁免清單。
5. First-party editable 只能由本 repo 的 workspace membership、project name／version 與 exact local source path 身分確認。已證明的本專案來源須獨立列為 first-party source coverage，不能冒充 PyPI 查詢成功，也不能用任意 editable／同名套件套用豁免；第一方 runtime 的第三方傳遞相依，以及 build-system／backend 的第三方相依，都必須納入預期 query 並有缺漏反測試。
6. 保留 Guard #22／#23 的 severity、fail-closed、無憑證受控程序、no-install／no-fix 及原始 evidence 契約。來源核對使用必要官方資料，測試沿用既有 sandbox／mock；不建完整 repo／dependency 投影。

## 驗證與交付

實作須先以合成 fixture 取得缺失 query、重複／錯版本、跨平台 marker 遺漏、未受信任 CPU 映射及偽造 first-party 身分的 RED，再驗證相應 GREEN；既有 parser 與 evidence 案例必須保留。完整 query pairing、scope／來源 hash 與原始 audit 結果須交叉核對。設計接受、離線 GREEN、network audit、完整 DEV-B 與 release qualification 分別記錄，不能互相替代。

Coverage／error-path 切片已由主代理接受，121 focused passed（helper 25／guard 64／其他 32）、Ruff 通過，來源與原始 audit evidence hashes 核對符合。六個 scopes 已按完整 query 執行；Python runtime 110/110、build-test 152/152，均 0 skip；六個 first-party source 身分另列，不冒充外部查核。CPU 映射證明 advisory 身分來源，不宣稱 CPU 與 upstream wheel 的 binary equivalence。

首批完整 coverage 仍找到 build-test 四項 UNRATED 與 Node findings，原 FAIL 保留。Python build/test 工具定向修復後，fresh Python runtime 110/110、build-test 152/152 均 0 skip／0 finding；最後整合守衛亦完成Node四個fresh scopes，各severity皆0；101-path cumulative技術DEV-B已由主代理接受。上述不代表所有平台runtime qualification、完整P11或release PASS；實測與審查狀態集中於接續計畫。

## 回復與限制

可回復本決策的 helper、呼叫接線及來源紀錄；回復後未證明的 CPU／editable 覆蓋必須重新呈現 INCOMPLETE，不能回到忽略 skip 的成功狀態。本決策不調整 managed-data 隔離、不啟用 Azure／provider-v3／8 IDE host lanes，也不移除 consumer 或 rollback 證據條件。

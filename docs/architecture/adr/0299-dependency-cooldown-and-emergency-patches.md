# ADR-0299：依賴冷卻期 3 天、緊急修補通道與每日檢查

- 狀態：✅ 已接受
- 日期：2026-11-06
- 決策者：維護者
- 相關：取代 [ADR-0241](0241-model-layer-stage-0.md) 決策 9 與「緊急安全更新也要晚 7 天」；[ADR-0222](0222-dependency-audit-gate-hardening.md) 的稽核門檻不變
- 影響範圍：`pyproject.toml`、`uv.lock`、`ToukaChat/.npmrc`、`ToukaIDE/.npmrc`、`.github/dependabot.yml`、`.github/workflows/dep-audit.yml`、`scripts/dep_audit_report.py`、`scripts/hermetic_gate/npm_offline_gate.py`、`scripts/guard_node_cve.py`、`docs/rules/deploy.md` 禁令 69

## 背景

ADR-0241 定了 Python 套件 7 天冷卻期，連緊急安全更新也要等。之後幾件事讓它不再合適：

- 修補的時間窗變短。2026-10 的一個 PHP 重大漏洞，第一次攻擊回報比官方修補早了約 6 小時。AI 協助開發讓攻擊與修補都變快，等 7 天的代價變高。
- 回放分析（單一來源）估計，冷卻期 3、7 天分別擋下約 81%、85% 的被劫持 npm 版本，被延後的修補是 23%、46%。從 3 天拉到 7 天多擋約 4 個百分點，修補的延後卻變兩倍。
- 三邊不一致：Python 和 GitHub Actions 由工具強制，npm 只靠慣例。條文散在 8 個檔案，有的互相矛盾。
- CI 不跑 Guard #22、#23，只有本機會跑。新公告出來時，沒有東西會主動告訴維護者。

## 決定

1. **冷卻期 3 天，三邊一致，工具都會擋**：uv `exclude-newer`、npm `min-release-age`（兩份 `.npmrc`）、Dependabot `cooldown`。五處數字由 `tests/unit/test_uv_dependency_cooldown.py` 釘住。
2. **npm 實測**（2026-11-06，npm 11.19.1）：`npm ci` 不受影響，照 lock 裝；`npm install` 遇到太新的版本明確報錯；`min-release-age-exclude` 只放行指定的套件，它的依賴仍照冷卻期。CI 和 Docker 建置都用 `npm ci`。
3. **緊急修補兩級**，條件與流程全文在 `docs/rules/deploy.md` 禁令 69，這裡不重複。一般緊急修到 dev 再通知維護者；嚴重漏洞每次先問維護者，同意一次就包含合併 main 與發版。
4. **放行的寫法**：npm 只在更新 lock 的那條命令帶例外，不寫進 `.npmrc`，因為 lock 才是安裝的依據，而且 Guard #23 逐字比對 `.npmrc`。Python 要寫進 `pyproject.toml`，同時列進測試的例外清單。2026-11-06 實測（uv 0.12.1）：值要填所有檔案都上架之後的時間，填剛好的上架時間選不到；修補版要求的新依賴一樣受冷卻期管，要一起列（boto3 新版要求同樣新的 botocore，只放行 boto3 選不到新版）。
5. **每日檢查**：`.github/workflows/dep-audit.yml` 每天跑現有的 Guard #22、#23，掃 main 和 dev，不花 API 額度。
   - **權限分開**：掃描 job 會裝依賴、跑分支上的程式，只能讀。回報 job 才能寫 issue，它只跑 main 上只用標準函式庫的小腳本，只有從 main 觸發才會跑。
   - **結果檔不可信**：小腳本只篩出格式固定的欄位，都放反引號裡，其他文字不進 issue。
   - **用 issue 通知**：排程失敗的通知只發給建立排程的人，也要在通知設定打開才收得到。所以每條分支一張 issue，指派給維護者。內容沒變不重複通知；那條線掃乾淨才關；這次沒跑完不關也不蓋掉內容。
   - **例外到期提醒**：掃描 job 算出 Python 例外的到期日，回報 job 把過期的列進 issue。測試不依日期變紅，否則 CI 會在某一天突然紅。

## 後果

- 被劫持的版本多放過一點；修補少延後一半。這是維護者接受的取捨。
- 每日檢查進 main 之後才會開始跑，GitHub 的排程只跑預設分支。同一份文件也寫，排程可能延後或被丟掉，公開 repo 60 天沒有動靜會自動停用（[官方文件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)，2026-11-06 查）。
- **沒收到 issue 不等於安全**：弱點資料庫還沒收錄就查不到，幾小時內爆發的事件還是要靠新聞或人告知；排程被丟掉、回報步驟自己失敗時也不會有通知。
- 掃描本身沒跑完也會開 issue，偶爾會因為網路問題多一次通知。
- Issue 是公開的，但只列套件名、版本和公告編號；lock 檔本來就公開，任何人都能跑同樣的掃描。
- Node 的檢查結果沒有版本，issue 上那一欄留空。

## 驗證

- `tests/unit/test_uv_dependency_cooldown.py`：五處數字一致、Python 例外要列在清單裡。
- `tests/unit/test_dep_audit_report.py`：回報邏輯與不可信輸入，用存好的輸出，不連網。
- `tests/lint/test_dep_audit_workflow.py`：權限分開、回報只從 main 跑、action 釘 commit SHA。
- 手動連網的驗證結果寫在 PR 說明。進 main 之後要手動試跑一次，含一次模擬有問題，確認通知收得到。

## 回復

不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。改為還原程式、設定與測試，本 ADR 改「⛔ 已撤回」並在 CHANGELOG 加一列。每日檢查如果太吵，在 GitHub 上停用那個 workflow 即可。

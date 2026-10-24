# ADR-0282：braces 同版本防禦回補整套退役

- 狀態：✅ 已接受（維護者 2026-10-24 核准）
- 日期：2026-10-24
- 取代 ADR-0237；修訂 ADR-0238（決策 3 與「影響」裡「braces 回補維持不變」一句）與 ADR-0222（Guard #23 的 lock 檢查與來源一致性）。
- 相關：ADR-0278（CI frontend job 的步驟順序，裡面提到的 braces 補丁步驟已不存在）、ADR-0236（已歸檔）
- 影響範圍：`scripts/guard_node_cve.py`、`scripts/guard_all.py`、`scripts/hermetic_gate/npm_offline_gate.py`、`scripts/guard_eslint_ratchet.py`、`scripts/list_theme_violations.py`、`scripts/summarize_theme_violations.py`、`ToukaIDE/scripts/build_marketplace_candidate.py`；ToukaChat 與 ToukaIDE 的 `package.json` scripts；`ToukaChat/Dockerfile`、`ToukaChat/Dockerfile.prod`；`.github/workflows/native-tool-contracts.yml`；`.gitattributes`、NOTICE；刪除 `ToukaChat/container-security/braces-backport/`、`scripts/braces_backport_evidence.py`、`tests/fixtures/braces-backport/` 與四個 `test_braces_*.py`；相關測試、SECURITY、SSOT_MASTER_MAP、NOQA_REGISTRY、tests/README

## 背景

- GHSA-vfj7-8cjw-p6xm（CVE-2026-93687）說 braces ≤ 3.0.3 遇到深層巢狀 pattern 會耗盡 call stack。上游沒有修正版（最新仍是 2024-05-21 的 3.0.3）。ADR-0237 因此對安裝好的 3.0.3 套用固定 PR72 的 depth 上限，在 npm scripts、Dockerfile、CI 與 Marketplace 打包裡先 apply 再唯讀 verify，並讓 Guard #23 的 `node-lock-and-braces-backport-v1` profile 只在補丁與固定相依圖都吻合時，把 braces 的 HIGH 列為已緩解。
- GitHub 在 2026-10-23T12:53Z 撤回這則公告，理由是「實際上無法被利用」；npm 的公告資料不再回傳 braces。
- braces 在 Chat、IDE 的 lock 裡都只有一份 `node_modules/braces`，標 `dev: true`；唯一依賴它的 micromatch 也是開發相依。產品程式沒有用到補丁才有的行為（`maxDepth` 只出現在補丁與測試 fixture）。
- 那套證據檢查要求 full 範圍的 high 必須等於 braces 相依圖。公告撤回後，只要出現任何別的 high，Guard #23 就以「braces blocking graph coverage」中止，IDE 的兩個範圍根本不會被掃到。

## 決定

1. **回補整套退役**：刪除修補工具、補丁、證據程式、專用測試與 fixture。兩個專案的 npm scripts 不再先跑 `braces:verify`，`install:controlled` 改成 `npm ci --ignore-scripts`（名稱保留，呼叫處不變）。Dockerfile、CI、Marketplace 打包、離線建置檢查與三支直接執行的 Python 工具都拿掉 apply／verify。braces 回到官方原版 3.0.3。
2. **Guard #23 回到單純的 npm audit**：拿掉 `--profile`，`guard_all.py` 不帶參數呼叫。high／critical 照常阻擋，四個範圍都會掃完。原本和 braces 無關的檢查都保留：tracked `.npmrc` 六鍵、只收官方 registry 的 lock、固定 argv、不帶憑證的子環境、npm audit v2 JSON 與 exit code 一致、證據保存。
3. **掃描途中來源被改就中止**：原本這項只在 braces profile 裡生效。現在固定對 `ToukaChat/.npmrc`、兩個 `package.json`、兩份 lock 在開跑時（讀 policy 之前）取 sha256，結束前再比一次，不同就以證據無效中止。guard 自己的模組開跑時已載入，不列入。
4. **收緊 lock 完整性**：Guard #23 的 lock 檢查在 URL 檢查之後，呼叫 `npm_offline_gate.validate_lockfile`，每個套件都要有官方 registry 的 resolved 與 `sha512-` integrity，錯誤包成 `AuditExecutionError`。ADR-0222 原本只驗 resolved URL；SECURITY 與 SSOT 早就寫著「SHA-512」，現在程式與文件一致。CI 的 `test_p8b_supply_chain_gate.py` 也對 IDE 的 lock 呼叫 `validate_lockfile`（原本只驗 Chat）。
5. **失敗時列出套件**：範圍內有阻擋項目時，另外印出每個 high／critical 套件的名稱與 GHSA 編號，`via` 是字串的印成「via X」。只是輸出，判定與回傳值不變：只在有阻擋時解析、`via` 缺少或形狀不對就略過、不印標題與網址等來自網路的自由文字，套件名稱不符合 npm 名稱格式就印 `<unprintable>`。
6. **兩個和 braces 無關的檢查保留**：兩個 `package.json` 不能有 lifecycle hook（併入 `test_p8b_supply_chain_gate.py`）、knip 的預載腳本（併入 `test_knip_worktree_parity.py`）。

## 後果

- 產品沒有差別。開發指令少一步檢查；Chat 映像下次重建時 npm 那一層快取會失效。
- 拿掉的檢查：不再驗安裝好的 braces 是補過的版本；Chat 正式映像不再驗執行環境裡沒有 braces（braces 本來就是開發相依，正式映像用 `--omit=dev` 安裝）。
- 收緊的檢查：lock 裡任何套件缺 SHA-512 就變紅，本機 Guard #23 與 CI 都擋。
- Guard #23 退役後仍是紅的，原因是 body-parser（修正版 2.4.0 等 7 天冷卻期）與 IDE 打包工具帶入的 @azure/msal-node（等上游），兩者都記在 SECURITY.md。
- 撤回的路：revert 本 ADR 的變更後，兩個專案各跑一次 `install:controlled`，補丁才會重新套上。公告若日後恢復，Guard #23 會照常變紅，再另行評估。
- 本機已經裝好的 `node_modules` 裡 braces 仍是補過的內容，下次 `npm ci` 就會換回原版；兩者只有在巢狀超過上限或明確傳 `maxDepth` 時行為不同，repo 裡沒有這種用法。

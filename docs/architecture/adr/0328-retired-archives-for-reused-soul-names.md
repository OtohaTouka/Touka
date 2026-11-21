# ADR-0328：刪掉的靈魂留下的封存與個人空間不給同名的新靈魂；退役資料夾 `data/retired`

- 狀態：✅ 已實作
- 日期：2026-11-21
- 決策者：維護者（總覽板第 96 項：照同名接手計畫第 2.1 版依序做；第 98 項回「甲」：PR 3 照新估算拆成兩個 PR）。封存搬到 `data/retired`、個人空間補刪而不是拒絕建立，是 PR 3 細節第 2.1 版裡継初定的（DEV-A 兩輪通過）
- 相關：[ADR-0326](0326-deleted-soul-leftovers-not-inherited.md)（同名接手；本 ADR 是它的 PR 3b）、[ADR-0325](0325-mission-board-system-folder.md)（`data/missions` 為什麼代理人碰不到）、[ADR-0293](0293-agent-folders-no-follow.md)（不跟隨連結的檔案操作）、[ADR-0142](0142-soul-crud-repair-and-admin-stepup.md)（刪除靈魂的儲存範圍）
- 修訂 ADR-0293（多一個不跟隨的跨資料夾搬移 `move_entry_no_follow`）、ADR-0326（決定 11 指向本 ADR）
- 影響範圍：ToukaMemory `utils/no_follow.py`；ToukaProxy `services/resource_manager.py`、`services/agent_leftovers.py`、`utils/soul_io.py`；測試名單 `tests/zone_mutations.json`、`tests/lint/test_resource_manager_permission_repair.py`、`tests/lint/test_zone_path_guard.py`、`tests/support/link_defenses.py`

## 背景

靈魂的兩個資料夾只看名字：

- **封存** `legacy/<名字>`：舊對話庫，存滑動視窗剪下的舊段與「切分存入舊對話」。刪除靈魂時會刪，但刪不掉只列進 `skipped`；自訂模型、一般模型用「切分存入舊對話」存的封存不會被刪（自訂模型讀不到自己的封存，刪自訂模型也不碰它）。
- **個人空間** `personal_drive/<名字>`：刪除靈魂時刪不掉就留下。

之後有人建同名的靈魂，新靈魂直接讀到舊封存與舊檔案；新靈魂指給別人擁有時，那個人也一起拿到。ADR-0326 決定 8 把這件事排在 PR 3；對話在 PR 3a 做完（ADR-0326 決定 10），這是 PR 3b。另立 ADR 的理由：多了新的資料位置 `data/retired` 與新的不跟隨搬移。

## 決定

### 1. 退役資料夾 `data/retired`

- SSOT：`resource_manager.get_retired_dir()`（`RETIRED_DIR_NAME`），和 `legacy` 同一個 data 根、同一個掛載點。
- `resource_manager.is_protected_path` 加上 `retired`（和 `legacy`、`soul` 一樣）：專案根設到 data 底下時也擋得住代理人的寫入。
- 代理人碰不到的其他理由同 ADR-0325 的 `data/missions`：終端沙盒、執行器容器、隔離的第三方 MCP、本機小幫手只給代理人資料夾；`retired` 不在任何錨點底下，不用加進別名檢查。
- 只有決定 3 的搬移會寫它。沒有介面，管理員自己到資料夾看或刪；備份照 data 整個包含。

### 2. 不跟隨連結的跨資料夾搬移

- `no_follow.move_entry_no_follow(src_parent, old, dst_parent, new)`：和原本的同資料夾改名同一套。兩個上層都不跟隨連結開，不建上層；`old` 是連結（含 Windows junction）就拒絕；`new` 已存在丟 `FileExistsError`（只差大小寫的同一個項目照常改名）；只改名、不複製，跨裝置時照 `os.rename` 丟錯。
- `rename_entry_no_follow` 改成呼叫它（同一個上層）。
- 不用 `os.replace`、`shutil.move`：POSIX 上目標是空資料夾時 `os.replace` 會直接蓋過去，也不擋 junction；`shutil.move` 跨裝置會退回複製（連結也一起複製）。

### 3. `resource_manager.retire_legacy_dir(identity) -> bool`

- 從**解析後的** data 根往下操作：data 路徑上有連結（Linux 連到別顆磁碟、macOS 的 `/var`、Windows junction）時，不解析的話每次都被不跟隨的檢查拒絕。
- `legacy/<名字>` 不存在回 False；是連結丟 `LinkRefusedError`；`retired/legacy` 用 `make_dirs_no_follow` 建（`retired` 是連結時拒絕）。
- 新名字 `<毫秒時間>-<名字>`；UTF-8 超過 200 bytes 時改成 `<毫秒時間>-sha-<sha256(名字) 前 16 碼>`（名字太長時改名會失敗，這個名字就永遠建不起來）；同一毫秒撞名加 `-2`…，試 5 次仍撞名丟 `FileExistsError`。
- 其他錯誤照實往上丟，封存留在原地。掛 `permission_repair(retry=False)`。

### 4. 建立靈魂時（`agent_leftovers.clear_for_new_soul` 最後一步）

- 名字不屬於任何代理人時（和排程、機器人、身分設定、統計、對話同一次判斷，ADR-0326 決定 3）：先 `retire_legacy_dir`，再 `soul_io.remove_soul_drive`（原本的 `_remove_soul_drive` 改成公開）。
- 任何失敗丟 `SOUL_VAULT_ERROR`，不建立。
- 個人空間的根是連結：只刪連結、不碰另一端（區內方法的既有行為）。刪除靈魂遇到這種連結是 409、什麼都不刪（ADR-0293 R11：靈魂還在，要先處理連結）；建立時靈魂已經不在，拒絕的話管理員沒有畫面上的路可以處理，只刪連結又不碰另一端，所以兩邊不同。
- 放最後：檔案失敗時，前面清掉的都是舊東西，不用復原。
- 用編輯建立判定為修復（名字還屬於某個代理人）時不動。
- `create_soul` 的 D6「既有的 legacy、個人空間不回滾」照舊：清完就不存在，`newly_created` 會把它們算成新建（之後失敗時回滾刪的是新建的空資料夾）。

### 5. 不改刪除

刪除靈魂照舊刪 `legacy`、`personal_drive`（沒刪成列 `skipped`）。自訂模型的刪除、改名不動封存：它自己讀不到，下次有靈魂用這個名字時搬走。

## 沒選的做法

- 封存刪掉：封存是人類對話的舊段，計畫寫「不刪、搬到只有管理員看得到的地方」。
- 個人空間也搬到 `retired`：個人空間的根可以設在別的磁碟（錨點），跨磁碟搬會失敗；它只有刪除沒做完才會留下，刪除本來就要刪它。
- 個人空間或封存殘留時拒絕建立：管理員沒有畫面上的路可以處理。
- 封存換鍵（例如記在對話資料庫、不再只看名字）：要改舊對話庫的讀寫，範圍大。
- 自訂模型刪除時也搬封存：它自己讀不到，搬不搬對安全沒差別，建立靈魂時會處理。

## 已知限制

- POSIX 沒有 `RENAME_NOREPLACE`：「查目標不存在」到「改名」之間若有人建出空資料夾會被蓋過去。`retired` 只有這段程式寫，可以接受。
- Windows 沒開長路徑時，`retired/legacy/<毫秒時間>-<名字>` 比原路徑長約 16 個字元，原路徑接近 260 時搬移失敗、建立回 `SOUL_VAULT_ERROR`。
- Windows 上封存裡有檔案開著（防毒軟體、剛好在讀）時改名失敗，建立回 `SOUL_VAULT_ERROR`，稍後再建一次。Docker 若把 `legacy` 另外掛載（不在 data 同一個掛載點），跨裝置搬移失敗、建立不起來（預設的 compose 是同一個掛載點）。
- 退役的封存沒有介面，要管理員自己到 `data/retired` 看或刪，不會自動清。
- 名字還有人用（登記表有、`custom/<名字>` 留著）時不搬（和 ADR-0326 PR 1 同一套規則）。
- 升級前已經被同名靈魂接手的封存不會回頭分開。

## 驗證

- `tests/unit/test_samename_archive_security.py`：真的靈魂路由與 conftest 沙盒，殘留直接寫進 data 根。涵蓋建立時搬走封存（內容原樣、新靈魂的封存是空的）、補刪個人空間、根是連結只刪連結、搬不走或刪不掉就不建立、名字有人用不動、用編輯建立也做、封存是連結時拒絕；搬移本身：data 路徑上有連結照樣搬、只改名不複製（同一個 inode）、沒有封存不建 `retired`、名字太長用雜湊、撞名換名字與試到上限、`legacy/<名字>` 或 `retired` 是連結時拒絕、`retired` 是受保護路徑。
- `tests/unit/test_no_follow_zone.py::test_move_entry_between_folders`：跨資料夾搬、目的已存在、來源不存在、目的上層不存在不建、來源或目的上層是連結、名稱不是單段。
- 改壞清單 `tests/zone_mutations.json` 加 `r10_retire_legacy_dir_copies`（搬移變成複製時對應測試變紅）。

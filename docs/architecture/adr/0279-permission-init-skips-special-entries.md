# ADR-0279：GHCR 啟動權限檢查遇到特殊項目改成略過

- 狀態：✅ 已接受
- 日期：2026-10-23
- 影響範圍：`ToukaHeartbeat/permission_init.py`；`tests/unit/test_deployment_permissions.py`；`tests/docker/permission_init_real.py`（新）；`.github/workflows/isolation.yml`（容器 job 一步、觸發路徑）與 `tests/lint/test_isolation_ci_workflow.py`；README_QUICKSTART、SSOT_MASTER_MAP、HANDOVER、`tests/README.md`
- 相關：ADR-0221（GHCR init 的四個角色、只在既有掛載上調整中繼資料；角色與指令都不變）、ADR-0272（已知限制第 6 條由本 ADR 處理）

## 背景

- GHCR 部署每次 `up` 先跑 `touka-init`（`permission_init.py`），把 data、`hf_cache`、Proxy 設定、心跳設定四個掛載的擁有者調成 1001。舊規則：只要碰到不是「資料夾或單一連結的一般檔」的項目、setuid／setgid、延伸屬性，或超過 10 萬項、64 層、60 秒，就整個拒絕，Proxy 因 `service_completed_successfully` 起不來。
- 嵌入模型經 Hugging Face hub 下載到 `hf_cache`（`embedding_service.py` 的 `hf_hub_download`、`vector.py` 的 SentenceTransformer），repo 沒關它的符號連結；Linux 上 `snapshots/` 一律是指向 `blobs/` 的符號連結。所以用過記憶之後，下一次 `up`（例如升級）必定被擋。真核心重現：改動前的 init 在含 Hugging Face 快取形狀的資料上，預檢就回 `UNSAFE_METADATA`。主機重開機由 Docker 直接重啟 Proxy、不經 init，不受影響。
- 代理人在 `personal_drive`、`mcp_servers` 建 Python venv 或跑 `npm install` 也會產生符號連結，npm 專案動輒數萬個檔。
- CI 從沒有實際跑過 `touka-init`（只有模擬中繼資料的測試；GHCR 形狀那一步也只起執行器）。

## 決定

1. **分類**：盤點時只用不跟連結的中繼資料（`lstat`）決定每一項：

   | 項目 | 處理 |
   |---|---|
   | 資料夾（含 setuid／setgid） | 照常調整。Linux 對資料夾改擁有者不會清掉 setgid，改完照舊核對權限位元 |
   | setgid 資料夾而且還要補擁有者權限位元 | 盤點時就 `UNSUPPORTED_METADATA`、什麼都不改：補位元時核心會清掉 setgid（init 不在新群組 1001、也沒有 CAP_FSETID），否則會改到一半才失敗、下一次就默默拿掉 setgid 通過 |
   | 單一連結、沒有 setuid／setgid 的一般檔 | 照常調整；只開讀中繼資料用的描述元，不讀內容 |
   | 帶延伸屬性的檔或資料夾 | 只為列出延伸屬性名稱而開（延伸屬性只能從已開的描述元讀），不修改；資料夾不往下走（`xattr`） |
   | 符號連結、FIFO、socket、裝置檔、多連結的一般檔、setuid／setgid 的一般檔 | 不開啟、不跟進、不修改（`symlink`、`fifo`、`socket`、`device`、`hardlink`、`setid_file`） |
   | `data/personal_drive/<代理人>/…`、`data/mcp_servers/<伺服器>/…` 裡名稱在工具資料夾清單的資料夾（工作區第一層不算） | 不往下走、不修改（`tool_dir`） |

2. **根目錄嚴格**：四個掛載的根目錄，以及 `data/personal_drive`、`data/mcp_servers` 本身，遇到連結、setuid／setgid、延伸屬性照舊直接失敗。兩個工作區根是一般檔時，舊版當普通檔處理，這次起也直接失敗（本來就是無效配置，README 與執行器都要求資料夾）。SELinux 主機（根目錄帶標籤）因此照舊明確失敗，不會變成「通過卻什麼都沒改」。
3. **漂移照舊失敗**：最後的驗證掃描把被略過的項目一起比對（種類與中繼資料都要相同），盤點後被換掉、改了時間或型別都算 `METADATA_DRIFT`。修改照舊只經核對過的描述元；掛載重疊、別名、巢狀掛載、帳號與權限能力檢查都不變。
4. **上限**：被略過的項目照樣算進 10 萬項、64 層與清單位元組的上限。名稱的位元組數用 `os.fsencode` 計算，不是合法 UTF-8 的檔名照常處理（舊版會因編碼錯誤失敗）。
5. **工具資料夾清單**：`TOOL_DIR_NAMES` 是 `ToukaProxy/src/utils/touka_dirs.py` `SCAN_SKIP_NAMES` 的副本（init 是獨立程式、以 `python -I` 執行、不能匯入 Proxy），由 `test_tool_directory_names_match_the_project_scan_skip_list` 釘住兩份完全相同，目前是 `node_modules`、`.venv`、`venv`、`__pycache__`、`.git`、`dist`、`build`、`.tox`、`.mypy_cache`、`.next`、`target`、`vendor`、`.cache`、`coverage`、`out`、`.gradle`、`.terraform`。只在代理人或伺服器資料夾裡面套用（路徑至少第 4 段），工作區第一層（例如叫 `build` 的代理人）與工作區以外（例如 `data/soul`）的同名資料夾照常檢查；只限資料夾，同名的一般檔照常處理。
6. **輸出**：結果 JSON 多一個 `skipped`，固定列出八種略過的件數；不列路徑或檔名。compose 只看結束碼。
7. **真核心測試**：`tests/docker/permission_init_real.py` 在主機上用 docker 跑三種情況（照常通過且略過的項目與角色外的 root 檔都不變；setgid 資料夾還要補擁有者權限位元時預檢就拒絕；根目錄帶延伸屬性照舊失敗；兩個失敗情況都要求前後完全沒有改動）。測試資料只放在腳本自己建的 `touka-pinit-test-*` volume，由映像內的 root 建立；init 以 GHCR compose 的同一組權限（root、只留 CHOWN／FOWNER／DAC_OVERRIDE、不連網）跑，每個角色是同一個 volume 的 subpath，多連結檔才能指到角色外、同一個檔案系統上的檔；角色外的 `outside` 也以 subpath 掛進容器（不在任何角色底下），指到外面的符號連結才真的解析得到，init 若跟進就會改到外面那個 root 的檔。CI 容器 job 在建好 Proxy 映像後跑這一步（用映像裡的 python 與 appuser 跑 checkout 的 `permission_init.py`），不新增 `docker build`；觸發路徑加 `ToukaHeartbeat/permission_init.py`。

## 後果

- 用過記憶、或代理人在工作區建過 venv／npm 專案的 GHCR 部署，`up` 照常啟動。
- 已知限制：
  - setgid 資料夾如果還要補擁有者權限位元，預檢就拒絕（見決定 1）。代理人能在自己的工作區建這種資料夾讓 `up` 失敗；同樣能讓 `up` 失敗的還有：把工作區根本身改成 setgid 或加延伸屬性、建名稱不在清單上又超過上限或 64 層的資料夾。這些是失敗、不是放寬，修法是由部署者刪掉那些項目。
  - 被略過的項目擁有者不調整：主機放進來的多連結檔、帶 ACL 的檔、工作區的工具資料夾。清單裡的 `.git`、`vendor`、`build` 等不一定能重建，從主機複製進去、擁有者不是 1001 時代理人可能改不了，要由部署者自己改（Linux 主機上 `sudo chown -R 1001:1001 <資料夾>`；README_QUICKSTART 有寫）。
  - `touka-init` 假設執行時沒有人在寫目標資料夾。README 的手動流程改成先停 executor 與 proxy；升級時 `up -d touka-proxy` 會在舊執行器還在跑時執行 init，這時同時寫入只會造成漂移失敗（被改的一律是核對時只在樹內、單一連結的 inode），重跑即可。
  - 代理人可以把任何資料夾取名 `node_modules` 讓 init 不往下走，但只影響它自己的工作區。
  - 工作區裡名稱不在清單上、又超過上限的大資料夾，init 照舊失敗。
- `permission_init.py` 在 repo 以 CRLF 逐位元組保存（`.gitattributes` 的 `-text`），修改照原樣維持。

## 驗證

- 模擬中繼資料（101 個）：每一類略過、根目錄與工作區根照舊失敗、只需改擁有者的 setgid 資料夾照常處理、還要補位元的預檢拒絕、工具資料夾只在代理人或伺服器資料夾裡略過且只限資料夾、略過算進項目數、層數與清單位元組上限、無法解碼的檔名、略過的項目改型別或改時間仍失敗、輸出不含名稱、清單和 `SCAN_SKIP_NAMES` 相同。
- 突變 16 項全紅：跟進符號連結、處理多連結檔、處理 setid 檔、拿掉略過清單的漂移比對、根目錄不嚴格、工具資料夾在工作區外也略過、工具資料夾在工作區第一層也略過、同名一般檔也略過、略過不算項目數、略過完全不計數、拿掉層數檢查、拿掉累計清單位元組檢查、帶延伸屬性的照常修改、工作區根不嚴格、拿掉 setgid 預檢、名稱改回 UTF-8 計數。
- 真核心（Docker Desktop，WSL2 核心 6.18）：三種情況都符合（含指到外面、真的解析得到的連結）；改動前的 init 在「照常通過」情況預檢回 `UNSAFE_METADATA`。

## 撤回

撤回程式與測試的改動即可，不寫任何資料；init 回到看到符號連結就拒絕。已被 init 改成 1001 的檔不會被改回。本 ADR 保留並標「已撤回」，不刪檔。

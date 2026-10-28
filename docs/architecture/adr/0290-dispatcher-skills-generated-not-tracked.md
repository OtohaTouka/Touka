# ADR-0290：兩個調度員技能檔改成啟動時產生、不進版控

- 狀態：✅ 已實作。維護者 2026-10-28 決定不再追蹤兩個調度員檔、改到設定自癒之後才產生、內容沒變就不寫；一頁計畫經 DEV-A 兩輪後由維護者核准。
- 日期：2026-10-28
- 決策者：維護者
- 相關：ADR-0066（技能目錄格式）、ADR-0096（啟動健檢與 bootstrap 開關）、ADR-0102（MCP server 名稱遷移）、ADR-0148／0161（技能調度員內容每次請求重算、MCP 動態掛載開關）、禁令 63。決策都不變。
- 影響範圍：ToukaProxy `services/skill_service.py`、`main.py`、`.gitignore`；刪除版控裡的 `config/skills/system-mcp-dispatcher/SKILL.md`、`config/skills/system-skill-dispatcher/SKILL.md`；tests `test_public_repo_hygiene.py`、`test_skill_dispatcher.py`、`test_skill_service_dir.py`、`test_lifespan_bootstrap_wiring.py`；SSOT_MASTER_MAP、README_QUICKSTART、HANDOVER、CHANGELOG、tests/README。

## 背景

- `system-mcp-dispatcher`、`system-skill-dispatcher` 兩個 SKILL.md 由 `SkillService` 依 MCP 設定（`server_config.json`）與已裝技能算出。以前 `skill_service.py` 在模組載入時就呼叫兩個 sync，每次都整份重寫（temp＋`os.replace`）。
- 兩個檔又被 `ToukaProxy/.gitignore` 用 `!` 規則放回版控。
- 2026-06-14 已把檔裡的 `version` 從時間戳記改成內容雜湊，內容本身不再每次變。但 Docker 的 Proxy 經 bind mount 寫出的新檔，Windows 上的 Git for Windows 2.53 會誤判成改過：HEAD、index、工作檔三者 blob 相同仍顯示 M，`git -c core.fscache=false update-index --refresh` 後才恢復。所以每次重啟 Touka，主資料夾都顯示這兩個檔被改。
- 加過 MCP 伺服器或裝過技能的本機，兩個檔和版控內容真的不同。MCP 調度員列出本機的伺服器名與啟動指令，可能被誤帶進公開 repo 的 commit。

## 決定

1. **不進版控**：`ToukaProxy/.gitignore` 拿掉兩條 `!config/skills/system-*-dispatcher/`，兩個檔從版控刪除（`git rm`）。另外兩個自帶技能（`deep-research-coordinator`、`document-generator`）照舊追蹤。
2. **產生時機在 lifespan**：`skill_service.py` 載入時不再寫檔；`main.py` 的 lifespan 在設定自癒（bootstrap 開啟時的 `ensure_configs`）與 ADR-0102 的 MCP 名稱遷移之後，依序呼叫 `sync_system_dispatcher()`、`sync_skill_dispatcher()`。
   - 不看 bootstrap 開關。`config.py` 的 `is_bootstrap_enabled()` 說明是「關閉時嚴禁自動寫入系統檔（含首次生成密鑰的持久化）」。調度員檔不在這條範圍：它和 MCP 存檔（`routers/mcp.py`）、技能安裝／更新／刪除（`skill_service.py`）後觸發的 sync 是同一類由現有設定算出的產生物，這些和原本載入時的產生都不看開關。改成受開關控制的話，關閉 bootstrap 的部署在第一次改 MCP 或技能之前都不會有這兩個檔。
   - 兩個 sync 自己接住錯誤並記錄，失敗不影響啟動；讀 MCP 設定（`get_config_dir()`、`read_json`）也收進 `sync_system_dispatcher` 的 try。
3. **內容沒變就不寫**：新私有函式 `_write_if_changed`。檔不存在就寫；讀舊檔用通用換行（只差 CRLF 也算相同）；讀不到（`OSError`、`UnicodeDecodeError`）記警告後照寫，壞掉的檔才修得回來；相同就不寫。沒寫時照樣更新記憶體裡的技能清單（`_metadata`）。

## 後果

- 重啟 Touka 後 git 不再顯示這兩個檔被改；本機的 MCP 伺服器名與技能清單不會進 commit。
- 移到名稱遷移之後，順便修好一個潛在問題：以前載入時產生的 MCP 調度員，會列出遷移前的舊 server 名，要等下一次 MCP 存檔才更新。
- 載入 `src` 仍有其他副作用（例如 bootstrap 開啟時 import `src.config` 會補設定檔與 `.env`），禁令 63 照舊適用；這裡只保證載入時不寫這兩個檔。
- 執行器（`docker-compose.ghcr.yml` 的 `skills` subpath、`.github/workflows/isolation.yml`）需要映像裡有 `config/skills`；這靠另外兩個自帶技能仍被追蹤。
- **升級注意**：任何資料夾更新到這一版時，git 要把兩個檔從資料夾移除。本機兩檔和版控相同就直接移除，Touka 下次啟動會產生回來；不同（加過 MCP、裝過技能）時 git 會拒絕更新，Windows 誤判成改過時也可能拒絕。兩種情況都先執行下面這行再更新：

  ```bash
  git restore -- ToukaProxy/config/skills/system-mcp-dispatcher/SKILL.md ToukaProxy/config/skills/system-skill-dispatcher/SKILL.md
  ```

- 更新後到重啟前：只有很久以前在技能選單勾過 `system-mcp-dispatcher` 的舊對話暫時少了它；輸入列的 MCP 開關（`ctx.mcp_dispatcher`）只加 `connect_mcp_server`，不讀這個檔，不受影響。重啟一次就恢復。
- GHCR：新映像不含這兩個檔；舊的設定 volume 留著舊檔，內容不同時才改寫；新 volume 由 lifespan 產生。
- 撤回：revert 即恢復原樣；資料夾裡被忽略的兩個檔會被版控內容蓋過（git 把被忽略的檔當成可覆蓋），下次啟動再依本機設定重寫。本 ADR 檔保留，在狀態註記撤回。

## 替代方案

- **只做「內容沒變就不寫」、照舊追蹤**：改動最小，平常重啟不再弄髒；但新增 MCP 或技能後檔案照樣和版控不同，本機設定被帶進 commit 的風險還在。不採用。
- **調度員只存在記憶體、不寫檔**：最根本，但會牽動技能清單、啟用流程（`get_skill`、舊對話）與前端顯示，改動大得多，適合之後和技能系統一起整理。不採用。
- **每次重啟後清 git 的狀態快取**（`update-index --refresh`）：每次都要清，真差異也照樣出現。不採用。

## 驗證

- 先寫的測試在實作前全紅：
  - `test_public_repo_hygiene.py`：兩個調度員路徑加進 `MUST_BE_IGNORED`。
  - `test_skill_dispatcher.py`：AST 掃 `ToukaProxy/src` 全部模組，import 時會執行的地方（模組層、class 本體、裝飾器與預設值）沒有呼叫兩個 sync；清單沒變再 sync 不寫；舊檔只差 CRLF 不寫；沒寫時技能清單仍有調度員。
  - `test_skill_service_dir.py`：MCP 調度員 registry 沒變再 sync 不寫；沒寫時技能清單仍有它；`read_json` 拋錯只記錄、不往外丟。
  - `test_lifespan_bootstrap_wiring.py`：lifespan 在 MCP 名稱遷移之後依序呼叫兩個 sync；bootstrap 開啟時 `ensure_configs` 在 sync 之前（設定目錄與 `.env` 導向 tmp，第二個 sync 丟哨兵例外結束啟動）。
- 守門案（實作前就綠）：舊檔內容不同照寫、舊檔不是 UTF-8 照寫；檔不存在照寫由既有測試涵蓋。
- 突變檢查：10 個突變（拿掉忽略規則、加回載入時寫檔、拿掉 lifespan 呼叫、拿掉內容比對、逐位元組比較、讀檔失敗就不寫、檔不存在就不寫、檔存在一律不寫、兩個 sync 各自沒寫時跳過技能清單）都讓測試變紅。

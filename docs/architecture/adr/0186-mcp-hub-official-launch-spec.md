# ADR-0186: MCP Hub 官方條目啟動規格 SSOT 化（uvx/npm 正名＋filesystem 允許目錄哨兵）

> C2-3 現況補註：來源分類不改寫 argv；合法 scaffold 的 APPDATA 資料哨兵在 profile 中對映到 personal_drive，試跑與正式啟動一致。一般第三方的資料根參數會先改判自建，不能沿用第三方資料夾授權；直接啟動維持原哨兵語意。詳見 ADR-0275。

## 狀態

✅ 已接受（MCP connectors 能力調查 F1+F2；維護者核准計畫後實作——裁決 A＝未知目錄 fallback 有聲、B＝args 哨兵鏡射 env 雙解析語意、C＝E2E 由 agent 代跑）

## 日期

2026-07-24

## 背景

`MCPHubService.fetch_official_servers` 把官方 repo（`modelcontextprotocol/servers`）`src/` 下每個目錄一律映射成 `npx -y @modelcontextprotocol/server-{name}`。2025 年官方大搬遷後 `src/` 僅剩 7 目錄，其中逐條向 registry 實證（2026-07-24）：

- **F1（4/7 條 npm 404）**：`fetch`／`git`／`time` 實為 **PyPI** 套件（`mcp-server-*`，須 `uvx`；npm 同名不存在）；`sequentialthinking` 的 npm 正名帶連字號（`server-sequential-thinking`）。硬編映射產出的四條安裝後 npx 必 404。
- **F2（filesystem 初始化即拋錯）**：官方 filesystem server 明言「無命令列允許目錄、且 client 不宣告 roots 協定 → 初始化 throw」；Touka 的 `ClientSession` 未宣告 roots，原條目無引數＝裝了必壞。

合計 7 條官方貨架 5 條裝了連不上（僅 everything/memory 倖存）。實證版本：npm `server-everything` 2026.7.4／`server-filesystem` 2026.7.10／`server-memory` 2026.7.4／`server-sequential-thinking` 2026.7.4；PyPI `mcp-server-fetch`/`-git`/`-time` 皆 2026.7.10。

## 決策與實作

- **`src/utils/mcp_official_map.py`（新 SSOT）**：`OFFICIAL_LAUNCH_SPECS`（tuple 純資料表，7 條實證規格）＋`official_launch_spec(dir_name) -> (command, args, known)` 純函式（體例比照 `mcp_category`：零 IO/時間/環境依賴）。未知目錄 fallback npx 慣例＋`known=False`（**裁決 A**：新官方 server 保持可見，由呼叫端記 WARNING＝無聲變有聲）。
- **`fetch_official_servers` 查表化**：command/args 改自查表；其餘欄位組裝不變；`known=False` 時 `logger.warning`。
- **args 哨兵解析（SSOT 延伸）**：`mcp_sentinels.resolve_args_sentinels`（逐元素委派 `resolve_env_sentinel`＝env/args 同一組哨兵）。兩個消費點鏡射 env 既有雙解析語意（**裁決 B**）：`install()` args 逐元素過 `_resolve_env_sentinel` 落盤絕對路徑（`@TOUKA_OUTPUT@` 出現在 args 同樣確保建夾）；`connect_mcp_server` 啟動時再解析（磁碟手寫哨兵亦可攜還原）。
- **filesystem 允許目錄**：映射表 args 尾帶 `@TOUKA_APPDATA@` → 解析為 app_data 絕對路徑（F2 修）。
- **邊界註記**：filesystem 讀寫界＝**app_data 整層**（與 mcp-docgen 哨兵 backstop 同界）；`vault_jail_arg` 是 tool-call 參數層機制、不適用進程啟動引數，故此界**無 per-agent vault 細分**——細化屬另案。
- 環境前提：容器（[Dockerfile:5](../../../ToukaProxy/Dockerfile) COPY /uvx）與原生 dev（README 前置 uv）皆備 uvx；`is_command_safe("uvx")` 通過既有防護。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 map `git` 改回 npx 舊映射 | 僅 `test_official_python_servers_use_uvx` 紅 |
| M2 connect 端移除 `resolve_args_sentinels` | 僅 `test_connect_resolves_args_sentinels_and_keeps_env_behavior` 紅 |
| M3 map `filesystem` 去哨兵引數 | 僅 `test_official_filesystem_args_carry_appdata_sentinel` 紅 |
| baseline | 16 passed（三檔）全綠 |

### E2E 實跑（維護者裁決 C＝代跑；走生產碼路徑、hub/config DI 至 tmp）

真 `sync()` 106 條（7 官方規格全數正確落盤）→ 真 `install()`（git＝uvx 路線；filesystem＝哨兵路線，args 落盤 `<repo>\data` 絕對路徑）→ 真 stdio `connect_mcp_server`＋`list_tools`：**git CONNECTED 12 工具**（git_status/git_diff…）、**filesystem CONNECTED 14 工具**（read_file/write_file…）。

Windows 插曲（環境、非本修）：首跑 `uvx mcp-server-git` 在全域 uv cache 安裝 pywin32（`mcp` SDK 的 win32 相依）時撞長命檔案鎖（os error 32）、中斷後 wheels cache 殘 `.tmp` 目錄毒化，`uv cache clean pywin32` 同鎖無效；以乾淨 `UV_CACHE_DIR` 隔離後一次成功＝**uvx 路線本身無恙**。容器（Linux）無 pywin32 相依，不受此影響。維護者後續可於鎖釋放（如重開機）後 `uv cache clean pywin32` 清理。

## 測試

- `tests/unit/test_mcp_hub_service.py` +5（uvx 三條／正名／filesystem 哨兵／unknown fallback＋WARNING／install args 解析）
- `tests/unit/test_mcp_sentinels.py` +1（args 逐元素解析）
- `tests/unit/test_mcp_client_connect.py` 新檔 +1（捕獲 `StdioServerParameters`：args 解析＋env/TOOL_BASE_URL 回歸）

TDD：7 紅（6 行為紅＋1 ImportError）先行 → 實作轉綠 → 三變異單點紅互證。

## 後果

- 官方貨架 7 條全數可裝可連（原僅 2/7）；官方 repo 未來增減＝改表一行（OCP）。
- 未知官方目錄仍上架但有聲告警；套件未實證的風險由 log 顯式承擔。
- 同批調查的 F3（Smithery 安裝命令）／F4（enabled 半承接）／F5（種子單薄）**不在本批**——由維護者點名。

## 教訓

1. **Guard #28 的引用執法真的咬人**：實作先在 docstring 引用 ADR-0186、文件未建 → 全量唯一紅＝`check_references` 指名三個引用檔。「引用先於文件」被守衛正確攔下，本檔補上即綠。
2. **Windows uv cache 中斷安裝會毒化且被長命 handle 扣住**：`uv cache clean` 對被鎖條目無效；乾淨 `UV_CACHE_DIR` 是隔離「路線問題 vs 環境問題」的正解。

## 關聯

- 守護對象：`MCPHubService`（ADR-0069 `register_server` 創建通道同檔）；`derive_server_key`／技能接線健檢（ADR-0102）
- 前例：`mcp_category.py`（純函式 SSOT 體例）；`mcp_sentinels.py`（哨兵 SSOT＝本批延伸點）
- 調查源：MCP connectors 能力評估（2026-07-24，chat；F1–F5 台帳見 memory `touka-mcp-hub-review`）

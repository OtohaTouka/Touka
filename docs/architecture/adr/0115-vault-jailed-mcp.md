# ADR-0115：Vault-jailed MCP — 依身分把「檔案型 MCP 伺服器」鎖進各 agent 自己的 vault

- **狀態**：✅ 已接受（2026-06-25；Touka 派發注入＋docgen v0.6 經 TDD 完成並全綠驗證，經對抗式安全複審）
- **日期**：2026-06-25
- **影響範圍**：ToukaProxy（`services/mcp_vault_jail.py` SSOT／`services/tool_executor.py` 派發注入／`protocols/registry.py` `vault_jail_arg` 欄／`utils/mcp_sentinels.py` env backstop＋`protocols/mcp/client.py` 連線期解析／`services/mcp_hub_service.py` install 傳遞／`services/settings_snapshot_service.py` 快照重蓋／`routers/mcp.py` API round-trip／config）＋ ToukaChat（`buildFileRefHint` 指向 vault-scoped read 工具）＋ **外部 mcp-docgen v0.6**（全 17 工具 `base_dir`，同管 input+output）
- **關鍵字**：vault-jailed MCP, per-agent isolation, dispatcher injection, base_dir, MCP path jail, identity-derived root, fail-closed, env backstop, sentinel, config drift fail-safe, cooperative vs enforced isolation, 聊天上傳多格式讀取

## 背景（Context）

聊天上傳改進需要「讓 AI 讀取上傳的 docx/xlsx/pptx/pdf」。決策是**複用 [[ADR-0079]] 的 mcp-docgen**（不重做一套解析），但 docgen 的檔案存取必須**鎖進當下 agent 自己的 vault**——一個 agent 永遠不能讀到另一個 agent 的檔案。

核心難題：**MCP 伺服器 = 一個長駐、共用的子行程，啟動時固定一個 jail 目錄（env）；但「現在是哪個 agent 在動作」是每次請求都在變的。** 一個共用行程只有一個 jail，對不上 N 個 agent——這正是 ADR-0079 當初把 docgen 輸出夾標為「v1 共用、非 per-agent」的原因。

更廣的設計問題（先前評估）：哪些 MCP 能收進各 agent vault？答案取決於該 MCP 的檔案模型——**路徑型**（每次呼叫帶路徑/根）可；**啟動固定根型**需改成路徑型或居中複製；**雲端/憑證型**（Drive/Notion/S3）隔離靠憑證/scope 而非本機目錄；**執行型**（shell/瀏覽器）需 OS 沙箱。本 ADR 針對**路徑型**建立可泛化機制。

## 決策（Decision）

### #1 不可違反鐵則
檔案型 MCP 的存取**永不跨 agent**；其檔案根目錄由**已驗證身分**決定，**模型不可指定**（即使惡意提示要求別的根，也被覆寫）。

### D1 — 機制：不「搬 jail」，而是「每次呼叫由 Touka 注入根目錄」
- **`ProtocolServerConfig.vault_jail_arg`（config SSOT）**：若某 MCP 伺服器設此欄，代表它的某個工具參數是「檔案根目錄」（如 mcp-docgen 的 `base_dir`）。哪些伺服器受管、根參數叫什麼，**由 config 衍生，不在派發層硬寫伺服器名**。
- **`mcp_vault_jail.apply()` 於唯一咽喉注入**：`tool_executor` 是 `call_tool_on_server` 的**唯一呼叫者**（複審證實）。派發前 `apply` 對標 `vault_jail_arg` 的伺服器，依身分**覆寫** `arguments[該參數]` ＝該 agent 的 personal vault 根（`resource_manager.get_vault_path("personal", identity)`，前置 `_assert_safe_agent_segment` 守衛 agent_id 為安全單段），並移除派發層注入的 `agent_id`（外部 MCP 未宣告）。**fail-closed**：解不出 vault 即拋例外、不放行。模型給的根一律被蓋掉。

### D2 — docgen 端（v0.6）：整台伺服器可被 per-agent jail
- 全 17 工具皆接受**可選 `base_dir`**，且 `base_dir` **同時是 input 與 output 的根**（不傳＝向後相容，沿用 env/預設）。Touka 一注入 `base_dir`，整台 docgen 的**讀與寫**都被鎖進該 agent vault（著作輸出亦落各 agent vault，create→convert→read roundtrip 全在 vault 內）。
- docgen **自我 jail**：`resolve_input_path`/`resolve_output_path` 以 `(base / path).resolve()` 正規化後 `relative_to(base)`，擋 `..`／絕對路徑／目錄 junction（複審實測：junction 也逃不出）。

### D3 — 三層安全模型（縱深防禦）
1. **Touka 注入正確根**（依身分，模型不可改）。
2. **docgen 在該根自我 jail**（擋 `..`/絕對/junction）。
3. **env backstop**：`utils/mcp_sentinels.py` 解析 `@TOUKA_APPDATA@`→app_data 根，於 `connect_mcp_server` **連線期**注入 `MCP_DOCGEN_INPUT_DIR`（故 `server_config.json` 可存可攜哨兵）。即使 `vault_jail_arg` 政策哪天漂移掉，docgen 的 `_configured_input_root` 仍把任何根**限在 app_data 內、而非全機**。

### D4 — 政策完整性（config drift fail-safe）
單點漂移不得讓隔離「fail open」：
- **install 傳遞**：`vault_jail_arg` 隨 curated `mcp_hub.json` 條目經 `mcp_hub_service.install()` 寫入 `server_config.json`（重裝不丟）。
- **MCP 設定 API round-trip**：`MCPServerItem` 納入 `vault_jail_arg`（否則 `extra='forbid'` 會整份拒含此欄的 config → GET 空清單＋POST 剝政策）；POST 存檔時 `_preserve_masked_env` 在前端漏傳時**自磁碟還原**（不可被前端清掉）。
- **快照匯入重蓋**：`settings_snapshot_service._clean_mcp_servers` 在 merge/replace 後，cleaned 缺 `vault_jail_arg` 則自既有還原（[[ADR-0112]] 舊/外來快照不致 fail open）。
- **漂移守衛測試**：斷言已註冊的 mcp-docgen 必帶 `vault_jail_arg`。

### D5 — 前端
`buildFileRefHint` 指向 vault-scoped 的 `read_pdf`/`read_docx`/`read_xlsx`/`read_pptx`（相對 vault 路徑）；純文字檔降級走既有 `personal_drive read`。

## 替代方案（Rejected）
- **每個 agent 起一個 docgen 子行程**（各自 env jail）：N 個 agent → 資源與生命週期不可行。
- **加一個 Touka 原生 broker 工具包住 docgen**：多一層工具面；維護者的取向＝零重複工具→ 派發注入更貼合，且不需把 ProtocolManager 接進原生工具層。
- **改 wire format、前端送真實多模態/路徑陣列**：牽動持久化與對話樹，風險高 → 後端覆寫更內聚。
- **仰賴 docgen env 做 per-agent 隔離**：env 是 process-global，共用行程無法 per-agent → 必須走每次呼叫的參數。
- **把 vault-jail 政策放純使用者可改 config、無 API round-trip/還原**：一次設定存檔就 fail open（複審 High）→ 改為 API 契約＋磁碟還原＋快照重蓋。

## 後果（Consequences）

- ✅ 檔案型 MCP（docgen 為首例）的**讀與寫皆鎖進各 agent vault**；機制**泛用**——未來其他路徑型檔案 MCP 只要設 `vault_jail_arg` 即同享 per-agent 隔離，無需改派發層。
- ✅ 三層防禦＋政策完整性（API／install／快照／守衛）使**單點漂移不致 fail open**；複審證實核心讀取隔離四不變式成立（override 防竄改、agent_id 守衛擋盡 Windows 反斜線/碟符/UNC/null/unicode、input_path 連 junction 防逃、per-call 快取依注入根分鍵不跨 agent、咽喉完整）。
- ✅ 複用 docgen 解析、零重複工具面；docgen 對外**仍向後相容**（`base_dir` 為可選，其他軟體/coding agent 照常可用）。
- ⚠️ **限於路徑型 MCP**：雲端/憑證型 MCP 的隔離應靠**每 agent 憑證/scope**（非本機目錄）；**執行型** MCP 需 **OS 沙箱**（[[ADR-0076]]），目錄圍籬不足。收錄此類 MCP 時須於 Hub 標示信任警告。
- ⚠️ 第三方 MCP 為**合作式 jail**（信任其自我 jail 正確），非**強制**；真強制需 OS 層（容器/chroot/landlock）。自家 docgen 可信其程式碼，故合作式足夠。
- ⚠️ 端到端需 **mcp-docgen v0.6 經 `uvx` 可取**（PyPI 發佈或 dev 期指本機）；在那之前 Touka 全閘以 mock 轉綠。
- ⚠️ docgen **著作輸出改落各 agent vault**（非舊共用 temp 夾）——對 per-agent 隔離更佳、且 agent 能立即讀回自己的輸出，但取檔位置已變更。

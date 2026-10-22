# ADR-0195：MCP per-tool 治理（兩軸並存：per-installation ＋ per-role）

> C2-3 現況補註：origin 與 sandbox_paths 另存隔離設定檔；origin 刻意不進政策 carry-over，各寫入點依程式路徑決定，避免代理人程式繼承第三方來源。 詳見 ADR-0275。

- **狀態**：Accepted（[ADR-0196](0196-permission-representation-convergence.md) 修訂：
  per-role deny 改為 self-only＋全域地板；Owner 身分面旁路移除；權限設定載入改為
  啟動 fail-fast／熱重載 last-known-good；[ADR-0269](0269-advertised-tool-list-enforcement.md)
  補註：模型迴圈裡「這一輪列給模型的清單」也是執行條件，軸 B 仍獨立核對）
- **日期**：2026-07-26
- **相關**：[ADR-0188](0188-mcp-enabled-gate-registry-choke.md)（per-server enabled 咽喉）、[ADR-0143](0143-hitl-unification.md)（MCP 工具預設 dangerous 進 HITL）、[ADR-0141](0141-role-plane-separation.md)（角色平面）、[ADR-0191](0191-mcp-hub-tier-a-shelf-and-version-pinning.md)／[ADR-0194](0194-mcp-remote-shelf-and-manual-remote-entry.md)（出廠貨架 14 條，含 Stripe）
- **提案**：[MCP Connector 大廠對齊](../proposals/mcp-connector-parity-and-auth-alignment.md) §6／§8-6（維護者 2026-07-26 決定兩個軸向都做）

## 脈絡

Touka 的 MCP 治理粒度到此為止只有**整台**：`permission_service.is_mcp_server_allowed` 決定掛不掛某台伺服器，掛上去就是該台工具全開。批次 4（OAuth）評估過程中把這個缺口坐實為**有具體受害者**，三條實查：

1. `permission_service.get_allowed_tools` 對 Owner **直接短路回 `["*"]`**；
2. `is_mcp_server_allowed` 對 Owner **恆回 True**，且只有 per-server 粒度；
3. **唯一會查 `denied_tools` 的 `is_authorized` 生產零呼叫端**（全 repo grep 確認，命中的 `is_authorized` 皆為其他模組的同名區域變數）。

⇒ **Owner ＋ 任一已安裝 MCP 伺服器 ＝ 該伺服器全部工具，沒有任何收斂旋鈕**。而出廠貨架已含 `curated-stripe`（含 `create_refund`、通用寫入原語 `stripe_api_write`）。

同時發現第二個獨立缺口：**執行面對 MCP 完全沒有身分閘**。收斂只存在於廣告面（`ToolLayer`），模型只要從歷史脈絡、技能文本或猜測叫出一個未被廣告的工具名，就能直接執行——「廣告面＝授權面」在 MCP 這條路上只是慣例，不是保證。〔2026-10-15 註，ADR-0269：模型迴圈裡執行前會核對這一輪實際列給模型的（伺服器, 裸工具名），沒列出回 `not_advertised`，廣告面因此也成了執行條件；但列出不等於有權限，軸 B 仍在 Gate 2a 獨立核對。〕

大廠形狀：Codex 的 `enabled_tools`／`disabled_tools`、Antigravity 的 `disabledTools` 都寫在**伺服器條目**上（per-installation），而 Antigravity 另有 `mcp(server/tool)` 權限規則（per-role）——兩軸並存是業界形狀。

## 決策

**兩軸並存，取交集（任一拒絕即拒絕），且刻意分屬不同層。**

### 軸 A｜per-installation（供給面）

`ProtocolServerConfig` 新增 `enabled_tools: list[str] | None`／`disabled_tools: list[str]`。語意是「這台伺服器我這次安裝打算開放哪些工具」——**不是權限邊界，是 owner 對自己 agent 的自我約束**（等同 Square MCP 的 `DISALLOW_WRITES`、M365 MCP 的 `--read-only`）。因此：

> **所有身分一律適用，Owner 也不例外。**

這正是它**不能**走 `permission_service` 的理由：一旦接進去，Owner 就在 `get_allowed_tools`／`is_mcp_server_allowed` 短路而完全繞過，而 Owner 恰恰是唯一的實際受害者。故執法點在**供給面**：

- 廣告面：`ProtocolManager.get_all_tools()` 直接濾除 → 所有下游消費者（ToolLayer、debug 面板、觀測性、guard_service）天然一致，不需各自記得再濾一次。
- 執行面：`ProtocolManager.call_tool_on_server()` fail-closed 拋 `ToolDisabledError`，**置於斷路器之前**（政策關閉不是故障，不得累積失敗計數）。

`enabled_tools` 的 `None`（未設＝全開）與 `[]`（明示全關）**語意不同、不可合併**——合併會讓「使用者把工具全部取消勾選」變成「全部開放」。

### 軸 B｜per-role（身分面）

> **現行語意由 ADR-0196 修訂。** 下列內容記錄 2026-07-26 首次落地時的歷史設計；
> 現在所有身分面入口皆委派 `PermissionService.resolve_grants()`，Owner 不再硬編短路，
> 角色 deny 為 self-only，跨角色拒絕改由 `global_denied_tools`／
> `global_denied_mcp_tools` 表達。

`RoleConfig` 新增 `denied_mcp_tools: list[str]`，`allowed_mcp_servers` 擴充為接受 `server/tool` 條目。新方法 `permission_service.is_mcp_tool_allowed(server, tool, identity)`：

1. **deny 優先，且對 Owner 同樣生效**——與既有 `denied_tools` 在 `is_authorized` 的語意一致（Owner 拿 `*` 但 denied 仍逐項扣除）。
2. 整台授權（含 Owner 短路、`*`、裸伺服器名）→ 該台工具全開。
3. 僅被授予個別工具者，逐條比對。

`is_mcp_server_allowed` 同步放寬：角色只被授予 `server/tool` 時仍允許**掛載**該台（否則連線建不起來，工具面授權形同虛設）；工具面收斂交由 `is_mcp_tool_allowed`。

執法點：廣告面 `middleware/layers/tool.py`、**執行面 `tool_executor` 的 Gate 2a（此前完全不存在）**。〔2026-10-15 註，ADR-0269：模型迴圈裡 Gate 2a 之前多一道「這一輪清單」核對（`ToolExecutor._offer_denial`）；兩道是「且」，有列出仍要過 Gate 2a，`/debug/execute` 不做清單核對。〕

### 比對 SSOT

新增 `src/utils/mcp_tool_policy.py`（純函式、零 IO）：`is_tool_enabled`／`matches_tool_ref`／`server_of_ref`／`carry_over_policy`。工具名一律為**裸名**（MCP 伺服器回報的 `tool.name`），非廣告面的 `server__tool` 前綴形式；`get_all_tools` 另投影一個 `tool` 鍵為 SSOT，避免每個消費端各自 `split("__")`（伺服器名含雙底線時就會切錯）。

刻意**不支援** `create_*` 這類前綴 glob：勾選面是一份明確清單，模糊比對會讓「我到底開了什麼」不可讀，也讓上游新增工具靜默落進既有樣式。

### 治理 UI 的資料源

新端點 `GET /v1/mcp/{server_name}/tools` 回**未經軸 A 過濾**的完整工具面＋各自開放狀態。刻意不複用 `get_all_tools`：那條路已濾除關閉者，拿來渲染勾選框會讓取消勾選的工具直接從清單消失、再也勾不回來。

## 順帶修掉的既有缺陷：政策欄位在整條目重寫時 fail open

盤點 `server_config.json` 的寫入路徑時發現，`vault_jail_arg` 當初那條「政策因例行操作 fail open」的複審缺陷還有**四處未修**：

| 路徑 | 症狀 |
|---|---|
| `mcp_hub_service.register_server` | 以字面量重建整個 entry，同名重註冊抹掉該台政策 |
| `tools/builtin/system.py`（技能市集安裝） | 同上 |
| `tools/builtin/evolution.py`（自我演化部署） | 同上 |
| `settings_snapshot_service._clean_mcp_servers` | 匯入舊快照（replace 模式）政策 fail open |

前三者統一走新的 `mcp_tool_policy.carry_over_policy()`（同時保住 `vault_jail_arg`，一併修掉舊漏）；第四者比照既有 `vault_jail_arg` 分支回填。

補測這三個接線點時（`test_mcp_policy_carryover_wiring.py`）又揭出三件事：

1. **`carry_over_policy` 原本就地改寫輸入並回傳同一個物件**，於是呼叫端手上的「原始 `mcp_config`」與「帶回政策後的 `entry`」是同一份——`pm.add_server(..., ProtocolServerConfig(**mcp_config))` 這種用錯變數的寫法今天靠別名僥倖正確，日後任一處改成建副本就無聲斷裂，**且變異測試殺不掉**（實測：把熱掛載改回 `mcp_config`，測試照樣全綠）。改為**回傳副本的純函式**後該變異立刻轉紅。
2. **技能市集安裝與演化部署的熱掛載都以未帶回政策的設定呼叫 `pm.add_server`**。修正後落盤與執行期 registry 是同一份；修正前的窗口是「檔案裡工具是關的、跑著的那份是開的，等下次重啟才收斂」。
3. **`system.py` 的 `carry_over_policy` 目前不可達**——上游有「已安裝就早退」守衛，`old_entry` 恆為 `None`。保留而非刪除（該守衛一旦放寬，抹除就會無聲回歸），但在程式碼與測試中誠實標註為純防禦，不假裝三條路徑等價。`_preserve_masked_env` 亦補上兩條還原分支，且**判準刻意不對稱**：

- `enabled_tools` 用 `is None` 判——`[]` 是使用者明示全關，不得被誤判成「未回傳」而還原成舊值。要真正「全部開啟」請送 `["*"]`。
- `disabled_tools` 用 falsy 判——此欄不由 UI 驅動，是手寫設定的硬阻擋逃生口，例行存檔不得抹除。

## 後果

**正面**

- 出廠即含金流寫入原語的貨架條目（Stripe／未來的 PayPal／Square）終於有收斂旋鈕，且該旋鈕連 Owner 都綁得住。
- 執行面補上 MCP 的身分閘——這是「wiring/接線無測」缺陷家族在 MCP 面的最後一個開口。
- 全程可進 CI 驗證（與批次 4 的 OAuth 成功路徑永遠無法自動化形成對比，這也是它被排在批次 4 之前的理由）。
- 兩契約 parity 首次有機器執法（`test_mcp_config_contract_parity.py`）——此前漏宣告一個欄位會讓整個 MCP 設定頁靜默空白，無任何測試會亮。

**代價／已知邊界**

- ~~`denied_mcp_tools` 沿繼承鏈聯集，Base deny 會傳播到 Owner~~ → **ADR-0196 已修訂**：
  角色 deny 改為 self-only，角色本地裸 `*` 可表達 deny-all；真正跨角色的拒絕改用
  `global_denied_mcp_tools`，並明確約束 Owner。`test_rbac_mcp_tool_inheritance.py`
  已翻轉為釘住新語意。
- ~~`_load_config` 對驗證失敗退回較寬 `DEFAULT_PERMISSIONS_CONFIG`~~ →
  **ADR-0196 已修訂**：啟動期既有非空壞設定 fail-fast；熱重載失敗保留
  last-known-good、記 AUDIT 並標 degraded；只有不存在／空白／`{}` 的 bootstrap
  可使用最小 fallback。
- 軸 A 對 `ToukaUI` 的 `openDiff`／`closeDiff` 不生效——那條在 `call_tool_on_server` 開頭即早退，且不是 registry 條目。刻意保留，不是疏漏。
- ~~`guard_permissions_registry`（Guard #32）不涵蓋 MCP 面~~ → **已補**：新增 `find_invalid_mcp_tool_refs`，對 `allowed_mcp_servers`／`denied_mcp_tools` 逐條驗**字串形狀**。比對規則的 SSOT 是 `mcp_tool_policy.validate_tool_ref`，守衛只是它的執法點（在守衛裡重刻規則＝保證與 `matches_tool_ref` 漂移）。
  - **形狀驗證維持在 Guard #32**：MCP registry 是動態的，Pydantic 負責 wire
    結構，CI 守衛負責 reference 形狀與 tracked 設定／fallback parity；執行期載入生命週期
    則依 ADR-0196 採啟動 fail-fast、熱重載 last-known-good。
  - **殘留邊界（無法消除）**：只驗形狀、**驗不了存在性**。`stripe/creat_refund`（拼錯工具名）形狀合法故放行，權限仍會「看似有實則無」。MCP 工具由 hub 動態提供、隨伺服器版本變動，無法靜態窮舉——此限制由 `test_mcp_refs_allow_misspelled_tool_name` 寫成合約，避免日後有人誤以為它擋得住拼字錯誤。
- 前端 `McpServerForm` 與 `buildInstalledEntry` 原本都以字面量重建條目，本案一併改為帶回政策欄位；但這是「重建式表單」的結構性風險，日後新增任何條目欄位都要再改一次。

## 替代方案

- **只做 per-role**：解決不了本案的實際受害者（Owner 短路），否決。
- **只做 per-installation**：失去身分維度，Base／Guest／子代理無法差異化，否決。
- **把軸 A 併進 `permission_service`**：形式上更「統一」，但 Owner 短路會使其失效，且混淆「自我約束」與「權限邊界」兩種本質不同的語意，否決。

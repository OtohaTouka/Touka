# ADR-0190: MCP Hub 安裝徽章比對鍵（server_key 後端投影＋哨兵位通配）

## 狀態

✅ 已接受（MCP connectors 調查 F6，owner 點名修復；F1–F6 全線收官）

## 日期

2026-07-25

## 背景

F6（F3 批發現）＝Hub 卡片「已安裝／可更新」徽章**恆滅**：前端 `computeMcpStatus`（`mcp_version.ts`）以 `hub.id`（帶 `curated-`／`official-` 前綴，如 `official-fetch`）查 `server_config.json` 的 `mcpServers` 鍵，但安裝落盤鍵＝`derive_server_key(name)`（ADR-0102，如 `fetch`）——恆 miss ＝已裝條目永遠顯示「一鍵安裝」，且可重複安裝覆蓋既有設定；`InstalledMcpConfig` 型別註解「key = hub.id」同錯。原單元測試以 hub.id 當 config 鍵餵資料＝把 bug 編死成綠測（F1「測試餵非真實形狀」教訓的前端翻版）。

次生陷阱：僅修鍵仍不夠——install 落盤 args 是哨兵**解析後**的各機絕對路徑（ADR-0186），而 hub 建議 args 是 `@TOUKA_APPDATA@` 原文 → 種子 filesystem 修鍵後將恆判假陽性「可更新」。

## 決策與實作

1. **後端投影 `server_key`（衍生規則 SSOT 不外洩）**：`MCPHubServerItem` 宣告 `server_key: str | None`（ADR-0187 教訓：response_model 剝除未宣告欄位）；`GET /hub` 端點逐條 `item["server_key"] = derive_server_key(item["name"])`。前端**只消費、不得重刻**衍生規則（與 `skill_mcp_wiring` 消費同一函式一致）；openapi.json＋api.ts codegen 重生（`McpHubServer` wire 型別自動獲得欄位）。
2. **前端查表鍵改 `server_key ?? id`**：`id` 後備＝無 `server_key` 舊 payload 相容（pin 測試釘死不誤報未裝）。
3. **args 哨兵位通配比對**：`argsMatchRecipe` 逐元素、長度必等；hub 側元素匹配 `^@TOUKA_[A-Z0-9_]+@$`（SSOT＝後端 `mcp_sentinels.py`，前端無從得知各機解析值）時接受任意已解析值；**通配僅限哨兵位**——非哨兵位漂移、長度不符照樣 `update_available`。
4. **`InstalledMcpConfig` 型別註解修正**：key＝`derive_server_key(name)`（手動新增條目＝使用者自取名）。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 移除後端投影迴圈 | 僅 `test_mcp_hub_server_key` 2 紅（鄰域 test_code_review_fixes 26 綠） |
| M2 前端查表退回 `hub.id` | 8 紅（單元 6＋McpHubView 徽章整合 2） |
| M3 移除哨兵通配分支 | 僅「哨兵位對到已解析絕對路徑」1 紅（10 綠） |

## 測試

- `tests/unit/test_mcp_hub_server_key.py`（新，2）：投影值＝`derive_server_key(name)` SSOT 等式＋response_model 宣告 pin
- `mcp_version.test.ts`：重寫為真實形狀（config 鍵＝衍生鍵）＋哨兵通配×3＋舊 payload 後備 pin（7→11）
- `McpHubView.test.tsx` +2：衍生鍵 config → 卡片渲染「已安裝」／配方漂移 →「可更新」（徽章整合級）

TDD：後端 2 紅＋前端 8 紅先行（8 綠＝pin 與既有）→ 投影＋查表＋通配轉綠 → 三變異單點紅。官方型檢閘 `guard:typecheck` 零錯。

## 後果

- Hub 徽章首次真實反映安裝狀態；已裝且配方一致時安裝鈕轉禁用「已經安裝」＝UI 不再重複安裝覆蓋使用者 env 設定。
- 種子 filesystem（哨兵 args）正確顯示「已安裝」而非假陽性「可更新」。
- MCP connectors 調查 F1–F6 全線收官（ADR-0186～0190）。

## 教訓

跨層鍵約定必須由**持有衍生規則的一端投影**給消費端；消費端測試必須餵「另一端實際產生的形狀」——拿自己想像的鍵形餵測試，就是把鍵不匹配 bug 編死成綠測。

## 關聯

- ADR-0102（`derive_server_key` 安裝命名 SSOT）／0186（哨兵 install 落盤解析）／0187（response_model 剝除教訓＋F6 發現處）／0189（種子貨架）
- 台帳：memory `touka-mcp-hub-review`（F1–F6）

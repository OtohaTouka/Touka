# ADR-0189: MCP Hub 出廠種子擴充（官方三條實證條目＋配方 SSOT parity）

## 狀態

✅ 已接受（MCP connectors 調查 F5，維護者點名修復；接續 ADR-0186／0187／0188）

## 日期

2026-07-25

## 背景

F5＝出廠貨架近乎空。種子 `mcp_hub.json.example`（tracked；bootstrap 於 live 檔缺席時 `copy2` 自癒植入＝fresh install 基線）僅含 curated `mcp-docgen` 1 條，且 sync 純手動——新使用者打開 Hub 只看到一條，與桌面版 connectors「開箱一排」的體驗差距即源於此。

## 決策與實作

1. **種子 +3 官方條目**：`fetch`／`filesystem`／`git`——全數 ADR-0186 啟動規格＋**真 stdio 連線實證**（git 12 工具／filesystem 14 工具＝ADR-0186 E2E；fetch 1 工具＝本批 E2E）；免金鑰、容器可跑（映像自帶 npx/uvx）、涵蓋 Browser／File／Dev 三分類；filesystem 帶 `@TOUKA_APPDATA@` 哨兵（可攜，install／connect 雙端解析）。官方另四條**刻意不入種子**：everything（client 測試用）、memory（與 ToukaMemory 職能重疊，避免雙記憶混淆）、sequentialthinking／time（邊際效用低）——手動 sync 一鍵即得。
2. **配方 SSOT 抽取**：`official_hub_entry(dir_name) -> (dict, known)`（`mcp_official_map` 純函式）＝官方條目「完整 dict 配方」的唯一事實來源；`fetch_official_servers` 改為消費配方（SRP：service 只剩 IO＋fallback 告警）。**種子＝配方快照**：seed-integrity parity 測試對每條種子官方條目與配方輸出**逐欄精確相等**——ADR-0186 啟動規格表未來演進時，種子漂移立刻紅。
3. **種子官方條目 `source: "official"`（非 curated）**：sync 只 survive curated → 種子官方條目於首次 sync 被抓取結果**原位取代、不重複**（行為 pin 測試釘死）。
4. **自動 sync 刻意不做**（scope 取捨，非遺漏）：開機靜默外呼 GitHub／MCP Registry 牴觸 local-first 隱私姿態（README「所有資料存於本機、不上雲」承諾）；出廠貨架由種子解決、新鮮度由手動 sync 鈕承擔。若要 opt-in 自動同步＝另案由維護者點名。

### 變異驗證（逐條隔離、皆已還原）

| 變異 | 結果 |
|------|------|
| M1 種子 git 條目啟動規格竄改（uvx→npx 舊式） | 僅 `test_seeded_official_entries_match_ssot_recipe` 紅 |
| M2 整刪種子 official-fetch 條目 | 僅 `test_seed_ships_official_starters` 紅 |
| baseline | seed_integrity 4 passed＋MCP 鄰域（hub/registry/enabled/Guard#34 範本）51 passed |

### E2E 實跑

種子 `official-fetch` 條目原樣經 `ProtocolServerConfig` 走生產 connect 路徑：`uvx mcp-server-fetch` **CONNECTED，1 工具（fetch）**＝種子三官方條目全數 spawn 實證（Anthropic 官方發行者，符合既定 spawn 邊界）。

## 測試

- `tests/unit/test_mcp_hub_seed_integrity.py` +2（出廠貨架存在性／配方 SSOT 逐欄 parity）
- `tests/unit/test_mcp_hub_service.py` +1（sync 取代種子官方不重複 pin）

TDD：parity ImportError 紅先行（`official_hub_entry` 未存在）＋starters 紅 → 配方抽取＋種子落檔轉綠 → 兩變異單點紅。

## 後果

- fresh install 開箱即有 4 條可用貨架（自家文件引擎＋官方網頁抓取／檔案／git），全數實證可連。
- 官方條目 dict 形狀自此單點維護（service／種子共用配方）；種子與代碼零漂移保證。
- F6（前端安裝徽章鍵不匹配）不在本批——由維護者點名。

## 教訓

「tracked `.example`＝fresh install 種子」的自癒機制（bootstrap ESSENTIAL_CONFIGS）讓種子擴充零 runtime 代碼變更——但快照型種子必須綁 parity 測試，否則就是下一個 F1（供應鏈規格漂移、無聲壞掉的貨架）。

## 關聯

- 前作：ADR-0186（啟動規格＋git/filesystem E2E）／0187（sync 三源合併）／0188（enabled 咽喉）
- 機制：bootstrap ESSENTIAL_CONFIGS `.example` 自癒；`errors.json.example`＋Guard #34 前例
- 台帳：memory `touka-mcp-hub-review`（F1–F6）

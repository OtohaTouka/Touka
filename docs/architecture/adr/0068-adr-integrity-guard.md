# ADR-0068: ADR 完整性守衛（防止編號跳號與斷鏈）

## 狀態

✅ 已接受

## 日期

2026-05-31

## 背景

Touka 以連續編號的 ADR（架構決策紀錄）記錄每個重大決定。在 Skill 系統標準化期間，AI 將編號 0059–0063 寫入測試 docstring 與其他文件（如 ADR-0066 的「相關 ADR」連結），**卻未同時建立對應的 ADR 檔案**，造成：

1. **斷鏈**：`ADR-0063` 被引用但 `0063-*.md` 不存在（mkdocs 僅以 warning 提示，不阻斷建置）。
2. **跳號**：索引從 0058 直接跳到 0064，缺 0059–0063。

此問題沒有任何自動機制偵測，是人工翻查 ADR 索引時才發現。由於本專案的程式碼與文件全部由 AI 生成、維護者不審閱程式碼，依賴「AI 記得補檔」並不可靠——這次正是因此失敗。

## 問題

1. 如何自動偵測「ADR 編號被引用但檔案不存在」？
2. 如何自動偵測「ADR 編號跳號（缺號）」？
3. 如何確保索引（`adr/README.md`）與實體 ADR 檔案始終一致？

## 決策

新增**第 28 道架構守衛** `scripts/guard_adr_integrity.py`，整合進 `guard_all.py`（每次架構檢查必跑），執行三項檢查，任一失敗即 `sys.exit(1)` 並指出問題：

1. **引用完整性**：掃描 `.md/.py/.ts/.tsx`（排除 `node_modules`/`.venv`/`site` 等），凡出現 `ADR-NNNN`（涵蓋 `ADR-NNNN`、`ADR NNNN`、`（ADR-NNNN）`、`ADR-NNNN`、`ADR-NNNN：` 等格式）即要求 `docs/architecture/adr/NNNN-*.md` 存在。
2. **編號連續性**：ADR 檔編號必須連續 `0001..最大號`；缺號即報錯，除非登記於 `WITHDRAWN_ADRS` 白名單（明確作廢）。
3. **索引同步**：`adr/README.md` 索引表與實體檔案雙向對齊（每檔有列、每列有檔）。

守衛採 `guard_noqa_registry.py`（ADR-0029）的「doc-vs-reality 同步」純函式範式，便於 TDD（`tests/unit/test_guard_adr_integrity.py`）。

並於 `AGENTS.md` 新增「ADR 編號主權」規則作為輔助保險：配給 ADR 編號**必須同時**建檔並補索引列，**嚴禁**引用尚未建檔的編號——讓 AI 主動做對，守衛則為自動安全網。

## 設計決策

### 為何用守衛而非僅靠流程規則

僅在 `AGENTS.md` 寫規則屬「靠 AI 自律」，而這次斷鏈正是自律失敗的結果。守衛是**確定性、會阻斷**的執法，與專案既有 27 道守衛同款；流程規則與守衛並用（雙保險）才穩固。

### 為何引用偵測要求 `ADR` 前綴

要求 `ADR[-\s]` 前綴可避免把時間（`3000ms`）、日期（`2026-05-31`）等四位數字誤判為 ADR 引用，將誤報降到最低。

### 測試的自我防誤判

守衛會掃描自己的測試檔，故測試中所有「假」ADR 編號一律以 `f"ADR-{n:04d}"` 動態組字串，避免在原始碼留下靜態的不存在編號而自我觸發斷鏈。

## 後果

### 正面

- 「引用了不存在的 ADR」「跳號」「索引不同步」三種狀況再也無法悄悄通過——`guard_all.py` 會立即紅燈。
- 對不寫程式的維護者而言，這是一道不需人工盯防的自動防線。
- 連帶在導入時對全 repo 做了一次完整稽核（確認 0001–0068 連續、無斷鏈）。

### 負面

- 新增一道守衛即新增一處需與「守衛總數」文件同步的計數（27→28）。
- 連續性檢查對「刻意作廢編號」需手動登記 `WITHDRAWN_ADRS`（屬罕見情況）。

## 相關 ADR

- [ADR-0029](0029-noqa-registry-sync-guard.md) — NOQA Registry 同步守衛（本守衛沿用其 doc-vs-reality 範式）
- [ADR-0032](0032-module-boundary-guard.md) — 模組邊界守衛（守衛系 ADR 先例）
- [ADR-0034](0034-endpoint-auth-dependency-guard.md) — 端點認證守衛（守衛系 ADR 先例）
- [ADR-0063](0063-skill-hub-external-sync.md) — 觸發本守衛的斷鏈案例之一
- [ADR-0066](0066-skill-system-anthropic-standard-migration.md) — 其「相關 ADR」連結曾因 0063 缺檔而斷鏈

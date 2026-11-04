# 規則分流

根 `AGENTS.md` 每個對話都會載入，只放全系統規則、鎖住區段與禁令清單。綁特定路徑的規則全文放在這裡，依模組分檔；`AGENTS.md` 留一行索引，編號不變（ADR-0296）。

## 怎麼用

```bash
python scripts/applicable_rules.py --paths ToukaChat/src/app/page.tsx   # 計畫階段，可以給資料夾
python scripts/applicable_rules.py                                      # 開工後，自動看這次改了什麼
python scripts/applicable_rules.py --print-rules                        # 連全文一起印
```

開工前、交付前、審查時各跑一次。認不出的路徑要讀全部規則檔；算不出比對基準就失敗，這時用 `--base` 或 `--paths`。

## 檔案

| 檔案 | 內容 |
| --- | --- |
| `<模組>.md` | 這個模組為第一個模組的規則全文 |
| `modules.json` | 模組與每條規則對到哪些路徑，以及不適用的路徑 |
| `../handover/<模組>.md` | 這個範圍沒做完、有未解限制或要特別操作的事 |

## 改規則

- 新增禁令一律往後追加，不得重編。
- 全系統規則標 `〔全系統〕`，全文留在 `AGENTS.md`。
- 綁路徑的規則標 `〔模組：x、y〕`，在 `modules.json` 寫路徑（安全相關的寧寬勿窄），全文放第一個模組的規則檔，那一行改成一句摘要加 `全文：docs/rules/x.md`。全文暫時留在 `AGENTS.md` 的列進 `inline_rules`。
- 有路徑例外、可以繞過的全系統規則，摘要要寫明，也要寫明繞過仍算違規。
- 新增的檔案要屬於某個模組，或列進 `not_applicable`。

`scripts/guard_doc_routing.py`（Guard #46）核對標籤、模組、全文的位置與份數、路徑，以及最後一條禁令落在 `AGENTS.md` 前 32,768 位元組內（Codex 只讀這麼多）。規則文字的增刪、有沒有重編、摘要準不準，由 PR 審查把關。`AGENTS.md`、`HANDOVER.md` 超過大小上限只會提醒。

## 開發禁令清單的說明

搬自根 `AGENTS.md`，沒有改寫。

> 🚧 **開發禁令清單（Guardrails — 唯一正本）**：以下為全系統強制鐵則，**每一條都源自一次真實事故**（例如一次 SSOT 重構曾炸掉系統大部分功能）。**嚴禁繞過或弱化任何一條。** 編號為穩定識別碼——程式碼與多份 ADR 以「禁令 N」引用，**新增規則一律往後追加、不得重編**。完整脈絡見各條所附 ADR。

## 資訊安全主題摘要

搬自根 `AGENTS.md`，沒有改寫。

> 🛡️ **資訊安全主題摘要（禁令 15–26 之背景）**：歷史審計批次與修補細節見 `SECURITY.md`；以下七個跨切面安全原則持續有效（ADR-0033 + ADR-0034）：① **密鑰主權**（無公開已知硬編碼預設密鑰，短壽命密鑰啟動生成、持久密鑰首次隨機生成並持久化）；② **身份主權**（沒登入一律拒絕、禁任何 Header 作身份來源、狀態修改端點必加角色守衛 N1/N2/N3/N10）；③ **結構化邊界**（`is_relative_to()`、Shell metachar CONFIRM、`json.dumps()` 轉義、Pydantic `model_validate()`、固定 SQL 字面量 N12）；④ **統一安全守衛層**（純函式抽出、並發鎖定原語 N4/N5/N11、`follow_redirects=False` N8）；⑤ **沙箱白名單優先**（黑名單+顯式移除雙保險、連線錯誤消毒 N7/N9）；⑥ **端點認證強制**（Guard #24、Grandfather List 清零，ADR-0034）；⑦ **服務 Token SOLID-SRP**（`verify_heartbeat_token()` Depends 唯一）。完整弱點清單與修復追蹤見根目錄 `SECURITY.md`；每項對應一個 `test_*_security.py`／`test_batch*.py`（TDD）。

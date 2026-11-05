# ADR-0298：規則文字和實際檢查對齊

- 狀態：✅ 已實作
- 日期：2026-11-05
- 決策者：維護者
- 相關：[ADR-0025](0025-print-ban-and-basicconfig-ban.md)、[ADR-0027](0027-raise-string-ban.md)、[ADR-0031](0031-console-ban.md)（掃描範圍更正）、[ADR-0278](0278-lint-ratchets-in-ci.md)（`lint_ratchet.py`；ruff、pyrefly 的完全相等不變）、[ADR-0296](0296-doc-routing.md)（規則分流時查到這些不一致）。
- 影響範圍：`scripts/lint_ssot.py`、`scripts/lint_ratchet.py`、`scripts/guard_dead_code.py`、`tests/lint/vulture_baseline.json`、`ToukaChat/scripts/guard_frontend.ts`、`ToukaChat/scripts/lib/contract_type_guard_logic.ts`、`ToukaChat/scripts/contract_type_baseline.json`、`ToukaChat/scripts/lib/console_guard_logic.ts`、`ToukaChat/eslint.config.mjs`；禁令 7、9、33、43、46、50、51 的文字。產品程式不動，守衛數量不變。

## 背景

分流規則時查到 9 處禁令文字和實際檢查不一致。逐項試過工具與故意違規的片段，結果：

| 禁令 | 查到什麼 |
|---|---|
| 7、10 | `lint_ssot.py` 只看路徑含 `/src/`，Heartbeat、Dispatch 掃不到。CI 的 ruff 計數通常擋得到新增的 `print` 和多字訊息，但只看總數，別處少一個就抵掉，單字訊息也漏掉 |
| 9 | 2026-05 已拿掉 `connections.ts` 的白名單，文字沒跟上 |
| 13 | `components` 沒人查 `console`：ADR-0031 以為歸 `guard_hooks.ts`，那道不查 |
| 33 | 函式名舊 |
| 43 | 規則名 `TIME_BAN` 不存在，照寫豁免標記不會生效 |
| 46 | 未用函式的信心值是 60，檢查門檻是 80，後端未用的函式、方法、類別完全沒人擋；後端未用的檔案也沒檢查 |
| 50 | 查不到任何「白名單」，只禁 `@radix-ui/*`，`radix-ui` 總包擋不到 |
| 51 | 文字寫的 AST 檢查不存在；唯一相關的 ESLint 區塊只管一個名字，而且蓋掉了深層相對匯入的警告 |
| ruff 兩條規則 | 不是缺口：ADR-0278 刻意放在 pytest 裡，CI 有跑。不動 |

## 決定

1. **`print`、硬編碼錯誤字串掃 Heartbeat、Dispatch**：`lint_ssot.py` 加 `_is_business_source_path`，兩個資料夾用路徑分段比對。不能比對 `/ToukaHeartbeat/` 子字串：guard_all 和 CI 從 repo 根掃 `.`，規則收到的是 `ToukaHeartbeat\models.py` 這種相對路徑，比對不中。納入後 0 個新違規（raise 7 處原本就有 noqa）。
2. **`console` 掃 `src/components/`**：`guard_console.ts`（Guard #15）。目前 0 處。這道只在本機全套檢查跑，CI 不跑。
3. **`radix-ui` 也不能直接引用**：`no-restricted-imports` 加精確名稱 `radix-ui` 與 pattern `radix-ui/*`，封裝層 `src/components/ui/**` 照舊放行。精確名稱不能寫成 pattern，否則會連 `@/components/radix-ui` 一起擋。其他底層元件沒有規則：真要做白名單，封裝層外約 100 行原生按鈕、輸入框要改或豁免，好處很小，所以禁令 50 拿掉「強制實施組件使用白名單」，「不得直接用未封裝的底層元件」照留。
4. **後端未用的函式、方法、類別不得增加**：`lint_ratchet.py` 加 `vulture`，測試 `tests/lint/test_vulture_ratchet.py` 在 CI 的 pytest 裡跑，Guard #20 也會跑。
   - 參數由測試釘住：門檻 60，排除靠註冊才用到的裝飾器（`@router.*`、`@*_router.*`、`@app.*`、`@mcp.*`、`@field_validator`、`@model_validator`、`@computed_field`、`@self.client.event`），`@classmethod`、`@staticmethod`、`@property` 不排除。不排除時門檻 60 有 628 筆，大多是誤報。
   - 只留函式、方法、類別、屬性；鍵是「相對路徑:種類:名稱」，路徑用 `/`。既有 108 筆（方法 63、函式 37、類別 6、屬性 2）凍結成清單，清理另列。維護者接受凍結時是 107 筆，開工重算多了一筆 `hooks/background.py:in_flight_count`（只有測試在用），列在 PR 說明。
   - 比對**單向**：多出來才紅，少了不紅，`--update` 收緊。和 ruff、pyrefly 的完全相等不同，因為別處新增一個同名的用法就會讓項目消失，和那個 PR 無關（實測：加一個 `x.covers`，`mcp_origin.py:method:covers` 就消失）。前例是 Guard #35。
   - 分清跑完（結束碼 0、3）和壞掉（1、2），看不懂的輸出行也算壞掉：單向比對下，格式變了全部比對不到會變成 0 筆而通過。
   - 門檻 80 的掃描也加 `ToukaDispatch`（0 筆）。基準只要變長，PR 說明要逐項列出。
5. **合約同名的手寫型別不得增加**：`guard_frontend.ts`（Guard #2，`npm run guard:api`，CI 有跑）加 `contract_type_guard_logic.ts`（ts-morph）。
   - 真別名：右側是根為 `components`、`operations`、`paths` 的索引型別，或全部由這種型別組成的聯集；`Omit`、`Pick`、交集、泛型都不算。合約名是 `openapi.json` 的 201 個 schema 加 `lib/types.ts` 的 34 個真別名（其中 9 個不是 schema 名，聯集 210）。
   - 掃全部生產 `.ts`／`.tsx`，含 `lib/types.ts`，不含測試、`src/types/api.ts`、`src/generated/`。名字屬於合約、宣告卻不是真別名就算。
   - 既有 10 個凍結在 `ToukaChat/scripts/contract_type_baseline.json`，由維護者接受，清理另列：7 個欄位名相同或是子集（`BackupInfo`、`BackupStats`、`GroupWhitelist`、`SlidingWindowConfig`、`SetupStatus`、`ModelInfo`、`SystemNotice`），3 個只是撞名、內容不同（`SoulConfig`、`lib/types.ts` 的 `AgentIdentity`、`SkillInstallRequest`）。
   - 要**完全相等**（同 ADR-0278 §1）：這份清單沒有無關同名的雜訊，改善要留在差異裡。後端改 schema 名時常常要一起重拍。
   - 刪掉 ESLint 第二個 `no-restricted-syntax` 區塊：它只管 `McpHubServer`、只是警告，由這道取代；深層相對匯入的警告恢復（只是警告，Guard #35 不算）。
6. **文字更正**：禁令 7 加「`ToukaProxy/plugin_manager.py` 是命令列工具，目前不掃」；9 更正掃描範圍與白名單；33 換函式名；43 換規則名；46、50、51 照上面寫清楚擋得到什麼、擋不到什麼。「嚴禁」的那句都不縮小。同時更正 `SSOT_MASTER_MAP.md` 的規則對照表（`TID251` 其實是 ruff、`CONSOLE_BAN` 靠 noqa、`ENV_SSOT`／`ERROR_SSOT` 少了 `_VIOLATION`）。

## 碰不到的地方

- 死碼：
  - 名字和全專案別處相同就不會報（例如 `def run()`），只有測試在用的會報。
  - `# noqa`（含專案自訂代號）與白名單能隱藏項目，全專案同名都算；靠審查看。
  - 舊鍵留著時，同路徑同名再變死碼不會紅；`--update` 也會收下新增項目。
  - vulture 不看 `.gitignore`。
  - 後端未用的檔案沒有檢查。
  - 會誤紅：只有測試在用的新函式；先加函式、下個 PR 才接上；搬動或拿掉檔案（實測拿掉一個檔會讓別的檔多 2～11 筆）。
- 合約型別：
  - 改名就抓不到；只比名字，不比內容，既有 10 個是否漂移不會發現。
  - 只看 `interface` 與 `type`，`class`、`enum` 不算。
  - `lib/types.ts` 的別名改成手寫，那個名字就不再算合約名；不是 schema 名的 9 個別名（例如 `McpHubServer`）會因此失守，schema 名不受影響。
- `print`、硬編碼錯誤字串：`WHITELIST_FILES` 依檔名放行，`ToukaHeartbeat/config.py`、`awaken.py` 裡的不會紅。
- `console`：只抓直接的 `console.log(...)` 這類呼叫，`window.console`、解構都抓不到。
- ESLint：動態 `import()` 擋不到。
- Guard #15、#20 不在 CI，只有本機全套檢查會跑；死碼的基準比對在 CI 的 pytest 裡。
- `lint_ssot.py` 的其他規則（例如禁令 42 的 `os.system`）仍只看 `/src/`，沒有一起改。

## 回復

Revert 這個 PR。後面已有更新編號的 ADR 時不要整個 revert（Guard #28 會紅），只還原檢查程式、兩份基準與文字，本檔改標「⛔ 已撤回」。

# ADR-0209：Hermetic 測試設定範本 hydration

- 狀態：✅ 已接受（經維護者人工審查）
- 日期：2026-08-09
- 決策者：維護者
- 影響範圍：`ToukaProxy/src/core/bootstrap.py`、`ToukaProxy/src/config.py`、`tests/conftest.py`、hermetic collection gate

> **2026-08-14 修訂（[ADR-0214](0214-standalone-smoke-data-and-metered-api-boundaries.md)）**：
> tracked examples hydration 與 absolute app-data override 契約擴及 standalone smoke；Memory 端
> Docker resolver 亦必須保留顯式 absolute `MEMORY_APP_DATA_DIR`，不能逃逸至 `/app/data`。

## 背景

P8-B 供應鏈 RED gate 必須在 immutable、read-only、network-none runner 中以正式 root
`tests/conftest.py` collection。既有測試沙盒在任何 `src.*` 匯入前建立空的
`TOUKA_CONFIG_DIR`，同時停用 production bootstrap；因此 tracked
`ToukaProxy/config/*.json.example` 沒有被轉成沙盒內的 runtime JSON。`ErrorCode` 最後只剩
最小 fallback，`src.main` 在產品測試執行前即 collection 失敗。

同一問題還暴露兩個配置邊界：停用 bootstrap 時不得寫入唯讀 candidate；Docker 中顯式提供的
絕對 `MEMORY_APP_DATA_DIR` 必須保留，只有相對值才可回退到 `/app/data`。

## 決策

1. 在 `src.core.bootstrap` 提供純粹、可指定來源與目標的
   `hydrate_config_examples(source_dir, target_dir)`：
   - 來源只讀取已追蹤的 `*.json.example`；
   - 目標只建立缺少的同名 `.json`；
   - 已存在目標永不覆寫；
   - 迭代順序固定，結果可重現。
2. Production `ensure_system_bootstrap()` 以同一 config 目錄同時作來源與目標，維持原有自癒語意。
3. Root `tests/conftest.py` 在匯入其他 `src.*` 前，將 tracked examples hydration 到 session temp
   config；所有寫入只落測試沙盒，不回寫 repository 或 `data/`。
4. `TOUKA_BOOTSTRAP_ENABLED=false` 是 import-time 寫入的硬閘；停用時 `_trigger_bootstrap()` 不得
   呼叫 production bootstrap。
5. Docker 內顯式絕對 `MEMORY_APP_DATA_DIR` 是測試／部署覆寫；相對值才使用 `/app/data` 基準。
6. `--confcutdir=tests/hermetic` 只驗 pre-conftest contract；正式產品 RED／GREEN 必須使用 root
   conftest 並完整 collection 預期 test IDs。

## 後果

- fresh checkout 與密封 runner 共用 tracked examples 作為設定 SSOT，不再依賴被 `.gitignore`
  排除的本機 runtime JSON。
- production bootstrap、測試 sandbox 與 Docker app-data 規則各自有單一明確入口。
- hydration 只補缺，不覆寫既有設定；若範本本身錯誤，既有 schema／config tests 仍會 fail-loud。
- 此 ADR 不放寬 `data/` 保護、不新增 writable bind mount，也不把 meta-test 冒充產品 gate。

## 驗證

- helper 先以缺失符號取得 RED，再驗證缺少目標建立、既有目標保留、來源不變。
- Docker meta-test 驗證 disabled bootstrap、顯式 app-data 與 hydration contract。
- 相同密封條件下正式 P8-B gate 完整收集並執行 7 tests；失敗只允許產品 assertion。
- Ruff、Guard #19、Guard #28、guard_all 與相關 bootstrap/config tests 必須全綠。

## 回滾

可一起移除 helper、conftest hydration、兩個 hermetic tests 與本 ADR。不得以重新啟用 workspace
bootstrap 寫入、加入 `/app/data` mount，或複製未追蹤 runtime config 作為回滾方案。

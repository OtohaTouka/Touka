# ADR-0145: 靈魂模型註冊 SSOT 化——model_manager 單例權威，修復 models.json 快取／磁碟競態

## 狀態

✅ 已接受（隨機次序 flaky 測試追查揭出的潛在正式環境資料流失，同批修復）

## 日期

2026-07-11

## 背景

隨機次序 flaky 測試 `tests/unit/test_soul_crud_real_writes.py::test_create_soul_persists_files_and_registrations`
偶發失敗（單獨執行綠、與相鄰檔兩兩對撞雙向綠、重洗牌綠、全套僅稀有一次紅）。決定性重現後
確證的根因**不是**殘留檔污染，而是一條**單例記憶體 vs 檔案直寫**的競態：

1. `create_soul` 經 `routers/souls.py` 的 `_update_models_config` 以
   `resource_manager.read_json/write_json` **直接寫 `models.json`**，插入
   `{"id": "ToukaSoul<id>", "owned_by": "touka"}`——但**完全不更新 `model_manager`
   單例的記憶體 `_models_cache`**（ADR-0142 修復 Soul Protection 時沿用的既有直寫路徑）。
2. 每次 `TestClient(app)`／正式啟動都 `spawn_logged(initialize_and_verify_models())`
   射一個背景任務，await `check_models_health()`。
3. `owned_by in ("touka","proxy")` 的模型被**無網路無條件**視為健康
   （`services/models/health.py`），故只要 `_models_cache` 含任一此類模型，健檢即
   `updated=True` → `_save_config()` 以**過時快取整包覆蓋 `models.json`**，抹掉剛寫入的
   `ToukaSoul<id>`。當覆蓋落在「create 寫檔」與「斷言讀檔」之間 → 測試紅。

`agent_service` 每次重讀 `agents_registry.json`（無單例鏡像），故僅 `models.json` 受害。
此競態同時是**潛在正式環境 bug**：建立靈魂後任一次 `_save_config` 觸發（每次對話的
`mark_model_seen`、每小時 `check_models_health`、`patch_model_settings` 等）都會靜默把
`ToukaSoul<id>` 自 `models.json` 移除，使該靈魂在 `models.json` 持久化的
enabled／visibility／name 覆寫流失（`get_models` 仍經 `agent_service` 顯示靈魂本體，故非致命）。

## 決策

**讓 `model_manager` 成為靈魂模型註冊的唯一權威（SSOT）**，快取與磁碟永遠一致——不再有
可被背景 `_save_config` 回寫抹除的「只在檔案、不在快取」的孤立條目。

- `services/models/manager.py` 新增 `register_soul_model(soul_id)`／`unregister_soul_model(soul_id)`：
  對 `_models_cache` 增／刪 `ToukaSoul<id>` 條目後 `_save_config()`。裸插入
  `{"id","owned_by":"touka"}` 與舊直寫等價（enabled／visibility 由 `get_models` 讀取時補預設，
  不寫入以免行為漂移）；已存在／不存在皆冪等。
- `routers/souls.py` 的 `_update_models_config` 改為委派上述方法（**保留函式本體**——create 回滾
  測試 monkeypatch 它）。`_update_agents_registry` 不動（無單例鏡像、無競態）。
- 放棄舊碼「`models.json` 缺失／損毀則略過」守衛：bootstrap 啟動即佈檔、`_save_config` 缺檔會
  自建、且此略過是全系統任一 `_save_config` 早已不保證的假保護（無測試依賴）。
- **測試決定性硬化（defense-in-depth）**：`test_soul_crud_real_writes.py` 的 `registry_snapshot`
  fixture 改為**無條件**把 `models.json`／`agents_registry.json` 佈為 `{}`（對齊本檔「fresh-install」
  前提）並 `model_manager.load_initial_config()` 歸零快取，使背景健檢對空快取即早退。此為對
  「單例鏡像 config」這類競態的通用防護，非 SSOT 修復的替代。

## 後果

- **正面**：flake 消除（決定性重現由紅轉綠）；正式環境「建立靈魂後模型註冊被靜默抹除」
  的資料流失根治；souls↔model_manager 契約收斂為單一權威，杜絕未來同類分裂。
- **代價／限制**：
  - `routers/souls.py` 新增對 `model_manager` 的模組級依賴（已驗證無循環匯入，與
    `settings_snapshot_service` 同先例）。
  - `register/unregister` 以 `soul_id: str` 為低階註冊鍵（`# noqa: IDENTITY_SSOT`，同 souls
    路由 path-param 與 `mission_lock` 字串鍵慣例；身分校驗由呼叫端完成）。
- **不做**（刻意）：不改 `check_models_health`／背景健檢本身（其行為對已同步的快取正確）；
  不為 `agents_registry.json` 加單例（本就每次重讀、無競態）。

## 相關

- [ADR-0142](0142-soul-crud-repair-and-admin-stepup.md)（靈魂 CRUD 修復——本案取代其沿用的
  `_update_models_config` 直寫路徑）
- 追查與重現：`tests/unit/test_soul_crud_real_writes.py` 檔頭；`services/models/manager.py`
  `check_models_health`／`_save_config`；`services/models/health.py`（touka/proxy 無條件健康）

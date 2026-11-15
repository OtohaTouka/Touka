# ADR-0315：嵌入模型換錯時的三個防呆

- 狀態：✅ 已實作。一頁計畫由維護者核准（2026-11-15）。
- 日期：2026-11-15
- 決策者：維護者
- 相關：[ADR-0314](0314-new-installs-default-to-multilingual-embedding.md)（新安裝預設 e5，本 ADR 做它的三個後續）、[ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)（下載、切換、重新嵌入三動作）、[ADR-0070](0070-chinese-vector-memory-migration.md)（重建工具）、[ADR-0256](0256-memory-store-upgrade-record-and-sources.md)（事實的 `embedding_model` 只當參考）、[ADR-0288](0288-memory-io-on-one-worker-thread.md)（記憶工作者）
- 影響範圍：`ToukaMemory/src/memory_server/utils/embedding_index.py`（新）、`utils/qdrant_meta.py`（新）、`services/index_check.py`（新）、`vector.py`、`services/reembed.py`、`services/manager.py`、`factual.py`、`config_utils.py`、`ToukaMemory/scripts/reembed_memory.py`、`ToukaProxy/src/services/embedding_service.py`、`routers/config.py`、`routers/setup.py`、`services/setup_service.py`、`routers/observability.py`、`main.py`、`services/memory_scrub/qdrant_meta.py`、`ToukaProxy/config/errors.json.example`、`ToukaChat` 的記憶提示與精靈警告

## 背景

兩個嵌入模型的向量長度不同（英文 `all-MiniLM-L6-v2` 與精靈的多語小模型 `paraphrase-multilingual-MiniLM-L12-v2` 都是 384，`intfloat/multilingual-e5-base` 是 768；2026-11-15 讀 Hugging Face 官方模型頁）。換錯模型時舊記憶的語意搜尋失效，要離線重建才恢復，事實不會掉。ADR-0314 讓新安裝預設 e5、程式內建預設不改，審查時列了三個後續：

1. 沒有地方可靠地記「記憶的語意索引是用哪個模型建的」。事實的 `embedding_model` 只當參考：補索引（`reindex_facts`）對向量庫已有 point 的事實一律記**目前**的模型名，換錯後跑一次就說謊；舊事實是 NULL；重建工具不寫事實。向量庫（Qdrant 本機模式 `meta.json`）只記每個 collection 的向量長度。
2. 能直接寫 `EMBEDDING_MODEL`、不用確認的入口有兩個：設定頁背後的 `POST /config/env`，以及首次設定精靈的 `POST /admin/setup/apply`（`embedding_model` 欄）。畫面都沒拿來改模型。精靈切換端點的確認只在精靈打得開時有用（`hasSetupIssues`），而且它把「目前設定」當成記憶用的模型：設定檔不見照範例重建成 e5 後，換回原本的模型反而被勸阻。
3. 重建工具 `reembed_memory.py` 的 `--model` 預設寫死 e5。

## 決定

### 1. 索引備註

建立 collection（`VectorStore._ensure_collection`）與重建（`reembed_agent`）時，在 Qdrant 官方的 collection metadata 記 `{"touka_embedding_model": <設定的名稱>, "touka_embedding_dim": <長度>}`（`embedding_index.index_marker`）。不補記既有 collection：不寫入使用者現有的向量庫。qdrant-client 1.18 本機模式把 metadata 存在 `meta.json`；1.18 本來就在每個 collection 寫 `"metadata": null`，退回不認得這欄的更舊套件本來就打不開，這次不會變差。快照清除的 `meta.json` 格式和比對共用 `memory_server/utils/qdrant_meta.py` 一份定義（禁令 17）。

### 2. 判定（`embedding_index.judge`）

只看事實庫裡有記憶（含封存）的 agent 對應的 collection（`FactualStore.agent_ids_with_facts`），刪掉的靈魂留下的舊索引不算。事實庫只有 agent id 字串，換成 collection 名稱用 `collection_names.collection_name_for_agent_id`（和 `collection_name_for(identity)` 同一條規則；ToukaMemory 不能建 `AgentIdentity`，`IDENTITY_SSOT` 豁免登記在 NOQA_REGISTRY）。對某個模型，每個 collection 判成：

- 有備註：名稱相同就相符，不同就不符（依據：名稱）。名稱正規化一個函式：去空白、沒有 `/` 補 `sentence-transformers/`、忽略大小寫（`same_model`；`resolve_repo_id` 也在這裡，Proxy 的下載沿用）。
- 沒備註：模型在已知長度表（`KNOWN_EMBEDDING_DIMENSIONS`）、長度不同就不符（依據：長度）；長度相同且表裡只有它是這個長度才相符；其他判斷不了。只靠長度的相符只用在側欄（不出提示），**不算換模型的證據**：表外的同長度模型（例如其他 768 的模型）光看長度分不出來（成果審查 M2，`embedding_index.needs_sample`）。

### 3. 側欄提示

- `refresh_index_status(factual)` 每次即時讀 `meta.json`（經資源代理的文字讀取）與有記憶的 agent，**不建構事實庫**：呼叫端傳已經存在的實例，沒有就保留上一次結果；讀不到也保留上一次結果，同一段失敗只記一次日誌。啟動時就讀不到＝沒有結果＝不提示。
- 什麼時候算：啟動時在 `FactualStore.shared()` 之後同步算一次，不符時記 warning（模型名與筆數，不含路徑）；`GET /v1/memory` 在記憶工作者上重算（5 秒內算過就直接用，最多等約 2 秒，逾時或例外用上一次結果）；精靈換模型後把快取標成過期，由下一次 `/v1/memory` 重算（換模型會重置共用事實庫，當下沒有實例可用）。
- 模型載入後的抽查（`services/index_check.py`）：`MemoryManager.get_vector_store` 掛上回呼，模型第一次載入後在同一條執行緒跑；直接建 `VectorStore`（例如離線重建工具）不跑，沒有啟動時算好的狀態也不跑。對「判斷不了」與「只靠長度判相符」的舊 collection：實際長度不同判不符；長度相同就依 id 順序取最前面最多 3 筆有內容的點重算，和存的向量比 cosine，任一筆低於 0.8 判不符、全部達 0.99 判相符、其他判斷不了。結果只放記憶體。
- 提示代碼 `embedding_model_mismatch`（warning），欄位 `count`、`basis`（`name`／`length`／`sample`）、`configured_model`、`index_model`、`index_dim`；前端依 `basis` 三種句子，都寫具體修法。不含 agent id、路徑。

### 4. 換模型的確認（一條規則，三處共用）

- **這一類問題**：規則依賴的資料可能過期、不完整或讀不到，而缺資料時規則偏向放行（計畫審查連三輪的 HIGH 都在這裡）。**做法**：預設要確認；只有「評估當下在記憶工作者上即時讀到」的證據證明安全才放行；評估只用即時的名稱與長度判定，不用側欄狀態、不用抽查結果。
- **判定表**（由上往下第一個符合的那列決定）：

  | 情況（評估當下即時讀） | 精靈切換 | 兩個設定介面 |
  |---|---|---|
  | 請求沒改到設定檔裡的模型（只存金鑰等；設定檔和生效值不同也一樣） | — | 照收，不查記憶 |
  | 目標＝目前實際在用的模型 | 不是換模型 | 照收 |
  | 事實庫讀得到、沒有任何記憶 | 不用確認 | 照收 |
  | 事實庫與 `meta.json` 都讀得到、有記憶，但這些靈魂都沒有索引 | 不用確認（沒有向量會失效） | 照收 |
  | 有記憶的靈魂索引至少一個、而且全部由名稱證明對目標相符（只靠長度的不算） | 不用確認 | 照收 |
  | 任一有記憶的靈魂索引對目標不符 | 要確認，警告寫記憶實際用的模型（或長度） | 拒絕 |
  | 其他（判斷不了、讀不到、`meta.json` 不存在、評估出錯） | 要確認，警告寫看不出 | 拒絕 |

- 「改了設定檔裡的模型」：`POST /config/env` 把寫入前與寫入後的內容都用 dotenv 解析（涵蓋前後空白、`export`、值裡夾換行），沒有這行或值是空的都當內建預設；鍵名寫法不同（`EMBEDDING_MODEL`、`embedding_model`…）的每一行各算一個模型——Linux 只認 `EMBEDDING_MODEL`、Windows 認第一個寫法，所以任何一個寫法的值變了都算改到模型，寫完後不同寫法各寫各的模型時一律要確認（設定介面拒絕；成果審查 M1）。`POST /admin/setup/apply` 在設定檔每個寫法都已經是請求的值時才算沒改到。新加的鍵前面若最後一行沒換行會先補上，不會黏成模型名稱的一部分。
- 執行緒（ADR-0288）：有改到模型時路由先 `await run_memory_job(評估)`，再進 `ENV_FILE_LOCK` 寫檔，持鎖時不 await。持鎖時重讀設定檔再核對：`/config/env` 再算一次目標，不同就整個拒絕；`SetupService.apply` 預設不寫會換模型的值，路由評估過才帶 `embedding_switch_checked=True`，沒帶而鎖內讀到會換模型時整個拒絕（評估後檔案被別人改掉也擋得住）。拒絕放在既有 `try/except → SYSTEM_ERROR` 外面，回 `EMBEDDING_MODEL_SWITCH_NEEDS_CONFIRMATION`（409），整個請求不寫任何鍵。
- 精靈：`evaluate_switch` 多回 `index_model`、`index_dim`、`index_outcome`；前端已知名稱時警告寫索引的模型，只知道長度不同時寫長度，其他（判斷不了、讀不到）說看不出來。

### 5. 重建工具

`--model` 沒給時用設定裡的模型（`config_utils.get_embedding_model_and_source`；載入 `memory_server` 時 `bootstrap_env` 已讀設定檔），開跑印出模型與來源；指定的和設定不同時印警告、照跑。重建完的 collection 有備註。

## 後果

- 換錯模型時側欄講清楚記憶用哪個模型、怎麼修；換模型的入口都先確認。平常沒有變化。
- 第一次用記憶、模型載入後，每個沒備註的舊索引多算最多 3 筆。`/v1/memory` 在記憶正在忙時偶爾多等最多約 2 秒。
- 舊索引長度相同（兩個 384 小模型）時，啟動時判斷不了；換模型要確認，警告寫「看不出」。

### 已知限制

- 抽查門檻（0.99、0.8）是推測值，沒用真模型校準；要校準另開人工 probe，下載模型逐次取得同意。
- 混用的索引（長度相同、確認後新記憶用新模型寫進去）若取到的 3 筆剛好都是新寫的就看不出來。
- e5 前綴靠名稱判斷：同一個 e5 用不含 `e5` 的本機路徑設定時，抽查可能落在判斷不了。
- 還原舊快照後，條目沒變的舊 collection 會沿用過時的抽查結果，直到重啟；只影響側欄提示，不影響確認規則。
- `/config/env` 持鎖時不重查生效值；同一瞬間精靈也在換模型時可能用到舊的生效值。
- 「目標＝目前實際在用的模型」那列不看索引：已經換錯（生效中是 e5、索引是 MiniLM）、使用者把設定檔改回 MiniLM 但還沒重新啟動時，設定介面寫回 e5 會照收，把修正蓋掉；重新啟動後側欄照樣提示。
- 事實庫或 `meta.json` 從啟動起就讀不到時不提示。

## 範圍外（這次不修）

- 【推測，讀程式推得，沒實測】Docker 用精靈換模型後只重啟同一個容器（不是重建），可能退回容器建立時的舊模型：compose 在建立時把設定檔灌進環境，載入設定檔不覆蓋已有的值。
- 重建工具遇到名字只差標點的兩個靈魂（例如 `a-b` 與 `a_b` 共用一個 collection）時，重建第二個會清掉第一個的向量；事實還在，補索引就回來。
- 執行重建工具時，會經資源代理間接載入 `src.config`，照 bootstrap 規則可能補寫 `HEARTBEAT_TOKEN` 進 `.env`（既有行為）。

## 撤回

整個 revert 即可。已建立的 collection 留著備註，舊程式不讀它；不改事實、不改向量。

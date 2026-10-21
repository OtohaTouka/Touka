# ADR-0265：記憶系統第二批段 C：查重門檻與查詢下限依嵌入模型家族查表（e5-base 校準）

- 狀態：✅ 已接受（Owner 2026-10-15 核准記憶系統第二批計畫；本 ADR 是第 8 段的 PR；下載嵌入模型經 Owner 同意，只用合成句子）
- 日期：2026-10-15
- 決策者：Owner
- 影響範圍：ToukaMemory（`vector.py`、`services/lifecycle.py`、`services/manager.py`）；tests（新人工 probe、合成資料、smoke manifest）
- 相關：ADR-0070（e5 前綴的由來）、ADR-0263（門檻常數搬到 `lifecycle.py`）、ADR-0256（語意結果回查事實庫）、ADR-0253（重複掃描）、ADR-0214（人工 probe 的分類），決策都不變；ADR-0070、ADR-0263 已加反向註記

## 背景

- 語意查重門檻 0.92（存記憶時擋重複、重複掃描列出候選）與語意查詢下限 0.3（`VectorStore.query_vectors` 的 `score_threshold`）沒有任何校準紀錄，是只用英文 MiniLM 時定的。
- ADR-0070 把建議模型換成 `intfloat/multilingual-e5-base`，查詢加 `query: `、事實加 `passage: `。e5 的 cosine 分數整體偏高，兩個值在 e5 下合不合理從沒量過。
- pytest 強制離線、不能載真模型，只能用人工 probe 下載模型量。

## 決策

### 1. 家族判斷只有一個：`vector.embedding_family`

- `embedding_family(model_name)`：名稱（不分大小寫）含 `e5` 回 `"e5"`，其他回 `None`。這就是原本 `_format_for_embedding` 判斷要不要加前綴的規則；`_format_for_embedding` 改呼叫它，行為不變。
- 兩個門檻的表都以家族為鍵，所以「加不加 e5 前綴」和「用哪組門檻」永遠一致：加了 e5 前綴的一定用 e5 的值，沒加的一定用預設值。

### 2. 兩張表

| | 預設（非 e5，含程式預設的 `all-MiniLM-L6-v2`） | e5 家族 | 位置 |
|---|---:|---:|---|
| 語意查重（分數「大於」門檻就擋；重複掃描是「達」門檻就列） | 0.92 | **0.93** | `lifecycle.DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD`、`DUPLICATE_SIMILARITY_THRESHOLDS`、`duplicate_similarity_threshold()` |
| 語意查詢下限（分數 ≥ 下限才回傳） | 0.3 | **0.65** | `vector.DEFAULT_QUERY_SCORE_FLOOR`、`QUERY_SCORE_FLOORS`、`query_score_floor()` |

- 存記憶的查重（`check_new_memory`）與重複掃描（`MemoryManager.find_duplicates`）都用 `get_embedding_model_name()` 查表。
- `VectorStore.query_vectors` 的 `score_threshold` 預設由 `0.3` 改成 `None`：沒給時用這個向量庫自己的 `model_name` 查表（和前綴同一個來源；預設就是 `get_embedding_model_name()`），明給照用。目前沒有呼叫端明給。
- 不變式（測試釘住）：每個家族的查詢下限都低於它的查重門檻（查重的候選先經過查詢下限）；表的鍵只能是 `embedding_family` 會回的值。

### 3. 只量了 e5-base，其他尺寸套同一組值是推測

- e5 的 small、large 等尺寸沒有另外量，套用 e5-base 的值是【推測】。
- 理由：e5 的相似度普遍擠在偏高區間【推測】；0.3 對 e5 幾乎不過濾（這次 e5-base 所有配對最低 0.621），套家族值比套已知不適合 e5 的預設好；而且門檻跟著前綴走，不會出現「加了 e5 前綴卻用 MiniLM 時代門檻」的組合。換成其他尺寸時建議重跑 probe 確認。

### 4. 呼叫端只換成查表

- `manager.py`（Guard #45 consumer）只改匯入與 `find_duplicates` 比較那一行，不加函式、參數或讀取；基準不動。
- `lifecycle.py` 只改常數區、查表函式、匯入與 `check_new_memory` 的比較；從 `vector.py` 匯入 `embedding_family`（沒有循環匯入：`vector.py` 不匯入 services）。

## 校準方法

### 人工 probe

- `tests/smoke/manual/embedding_threshold_probe.py`：不載入任何專案程式（禁令 63）、不讀金鑰檔、不連 LLM 廠商；網路只連 Hugging Face。
- 檔名不符合 pytest 與 smoke 的自動找檔規則；`tests/smoke/manifest.py` 登記成人工。`metered_api=True` 是保守標示：Hugging Face 下載不收費，但和其他人工 probe 一樣列為「要人類同意才跑」，也符合既有治理測試（人工 probe 全為 True）。
- 模型釘 commit，只用 `snapshot_download(allow_patterns=…)` 抓 sentence-transformers 要用的 safetensors 一套 9 個檔，再從本機資料夾 `local_files_only` 載入；下載前向 API 查大小，合計超過 1.2 GB 就停，下載後逐檔比對大小。
- 先把 `HF_HOME`、`SENTENCE_TRANSFORMERS_HOME` 指到 `--cache-root`；Hugging Face 會寫入的位置（hub、assets、xet、modules）不在底下就不下載。沒帶 `--human-approved-model-download` 時只讀快取。`token=False`，不送任何 token。
- probe 自己複製了 `query: `／`passage: ` 前綴；`tests/unit/test_embedding_thresholds.py` 用 AST 讀出、和 `_format_for_embedding` 核對（不 import probe、不載模型）。

### 配對方式（照產品）

- 查重：新句子加 `query: `、舊記憶加 `passage: `，cosine（產品的 Qdrant collection 用 COSINE）。
- 查詢：問題加 `query: `、記憶加 `passage: `，cosine。

### 資料

- `tests/fixtures/embedding_threshold_pairs.json`：39 組全合成（中文 15、英文 12、中英夾雜 12），沒有真實對話或個人資料。
- 每組一句舊記憶，五類新句子：等同（同一件事換說法，夾雜組含另一種語言寫的同一件事）、更新（同一件事改了值）、矛盾、相關（同主題不同事）、無關；外加一句問句與一個關鍵字查詢。
- 查重配對：等同 78、更新 40、矛盾 39、相關 39、無關 39；每一句新句子對其他 38 組的舊記憶另算「其他主題」8,930 對（存記憶時新句子比的是全部舊記憶）。
- 查詢配對：直接回答 392（同組舊記憶＋等同／更新／矛盾句）、相關 78、無關 78、其他主題 2,964。

### 選法

- 查重寧可漏擋也不要誤擋：更新要走取代、矛盾不是同一件事，擋下它們會讓新資訊存不進去。取「誤擋為 0」的最低格（0.005 一格）再加 0.01。
- 查詢下限寧可多回傳也不要漏掉有關的：漏掉等於忘記，多回傳的無關記憶排在後面、有 `limit` 擋數量。取「有關的（直接回答＋相關）全部留下」的最高格（0.01 一格）再減 0.05。

## 結果（2026-10-15，e5-base，CPU；sentence-transformers 6.0.0、transformers 5.12.1、torch 2.13.0+cpu、huggingface_hub 1.21.0）

### 下載

- 模型 `intfloat/multilingual-e5-base`，釘 commit `d128750597153bb5987e10b1c3493a34e5a4502a`（repo 全部檔案合計 5,322,810,412 bytes，含 `pytorch_model.bin`、`onnx/`、`openvino/`，都沒抓）。
- 實際下載 9 個檔，大小和 API 相同：`model.safetensors` 1,112,201,288、`tokenizer.json` 17,082,660、`sentencepiece.bpe.model` 5,069,051、`config.json` 694、`tokenizer_config.json` 418、`modules.json` 387、`special_tokens_map.json` 280、`1_Pooling/config.json` 200、`sentence_bert_config.json` 57，**合計 1,134,355,035 bytes**。
- 暫存快取資料夾刪除前合計 1,134,455,008 bytes（多出 hub 的 `CACHEDIR.TAG` 195 bytes 與一份 xet 下載 log 99,778 bytes）。跑完整個刪除並確認不存在；使用者家目錄沒有 Hugging Face 或 torch 快取（下載前後都沒有）。
- probe 跑兩次（第一次下載＋量測，第二次離線只讀快取），分數相同。

### 查重

- 等同句 0.819～0.941（中位數 0.901）。不是重複的最高 0.920（更新「已經拿到 AWS 認證」對「在準備 AWS 認證」），其次 0.916（矛盾「生日不是三月十四日」對「生日是三月十四日」）。
- 誤擋為 0 的最低格 0.920，剛好是原本的值、沒有邊際 → 選 **0.93**。

| 門檻 | 擋下等同句 | 誤擋更新 | 誤擋矛盾 | 誤擋相關 | 誤擋無關＋其他主題 |
|---|---|---|---|---|---|
| 0.90 | 39/78（50.0%） | 2/40 | 1/39 | 0/39 | 0 |
| 0.91 | 32/78（41.0%） | 1/40 | 1/39 | 0/39 | 0 |
| 0.92（原值） | 19/78（24.4%） | 0/40 | 0/39 | 0/39 | 0 |
| **0.93** | **8/78（10.3%）** | 0/40 | 0/39 | 0/39 | 0/8,969 |
| 0.94 | 2/78（2.6%） | 0 | 0 | 0 | 0 |

- 0.93 下擋下的等同句：中文 4/30、英文 0/24、夾雜 4/24。

### 查詢下限

- 有關的最低 0.707（英文問句「Is the user training for any race?」對相關句「bought new running shoes」）；無關的落在 0.631～0.872。
- 有關的全部留下的最高格 0.70 → 選 **0.65**。0.65 下直接回答 392/392、**相關 78/78** 全留（中文、英文、夾雜各自全留）；無關 3,042 對只濾掉 1 對。
- 0.70 時有關的也全留，但只多濾掉約 6% 的無關、沒有邊際；0.71 起開始漏相關句（77/78），0.75 時直接回答剩 95.7%、相關 94.9%。

## 取捨

- 門檻跟前綴共用家族判斷，而不是用完整模型名稱精確比對（本段第一版）：精確比對會讓前綴（子字串判斷）和門檻不一致，例如 e5 其他尺寸加了 e5 前綴卻用 0.3；代價是其他尺寸沒量過也套 e5-base 的值（決策 3）。
- 不把全域預設改成 e5 的值：MiniLM 沒量過，不改它的行為。
- 不依語言分門檻：要先偵測語言、中英夾雜沒有明確歸屬。
- 查重不改成兩邊都用 `passage:`：實測誤擋為 0 時只擋下 15.4%（門檻 0.985），沒有比產品的 `query:`→`passage:`（0.920 時 24.4%）好。

## 和計畫不同的地方

- 計畫寫「以 `get_embedding_model_name()` 選」：查重照做；查詢下限在 `VectorStore` 裡用 `self.model_name`（和前綴同一個來源，正式環境兩者相同）。
- 技術主管 DEV-B 追加：表的鍵由完整模型名稱改成 `embedding_family`（見取捨第一點）。
- `tests/smoke/manual/README.md` 不在共用文件清單，但為了不和同時新增的人工 probe 段落衝突，留到文件階段才寫。

## 影響

- e5 的語意查重只擋得住幾乎照抄的重述，換個說法的同一件事大多會存成新的一筆，交給 `manage_dups` 整理；比原值少擋（24.4% → 10.3%），但不會擋下更新與矛盾。
- e5 的查詢下限從 0.3（對 e5 等於沒有下限）變 0.65；在這份資料上不會少回傳任何有關的記憶。
- 非 e5 模型的行為完全不變。
- **`.env` 的 `EMBEDDING_MODEL` 要寫完整的 `intfloat/multilingual-e5-base`**。少了 `intfloat/` 時，sentence-transformers 6.0.0 會把沒有 `/` 的名稱補成預設組織 `sentence-transformers/multilingual-e5-base`（`sentence_transformers/base/model.py` 223–228 行，`SentenceTransformer` 的預設組織是 `sentence-transformers`）；Hugging Face 上沒有這個公開 repo（不帶 token 查詢回 401），**模型會直接載入失敗**，不是門檻悄悄退回預設（照家族規則它也會拿到 e5 的門檻，但走不到那一步）。
- openapi、Guard #45 基準、NOQA、權限設定檔、AGENTS.md 都不變。

## 已知限制

- 嵌入分數分不開「同一件事換說法」和「同主題改了值／加了否定」：等同句最低 0.819，不是重複的最高 0.920，大幅重疊，調門檻救不回來。矛盾偵測不能靠嵌入分數。
- 英文分數整體比中文、中英夾雜低約 0.03～0.05（等同句中位數 0.873 對 0.913／0.911）；同一個門檻在英文幾乎擋不到重述（英文等同句最高 0.906）。
- 絕對下限分不開有關與無關（兩者都落在 0.63～0.87）；0.65 只是安全網，排序與 `limit` 才是主要機制。「其他主題」裡分數最高的幾對其實是主題相近的不同組（例如「都聽什麼音樂？」對「喜歡聽爵士樂」0.866），所以無關的上限被高估；不影響選法（只看有關的最低分）。
- 只量了 e5-base；e5 其他尺寸套同一組值是推測（決策 3）。
- 家族規則沿用前綴的子字串判斷（名稱含 `e5` 就算），和前綴的既有行為一樣寬；名稱剛好含 `e5` 的非 e5 模型會同時拿到前綴與 e5 門檻（既有行為，本 ADR 不改）。
- 資料是合成短句、39 組；真實記憶較長、較雜，分數可能整體偏移【推測】。兩個值都留了邊際，但沒有用真實資料驗證。
- 重複掃描和存記憶共用同一個門檻；掃描只列給人看、不會擋任何東西，用 0.93 幾乎列不出換句話說的重複【推測：之後可讓掃描用較低的門檻，例如 0.85 時列出 92% 的重述，也會列出 47.5% 的更新】。

## 決策落點表

| 決策 | 檔案 | 位置 | 測試 |
|---|---|---|---|
| 1 家族判斷 | `ToukaMemory/src/memory_server/vector.py` | `embedding_family`、`_format_for_embedding` | `test_embedding_thresholds.py`、`test_memory_embedding_prefix.py` |
| 2 查重門檻 | `ToukaMemory/src/memory_server/services/lifecycle.py`；`services/manager.py` | `DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD`、`DUPLICATE_SIMILARITY_THRESHOLDS`、`duplicate_similarity_threshold`、`check_new_memory`；`find_duplicates` | `test_embedding_thresholds.py`、`test_memory_lifecycle.py`、`test_memory_p0_repairs.py` |
| 2 查詢下限 | `ToukaMemory/src/memory_server/vector.py` | `DEFAULT_QUERY_SCORE_FLOOR`、`QUERY_SCORE_FLOORS`、`query_score_floor`、`VectorStore.query_vectors` | `test_embedding_thresholds.py`、`test_memory_index_state.py` |
| 校準工具 | `tests/smoke/manual/embedding_threshold_probe.py`；`tests/fixtures/embedding_threshold_pairs.json`；`tests/smoke/manifest.py` | probe 常數、選法；`MANUAL_LIVE_SPECS` | `test_embedding_thresholds.py`、`test_smoke_sandbox_governance.py`、`test_model_layer_redirects.py` |

## 驗證

本機 Windows、3.12 venv（`uv run --locked`）、fnm Node 24.21.0。自動測試都在 conftest 沙盒或 pytest `tmp_path` 裡：向量庫用 autospec 替身或 tmp 本機 Qdrant 加替身編碼器，不載入嵌入模型、不讀 `data/`、不打付費 API，也沒有啟動 Touka。

- 新測試 `test_embedding_thresholds.py` 31 個：同一張名稱對照表同時斷言前綴與兩個門檻一致（完整 e5-base、大小寫與空白、沒有組織的寫法、e5-large、兩個 MiniLM、空字串、未知）；預設值；下限 < 門檻與表的鍵；校準過的家族＝probe 量的模型所屬家族；存記憶的查重、重複掃描、查詢下限的實際行為；probe 前綴、只抓 safetensors 9 檔、不載入專案程式、只登記成人工；合成資料完整。
- 先紅後綠：probe 與 manifest 未寫時 4 個紅；查表未實作時 13 個紅；改成家族鍵前 11 個紅。實作後全綠。
- 突變：查重表或查詢下限表改回完整名稱精確比對，各有 4 個測試變紅。
- 全量（merge dev `a1413f8e` 後的 `4f8a7e9a`）：pytest 9719 passed／7 skipped／6 failed（6 個是本機 Docker Desktop 的 `test_local_compose_bindings`，以 CI 為準；7 個跳過與 ADR-0251 的基準相同）；`guard_adr_integrity.py`（0001..0265 連續）、`guard_noqa_registry.py`、`guard_native_tool_contracts.py`、`guard_module_boundaries.py`、`lint_ssot.py`、文件契約測試、ruff（改到的檔）通過。

## 回復

- 不要整個 revert 合併（之後的 ADR 會讓 Guard #28 變紅）。程式可單獨撤：`lifecycle.py`、`manager.py`、`vector.py` 三個檔回到原本的常數 0.92／0.3；不涉及資料或結構變更。
- probe、合成資料與 manifest 那一筆可以留著（不影響產品）。

## 附錄：分數分布（全部語言合計）

| 配對 | 類別 | 筆數 | 最小 | Q1 | 中位數 | Q3 | 最大 |
|---|---|---:|---:|---:|---:|---:|---:|
| 查重 | 等同 | 78 | 0.819 | 0.880 | 0.901 | 0.920 | 0.941 |
| 查重 | 更新 | 40 | 0.753 | 0.835 | 0.849 | 0.873 | 0.920 |
| 查重 | 矛盾 | 39 | 0.722 | 0.796 | 0.821 | 0.850 | 0.916 |
| 查重 | 相關 | 39 | 0.739 | 0.786 | 0.822 | 0.845 | 0.882 |
| 查重 | 無關 | 39 | 0.657 | 0.727 | 0.780 | 0.797 | 0.834 |
| 查重 | 其他主題 | 8,930 | 0.621 | 0.760 | 0.782 | 0.802 | 0.877 |
| 查詢 | 直接回答 | 392 | 0.711 | 0.803 | 0.830 | 0.854 | 0.903 |
| 查詢 | 相關 | 78 | 0.707 | 0.783 | 0.806 | 0.832 | 0.876 |
| 查詢 | 無關 | 78 | 0.671 | 0.732 | 0.781 | 0.797 | 0.832 |
| 查詢 | 其他主題 | 2,964 | 0.631 | 0.736 | 0.772 | 0.796 | 0.872 |

依語言的等同句（查重）：中文 0.844～0.938（中位數 0.913）、英文 0.819～0.906（0.873）、夾雜 0.871～0.941（0.911）。

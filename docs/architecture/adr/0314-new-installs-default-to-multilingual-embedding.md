# ADR-0314：新安裝預設用中英都好的嵌入模型

- 狀態：✅ 已實作。一頁計畫由維護者核准（2026-11-15）。
- 日期：2026-11-15
- 決策者：維護者
- 相關：[ADR-0070](0070-chinese-vector-memory-migration.md)（維護者的安裝改用 e5）、[ADR-0053](0053-first-run-setup-wizard.md)（設定精靈）、[ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)（嵌入模型下載與切換）、[ADR-0265](0265-embedding-thresholds-by-model-family.md)（e5 的門檻）
- 影響範圍：根目錄與 `ToukaProxy/` 的 `.env.example`、`ToukaProxy/src/services/embedding_service.py`、`ToukaChat/src/components/setup/steps/Step1SystemHealth.tsx`、`ToukaChat/src/locales/*/translation.json`

## 背景

ADR-0070 決定的是維護者自己的安裝改用 `intfloat/multilingual-e5-base`，做法是改那台的設定檔再離線重建，沒有改新安裝的預設，也沒有改設定精靈。新安裝的設定檔都從範例設定複製（說明文件請使用者複製，設定檔不見時 bootstrap 也會照範例重建），範例裡這一行是註解，所以新安裝一律用程式內建的英文模型 `all-MiniLM-L6-v2`，中文記憶查得比較差，也沒有人告訴使用者。精靈的選單只有兩個小模型，沒有 e5。

## 決定

1. **範例設定寫明 e5。** 根目錄與 `ToukaProxy/` 的 `.env.example` 都寫 `EMBEDDING_MODEL=intfloat/multilingual-e5-base`。名稱要完整，少了 `intfloat/` 會被補成不存在的 `sentence-transformers/multilingual-e5-base` 而載入失敗。
2. **程式內建預設不改，維持 `all-MiniLM-L6-v2`。** 設定檔沒寫模型的舊安裝（GHCR 大多是）升級後若換模型，向量長度從 384 變 768，舊記憶的語意搜尋失效、新向量寫不進去，要離線重建才恢復。改範例只影響新建的設定檔，已經有設定檔的安裝不受影響。
3. **精靈的選單加上 e5，標成「中文推薦」，放在最後。** 預選的是設定檔裡的模型，所以新安裝預選 e5，舊安裝預選自己原本的模型。
4. **精靈先把目前的模型下載好。** 原本選的就是目前的模型時直接往下走；改成先呼叫下載端點並顯示進度，後端已下載好的直接回完成、不重抓，下載完不呼叫切換。下載失敗（例如離線）時顯示錯誤和「先略過」，按下才往下走，之後第一次用記憶時再下載（和原本一樣）。換成別的模型時下載失敗不給略過：略過等於沒套用使用者的選擇。輪詢下載狀態時，除了「下載中」都算結束：後端重啟後狀態回到 idle，不能一直等。開發模式會把元件掛上、卸下、再掛上一次，掛上時要把「已卸下」的旗標重設，否則輪詢一開始就停。
5. **下載只抓 sentence-transformers 用得到的檔**（`select_download_files`）。排除 `onnx/`、`openvino/` 資料夾與 `.h5`、`.ot`、`.msgpack`；最上層有 safetensors 權重時，排除 `pytorch_model*.bin` 與它的索引檔。斷詞檔（`vocab.txt`、`sentencepiece.bpe.model`、`tokenizer.json`）與模組資料夾（如 `2_Dense/`）裡的權重一律保留。e5 一庫約 5.3GB，排除後約 1.1GB。第一次載入時 sentence-transformers 6.0 經 transformers 的 `from_pretrained` 逐檔取用，safetensors 優先，不會補抓被排除的檔（2026-11-15 讀 `sentence_transformers/base/modules/transformer.py` 與 `transformers/modeling_utils.py` 確認，沒有實際下載）。
6. **進度按位元組回報。** 排除之後 e5 幾乎只剩一個約 1.1GB 的檔，原本只按檔數算，進度會一直停在 0%。下載時把 `_DownloadProgress` 當 `hf_hub_download` 的 `tqdm_class`，一般下載與 Xet 下載都會逐段回報。同一次下載裡的重試沿用同一個物件，伺服器不接受續傳時扣回；中斷的檔下次重試從頭抓。Xet 的進度回呼可能在下載結束後才到，結束時先停止回報，不讓它把「完成」或「失敗」蓋回「下載中」。
7. **上次下載失敗的，重試時不看快取。** 抓到一半時快取資料夾已經建立，`is_cached`（`snapshot_download(local_files_only=True)`）會當成已下載；重試改成真的再下載，已經抓完的檔不會重抓。

## 後果

- e5 比英文小模型大：記憶體約多用 1GB（推測，沒實測），算向量也比較慢。VPS 說明的最低規格 2GB 補上：2GB 時改用英文小模型。
- 第一次下載約 1.1GB，存在 `hf-cache`，重建容器不用重抓。離線時和原本一樣：事實照存，語意搜尋暫停並在回覆裡說明。
- **設定檔不見時要注意。** bootstrap 照新範例重建設定檔，模型就變成 e5；原本用英文模型、已經有記憶的安裝，語意搜尋會失效，要把這一行改回原本的模型。範例設定的註解寫了這件事。
- 沒有失敗紀錄時仍是既有行為：後端重啟前下載到一半中斷的模型，`is_cached` 會當成已下載，精靈直接往下走，第一次用記憶時才補抓剩下的檔案。
- **沒把 `.env` 存在容器外的部署要明寫模型。** 映像裡帶著 `.env.example`；沒設 `TOUKA_ENV_FILE`（例如直接 `docker run`，或自己寫的 compose／k8s）時，每次建新容器 bootstrap 都照範例重建容器裡的 `.env`，記憶系統讀到它，升級後模型就換成 e5，舊記憶的語意搜尋失效。出貨的兩份 compose 都設了 `TOUKA_ENV_FILE`，不受影響。這類部署要用環境變數明寫 `EMBEDDING_MODEL`。
- 管理用的 `GET /config/env` 以根目錄範例為欄位定義，現在會多列 `EMBEDDING_MODEL`；畫面只取供應商金鑰，不會多出可改模型的欄位。

## 後續（這次不做）

- 已經有記憶的安裝改用 e5：要停機做離線重建，另外再談。
- ~~重建工具改成預設讀設定檔裡的模型。~~ 由 [ADR-0315](0315-embedding-model-mismatch-guards.md) 完成（2026-11-15）。
- ~~`POST /config/env` 寫 `EMBEDDING_MODEL` 時沒有「已有記憶要先確認」的防呆（精靈的切換端點有）。~~ 由 [ADR-0315](0315-embedding-model-mismatch-guards.md) 完成：`POST /config/env` 與 `POST /admin/setup/apply` 都和精靈用同一條確認規則。
- ~~啟動時比對記憶用的模型，對不上就明講；也能擋住其他換錯模型的情況。~~ 由 [ADR-0315](0315-embedding-model-mismatch-guards.md) 完成：語意索引記下用的模型，啟動與側欄在對不上時明講。

## 撤回

整個 revert 即可。已經用新範例裝好的安裝，設定檔裡留著 e5，改回舊範例不影響它們。

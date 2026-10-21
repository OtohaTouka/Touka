# ADR-0070: 中文向量記憶遷移 — multilingual-e5-base + qdrant 升級 + re-embed 工具

## 狀態

✅ 已接受（**延伸：[ADR-0118](0118-setup-wizard-reliability-and-embedding-lifecycle.md)**——嵌入模型的即時下載（進度）與切換防呆：既有記憶時需確認、re-embed 仍限離線、事實 `core_memory.db` 永不被動）（依賴宣告載體修訂 by [ADR-0117](0117-uv-workspace-single-lock-migration.md)：qdrant-client 自 requirements.txt 遷 pyproject/單一 uv.lock；版本 pin 值不變）

## 日期

2026-06-01

## 背景

ToukaMemory 的向量記憶原用 `all-MiniLM-L6-v2`（384 維、英文取向），對中文表現差；而 agent 記憶為**中英混合**（Alice/Bob 中文人格、Charlie 英文/程式碼）。同時 venv 的 `qdrant-client` 停在 **1.7.3**，**讀不了**較新 qdrant 寫入的本地 DB（`CreateCollection` 不認 `metadata`/`strict_mode_config`/`datatype` 等欄位而崩潰），且 `vector.py` 已使用 `query_points`（qdrant ≥1.10 才有的 API）。

關鍵前提：記憶採**雙庫**——`FactualStore`（`core_memory.db`，**無損事實**、SQLite，content 逐字保存）＋ `VectorStore`（Qdrant，語義**索引**）。事實永遠以 FactualStore 為準，向量索引可由事實重建。

## 問題

1. embedding 模型對中文不適合，且需改用**多語**（非中文單語，以兼顧 Charlie 的英文/程式碼）。
2. `qdrant-client 1.7.3` 過舊：讀不了既有資料、缺 `query_points`。
3. 換模型維度改變（384→768）需重建向量，但無批次 re-embed 工具；且 **Qdrant local/embedded 模式無法「原地」把既有 collection 改維度**（殘留舊維度 on-disk storage → `broadcast 768 into 384`）。

## 決策

1. **模型**：`EMBEDDING_MODEL=intfloat/multilingual-e5-base`（768 維、MIT、不限閘、不需 `trust_remote_code`、native sentence-transformers）。e5 需非對稱前綴 → 新增 `vector._format_for_embedding(model_name, text, is_query)`：e5 系列查詢加 `query: `、事實加 `passage: `，**非 e5 模型不加前綴**（向後相容）。SSOT 仍為 `config_utils.get_embedding_model_name()`（ADR-0053）。（ADR-0265：前綴判斷抽成 `vector.embedding_family`，查重門檻與查詢下限也用它查表。）
2. **qdrant**：`qdrant-client 1.7.3 → ~=1.18.0`（能讀既有資料 + 具 `query_points`）。**只升級 qdrant-client**，其餘被 `--upgrade` 連帶升上去的 pydantic/protobuf 等**全部還原**（守 `pydantic~=2.12.5` 主權、ADR-0016）。順帶把 `requirements.txt` 的 `qdrant-client>=1.7.0` 收緊為 `~=1.18.0`（關 SECURITY.md P20 寬鬆 pin）。
3. **re-embed 工具**：`memory_server/services/reembed.py::reembed_agent`（從 FactualStore **唯讀**讀某 agent 全部事實 → 先全部重算向量 → 才刪舊建新 collection、批次 upsert，保留 `created_at`/`archived`/payload、`point_id=uuid5(fact_id)`）＋ runner `ToukaMemory/scripts/reembed_memory.py`。
4. **遷移採「建新 + 原子置換」**：因 Qdrant local 無法原地改維度，先在臨時目錄**建一份全新 `_qdrant_db`**（讀真實 `core_memory.db`、所有 agent 重算為 768），驗證數量後**整包置換**真實 `_qdrant_db`（舊的移到 `backups/` 留底）。

## 設計決策

### 為何選 multilingual-e5-base 而非 bge-zh / EmbeddingGemma / gte-multilingual

bge-*-zh 是**中文單語**，會傷到 Charlie 的英文/程式碼記憶；EmbeddingGemma 被 HF 閘控（需 token/同意條款）；gte-multilingual-base 需 `trust_remote_code=True`（載入時跑遠端碼，安全顧慮）。e5-base 多語、MIT、不限閘、不跑遠端碼，對「主權/安全/自託管」最乾淨；代價僅為前綴（已於 vector.py 處理）。

### 為何 DebugAgent 也一併重建

切到 768 後，**任何維持 384 的 collection 在查詢時都會維度不符而報錯**。為全庫一致，DebugAgent 的 3 筆事實也重建為 768（debug 資料、事實不變，無害）。

### 資料安全（無損優先）

- `core_memory.db`（無損事實）**全程唯讀**——遷移前後 SHA256 不變（已驗證）。
- 強制備份關卡（`manage_backup.py` 因多 .env bug 暫不可用→改以驗證過的手動備份：core_memory.db hash 相符 + 完整 `_qdrant_db`）。
- 「建新 + 置換」而非原地改，且舊索引留底；逐 agent 數量核對（向量數 == 事實數）。

## 後果

### 正面

- 中文/多語檢索品質提升（遷移後 self-match 分數 0.96–0.97，Alice/Bob 中文、Charlie 英文皆正常）。
- qdrant 修復並升級（讀得了資料、`query_points` 可用）；`~=` 收緊依賴主權。
- 新增可重用、idempotent 的 re-embed 工具（換模型/重建索引皆可用）。
- 全庫維度一致（768），向量數與無損事實對齊（順帶修正了原索引的漂移：Alice 131→130、Bob 120→115、Debug 2→3）。

### 負面 / 取捨

- e5-base 模型 ~1.1GB（首次下載 + RAM），比 all-MiniLM 重。
- Qdrant **local/embedded 模式持久格式脆弱**（跨 client 版本、無法原地改維度）——本 ADR 以「建新+置換」規避；長遠若要更穩固，宜改用 Qdrant **server**（較大架構決策，未做）。
- re-embed 須**系統離線**執行（避免邊寫邊重建）。

## 相關 ADR

- [ADR-0053](0053-first-run-setup-wizard.md) — EMBEDDING_MODEL SSOT（`get_embedding_model_name()`）
- [ADR-0016](0016-dependency-sovereignty-and-sandbox-verification.md) — 依賴主權（`requirements.txt` 為 SSOT、`~=` 固定）
- [ADR-0002](0002-integrated-memory-mode.md) — 記憶系統整合模式
- [ADR-0022](0022-io-proxy-dependency-injection.md) — I/O Proxy / MCP 回退（記憶獨立模式）

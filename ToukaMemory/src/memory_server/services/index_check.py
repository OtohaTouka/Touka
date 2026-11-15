"""模型載入後，抽查舊索引是不是用這個模型建的（ADR-0315）.

只補名稱與長度都「判斷不了」的 collection（沒有備註的舊索引）：
- 實際向量長度和索引不同 → 不符（依據：長度）。
- 長度相同 → 依 id 順序取最前面最多 ``SAMPLE_POINTS`` 筆有內容的點，用目前模型重算，和存的向量比
  cosine：任一筆低於 ``SAMPLE_MISMATCH_SIMILARITY`` 判不符；
  全部達 ``SAMPLE_MATCH_SIMILARITY`` 判相符；其他判斷不了（不提示）。
  每次取的點一樣，結果不會時有時無。

結果只放記憶體（``embedding_index.record_sample_verdicts``），不寫向量庫、不改事實。門檻是推測值，
沒用真模型校準；混用的索引若取到的點剛好都是新模型寫的就看不出來；e5 前綴靠名稱判斷，
同一個 e5 用不含 ``e5`` 的名稱設定時可能落在判斷不了。由 ``MemoryManager.get_vector_store`` 掛上，
直接建 ``VectorStore``（例如離線重建工具）不會跑；還沒有啟動時算好的狀態也不跑。
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from memory_server.utils.embedding_index import (
    Basis,
    Verdict,
    current_index_status,
    judge,
    read_index_entries,
    record_sample_verdicts,
    refresh_index_status,
    relevant_entries,
)
from memory_server.vector import _format_for_embedding

if TYPE_CHECKING:
    from collections.abc import Sequence

    from memory_server.factual import FactualStore
    from memory_server.utils.embedding_index import IndexEntry
    from memory_server.vector import VectorStore

logger = logging.getLogger(__name__)

SAMPLE_POINTS = 3  # 每個舊索引最多抽幾筆
SAMPLE_MATCH_SIMILARITY = 0.99  # 每筆都達到才判相符（同一個模型重算約等於 1）
SAMPLE_MISMATCH_SIMILARITY = 0.8  # 任一筆低於就判不符（推測值，看不準的落在中間＝判斷不了）


def _cosine(first: Sequence[float], second: Sequence[float]) -> float | None:
    if len(first) != len(second):
        return None
    norms = math.sqrt(math.fsum(x * x for x in first)) * math.sqrt(math.fsum(y * y for y in second))
    if norms == 0:
        return None
    return math.fsum(x * y for x, y in zip(first, second, strict=True)) / norms


def _sample(vector_store: VectorStore, entry: IndexEntry) -> Verdict | None:
    records, _next = vector_store.client.scroll(
        collection_name=entry.name, limit=SAMPLE_POINTS, with_payload=True, with_vectors=True
    )
    similarities: list[float] = []
    for record in records:
        content = (record.payload or {}).get("content")
        stored = record.vector
        if not isinstance(content, str) or not content or not isinstance(stored, list):
            continue
        fresh = vector_store.get_embedding(
            _format_for_embedding(vector_store.model_name, content, is_query=False)
        )
        similarity = _cosine(stored, fresh)
        if similarity is not None:
            similarities.append(similarity)
    if not similarities:
        return None
    if min(similarities) < SAMPLE_MISMATCH_SIMILARITY:
        return Verdict.MISMATCH
    if all(similarity >= SAMPLE_MATCH_SIMILARITY for similarity in similarities):
        return Verdict.MATCH
    return Verdict.UNKNOWN


def check_after_model_load(vector_store: VectorStore, factual: FactualStore | None) -> None:
    """模型剛載入（在記憶工作者上）：補判舊索引並重算側欄狀態；沒有狀態或事實庫就不做."""
    if factual is None or current_index_status() is None:
        return
    entries = read_index_entries(factual.base_dir)
    if not entries:
        return
    model_dim = vector_store.model.get_sentence_embedding_dimension()
    verdicts: dict[str, tuple[IndexEntry, Verdict, Basis]] = {}
    for entry in relevant_entries(entries, factual.agent_ids_with_facts()):
        if judge(vector_store.model_name, entry)[0] is not Verdict.UNKNOWN:
            continue
        if entry.dim is not None and model_dim and entry.dim != model_dim:
            verdicts[entry.name] = (entry, Verdict.MISMATCH, Basis.LENGTH)
            continue
        verdict = _sample(vector_store, entry)
        if verdict is not None:
            verdicts[entry.name] = (entry, verdict, Basis.SAMPLE)
    record_sample_verdicts(vector_store.model_name, verdicts)
    refresh_index_status(factual)
    logger.info("[Embedding] checked %d old index(es) after the model loaded", len(verdicts))

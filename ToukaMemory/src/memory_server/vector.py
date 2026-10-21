"""語義感知庫 (Vector Store) 服務.

負責代理人的向量記憶存儲、相似度檢索與 Embedding 管理。
底層使用 Qdrant 作為向量資料庫，並透過 SentenceTransformer 進行語義向量化。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointIdsList, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer

# 跨專案匯入
from memory_server import _get_app_data_dir
from memory_server.config_utils import get_embedding_model_name, get_resource_proxy
from memory_server.utils.collection_names import (  # fact_point_id 原名照舊可從這裡匯入
    QDRANT_DIR_NAME,
    collection_name_for,
    fact_point_id,
)

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

_SCROLL_PAGE_SIZE = 256  # count_points_outside 每頁讀幾個點 ID
# 語意查詢的相似度下限：分數分布每個嵌入模型家族不同，依 embedding_family 查表，其他用預設值；
# e5 的值由人工 probe 用 e5-base 校準（tests/smoke/manual/embedding_threshold_probe.py）。
DEFAULT_QUERY_SCORE_FLOOR = 0.3
QUERY_SCORE_FLOORS: dict[str | None, float] = {"e5": 0.65}


def embedding_family(model_name: str | None) -> str | None:
    """嵌入模型的家族：e5 系列回 "e5"，其他回 None；加不加前綴、用哪組門檻都看這個."""
    return "e5" if "e5" in (model_name or "").lower() else None


def query_score_floor(model_name: str) -> float:
    """這個嵌入模型的語意查詢下限；表裡沒有的家族用預設值."""
    return QUERY_SCORE_FLOORS.get(embedding_family(model_name), DEFAULT_QUERY_SCORE_FLOOR)


def _format_for_embedding(model_name: str, text: str, *, is_query: bool) -> str:
    """e5 系列需要 query:/passage: 非對稱前綴；其他模型（如 all-MiniLM）原文不變。

    e5 (multilingual-e5-*) 在檢索時對「查詢」與「文件」用不同前綴以提升品質；
    切換非 e5 模型時不加前綴，保持向後相容。
    """
    if embedding_family(model_name) == "e5":
        prefix = "query" if is_query else "passage"
        return f"{prefix}: {text}"
    return text


class VectorStore:
    """向量存儲中心，負責維護各 Agent 的語義記憶空間.

    Attributes:
        base_dir (Path): 向量資料庫儲存的物理根路徑。
        model_name (str): 用於 Embedding 的模型名稱。
    """

    _client_cache: dict[str, QdrantClient] = {}

    @classmethod
    def close_all_clients(cls) -> None:
        """關閉所有快取 Qdrant clients；即使其中一個失敗仍完成其餘清理。"""
        clients = tuple(cls._client_cache.values())
        cls._client_cache.clear()
        first_error: Exception | None = None

        for client in clients:
            try:
                client.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error

    def __init__(
        self,
        base_dir: str | Path | None = None,
        model_name: str | None = None,
    ) -> None:
        """初始化向量存儲，建立 Qdrant 連線。.

        Args:
            base_dir (str | Path | None): 資料庫儲存路徑。若未指定則自動獲取。
            model_name (str): 使用的 SentenceTransformer 模型。
        """
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(_get_app_data_dir()) / "vault"

        self.model_name = model_name if model_name is not None else get_embedding_model_name()
        self._model: SentenceTransformer | None = None
        self._embedding_cache: dict[str, list[float]] = {}

        # SSOT: 資料庫物理位址
        qdrant_path = self.base_dir / QDRANT_DIR_NAME
        get_resource_proxy().make_dir(str(qdrant_path))
        
        # 使用類別變數快取 Client，防止同進程重複開啟造成的鎖定 (SSOT)
        client_key = str(qdrant_path.absolute())
        if client_key not in VectorStore._client_cache:
            VectorStore._client_cache[client_key] = QdrantClient(path=client_key)
            logger.info(f"🚀 [Vector] Qdrant DB initialized at {qdrant_path}")

        self.client = VectorStore._client_cache[client_key]

    @property
    def model(self) -> SentenceTransformer:
        """延遲載入 Embedding 模型以節省啟動時間."""
        if self._model is None:
            logger.info(f"📥 [Vector] Loading embedding model: {self.model_name}")
            self._model = SentenceTransformer(self.model_name, trust_remote_code=False)
        return self._model

    def get_embedding(self, text: str) -> list[float]:
        """將文字轉為向量，具備內部快取功能.

        Args:
            text (str): 欲向量化的文字內容。

        Returns:
            list[float]: 語義向量。
        """
        if text in self._embedding_cache:
            return self._embedding_cache[text]

        # 簡單的 LRU 概念：超過 1000 條則清空快取防止溢位
        if len(self._embedding_cache) > 1000:
            self._embedding_cache.clear()

        vector = self.model.encode(text).tolist()
        self._embedding_cache[text] = vector
        return vector

    def get_collection_name(self, identity: AgentIdentity) -> str:
        """根據代理人 ID 生成合法的 Qdrant Collection 名稱."""
        return collection_name_for(identity)  # 規則在 collection_names（撞名判定共用）

    def _ensure_collection(self, identity: AgentIdentity) -> None:
        """確保該代理人的向量空間已存在."""
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            dim = self.model.get_sentence_embedding_dimension()
            self.client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            logger.info(f"✨ [Vector] Created new collection for agent: {identity.agent_id}")

    def drop_agent(self, identity: AgentIdentity) -> int:
        """刪除某 agent 的整個向量 collection（破壞性重置 Batch 6）。回**實際刪除的向量點數**。

        ADR-0104 R9：刪除前先 count()，回真實點數（而非 1/0），讓重置報告的 purged 數誠實反映
        銷毀量。完整性自檢仍由 agent_point_count（刪後 collection 不存在→回 0）把關，與此計數無關。
        """
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            return 0
        n = int(self.client.count(collection_name=name).count)
        self.client.delete_collection(collection_name=name)
        logger.info(f"🗑️ [Vector] Dropped collection for agent: {identity.agent_id} ({n} points)")
        return n

    def has_agent_collection(self, identity: AgentIdentity) -> bool:
        """某 agent 的向量 collection 是否還在（空的也算；刪除靈魂前檢查用）。"""
        return bool(self.client.collection_exists(self.get_collection_name(identity)))

    def agent_point_count(self, identity: AgentIdentity) -> int:
        """某 agent 的向量數（完整性自檢；無 collection 回 0）。"""
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            return 0
        return int(self.client.count(collection_name=name).count)

    def delete_points(self, identity: AgentIdentity, point_ids: list[str]) -> int:
        """只刪指定的點（collection 名稱和別人撞名時的重置用）；回傳實際刪掉的數量."""
        name = self.get_collection_name(identity)
        if not point_ids or not self.client.collection_exists(name):
            return 0
        present = [
            record.id
            for record in self.client.retrieve(
                collection_name=name, ids=list(point_ids), with_payload=False, with_vectors=False
            )
        ]
        if present:
            self.client.delete(collection_name=name, points_selector=PointIdsList(points=present))
        return len(present)

    def count_points_outside(self, identity: AgentIdentity, keep_point_ids: set[str]) -> int:
        """數 collection 裡不屬於 keep 的點（撞名時的完整性自檢；別人的孤兒點也會算進來）."""
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            return 0
        count, offset = 0, None
        while True:
            records, offset = self.client.scroll(
                collection_name=name,
                limit=_SCROLL_PAGE_SIZE,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            count += sum(1 for record in records if str(record.id) not in keep_point_ids)
            if offset is None:
                return count

    def save_vector(
        self,
        identity: AgentIdentity,
        fact_id: str,
        content: str,
        tags: list[str] | None = None,
        importance: str | None = None,
        emotions: list[str] | None = None,
        created_at: int | None = None,
    ) -> bool:
        """將事實內容存儲至向量空間.

        Args:
            identity (AgentIdentity): 代理人身分對象。
            fact_id (str): 事實唯一識別碼。
            content (str): 記憶內容。
            tags (list[str] | None): 標籤清單。
            importance (str | None): 重要程度。
            emotions (list[str] | None): 情感標記。
            created_at (int | None): 事實的建立時間（秒）；補索引時沿用原值，未給才用現在。

        Returns:
            bool: 若成功儲存回傳 True。
        """
        self._ensure_collection(identity)
        name = self.get_collection_name(identity)

        try:
            # 使用 Proxy 側的統一時間工具 (跨專案同步)
            from src.utils.time_utils import get_current_epoch
            now = int(get_current_epoch()) if created_at is None else created_at

            vector = self.get_embedding(
                _format_for_embedding(self.model_name, content, is_query=False)
            )
            payload = {
                "fact_id": fact_id,
                "content": content,
                "tags": tags or [],
                "importance": importance,
                "emotions": emotions or [],
                "created_at": now,
                "archived": False,
            }

            self.client.upsert(
                collection_name=name,
                points=[PointStruct(id=fact_point_id(fact_id), vector=vector, payload=payload)],
            )
        except Exception:
            logger.exception(f"❌ [Vector] Failed to save vector for {identity.agent_id}")
            return False
        else:
            return True

    def indexed_fact_ids(self, identity: AgentIdentity, fact_ids: list[str]) -> set[str]:
        """這些事實裡已經有向量的那些（只看 point 在不在，不載入模型、不算向量）."""
        name = self.get_collection_name(identity)
        if not fact_ids or not self.client.collection_exists(name):
            return set()
        by_point = {fact_point_id(fact_id): fact_id for fact_id in fact_ids}
        records = self.client.retrieve(
            collection_name=name, ids=list(by_point), with_payload=False, with_vectors=False
        )
        return {by_point[str(record.id)] for record in records if str(record.id) in by_point}

    def query_vectors(
        self,
        identity: AgentIdentity,
        query: str,
        limit: int = 5,
        score_threshold: float | None = None,
        include_archived: bool = False,
        tags_any: list[str] | None = None,
        importance: str | None = None,
        emotions_any: list[str] | None = None,
        date_start: int | None = None,
        date_end: int | None = None,
    ) -> list[dict[str, Any]]:
        """在向量空間中執行語義檢索.

        Args:
            identity (AgentIdentity): 代理人身分對象。
            query (str): 檢索文字。
            limit (int): 最大回傳數量。
            score_threshold (float | None): 相似度下限；沒給時依這個向量庫的模型查表。
            include_archived (bool): 是否包含已封存的記憶。
            tags_any (list[str] | None): 標籤過濾。
            importance (str | None): 重要度過濾。
            emotions_any (list[str] | None): 情感過濾。
            date_start (int | None): 時間範圍起始。
            date_end (int | None): 時間範圍結束。

        Returns:
            list[dict[str, Any]]: 檢索結果列表。
        """
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            return []

        try:
            query_vector = self.get_embedding(
                _format_for_embedding(self.model_name, query, is_query=True)
            )
            from qdrant_client.http import models as rest

            must_conditions = []
            if not include_archived:
                must_conditions.append(
                    rest.FieldCondition(key="archived", match=rest.MatchValue(value=False))
                )
            
            if importance:
                must_conditions.append(
                    rest.FieldCondition(key="importance", match=rest.MatchValue(value=importance))
                )
            
            if tags_any:
                must_conditions.append(
                    rest.FieldCondition(key="tags", match=rest.MatchAny(any=tags_any))
                )
            
            if emotions_any:
                must_conditions.append(
                    rest.FieldCondition(key="emotions", match=rest.MatchAny(any=emotions_any))
                )
            
            if date_start or date_end:
                must_conditions.append(
                    rest.FieldCondition(
                        key="created_at", 
                        range=rest.Range(gte=date_start, lte=date_end)
                    )
                )

            filter_params = rest.Filter(must=must_conditions) if must_conditions else None
            if score_threshold is None:
                score_threshold = query_score_floor(self.model_name)

            search_results = self.client.query_points(
                collection_name=name,
                query=query_vector,
                query_filter=filter_params,
                limit=limit,
                score_threshold=score_threshold,
            )

            return [
                hit.payload | {"_score": hit.score} for hit in search_results.points
            ]
        except Exception:
            logger.exception(f"❌ [Vector] Error querying vectors for {identity.agent_id}")
            return []

    def archive_vector_by_fact_id(self, identity: AgentIdentity, fact_id: str) -> bool:
        """封存指定的向量記憶.

        Args:
            identity (AgentIdentity): 代理人身分對象。
            fact_id (str): 事實唯一識別碼。

        Returns:
            bool: 若成功封存回傳 True。
        """
        name = self.get_collection_name(identity)
        if not self.client.collection_exists(name):
            return False

        try:
            from qdrant_client.http import models as rest

            self.client.set_payload(
                collection_name=name,
                payload={"archived": True},
                points=rest.Filter(
                    must=[
                        rest.FieldCondition(key="fact_id", match=rest.MatchValue(value=fact_id))
                    ]
                ),
            )
        except Exception:
            logger.exception(f"❌ [Vector] Error archiving vector {fact_id}")
            return False
        else:
            return True

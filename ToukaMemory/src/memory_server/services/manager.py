"""記憶管理核心服務 (Memory Manager).

作為 ToukaMemory 的總調度中心, 負責管理事實庫、向量庫的生命週期,
執行代理人身分歸一化 (Normalization) 並確保記憶操作的原子性與防撞檢查。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server import get_app_data_dir
from memory_server.config_utils import (
    get_current_epoch,
    get_embedding_model_name,
    get_workspace_root,
)
from memory_server.factual import ArchiveResult, FactualStore
from memory_server.models.source_ref import describe_sources
from memory_server.services.event_recorder import record_memory_event
from memory_server.services.index_state import (
    archive_in_index,
    index_new_fact,
    live_semantic_hits,
)
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.lifecycle import (
    DUPLICATE_CANDIDATES_PER_FACT,
    SAFETY_FLAG_NOTE,
    SAVED_FLAGGED_NOTE,
    SEMANTIC_DOWN_NOTE,
    check_new_memory,
    describe_history,
    duplicate_similarity_threshold,
    flagged_last,
)
from memory_server.utils.historical_data import wrap_historical_data
from memory_server.utils.legacy_time import format_taipei, taipei_day_start
from memory_server.vector import SemanticSearchUnavailableError, VectorStore

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.models.source_ref import SourceRef
    from memory_server.protocols import ISourceResolver

logger = logging.getLogger(__name__)

# 重複記憶掃描：每次最多比對最近的這麼多筆事實（每筆一次向量查詢）。
# 相似度門檻與候選數（DUPLICATE_*）和存記憶的查重共用，定義在 services/lifecycle.py。
MAX_DUPLICATE_SCAN_FACTS = 200


class MemoryManager:
    """記憶與知識庫總管.

    Attributes:
        _factual_store (FactualStore | None): 事實存儲實例。
        _vector_store (VectorStore | None): 向量存儲實例。
    """

    _factual_store_singleton: FactualStore | None = None  # 測試替換用；平常是共用的事實庫
    _vector_store_singleton: VectorStore | None = None

    @classmethod
    def reset_singletons(cls) -> None:
        """重置全域單例 (主要用於測試環境隔離)."""
        cls._factual_store_singleton = None
        cls._vector_store_singleton = None
        FactualStore.reset_shared()

    def __init__(self) -> None:
        """初始化記憶管理器並設定快取機制."""
        self._registry: dict[str, Any] | None = None
        self._registry_last_loaded: float = 0
        self._legacy_sync_service: LegacySyncService | None = None

    def get_legacy_dir(self, identity: AgentIdentity, resource_proxy: Any = None) -> Path:
        """獲取指定代理人的 Legacy 目錄路徑 (SSOT)."""
        if resource_proxy and hasattr(resource_proxy, "get_vault_path"):
            path_str = resource_proxy.get_vault_path("legacy", identity)
            if path_str:
                return Path(path_str)

        # 備援邏輯 (SSOT 糾偏)
        path = Path(get_app_data_dir()) / "legacy" / identity.agent_id
        if resource_proxy and hasattr(resource_proxy, "make_dir"):
            resource_proxy.make_dir(str(path))
        else:
            path.mkdir(parents=True, exist_ok=True)  # noqa: IO_PRISON_VIOLATION
        return path

    def get_personal_drive_dir(self, identity: AgentIdentity, resource_proxy: Any = None) -> Path:
        """獲取指定代理人的個人硬碟目錄路徑 (SSOT)."""
        if resource_proxy and hasattr(resource_proxy, "get_vault_path"):
            path_str = resource_proxy.get_vault_path("personal", identity)
            if path_str:
                return Path(path_str)

        app_data = Path(get_app_data_dir())
        agent_id = identity.agent_id

        # 根據物理目錄存在性判定 (Path SSOT 備援邏輯)
        if (app_data / "soul" / agent_id).exists():
            path = app_data / "personal_drive" / agent_id
        else:
            path = app_data / "custom" / agent_id / "drive"

        if resource_proxy and hasattr(resource_proxy, "make_dir"):
            resource_proxy.make_dir(str(path))
        else:
            path.mkdir(parents=True, exist_ok=True)  # noqa: IO_PRISON_VIOLATION
        return path

    def get_soul_dir(self, identity: AgentIdentity, resource_proxy: Any = None) -> Path | None:
        """獲取指定代理人的靈魂腳本目錄路徑 (SSOT)."""
        if resource_proxy and hasattr(resource_proxy, "get_vault_path"):
            path_str = resource_proxy.get_vault_path("soul", identity)
            if path_str:
                return Path(path_str)

        path = Path(get_app_data_dir()) / "soul" / identity.agent_id
        return path if path.exists() else None

    def get_legacy_sync_service(self) -> LegacySyncService:
        """獲取遺產同步服務實例."""
        if self._legacy_sync_service is None:
            self._legacy_sync_service = LegacySyncService(self)
        return self._legacy_sync_service

    def _load_registry(self, resource_proxy: Any = None) -> dict[str, Any]:
        """從物理檔案載入代理人註冊表 (SSOT)."""
        now = get_current_epoch()
        if self._registry is not None and (now - self._registry_last_loaded < 60):
            return self._registry

        registry = {}
        wr = get_workspace_root()

        possible_paths: list[Path] = []
        if wr:
            # 優先使用 SSOT 提供的根目錄
            possible_paths.append(Path(wr) / "ToukaProxy" / "config" / "agents_registry.json")

        try:
            # 備援：基於當前檔案結構的動態探測
            repo_root = Path(__file__).resolve().parents[4]
            possible_paths.append(repo_root / "ToukaProxy" / "config" / "agents_registry.json")
        except IndexError:
            pass

        for path in possible_paths:
            path_exists = False
            if resource_proxy and hasattr(resource_proxy, "exists"):
                path_exists = resource_proxy.exists(str(path))
            else:
                path_exists = path.exists()

            if path_exists:
                try:
                    if resource_proxy and hasattr(resource_proxy, "read_json"):
                        data = resource_proxy.read_json(str(path))
                    else:
                        import json

                        data = json.loads(path.read_text(encoding="utf-8"))  # noqa: IO_PRISON_VIOLATION

                    if data:
                        registry = data
                        self._registry = registry
                        self._registry_last_loaded = now
                        logger.debug("✅ [Manager] Registry loaded from %s", path)
                        break
                except Exception:
                    logger.warning("Failed to load registry from %s", path)

        if not registry and self._registry is None:
            self._registry = {}
            logger.error("🚨 [Manager] CRITICAL: agents_registry.json not found.")

        return self._registry if self._registry is not None else registry

    def get_factual_store(self) -> FactualStore:
        """獲取事實庫實例：Proxy 啟動時建好的共用實例（FactualStore.shared）."""
        return MemoryManager._factual_store_singleton or FactualStore.shared()

    def get_vector_store(self) -> VectorStore:
        """獲取向量庫實例 (Singleton)."""
        if MemoryManager._vector_store_singleton is None:
            model = get_embedding_model_name()
            MemoryManager._vector_store_singleton = VectorStore(model_name=model)
        return MemoryManager._vector_store_singleton

    def purge_agent(self, identity: AgentIdentity) -> dict[str, int]:
        """破壞性清除某 agent 的全部記憶（事實＋向量）。回各 store 刪除數（Batch 6）。"""
        return {
            "facts": self.get_factual_store().delete_agent_facts(identity),
            "vectors": self.get_vector_store().drop_agent(identity),
        }

    def count_agent_memory(self, identity: AgentIdentity) -> dict[str, int]:
        """某 agent 的記憶殘留數（完整性自檢）。"""
        return {
            "facts": self.get_factual_store().count_agent_facts(identity),
            "vectors": self.get_vector_store().agent_point_count(identity),
        }

    def normalize_agent_id(self, identity: AgentIdentity) -> str:
        """執行代理人身分歸一化 (Identity Normalization)."""
        agent_id = identity.agent_id
        if not agent_id:
            return "Unknown"

        clean_id = agent_id.strip()
        lower_id = clean_id.lower()
        if lower_id.startswith("toukasoul-"):
            clean_id = clean_id[10:]
        elif lower_id.startswith("toukasoul"):
            clean_id = clean_id[9:]
        elif lower_id.startswith("@"):
            clean_id = clean_id[1:]

        registry = self._load_registry()

        for main_id in registry:
            if clean_id.lower() == main_id.lower():
                return main_id

        for main_id, aliases in registry.items():
            for alias in aliases:
                if clean_id.lower() == alias.lower() or alias.lower() in clean_id.lower():
                    return main_id

        return clean_id

    async def save_memory(
        self,
        identity: AgentIdentity,
        content: str,
        tags: list[str] | None = None,
        importance: str | None = None,
        emotions: list[str] | None = None,
        source_context: SourceRef | None = None,
    ) -> str:
        """永久存儲一條記憶內容, 具備防重複檢查.

        H3 fix: content is sanitized before persistence to prevent prompt-injection
        attacks that could poison the knowledge base for future retrievals.
        寫入檢查（消毒、安全標記、完全相同與語意查重）和 supersede 共用
        ``lifecycle.check_new_memory``。
        ``source_context`` 是可信參數：只由 Proxy 記憶閘道依執行資訊組成，模型帶了會被拒絕。
        """
        checked = check_new_memory(self, identity, content)
        if checked.blocked:
            return checked.blocked

        factual = self.get_factual_store()
        fact_res = factual.save_fact(
            identity,
            checked.content,
            tags,
            importance,
            emotions,
            source=source_context,
            safety_flags=checked.safety_flags,
        )
        if fact_res.get("status") == "error":
            return f"❌ 存儲失敗：{fact_res.get('message')}"

        fact_id = fact_res["id"]
        record_memory_event(identity, "memory_saved", fact_id)  # 事實已提交（可信活動事件）
        note = (SAVED_FLAGGED_NOTE if checked.safety_flags else "") + checked.dedup_note
        if index_new_fact(factual, checked.vector, identity, fact_res["record"]):
            return f"Memory successfully saved (ID: {fact_id}).{note}"

        # 向量寫不進去不刪事實：標成待補，之後用 reindex 補上（保留 (ID: …) 格式）
        logger.error("❌ [Manager] Vector write failed; fact kept for reindex: %s", fact_id)
        return f"已存，語意索引稍後補上 (ID: {fact_id}).{note}"

    def normalize_time_range(
        self, date_start: str | None = None, date_end: str | None = None, is_recent: bool = False
    ) -> tuple[int | None, int | None]:
        """內部方法：日期字串當成台北的一整天，換成 Unix 秒範圍（事實庫與向量庫用秒）."""
        start_ts = None
        end_ts = None

        if is_recent:
            start_ts = int(get_current_epoch()) - (7 * 24 * 3600)

        for date_str, is_start in [(date_start, True), (date_end, False)]:
            if not date_str:
                continue
            try:
                ts = taipei_day_start(date_str)
                if is_start:
                    if start_ts is None or ts > start_ts:
                        start_ts = ts
                else:
                    end_ts = ts + (24 * 3600 - 1)
            except ValueError:
                logger.warning("Invalid date format: %s", date_str)

        return start_ts, end_ts

    async def query_memory(
        self,
        identity: AgentIdentity,
        query: str = "",
        limit: int = 5,
        tags: list[str] | None = None,
        importance: str | None = None,
        importance_min: str | None = None,
        emotions: list[str] | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
        is_recent: bool = False,
        include_sources: bool = False,
        include_history: bool = False,
        source_resolver: ISourceResolver | None = None,
    ) -> str:
        """綜合執行向量與事實庫檢索；include_sources 時每筆附一行來源（只講狀態，唯讀）.

        include_history 時每筆附上被新記憶取代前的版本（只從這次的有效結果往回）。有安全標記
        的記憶附一行提示、排在沒標記的後面。向量庫取不到時只回事實庫的結果並註明。
        ``source_resolver`` 是可信參數：只由 Proxy 記憶閘道傳入，模型帶了會被拒絕。
        """
        factual = self.get_factual_store()

        start_ts, end_ts = self.normalize_time_range(date_start, date_end, is_recent)

        q_lower = query.lower().strip()
        auto_recent = not q_lower or any(k in q_lower for k in ["recent", "最新", "最近"])

        facts = factual.query_facts(
            identity=identity,
            query="" if auto_recent else query,
            tags_any=tags,
            importance=importance or importance_min,
            emotions_any=emotions,
            date_start=start_ts,
            date_end=end_ts,
        )

        vectors = []
        semantic_down = False
        if query and not auto_recent:
            try:
                hits = self.get_vector_store().query_vectors(
                    identity=identity,
                    query=query,
                    limit=limit,
                    tags_any=tags,
                    importance=importance or importance_min,
                    emotions_any=emotions,
                    date_start=start_ts,
                    date_end=end_ts,
                )
            except Exception:  # 向量庫取不到：只回事實庫（同樣只有有效的）並註明
                logger.exception("Vector store unavailable; answering from the fact store only")
                hits, semantic_down = [], True
            vectors = live_semantic_hits(factual, identity, hits)  # 封存或不存在的不回傳
        down_note = f"\n{SEMANTIC_DOWN_NOTE}" if semantic_down else ""

        seen_ids = set()
        candidates = []

        # 排序：將標記為 recent 或 5 的提前；有安全標記的排到最後才補位
        prio_keys = ["recent", "5"]
        sorted_facts = sorted(facts, key=lambda x: x.get("importance") in prio_keys, reverse=True)
        for f in sorted_facts:
            if f["id"] not in seen_ids:
                seen_ids.add(f["id"])
                candidates.append(f | {"type": "Exact Memory"})

        for v in vectors:
            f_id = v.get("fact_id")
            if f_id and f_id not in seen_ids:
                seen_ids.add(f_id)
                candidates.append(v | {"type": "Semantic Memory"})
        merged = flagged_last(candidates)[:limit]

        if not merged:
            return "No relevant memories found." + down_note

        output = ["=== 🧠 檢索到的記憶 ==="]
        for i, res in enumerate(merged, 1):
            d_str = format_taipei(res["created_at"], with_time=False)  # 與 continuity 同時區
            imp = res.get("importance") or "3"
            t_str = f" [標籤: {', '.join(res.get('tags', []))}]" if res.get("tags") else ""
            e_str = f" [情感:{','.join(res.get('emotions', []))}]" if res.get("emotions") else ""
            sc = f" (信心度: {res['_score']:.2f})" if "_score" in res else ""
            meta = f"({d_str}) [重要度:{imp}]{t_str}{e_str}"
            fact_id = res.get("fact_id") if res["type"] == "Semantic Memory" else res.get("id")
            id_label = f" [ID: {fact_id}]" if fact_id else ""
            line = f"{i}. {meta}{id_label}\n   「{res['content']}」{sc}"
            if res.get("safety_flags"):
                line += f"\n   {SAFETY_FLAG_NOTE}"
            if include_sources:
                line += "\n   " + await describe_sources(res, identity, source_resolver)
            if include_history and fact_id:
                line += describe_history(factual, identity, fact_id)
            output.append(line)

        # 來源行與舊版本都在界線裡（整段是歷史資料）；語意半失效的註明在界線外
        return wrap_historical_data("memory_query", "\n".join(output)) + down_note

    def find_duplicates(self, identity: AgentIdentity) -> list[dict[str, Any]]:
        """掃描最近的事實，列出向量相似度達門檻的重複候選（每對只列一次）.

        取不到向量庫或查詢失敗丟 ``SemanticSearchUnavailableError``：沒掃成不能說成沒有重複。
        """
        factual_facts = self.get_factual_store().query_facts(identity=identity)
        active = {fact["id"]: fact for fact in factual_facts}  # 候選回查事實庫用
        facts = factual_facts[:MAX_DUPLICATE_SCAN_FACTS]
        if not facts:
            return []
        try:
            vector = self.get_vector_store()
        except Exception as exc:
            logger.exception("Vector store unavailable; the duplicate scan cannot run")
            raise SemanticSearchUnavailableError from exc
        duplicates: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        for fact in facts:
            similar_items = vector.query_vectors(
                identity=identity, query=fact["content"], limit=DUPLICATE_CANDIDATES_PER_FACT
            )
            for similar in similar_items:
                similar_id = similar.get("fact_id")
                score = similar.get("_score", 0)
                if not similar_id or similar_id == fact["id"] or similar_id not in active:
                    continue  # 自己、已封存或事實庫沒有的候選都不列
                if score < duplicate_similarity_threshold(get_embedding_model_name()):
                    continue
                pair = (min(fact["id"], similar_id), max(fact["id"], similar_id))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                duplicates.append(
                    {
                        "id1": fact["id"],
                        "content1": fact["content"],
                        "importance1": fact.get("importance"),
                        "emotions1": fact.get("emotions"),
                        "id2": similar_id,
                        "content2": similar.get("content", ""),
                        "importance2": similar.get("importance"),
                        "emotions2": similar.get("emotions"),
                        "score": round(score, 3),
                    }
                )
        return duplicates

    def resolve_duplicate(self, identity: AgentIdentity, fact_id_to_archive: str) -> str:
        """解決重複記憶：封存其中一項（已被取代的舊版本會一併從歷史拿掉）.

        資料庫寫入失敗丟 ``FactWriteError``（閘道回結構化錯誤），不說成查無。本來就封存、
        也不在歷史裡的回資訊性的成功：沒有改動，不記事件、不動索引。
        """
        factual = self.get_factual_store()
        result = factual.archive_fact(identity, fact_id_to_archive)
        if result is ArchiveResult.NOT_FOUND:
            return f"❌ Fact ID '{fact_id_to_archive}' not found."
        if result is ArchiveResult.ALREADY_ARCHIVED:
            return f"Memory {fact_id_to_archive} was already archived; nothing changed."
        record_memory_event(identity, "memory_archived", fact_id_to_archive)  # 封存已提交

        try:
            vector = self.get_vector_store()
        except Exception:  # 事實庫已封存為準；語意結果一律回查事實庫，舊向量不會再出現
            logger.exception("Vector store unavailable; %s waits for reindex", fact_id_to_archive)
            vector = None
        archive_in_index(factual, vector, identity, fact_id_to_archive)  # 沒封存到就等 reindex
        if result is ArchiveResult.DETACHED:
            return (
                f"✅ Memory {fact_id_to_archive} was an archived older version; it has been"
                " removed from the version history."
            )
        return f"✅ Memory {fact_id_to_archive} has been archived."

"""記憶庫診斷工具 (Memory Diagnostics).

提供代理人記憶空間分析、向量庫健康檢查以及冗餘記憶掃描等功能。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server import get_app_data_dir
from memory_server.config_utils import get_resource_proxy
from memory_server.factual import EmbeddingState, find_unmigrated_jsonl_agents
from memory_server.services.index_state import reindex_facts
from memory_server.services.lifecycle import supersede_memory
from memory_server.utils.memory_worker import run_memory_job

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.models.source_ref import SourceRef
    from memory_server.services.manager import MemoryManager

logger = logging.getLogger(__name__)


def normalize_agent_id(identity: AgentIdentity, manager: MemoryManager) -> str:
    """將代理人身分歸一化為正式 ID。"""
    return manager.normalize_agent_id(identity)


async def memory_diagnostics(
    identity: AgentIdentity, manager: MemoryManager, **kwargs: Any
) -> str:
    """獲取您的記憶庫健康與空間分析報告。"""
    _ = kwargs  # 避免 Unused argument 警告
    agent_id = normalize_agent_id(identity, manager)
    parts = [f"=== Memory & Storage Diagnostics for [ {agent_id} ] ==="]

    def get_dir_size(path: Path) -> float:
        """計算目錄總大小 (MB)。"""
        if not path.exists():
            return 0.0
        total_size = sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()
        )
        return total_size / (1024 * 1024)  # MB

    app_data = Path(get_app_data_dir())
    parts.append(f"Legacy Vault Size: {get_dir_size(app_data / 'legacy' / agent_id):.2f} MB")
    parts.append(
        f"Personal Drive Size: {get_dir_size(app_data / 'personal_drive' / agent_id):.2f} MB"
    )

    try:
        count = await run_memory_job(
            lambda: manager.get_vector_store().agent_point_count(identity)
        )
        parts.append(f"[Brain] Active Memory Nodes (Vector Count): {count}")
    except Exception as e:
        parts.append(f"[Brain] Active Memory Nodes: (Check Failed: {e})")

    # 只列事實庫記的各狀態筆數，不抽查向量庫
    try:
        counts = await run_memory_job(
            lambda: manager.get_factual_store().count_embedding_states(identity)
        )
    except Exception:
        logger.warning("[Diag] cannot count semantic index states", exc_info=True)
        parts.append("[Index] Semantic index state: unavailable (memory store not readable)")
    else:
        summary = ", ".join(f"{state} {count}" for state, count in sorted(counts.items()))
        parts.append(f"[Index] Semantic index of active facts: {summary or 'no active facts'}")
        pending = sum(count for state, count in counts.items() if state != EmbeddingState.READY)
        if pending:
            parts.append(
                f"[Index] {pending} fact(s) not confirmed in the semantic index; "
                'memory_tools(action="reindex") checks them and fills in what is missing.'
            )

    # 安全標記：只列有效記憶的筆數（查詢排後並附註、隨機回想略過）
    try:
        flagged = await run_memory_job(
            lambda: manager.get_factual_store().count_flagged_facts(identity)
        )
    except Exception:
        logger.warning("[Diag] cannot count flagged memories", exc_info=True)
        parts.append(
            "[Safety] Flagged memories: unavailable (memory store schema not current or not "
            "readable)"
        )
    else:
        parts.append(
            f"[Safety] Active memories flagged as containing instruction-like text: {flagged}"
        )
        if flagged:
            parts.append(
                "[Safety] Queries list them last with a note; random recall skips them."
            )

    if {identity.agent_id, agent_id} & set(find_unmigrated_jsonl_agents()):
        parts.append(
            "[Legacy Facts] An old fact/facts.jsonl was never moved into the memory "
            "database; the file is left untouched."
        )

    history_file = app_data / "stats" / f"{agent_id}_search_history.jsonl"
    if history_file.exists():
        query_counts: dict[str, int] = {}
        try:
            content = get_resource_proxy().read_file_text(str(history_file))
            if content:
                for line in content.splitlines():
                    if line.strip():
                        q = json.loads(line).get("query", "").strip().lower()
                        if q:
                            query_counts[q] = query_counts.get(q, 0) + 1
            if query_counts:
                parts.append("\n[Search] Search History Analysis:")
                top_queries = sorted(
                    query_counts.items(), key=lambda x: x[1], reverse=True
                )[:5]
                for idx, (word, count) in enumerate(top_queries, 1):
                    parts.append(f"  {idx}. '{word}' (Searched {count} times)")
        except Exception as e:
            parts.append(f"\n[Search] Search History: Error analyzing logs ({e})")
    else:
        parts.append("\n[Search] Search History: No search records found yet.")

    return "\n".join(parts)


async def reindex_memories(
    identity: AgentIdentity, manager: MemoryManager, **kwargs: Any
) -> str:
    """手動補語意索引：只處理您自己、還沒確認在索引裡的記憶。"""
    _ = kwargs
    report = await run_memory_job(
        lambda: reindex_facts(manager.get_factual_store(), manager.get_vector_store(), identity)
    )
    reply = (
        f"Reindex checked {report.checked} fact(s): {report.already_indexed} already indexed, "
        f"{report.embedded} re-embedded, {report.failed} could not be indexed; "
        f"{report.remaining} still waiting."
    )
    if report.archived_synced or report.archived_failed:
        reply += (
            f" Archived memories left active in the index: {report.archived_synced} archived "
            f"there now, {report.archived_failed} could not be synced."
        )
    return reply


async def manage_duplicate_memories(
    identity: AgentIdentity,
    manager: MemoryManager,
    action: str = "scan",
    target_id: str | None = None,  # noqa: IDENTITY_SSOT
    content: str | None = None,
    tags: list[str] | None = None,
    importance: str | None = None,
    emotions: list[str] | None = None,
    source_context: SourceRef | None = None,
    **kwargs: Any,
) -> str:
    """管理高度相似的冗餘記憶；supersede 用新內容一步取代 target_id 那筆（舊的保留並連到新的）。

    ``source_context`` 是可信參數：只由 Proxy 記憶閘道依執行資訊組成，模型帶了會被拒絕。
    """
    _ = kwargs

    if action == "supersede":
        if not target_id or not content:
            return "Error: supersede needs 'target_id' and 'content'."
        return await run_memory_job(
            supersede_memory,
            manager,
            identity,
            target_id,
            content,
            tags=tags,
            importance=importance,
            emotions=emotions,
            source_context=source_context,
        )

    if action == "scan":
        dups = await run_memory_job(manager.find_duplicates, identity)
        if not dups:
            return "Scan complete: No significant duplicates found."

        output = [f"=== Detected {len(dups)} Potential Duplicate Pair(s) ==="]
        for i, pair in enumerate(dups, 1):
            output.append(f"Match Group {i} (Similarity: {pair['score']:.2f})")

            # Helper to format metadata
            def fmt_meta(imp: Any, emo: Any) -> str:
                """格式化重要性與情緒後設資料。"""
                parts = []
                if imp:
                    parts.append(f"Importance: {imp}")
                if emo:
                    emo_str = ", ".join(emo) if isinstance(emo, list) else str(emo)
                    parts.append(f"Emotions: {emo_str}")
                return f" [{' | '.join(parts)}]" if parts else ""

            meta_a = fmt_meta(pair.get("importance1"), pair.get("emotions1"))
            output.append(f"  - [A] ID: {pair['id1']}{meta_a}\n    Content: {pair['content1']}")
            
            meta_b = fmt_meta(pair.get("importance2"), pair.get("emotions2"))
            output.append(f"  - [B] ID: {pair['id2']}{meta_b}\n    Content: {pair['content2']}")
        return "\n".join(output)

    if action == "resolve":
        if not target_id:
            return "Error: You must provide a 'target_id'."
        return await run_memory_job(manager.resolve_duplicate, identity, target_id)

    return f"Error: Unknown action '{action}'."

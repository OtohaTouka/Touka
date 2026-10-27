"""刪除靈魂前的記憶檢查：還有記憶就擋下，引導先用每靈魂重置清掉。

刪除靈魂只清人格、設定與註冊，不清記憶；直接刪會留下孤兒記憶，同名新靈魂也會接手舊記憶。
事實庫任何狀態的列（含已封存、待補索引）或向量 collection 還在（空的也算）都算有記憶。
記憶系統不可用時無法確認，一律擋下（不能確認就不刪）。活動紀錄（可信活動事件）屬「記憶」
類別，同樣要先重置，否則同名新靈魂會接手舊靈魂的時間軸。
"""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from memory_server.utils.memory_worker import run_memory_job

from src.core.exceptions import ErrorCode, ToukaException
from src.services import memory_collection_guard
from src.utils.continuity_events_db import count_agent_events

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def _memory_manager() -> Any:
    """取得記憶管理器（經整合橋接，與重置原語同一個實例）。"""
    return importlib.import_module("memory_server.tools").get_manager()


def _memory_left(identity: AgentIdentity) -> tuple[dict[str, int], bool]:
    """記憶殘留數與 collection 在不在（在記憶工作者上跑）."""
    manager = _memory_manager()
    store = manager.get_factual_store()
    others = memory_collection_guard.colliding_ids(
        identity, memory_collection_guard.known_memory_ids(store)
    )
    if others:  # 撞名：collection 是共用的，只數不屬於別人任何事實的點、不看 collection 在不在
        keep = memory_collection_guard.point_ids_of(store, others)
        outside = manager.get_vector_store().count_points_outside(identity, keep)
        return {"facts": store.count_agent_facts(identity), "vectors": outside}, False
    counts = manager.count_agent_memory(identity)
    return counts, manager.get_vector_store().has_agent_collection(identity)


async def assert_memory_cleared(identity: AgentIdentity) -> None:
    """還有記憶就 409 ``SOUL_MEMORY_NOT_RESET``；無法確認就 503 ``SOUL_MEMORY_CHECK_FAILED``.

    記憶庫在記憶工作者上讀，不卡住主迴圈（ADR-0288）。

    Args:
        identity: 要刪除的靈魂（只用 agent_id 查記憶）。
    """
    try:
        counts, has_collection = await run_memory_job(_memory_left, identity)
    except Exception as exc:
        logger.warning("Soul delete blocked: cannot confirm %s has no memory", identity.agent_id)
        raise ToukaException(ErrorCode.SOUL_MEMORY_CHECK_FAILED) from exc
    facts, vectors = int(counts.get("facts", 0)), int(counts.get("vectors", 0))
    if facts or vectors or has_collection:
        logger.warning(
            "AUDIT: soul delete blocked for %s: memory remains (facts=%d vectors=%d collection=%s)",
            identity.agent_id, facts, vectors, has_collection,
        )  # fmt: skip
        raise ToukaException(
            ErrorCode.SOUL_MEMORY_NOT_RESET,
            facts=facts,
            vectors=vectors,
            vector_collection=has_collection,
        )


async def assert_timeline_cleared(identity: AgentIdentity) -> None:
    """還有活動紀錄就 409 ``SOUL_MEMORY_NOT_RESET``；讀不到就 503 ``SOUL_MEMORY_CHECK_FAILED``.

    Args:
        identity: 要刪除的靈魂（只用 agent_id 查活動事件）。
    """
    try:
        events = await count_agent_events(identity)
    except Exception as exc:
        logger.warning(
            "Soul delete blocked: cannot confirm %s has no activity records", identity.agent_id
        )
        raise ToukaException(ErrorCode.SOUL_MEMORY_CHECK_FAILED) from exc
    if events:
        logger.warning(
            "AUDIT: soul delete blocked for %s: activity records remain (events=%d)",
            identity.agent_id,
            events,
        )
        raise ToukaException(ErrorCode.SOUL_MEMORY_NOT_RESET, activity_events=events)

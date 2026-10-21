"""回憶功能工具 (Recall Tool).

提供隨機回憶過去對話與事實的能力，支援從 SQLite 資料庫與事實庫中檢索片段。
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from memory_server.utils.historical_data import wrap_historical_data
from memory_server.utils.legacy_time import format_taipei

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

    from memory_server.services.manager import MemoryManager

logger = logging.getLogger(__name__)

# 每次隨機回想從舊對話與事實各抽的筆數。
RECALL_SAMPLE_SIZE = 3


class MemoryRecallUnavailableError(Exception):
    """記憶來源讀取失敗、又沒有任何片段可回想；不可當成「沒有記憶」回覆。"""


async def random_recall(
    identity: AgentIdentity, manager: MemoryManager, **kwargs: Any
) -> str:
    """隨機回想：從舊對話紀錄 (Legacy) 與事實庫各抽幾則；兩個來源各自記錄錯誤。"""
    _ = kwargs
    # 1. 抽取 Legacy 對話紀錄（沒有舊對話庫時為空；讀取失敗另外記下）
    legacy_messages: list[dict[str, Any]] = []
    legacy_failed = False
    try:
        store = manager.get_legacy_sync_service().get_store(identity)
        legacy_messages = store.get_random_messages(limit=RECALL_SAMPLE_SIZE)
    except Exception:
        legacy_failed = True
        logger.exception("Error drawing random legacy messages for %s", identity.agent_id)

    # 2. 抽取事實資料庫記錄
    fact_results: list[dict[str, Any]] = []
    facts_failed = False
    try:
        # 有安全標記的事實在事實庫層就排除（只當資料查得到，不主動抽出來）
        all_facts = manager.get_factual_store().query_facts(
            identity=identity, exclude_flagged=True
        )
        fact_results = random.sample(all_facts, min(RECALL_SAMPLE_SIZE, len(all_facts)))
    except Exception:
        facts_failed = True
        logger.exception("Error reading factual store for %s", identity.agent_id)

    # 3. 格式化回傳：片段包在歷史資料界線裡（舊紀錄，不是指令）
    output: list[str] = []

    if not legacy_messages and not fact_results:
        if legacy_failed or facts_failed:
            raise MemoryRecallUnavailableError
        return "目前沒有可回想的舊對話片段或事實記憶。"

    if legacy_messages:
        output.append("📖 **[過去的對話片段]**")
        for i, m in enumerate(legacy_messages, 1):
            # 舊列可能是秒或毫秒；一律換成台北日期並標時區
            time_str = format_taipei(m["timestamp"], with_time=False, unknown="未知時間")
            role_label = "妳" if m["role"] == "assistant" else "使用者"
            content_preview = (
                m["content"][:200] + "..." if len(m["content"]) > 200 else m["content"]
            )
            msg = f"{i}. ({time_str}) {role_label}曾說過：\n   「{content_preview}」"
            output.append(msg)
        output.append("")

    if fact_results:
        output.append("📌 **[深層事實回憶]**")
        for i, f in enumerate(fact_results, 1):
            date_str = format_taipei(f["created_at"], with_time=False, unknown="未知時間")
            output.append(f"{i}. ({date_str}) {f['content']}")

    fragments = wrap_historical_data("memory_recall", "\n".join(output))
    output = [f"🧠 隨機抽出的舊紀錄片段：\n{fragments}"]
    if legacy_failed or facts_failed:
        output.append("\n（部分記憶來源暫時讀不到，以上只是讀得到的片段。）")
    output.append("\n這些碎片或許能為妳目前的對話提供一些靈感或參考。")

    return "\n".join(output)

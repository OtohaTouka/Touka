"""向量 collection 名稱撞名的判定（清除預檢、快照、一般重置、建立靈魂與自訂模型共用）.

collection 名稱把非英數字元換成底線，所以 ``a-b`` 與 ``a_b`` 會共用同一個 collection；不分大小寫的
檔案系統上 ``Jin`` 與 ``jin`` 也會落到同一個資料夾。這裡只判定、不改命名。判斷只依賴事實庫
（不載入向量模組），讀不到就讓呼叫端拒絕（不能確認就不放行）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_server.factual import FactualStore, MemoryScrubError
from memory_server.utils.collection_names import (
    collection_name_for,
    fact_point_id,
    names_fold_equal,
)

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.agent_service import agent_service
from src.utils import custom_io, soul_io

if TYPE_CHECKING:
    from collections.abc import Iterable


def known_memory_ids(store: FactualStore, *, ignore: frozenset[str] = frozenset()) -> set[str]:
    """已知的 ID：靈魂、自訂模型、agents_registry 的鍵、事實庫裡的 agent_id（四個來源的聯集）.

    ignore 只從前三個名稱來源扣掉（改名時的舊 ID）；事實庫不扣：改名不搬記憶，舊 ID 的記憶還在。
    """
    names = (
        set(soul_io.list_soul_ids())
        | set(custom_io.list_custom_ids())
        | set(agent_service.registered_ids())
    )
    return (names - set(ignore)) | store.list_agent_ids()


def colliding_ids(identity: AgentIdentity, known: Iterable[str]) -> set[str]:
    """Known 裡和目標撞到同一個 collection 名稱的其他 ID（完全相同的 ID 不算）."""
    target = collection_name_for(identity)
    return {
        other
        for other in known
        if other != identity.agent_id
        and names_fold_equal(collection_name_for(AgentIdentity(agent_id=other)), target)
    }


def point_ids_of(store: FactualStore, agent_ids: Iterable[str]) -> set[str]:
    """這些 ID 的所有事實（任何狀態）對應的向量點 ID."""
    return {
        fact_point_id(fact_id)
        for other in agent_ids
        for fact_id in store.agent_fact_ids(AgentIdentity(agent_id=other))
    }


def assert_id_free(identity: AgentIdentity, *, ignore: frozenset[str] = frozenset()) -> None:
    """建立靈魂或自訂模型前：新 ID 會和既有 ID 共用 collection 就拒絕；讀不到事實庫也拒絕.

    ignore：改名時排除被改名的自己。
    """
    try:
        known = known_memory_ids(FactualStore.shared(), ignore=ignore)
    except (MemoryScrubError, OSError, ValueError) as exc:
        raise ToukaException(ErrorCode.AGENT_ID_CHECK_FAILED) from exc
    others = colliding_ids(identity, known)
    if others:
        raise ToukaException(
            ErrorCode.AGENT_ID_COLLECTION_COLLISION, collides_with=sorted(others)[0]
        )

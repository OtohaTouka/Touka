"""向量 collection 名稱與點 ID 的純函式（不載入 qdrant／嵌入模型，Proxy 端判斷撞名也用這裡）.

`VectorStore.get_collection_name` 與快照清除、建立靈魂時的撞名判定共用同一條規則：
agent_id 裡非英數字元換成底線，所以 `a-b` 與 `a_b` 會對到同一個 collection。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

QDRANT_DIR_NAME = "_qdrant_db"  # vault 底下的 Qdrant 本機模式資料夾
QDRANT_META_FILE = "meta.json"  # Qdrant 本機模式記 collection 清單的檔
QDRANT_COLLECTION_DIR = "collection"  # 各 collection 資料夾的上一層
COLLECTION_PREFIX = "memory_"


def fact_point_id(fact_id: str) -> str:
    """一筆事實在向量庫的固定 point id（重寫同一筆會覆蓋，不會重複）."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, fact_id))


def collection_name_for(identity: AgentIdentity) -> str:
    """身分對應的 collection 名稱：非英數字元（含 Unicode 判定）換成底線."""
    return COLLECTION_PREFIX + "".join(c if c.isalnum() else "_" for c in identity.agent_id)


def names_fold_equal(first: str, second: str) -> bool:
    """兩個名稱在不分大小寫的檔案系統上是否可能是同一個（相等、casefold 或 upper 任一相同）.

    只用 casefold 會漏掉土耳其文的 ı／i（upper 相同），只用 upper 會漏掉 ς／σ 與 K（U+212A）。
    """
    return (
        first == second or first.casefold() == second.casefold() or first.upper() == second.upper()
    )

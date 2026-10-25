"""合成靈魂、合成對話與決定性替身編碼器（只寫進呼叫端給的沙盒根之下）。

合成靈魂不是 DebugAgent：人格情境要測的是靈魂本身。所有寫入（靈魂檔、session DB、記憶）
都在 pytest 沙盒或人工 probe 的程序專屬暫存沙盒；本模組不讀也不寫實體資料。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.models.identity import AgentIdentity
from src.services.continuity_events import event_id
from src.utils.continuity_events_db import EventRow

from tests.memory_eval.scenarios import PERSONA_TEXT, SOUL_ID

if TYPE_CHECKING:
    from pathlib import Path

    from tests.memory_eval.scenarios import Scenario

ENCODER_DIM = 256
# 替身編碼器在產品裡的嵌入模型名稱（實測腳本固定套用，不看 .env）。不含 e5：不加前綴，
# 查詢下限與查重門檻取產品對這個名稱的預設值。
STUB_EMBEDDING_MODEL = "touka-eval-stub-ngram"
DRIVER_USER_ID = "eval-human-owner"
SOUL_CONFIG = {"is_toukasoul": True, "enabled_tools": ["memory_tools", "mission_tools"]}
_MS = 1000
_HISTORY_STEP_MS = 30 * _MS  # 預載歷史相鄰節點相隔 30 秒
_EARLY_FACT_ROUND = 3
_E5_PREFIX = re.compile(r"^\s*(?:query|passage):\s*")


class SeedOutsideSandbox(ValueError):
    """寫入目標不在沙盒根之下：拒絕（不寫任何東西）。"""

    def __init__(self) -> None:
        super().__init__("memory eval seed target is outside the sandbox root")


def soul_id_for(number: int) -> str:
    """第 ``number`` 個情境用的合成靈魂 id（英數字）：每個情境一個新靈魂，記憶、對話、家對話、
    時間軸都不和前一個情境共用。"""
    return f"{SOUL_ID}{number:03d}"


def soul_identity(soul_id: str) -> AgentIdentity:
    """合成靈魂本身（前景代打後的執行身分）。"""
    return AgentIdentity.for_role(agent_id=soul_id, role="Owner", is_human=False)


def driver_identity(soul_id: str) -> AgentIdentity:
    """驅動對話的合成人類擁有者：Monitor 會照前景規則代打成靈魂（ADR-0154）。"""
    return AgentIdentity.for_role(
        agent_id=DRIVER_USER_ID, role="normal", is_human=True, owned_agents=[soul_id]
    )


def write_synthetic_soul(app_data_dir: Path, soul_id: str, *, sandbox_root: Path) -> Path:
    """在 ``<app_data_dir>/soul/<soul_id>`` 寫合成人格與設定；目標必須在沙盒根之下。"""
    soul_dir = (app_data_dir / "soul" / soul_id).resolve()
    if not soul_dir.is_relative_to(sandbox_root.resolve()):
        raise SeedOutsideSandbox
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(PERSONA_TEXT, encoding="utf-8")
    (soul_dir / "config.json").write_text(json.dumps(SOUL_CONFIG), encoding="utf-8")
    return soul_dir


def seed_identity_map(target: Path, example: Path, *, sandbox_root: Path) -> None:
    """沙盒的身分對映換成 tracked ``.example``（靈魂才會解析成 Owner）；目標須在沙盒根之下。"""
    if not target.resolve().is_relative_to(sandbox_root.resolve()):
        raise SeedOutsideSandbox
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


class _Vector(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class DeterministicEncoder:
    """SentenceTransformer 介面子集的決定性替身：字元 1／2-gram 雜湊袋、L2 正規化。

    不下載、不讀快取；共用字元多的句子相似度高（改述召回仍有訊號），同一句永遠同一向量。
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_sentence_embedding_dimension(self) -> int:
        return ENCODER_DIM

    def encode(self, text: str, **_kwargs: object) -> _Vector:
        # e5 的 query:／passage: 前綴不算內容（否則前綴本身會主導相似度）
        compact = "".join(_E5_PREFIX.sub("", text.casefold()).split())
        grams = [*compact, *(compact[i : i + 2] for i in range(len(compact) - 1))]
        vector = [0.0] * ENCODER_DIM
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest()
            vector[int.from_bytes(digest, "big") % ENCODER_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return _Vector(v / norm for v in vector)


def new_tree() -> dict[str, Any]:
    return {"messages": {}, "currentId": None}


def append_node(tree: dict[str, Any], role: str, content: str, stamp_ms: int | None) -> None:
    """在目前分支尾端接一個節點（前端對話樹形狀；有時間就帶後端毫秒時間戳）。"""
    node_id = f"n-{uuid.uuid4().hex[:12]}"
    parent = tree["currentId"]
    node: dict[str, Any] = {
        "id": node_id, "role": role, "content": content, "parentId": parent, "childrenIds": [],
    }
    if stamp_ms is not None:
        node["timestamp"] = stamp_ms
    if parent:
        tree["messages"][parent]["childrenIds"].append(node_id)
    tree["messages"][node_id] = node
    tree["currentId"] = node_id


def _history_pairs(scenario: Scenario) -> list[tuple[str, str]]:
    seed = scenario.seed
    pairs = list(seed.history)
    for i in range(1, seed.history_rounds + 1):
        user = seed.early_fact if i == _EARLY_FACT_ROUND and seed.early_fact else (
            f"第 {i} 輪：今天又整理了一箱舊書，編號 {i}。"
        )
        pairs += [("user", user), ("assistant", f"收到第 {i} 輪，辛苦了。")]
    return pairs


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """要植入的東西（純資料）：目前 session 的樹、家對話的樹、事實、前情摘要。"""

    session_tree: dict[str, Any]
    home_tree: dict[str, Any]
    facts: tuple[str, ...]
    summary: str


def plan_seed(scenario: Scenario, now_ms: int) -> SeedPlan:
    """依情境排出合成資料；有 ``last_turn_age_s`` 時最後一個節點落在 now - age。"""
    pairs = _history_pairs(scenario)
    age = scenario.seed.last_turn_age_s
    session_tree = new_tree()
    for index, (role, content) in enumerate(pairs):
        stamp = None
        if age is not None:
            stamp = now_ms - age * _MS - (len(pairs) - 1 - index) * _HISTORY_STEP_MS
        append_node(session_tree, role, content, stamp)
    home_tree = new_tree()
    for role, content in scenario.seed.home:
        append_node(home_tree, role, content, None)
    return SeedPlan(session_tree, home_tree, scenario.seed.facts, scenario.seed.summary)


def plan_timeline(scenario: Scenario, now_ms: int, soul_id: str) -> tuple[EventRow, ...]:
    """情境預放的活動事件：合成靈魂名下、產品的事件 id 規則，都記在另一個較早的對話。

    人類訊息不在這個情境的對話裡，所以不會被當成「這段對話上一輪」。
    """
    session_id = f"memory-eval-{scenario.id}-earlier"
    rows = []
    for event in scenario.seed.timeline:
        occurred = now_ms - event.age_s * _MS
        rows.append(EventRow(
            event_id=event_id(event.kind, soul_id, event.subject, occurred), agent=soul_id,
            kind=event.kind, occurred_at=occurred, subject=event.subject,
            session_id=session_id, metadata=dict(event.metadata),
        ))
    return tuple(rows)

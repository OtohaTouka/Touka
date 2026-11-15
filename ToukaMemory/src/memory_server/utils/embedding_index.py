"""嵌入模型和語意索引對不對得上（ADR-0315）.

不匯入 qdrant、sentence-transformers：Proxy 啟動時就能用，不載入向量模組。

- 索引備註：建立或重建 collection 時把用的模型記在 collection metadata（``index_marker``）。
- 判定一個 collection（``judge``）：有備註比名稱；沒備註比向量長度
  （``KNOWN_EMBEDDING_DIMENSIONS``），長度不同就是不符，長度相同只有表裡只有這個模型是這個長度
  才算相符；其他判斷不了。
  模型載入後的抽查（``services/index_check``）可以補「判斷不了」的，結果只放記憶體。
- 側欄的狀態（``refresh_index_status``）：每次即時讀 meta.json 與「有記憶的 agent」，不建構事實庫；
  讀不到就保留上一次結果。
- 換模型的評估（``evaluate_target``）：只用當下即時讀到的名稱與長度判定，不用狀態、不用抽查；
  證據缺、讀不到都回「讀不到」，由呼叫端當成要確認。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import ValidationError

from memory_server.config_utils import (
    get_current_epoch,
    get_embedding_model_name,
    get_resource_proxy,
)
from memory_server.utils.collection_names import (
    QDRANT_DIR_NAME,
    QDRANT_META_FILE,
    collection_name_for_agent_id,
)
from memory_server.utils.qdrant_meta import CollectionConfigView, QdrantLocalMeta

if TYPE_CHECKING:
    from collections.abc import Iterable

    from memory_server.factual import FactualStore

logger = logging.getLogger(__name__)

INDEX_MODEL_KEY = "touka_embedding_model"  # collection metadata：建立時設定的模型名稱
INDEX_DIM_KEY = "touka_embedding_dim"  # collection metadata：向量長度
_DEFAULT_REPO_OWNER = "sentence-transformers"  # 短名（沒有 /）時 sentence-transformers 補的組織
# 向量長度：2026-11-15 讀 Hugging Face 官方模型頁。只給沒有備註的舊索引判斷用。
KNOWN_EMBEDDING_DIMENSIONS = MappingProxyType(
    {
        "sentence-transformers/all-MiniLM-L6-v2": 384,
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
        "intfloat/multilingual-e5-base": 768,
    }
)


def resolve_repo_id(model_name: str) -> str:
    """SentenceTransformer 名稱 → Hugging Face repo id（沒有 ``/`` 時補上預設組織）."""
    name = (model_name or "").strip()
    return name if "/" in name else f"{_DEFAULT_REPO_OWNER}/{name}"


def _canonical(model_name: str) -> str:
    return resolve_repo_id(model_name).casefold()


def same_model(first: str, second: str) -> bool:
    """兩個名稱是不是同一個模型：短名與全名、大小寫、前後空白都不算不同."""
    return _canonical(first) == _canonical(second)


_DIMENSIONS = {_canonical(name): dim for name, dim in KNOWN_EMBEDDING_DIMENSIONS.items()}
_MODELS_PER_DIMENSION = Counter(KNOWN_EMBEDDING_DIMENSIONS.values())


def known_dimension(model_name: str) -> int | None:
    """表裡的模型回它的向量長度；表外的回 None（不載入模型、不連網）."""
    return _DIMENSIONS.get(_canonical(model_name))


def index_marker(model_name: str, dim: int) -> dict[str, object]:
    """建立或重建 collection 時寫進 metadata 的備註."""
    return {INDEX_MODEL_KEY: (model_name or "").strip(), INDEX_DIM_KEY: dim}


class Verdict(StrEnum):
    """一個 collection 對某個模型的判定."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class Basis(StrEnum):
    """判定依據；側欄提示依它選句子."""

    NAME = "name"  # 索引備註的名稱
    LENGTH = "length"  # 向量長度
    SAMPLE = "sample"  # 模型載入後抽幾筆重算


@dataclass(frozen=True)
class IndexEntry:
    """meta.json 裡一個 collection：名稱、向量長度、備註的模型（沒有就是 None）."""

    name: str
    dim: int | None
    model: str | None


class IndexUnreadableError(Exception):
    """meta.json 讀不到或看不懂（不是「沒有這個檔」）."""

    def __init__(self) -> None:
        super().__init__("The vector index list (meta.json) cannot be read")


def parse_index_entries(text: str) -> dict[str, IndexEntry]:
    """解析 meta.json 的內容；格式不對丟 ``IndexUnreadableError``."""
    try:
        meta = QdrantLocalMeta.model_validate_json(text)
        views = {
            name: CollectionConfigView.model_validate(config)
            for name, config in meta.collections.items()
        }
    except ValidationError as exc:
        raise IndexUnreadableError from exc
    return {
        name: IndexEntry(name=name, dim=view.size, model=view.text_field(INDEX_MODEL_KEY))
        for name, view in views.items()
    }


def read_index_entries(vault_dir: Path | str) -> dict[str, IndexEntry] | None:
    """經資源代理讀 vault 底下的 meta.json；沒有這個檔回 None，讀不到或看不懂丟例外."""
    path = str(Path(vault_dir) / QDRANT_DIR_NAME / QDRANT_META_FILE)
    proxy = get_resource_proxy()
    if not proxy.exists(path):
        return None
    try:
        text = proxy.read_file_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexUnreadableError from exc
    return parse_index_entries(text)


def judge(model_name: str, entry: IndexEntry) -> tuple[Verdict, Basis | None]:
    """一個 collection 對某個模型：有備註比名稱；沒備註比長度；其他判斷不了."""
    if entry.model is not None:
        verdict = Verdict.MATCH if same_model(model_name, entry.model) else Verdict.MISMATCH
        return verdict, Basis.NAME
    expected = known_dimension(model_name)
    if expected is None or entry.dim is None:
        return Verdict.UNKNOWN, None
    if expected != entry.dim:
        return Verdict.MISMATCH, Basis.LENGTH
    if _MODELS_PER_DIMENSION[expected] == 1:
        return Verdict.MATCH, Basis.LENGTH
    return Verdict.UNKNOWN, None


def relevant_entries(
    entries: dict[str, IndexEntry], agent_ids: Iterable[str]
) -> list[IndexEntry]:
    """只留還有記憶的 agent 的 collection（刪掉的靈魂留下的舊索引不算），依名稱排序."""
    names = {collection_name_for_agent_id(agent_id) for agent_id in agent_ids}
    return [entries[name] for name in sorted(names) if name in entries]


# --- 側欄的狀態 ------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionResult:
    """一個 collection 對目前設定的模型的判定."""

    entry: IndexEntry
    verdict: Verdict
    basis: Basis | None


@dataclass(frozen=True)
class IndexStatus:
    """目前設定的模型和每個有記憶的靈魂索引的判定（不含 agent id）."""

    configured_model: str
    results: tuple[CollectionResult, ...]

    def notice_fields(self) -> dict[str, object] | None:
        """側欄提示的欄位；沒有不符就是 None。不符的模型取最常見的那個."""
        mismatched = [result for result in self.results if result.verdict is Verdict.MISMATCH]
        if not mismatched:
            return None
        labels = Counter(
            (result.entry.model, result.entry.dim, result.basis) for result in mismatched
        )
        (index_model, index_dim, basis), _count = labels.most_common(1)[0]
        return {
            "count": len(mismatched),
            "basis": basis.value if basis else None,
            "configured_model": self.configured_model,
            "index_model": index_model,
            "index_dim": index_dim,
        }


@dataclass(frozen=True)
class _SampleVerdict:
    model: str  # 抽查時設定的模型（正規化後）
    entry: IndexEntry  # 抽查時 meta.json 的條目；條目變了就不沿用
    verdict: Verdict
    basis: Basis


_state_lock = threading.Lock()  # 狀態、計算時間、過期標記與抽查結果都用這一把
_status: IndexStatus | None = None
_computed_at: float | None = None
_stale = False
_unreadable_logged = False
_samples: dict[str, _SampleVerdict] = {}


def _result(model_name: str, entry: IndexEntry) -> CollectionResult:
    verdict, basis = judge(model_name, entry)
    if verdict is Verdict.UNKNOWN:
        with _state_lock:
            sample = _samples.get(entry.name)
        if sample and sample.model == _canonical(model_name) and sample.entry == entry:
            return CollectionResult(entry, sample.verdict, sample.basis)
    return CollectionResult(entry, verdict, basis)


def refresh_index_status(factual: FactualStore | None) -> IndexStatus | None:
    """即時重算側欄的狀態；不建構事實庫，沒有實例或讀不到就保留上一次結果."""
    global _status, _computed_at, _stale, _unreadable_logged
    if factual is None:
        return current_index_status()
    model = get_embedding_model_name()
    try:
        agent_ids = factual.agent_ids_with_facts()
        entries = read_index_entries(factual.base_dir) or {}
    except Exception:  # 讀不到不能當成「都對得上」：保留上一次結果，同一段失敗只記一次
        with _state_lock:
            first = not _unreadable_logged
            _unreadable_logged = True
        if first:
            logger.warning(
                "[Embedding] could not read the memory store or the vector index list "
                "(meta.json); keeping the last embedding model check",
                exc_info=True,
            )
        return current_index_status()
    status = IndexStatus(
        configured_model=model,
        results=tuple(_result(model, entry) for entry in relevant_entries(entries, agent_ids)),
    )
    with _state_lock:
        _status, _computed_at, _stale, _unreadable_logged = (
            status,
            get_current_epoch(),
            False,
            False,
        )
    return status


def current_index_status() -> IndexStatus | None:
    """上一次算好的狀態（可能還沒有）."""
    with _state_lock:
        return _status


def fresh_index_status(max_age_s: float) -> IndexStatus | None:
    """``max_age_s`` 秒內算過、而且沒被標成過期時回狀態，否則回 None（要重算）."""
    with _state_lock:
        if _status is None or _computed_at is None or _stale:
            return None
        if get_current_epoch() - _computed_at > max_age_s:
            return None
        return _status


def mark_index_status_stale() -> None:
    """換模型後呼叫：下一次問一定重算（結果保留，重算前照樣可讀）."""
    global _stale
    with _state_lock:
        _stale = True


def record_sample_verdicts(
    model_name: str, verdicts: dict[str, tuple[IndexEntry, Verdict, Basis]]
) -> None:
    """記下模型載入後的抽查結果（只在記憶體，只補「判斷不了」的）."""
    canonical = _canonical(model_name)
    with _state_lock:
        for name, (entry, verdict, basis) in verdicts.items():
            _samples[name] = _SampleVerdict(canonical, entry, verdict, basis)


def reset_index_status() -> None:
    """清掉狀態與抽查結果（測試隔離用）."""
    global _status, _computed_at, _stale, _unreadable_logged
    with _state_lock:
        _status, _computed_at, _stale, _unreadable_logged = None, None, False, False
        _samples.clear()


# --- 換模型的評估（一律即時） ------------------------------------------------------


class TargetOutcome(StrEnum):
    """換成某個模型時，即時讀到的證據說了什麼."""

    NO_MEMORIES = "no_memories"  # 事實庫讀得到、沒有任何記憶
    NO_INDEX = "no_index"  # 有記憶，但這些靈魂在向量庫裡都沒有索引
    MATCH = "match"  # 有記憶的靈魂索引至少一個、而且全部確定相符
    MISMATCH = "mismatch"  # 任一索引確定不符
    UNKNOWN = "unknown"  # 有判斷不了的
    UNREADABLE = "unreadable"  # 事實庫或 meta.json 讀不到、meta.json 不存在


@dataclass(frozen=True)
class TargetEvaluation:
    """評估結果；``index_model``／``index_dim`` 給精靈警告寫記憶實際用的模型."""

    outcome: TargetOutcome
    index_model: str | None = None
    index_dim: int | None = None


def evaluate_target(model_name: str, factual: FactualStore) -> TargetEvaluation:
    """換成 ``model_name`` 時即時評估；不用側欄狀態、不用抽查結果."""
    try:
        agent_ids = factual.agent_ids_with_facts()
        if not agent_ids:
            return TargetEvaluation(TargetOutcome.NO_MEMORIES)
        entries = read_index_entries(factual.base_dir)
    except Exception:  # 讀不到就當成證據缺：呼叫端要確認
        logger.warning("[Embedding] switch check could not read memories or the index list")
        return TargetEvaluation(TargetOutcome.UNREADABLE)
    if entries is None:
        return TargetEvaluation(TargetOutcome.UNREADABLE)
    relevant = relevant_entries(entries, agent_ids)
    if not relevant:
        return TargetEvaluation(TargetOutcome.NO_INDEX)
    judged = [(entry, judge(model_name, entry)[0]) for entry in relevant]
    mismatched = [entry for entry, verdict in judged if verdict is Verdict.MISMATCH]
    labels = Counter((entry.model, entry.dim) for entry in mismatched or relevant)
    index_model, index_dim = labels.most_common(1)[0][0]
    if mismatched:
        outcome = TargetOutcome.MISMATCH
    elif all(verdict is Verdict.MATCH for _entry, verdict in judged):
        outcome = TargetOutcome.MATCH
    else:
        outcome = TargetOutcome.UNKNOWN
    return TargetEvaluation(outcome, index_model, index_dim)

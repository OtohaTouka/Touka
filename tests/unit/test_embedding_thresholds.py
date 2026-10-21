"""嵌入模型的門檻依模型查表；校準用的人工 probe 前綴與產品相同、資料檔完整、不會被自動跑到.

語意查重門檻（``services/lifecycle.py``）與查詢下限（``vector.py``）各有一張表，鍵是
``vector.embedding_family``——和「加不加 ``query:``／``passage:`` 前綴」同一套判斷，所以前綴和
門檻永遠一致。e5 家族的值來自人工 probe（``tests/smoke/manual/embedding_threshold_probe.py``，
合成句子跑 e5-base）；其他模型（含 MiniLM）維持原本的 0.92／0.3。呼叫端只把常數換成查表。

probe 不載入專案程式，所以自己複製了一份 e5 的 ``query:``／``passage:`` 前綴；這裡核對那份和
``memory_server.vector._format_for_embedding`` 相同。自動測試不載模型、不 import probe：
probe 的常數用 AST 讀；向量庫一律用 autospec 替身或替身編碼器，只寫 tmp 沙盒。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, create_autospec

import pytest
from memory_server import vector as vector_module
from memory_server.factual import FactualStore
from memory_server.services import lifecycle
from memory_server.services.manager import MemoryManager
from memory_server.vector import VectorStore, _format_for_embedding
from src.models.identity import AgentIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT =Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "smoke" / "manual" / "embedding_threshold_probe.py"
PAIRS = ROOT / "tests" / "fixtures" / "embedding_threshold_pairs.json"
CATEGORIES = ("equivalent", "update", "contradiction", "related", "unrelated")
E5 = "intfloat/multilingual-e5-base"
MINILM = "all-MiniLM-L6-v2"
E5_DUPLICATE, E5_FLOOR = 0.93, 0.65  # 人工 probe 2026-10-15 的校準結果
DEFAULT_DUPLICATE, DEFAULT_FLOOR = 0.92, 0.3


def _probe_constants() -> dict[str, object]:
    """probe 模組層的字面量常數（只解析原始碼，不執行）."""
    tree = ast.parse(PROBE.read_text(encoding="utf-8"), filename=str(PROBE))
    values: dict[str, object] = {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and node.value is not None:
            try:
                values[target.id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


def _probe_import_roots() -> set[str]:
    tree = ast.parse(PROBE.read_text(encoding="utf-8"), filename=str(PROBE))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0] if node.level == 0 else ".")
    return roots


def test_probe_prefixes_match_the_product_formatter() -> None:
    constants = _probe_constants()
    model = constants["MODEL_ID"]
    assert model == "intfloat/multilingual-e5-base"
    prefixes = ((True, constants["QUERY_PREFIX"]), (False, constants["PASSAGE_PREFIX"]))
    for is_query, prefix in prefixes:
        assert _format_for_embedding(model, "合成句子", is_query=is_query) == f"{prefix}合成句子"


def test_probe_downloads_only_the_pinned_safetensors_set() -> None:
    constants = _probe_constants()
    files = constants["MODEL_FILES"]
    assert re.fullmatch(r"[0-9a-f]{40}", constants["MODEL_REVISION"])
    assert "model.safetensors" in files
    assert len(files) == len(set(files)) == 9
    assert not [name for name in files if name.endswith((".bin", ".onnx", ".xml"))]
    assert not [name for name in files if name.startswith(("onnx/", "openvino/"))]
    assert constants["MAX_DOWNLOAD_BYTES"] <= 1_200_000_000
    assert constants["DOWNLOAD_FLAG"] == "--human-approved-model-download"


def test_probe_loads_no_project_code_or_secrets() -> None:
    """禁令 63：probe 不載入專案程式；也不讀 .env、不碰廠商 SDK."""
    roots = _probe_import_roots()
    banned = {".", "src", "memory_server", "tests", "dotenv", "anthropic", "openai", "httpx"}
    assert not roots & banned
    assert ".env" not in PROBE.read_text(encoding="utf-8")


def test_probe_is_manual_only() -> None:
    """不符合 pytest 與 smoke 的自動找檔規則；manifest 登記成人工、需要人類同意."""
    assert not PROBE.name.startswith(("test_", "smoke_"))
    manifest = ast.parse((ROOT / "tests" / "smoke" / "manifest.py").read_text(encoding="utf-8"))
    manual = next(
        node.value
        for node in manifest.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MANUAL_LIVE_SPECS"
    )
    paths = {
        keyword.value.value
        for call in ast.walk(manual)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
        if keyword.arg == "path"
    }
    assert "manual/embedding_threshold_probe.py" in paths


def test_pairs_cover_five_categories_in_three_languages() -> None:
    data = json.loads(PAIRS.read_text(encoding="utf-8"))
    groups = data["groups"]
    assert set(data["categories"]) == set(CATEGORIES)
    assert len({group["id"] for group in groups}) == len(groups)
    for lang in ("zh", "en", "mixed"):
        assert sum(group["lang"] == lang for group in groups) >= 10, lang
    sentences: list[str] = []
    for group in groups:
        assert group["memory"].strip()
        assert group["queries"], group["id"]
        for category in CATEGORIES:
            assert group[category], (group["id"], category)
        sentences += [group["memory"], *group["queries"]]
        sentences += [text for category in CATEGORIES for text in group[category]]
    assert all(text == text.strip() and text for text in sentences)
    assert len(sentences) == len(set(sentences)), "同一句出現兩次"


# --- 依模型查表 -------------------------------------------------------------------

# (模型名稱, 加不加 e5 前綴)：門檻和前綴用同一套判斷，加前綴的一律用 e5 的校準值
MODEL_NAMES = [
    (E5, True),
    ("  INTFLOAT/Multilingual-E5-Base\n", True),
    ("multilingual-e5-base", True),  # 沒有 org：載入時會被補成另一個 repo，但判斷照前綴
    ("intfloat/multilingual-e5-large", True),  # e5 其他尺寸沒另外量，沿用 e5-base 的值
    (MINILM, False),
    ("paraphrase-multilingual-MiniLM-L12-v2", False),
    ("", False),
    ("sandbox-encoder", False),
    ("unknown/model", False),
]


@pytest.mark.parametrize(("model", "prefixed"), MODEL_NAMES)
def test_thresholds_follow_the_same_family_as_the_prefix(model: str, prefixed: bool) -> None:
    assert (_format_for_embedding(model, "x", is_query=True) != "x") is prefixed
    assert (_format_for_embedding(model, "x", is_query=False) != "x") is prefixed
    duplicate, floor = (E5_DUPLICATE, E5_FLOOR) if prefixed else (DEFAULT_DUPLICATE, DEFAULT_FLOOR)
    assert lifecycle.duplicate_similarity_threshold(model) == duplicate
    assert vector_module.query_score_floor(model) == floor
    assert (vector_module.embedding_family(model) is not None) is prefixed


def test_unknown_models_keep_the_old_values() -> None:
    assert lifecycle.DEFAULT_DUPLICATE_SIMILARITY_THRESHOLD == DEFAULT_DUPLICATE
    assert vector_module.DEFAULT_QUERY_SCORE_FLOOR == DEFAULT_FLOOR


def test_every_family_floor_is_below_its_duplicate_threshold() -> None:
    """查重的候選先經過查詢下限；下限不低於查重門檻時，查重永遠擋不到."""
    families = {vector_module.embedding_family(model) for model, _prefixed in MODEL_NAMES}
    tables = (lifecycle.DUPLICATE_SIMILARITY_THRESHOLDS, vector_module.QUERY_SCORE_FLOORS)
    assert all(set(table) <= families for table in tables), "表的鍵必須是 embedding_family 的值"
    for model, _prefixed in MODEL_NAMES:
        floor = vector_module.query_score_floor(model)
        assert floor < lifecycle.duplicate_similarity_threshold(model), model


def test_calibrated_family_matches_the_probe() -> None:
    """表裡校準過的只有 probe 量的那個模型所屬的家族."""
    measured = {vector_module.embedding_family(_probe_constants()["MODEL_ID"])}
    assert set(lifecycle.DUPLICATE_SIMILARITY_THRESHOLDS) == measured
    assert set(vector_module.QUERY_SCORE_FLOORS) == measured


# --- 呼叫端：存記憶的查重、重複掃描、語意查詢 ---------------------------------------

# (模型, 最像的那筆分數, 存記憶時擋不擋, 重複掃描列不列)
DUPLICATE_CASES = [
    (E5, 0.925, False, False),  # 舊門檻 0.92 會擋；e5 校準後不擋
    (E5, 0.935, True, True),
    (MINILM, 0.925, True, True),  # 沒校準的模型照舊
    (MINILM, 0.915, False, False),
    ("unknown/model", 0.921, True, True),
]


class _Embedding(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class _SandboxEncoder:
    """固定結果的替身編碼器；不下載任何東西."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_sentence_embedding_dimension(self) -> int:
        return 32

    def encode(self, text: str) -> _Embedding:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return _Embedding((byte - 127.5) / 127.5 for byte in digest)


def _identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"Threshold{uuid.uuid4().hex[:8]}", role="Owner")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FactualStore:
    factual = FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


@pytest.fixture
def vector(monkeypatch: pytest.MonkeyPatch) -> Any:
    stub = create_autospec(VectorStore, instance=True)
    stub.query_vectors.return_value = []
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", stub)
    return stub


@pytest.mark.parametrize(("model", "score", "blocked", "_listed"), DUPLICATE_CASES)
def test_saving_uses_the_threshold_of_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    store: FactualStore,
    vector: Any,
    model: str,
    score: float,
    blocked: bool,
    _listed: bool,
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    identity = _identity()
    old = store.save_fact(identity, "the user drinks green tea")["id"]
    vector.query_vectors.return_value = [{"fact_id": old, "_score": score}]

    checked = lifecycle.check_new_memory(MemoryManager(), identity, "the user loves green tea")

    assert (checked.blocked is not None) is blocked
    if blocked:
        assert old in checked.blocked


@pytest.mark.parametrize(("model", "score", "_blocked", "listed"), DUPLICATE_CASES)
def test_duplicate_scan_uses_the_threshold_of_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    store: FactualStore,
    vector: Any,
    model: str,
    score: float,
    _blocked: bool,
    listed: bool,
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    identity = _identity()
    first = store.save_fact(identity, "the user drinks green tea")["id"]
    second = store.save_fact(identity, "the user loves green tea")["id"]
    other = {"the user drinks green tea": second, "the user loves green tea": first}

    def similar(*, query: str, **_kwargs: object) -> list[dict[str, Any]]:
        return [{"fact_id": other[query], "content": query, "_score": score}]

    vector.query_vectors.side_effect = similar

    duplicates = MemoryManager().find_duplicates(identity)

    assert len(duplicates) == (1 if listed else 0)


@pytest.fixture
def real_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """真 VectorStore（替身編碼器、tmp Qdrant）；Qdrant 的查詢換成記錄參數的替身."""
    monkeypatch.setattr(vector_module, "SentenceTransformer", _SandboxEncoder)
    directory = tmp_path / "vectors"

    def build(model: str) -> VectorStore:
        built = VectorStore(base_dir=directory, model_name=model)
        built.client = MagicMock()
        built.client.collection_exists.return_value = True
        built.client.query_points.return_value = SimpleNamespace(points=[])
        return built

    yield build
    VectorStore._client_cache.pop(str((directory / "_qdrant_db").absolute())).close()


QUERY_FLOOR_CASES = [(E5, E5_FLOOR), (MINILM, DEFAULT_FLOOR), ("sandbox-encoder", DEFAULT_FLOOR)]


@pytest.mark.parametrize(("model", "floor"), QUERY_FLOOR_CASES)
def test_query_floor_follows_the_store_model(real_vector: Any, model: str, floor: float) -> None:
    built = real_vector(model)

    built.query_vectors(_identity(), "what does the user drink")

    assert built.client.query_points.call_args.kwargs["score_threshold"] == floor


def test_explicit_query_floor_is_passed_through(real_vector: Any) -> None:
    built = real_vector(E5)

    built.query_vectors(_identity(), "what does the user drink", score_threshold=0.5)

    assert built.client.query_points.call_args.kwargs["score_threshold"] == 0.5

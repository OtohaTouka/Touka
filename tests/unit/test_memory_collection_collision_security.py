"""collection 名稱撞名的兩道防護（G 組，對照設計 §2-4、§12-2）.

① 一般重置撞名時只刪目標的點、自檢只數不屬於別人的點；② 建立靈魂與自訂模型時拒絕撞名的 ID。
合成 ID、會話沙箱的記憶庫；每個測試用不同的 ID，避免共用的 Qdrant 互相干擾。
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import pytest
from memory_server import factual as fm
from memory_server.tools import get_manager
from memory_server.utils import collection_names as cn
from qdrant_client.models import Distance, PointStruct, VectorParams
from src.core.exceptions import ErrorCode, ToukaException
from src.models.custom_models import CustomModelRenameRequest, CustomModelUpdateRequest
from src.models.identity import AgentIdentity
from src.models.souls import SoulConfig, SoulCreateRequest
from src.routers import custom_models as custom_router
from src.routers import souls as souls_router
from src.services import agent_service as agent_service_module
from src.services import memory_collection_guard as guard
from src.services.agent_reset import delete_guard
from src.services.agent_reset import primitives as P
from src.services.agent_service import agent_service
from src.services.resource_manager import resource_manager
from src.utils import custom_io, soul_io

_SEQ = itertools.count()
ADMIN = AgentIdentity.for_role(agent_id="zz-admin-g", role="admin", is_human=True)


def _pair() -> tuple[str, str]:
    n = next(_SEQ)
    return f"zz-g{n}-a-b", f"zz_g{n}_a_b"


def _open(agent_id: str) -> str:
    return agent_id


def _seed(agent_id: str, facts: list[str]) -> list[str]:
    """存事實並把向量點直接放進（共用的）collection；回傳 fact_id."""
    manager = get_manager()
    store = manager.get_factual_store()
    identity = AgentIdentity(agent_id=agent_id)
    ids = [store.save_fact(identity, text)["id"] for text in facts]
    client = manager.get_vector_store().client
    name = cn.collection_name_for(identity)
    if not client.collection_exists(name):
        client.create_collection(
            name, vectors_config=VectorParams(size=4, distance=Distance.COSINE)
        )
    client.upsert(
        name,
        points=[
            PointStruct(
                id=cn.fact_point_id(fid), vector=[0.1, 0.2, 0.3, 0.4], payload={"fact_id": fid}
            )
            for fid in ids
        ],
    )
    return ids


def _seed_points(agent_id: str, fact_ids: list[str]) -> None:
    """替已存在的事實補上向量點（共用的 collection）."""
    client = get_manager().get_vector_store().client
    client.upsert(
        cn.collection_name_for(AgentIdentity(agent_id=agent_id)),
        points=[
            PointStruct(id=cn.fact_point_id(fid), vector=[0.4, 0.3, 0.2, 0.1], payload={})
            for fid in fact_ids
        ],
    )


def _points(agent_id: str) -> set[str]:
    client = get_manager().get_vector_store().client
    name = cn.collection_name_for(AgentIdentity(agent_id=agent_id))
    if not client.collection_exists(name):
        return set()
    records, _ = client.scroll(name, limit=1000, with_payload=False, with_vectors=False)
    return {str(r.id) for r in records}


# --- ① 一般重置 ---------------------------------------------------------------


async def test_g01_shared_collection_reset_only_removes_the_target_points() -> None:
    target, other = _pair()
    mine = _seed(target, ["target one", "target two"])
    theirs = _seed(other, ["their memory"])
    assert await P.purge_memory(target, _open) == 4
    assert _points(other) == {cn.fact_point_id(fid) for fid in theirs}
    assert not _points(target) & {cn.fact_point_id(fid) for fid in mine}
    assert get_manager().get_factual_store().count_agent_facts(AgentIdentity(agent_id=other)) == 1
    assert await P.remaining_memory(target, _open) == 0


async def test_g02_orphan_point_in_a_shared_collection_counts_as_remaining() -> None:
    target, other = _pair()
    _seed(target, ["target"])
    _seed(other, ["theirs"])
    await P.purge_memory(target, _open)
    client = get_manager().get_vector_store().client
    client.upsert(
        cn.collection_name_for(AgentIdentity(agent_id=target)),
        points=[
            PointStruct(
                id=cn.fact_point_id("fact_orphan_g02"), vector=[0.1, 0.1, 0.1, 0.1], payload={}
            )
        ],
    )
    assert await P.remaining_memory(target, _open) == 1


async def test_g03_no_collision_keeps_the_original_path(monkeypatch: pytest.MonkeyPatch) -> None:
    target = f"zz-g03-{next(_SEQ)}"
    _seed(target, ["solo"])
    manager = get_manager()
    called: list[str] = []
    real = manager.purge_agent

    def spy(identity: AgentIdentity) -> dict[str, int]:
        called.append(identity.agent_id)
        return real(identity)

    monkeypatch.setattr(manager, "purge_agent", spy)
    assert await P.purge_memory(target, _open) == 2
    assert called == [target]


@pytest.mark.parametrize(
    ("first", "second"),
    [("zz-Case", "zz-case"), ("zzς", "zzσ"), ("zzı", "zzi"), ("zzK", "zzk"), ("zz-a-b", "zz_a_b")],
)
def test_g04_fold_collisions_are_detected(first: str, second: str) -> None:
    assert guard.colliding_ids(AgentIdentity(agent_id=first), {second, "zz-unrelated"}) == {second}
    assert guard.colliding_ids(AgentIdentity(agent_id=first), {first}) == set()


async def test_g05_detection_error_never_falls_back_to_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = f"zz-g05-{next(_SEQ)}"
    _seed(target, ["x"])
    manager = get_manager()
    touched: list[str] = []
    monkeypatch.setattr(manager, "purge_agent", lambda identity: touched.append("purge_agent"))
    monkeypatch.setattr(
        manager.get_vector_store(), "delete_points", lambda *a: touched.append("delete_points")
    )

    def broken(self: object) -> set[str]:
        raise fm.MemoryScrubError(fm.ScrubFailure.UNREADABLE)

    monkeypatch.setattr(fm.FactualStore, "list_agent_ids", broken)
    with pytest.raises(fm.MemoryScrubError):
        await P.purge_memory(target, _open)
    assert touched == []
    assert _points(target)


async def test_g10_partial_vector_failure_keeps_facts_for_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, other = _pair()
    _seed(target, ["target"])
    _seed(other, ["theirs"])
    vector = get_manager().get_vector_store()
    real = vector.delete_points

    def fail_once(*args: object) -> int:
        monkeypatch.setattr(vector, "delete_points", real)
        raise RuntimeError("vector store hiccup")

    monkeypatch.setattr(vector, "delete_points", fail_once)
    with pytest.raises(RuntimeError):
        await P.purge_memory(target, _open)
    assert get_manager().get_factual_store().count_agent_facts(AgentIdentity(agent_id=target)) == 1
    await P.purge_memory(target, _open)
    assert await P.remaining_memory(target, _open) == 0


# --- ② 建立時拒絕 ---------------------------------------------------------------


def _sources(
    monkeypatch: pytest.MonkeyPatch, *, souls=(), customs=(), registry=(), facts=()
) -> None:
    monkeypatch.setattr(soul_io, "list_soul_ids", lambda: list(souls))
    monkeypatch.setattr(custom_io, "list_custom_ids", lambda: list(customs))
    monkeypatch.setattr(agent_service, "registered_ids", lambda: set(registry))
    monkeypatch.setattr(fm.FactualStore, "list_agent_ids", lambda self: set(facts))


async def _create_soul(agent_id: str) -> None:
    await souls_router.create_soul(
        SoulCreateRequest(agent_id=agent_id, config=SoulConfig(base_model=None)), ADMIN
    )


@pytest.mark.parametrize(
    "source",
    ["souls", "customs", "registry", "facts"],
)
async def test_g06_soul_creation_refuses_colliding_ids(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    new_id = f"zz-g06-{next(_SEQ)}-x"
    existing = new_id.replace("-", "_")
    _sources(monkeypatch, **{source: [existing]})
    soul_dir = Path(resource_manager.get_base_path()) / "soul" / new_id
    with pytest.raises(ToukaException) as exc:
        await _create_soul(new_id)
    assert exc.value.code is ErrorCode.AGENT_ID_COLLECTION_COLLISION
    assert exc.value.detail["collides_with"] == existing
    assert not soul_dir.exists()


async def test_g06_fold_collision_and_check_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, souls=["zz-G06-Case"])
    with pytest.raises(ToukaException) as exc:
        await _create_soul("zz-g06-case")
    assert exc.value.code is ErrorCode.AGENT_ID_COLLECTION_COLLISION

    def unreadable(self: object) -> set[str]:
        raise fm.MemoryScrubError(fm.ScrubFailure.UNREADABLE)

    monkeypatch.setattr(fm.FactualStore, "list_agent_ids", unreadable)
    with pytest.raises(ToukaException) as exc:
        await _create_soul("zz-g06-other")
    assert exc.value.code is ErrorCode.AGENT_ID_CHECK_FAILED


def test_g06_vector_layer_is_not_needed_for_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch)

    def broken(*_a: object, **_k: object) -> object:
        raise RuntimeError("vector layer down")

    monkeypatch.setattr(type(get_manager()), "get_vector_store", broken)
    guard.assert_id_free(AgentIdentity(agent_id="zz-g06-fine"))


async def test_g06_check_runs_before_the_existing_soul_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(guard, "assert_id_free", lambda identity, **_k: order.append("collision"))
    real_exists = Path.exists

    def exists(self: Path) -> bool:
        if self.parent.name == "soul":
            order.append("exists")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(souls_router, "validate_no_symlink_escape", lambda *_a: False)
    with pytest.raises(ToukaException):
        await _create_soul(f"zz-g06-order-{next(_SEQ)}")
    assert order[:1] == ["collision"]


async def test_g07_exact_existing_soul_keeps_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = f"zz-g07-{next(_SEQ)}"
    _sources(monkeypatch, souls=[agent_id])
    soul_dir = Path(resource_manager.get_base_path()) / "soul" / agent_id
    soul_dir.mkdir(parents=True)
    try:
        with pytest.raises(ToukaException) as exc:
            await _create_soul(agent_id)
        assert exc.value.code is ErrorCode.SOUL_ALREADY_EXISTS
    finally:
        soul_dir.rmdir()


async def test_g08_custom_models_refuse_colliding_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, souls=["zz_g08_soul"])
    with pytest.raises(ToukaException) as exc:
        await custom_router.update_custom_model(
            "zz-g08-soul", CustomModelUpdateRequest(name="x", systemPrompt="p"), ADMIN
        )
    assert exc.value.code is ErrorCode.AGENT_ID_COLLECTION_COLLISION
    calls: list[str] = []
    monkeypatch.setattr(custom_router, "_validate_model_access", lambda *_a: calls.append("access"))
    _sources(monkeypatch, customs=["zz g08 me"], souls=["zz_g08_taken"])
    with pytest.raises(ToukaException) as exc:
        await custom_router.rename_custom_model(
            "zz g08 me", CustomModelRenameRequest(new_id="zz-g08-taken"), ADMIN
        )
    assert exc.value.code is ErrorCode.AGENT_ID_COLLECTION_COLLISION
    # 改名時排除自己：只改符號、撞到的是自己的舊 ID，不擋（後面的步驟另外處理）
    monkeypatch.setattr(
        custom_router.custom_model_service, "rename_model", lambda *_a: calls.append("renamed")
    )
    monkeypatch.setattr(custom_router.user_service, "remove_owned_agent", lambda *_a: None)
    monkeypatch.setattr(custom_router.user_service, "add_owned_agent", lambda *_a: None)
    _sources(monkeypatch, customs=["zz g08 self"])
    await custom_router.rename_custom_model(
        "zz g08 self", CustomModelRenameRequest(new_id="zz-g08-self"), ADMIN
    )
    assert "renamed" in calls


# --- D16：撞名的靈魂重置後可以刪除 -----------------------------------------------


async def test_g09_collided_soul_can_be_deleted_after_reset() -> None:
    target, other = _pair()
    _seed(target, ["mine"])
    _seed(other, ["theirs"])
    await P.purge_memory(target, _open)
    delete_guard.assert_memory_cleared(AgentIdentity(agent_id=target))
    with pytest.raises(ToukaException):
        delete_guard.assert_memory_cleared(AgentIdentity(agent_id=other))


async def test_g09b_delete_check_counts_points_nobody_owns() -> None:
    target, other = _pair()
    _seed(target, ["mine"])
    _seed(other, ["theirs"])
    await P.purge_memory(target, _open)
    get_manager().get_vector_store().client.upsert(
        cn.collection_name_for(AgentIdentity(agent_id=target)),
        points=[
            PointStruct(
                id=cn.fact_point_id("fact_orphan_g09b"), vector=[0.1, 0.1, 0.1, 0.1], payload={}
            )
        ],
    )
    with pytest.raises(ToukaException) as exc:
        delete_guard.assert_memory_cleared(AgentIdentity(agent_id=target))
    assert exc.value.code is ErrorCode.SOUL_MEMORY_NOT_RESET


async def test_g09c_superseded_versions_are_reset_and_the_other_chain_is_kept() -> None:
    target, other = _pair()
    store = get_manager().get_factual_store()
    chains = {}
    for agent_id in (target, other):
        identity = AgentIdentity(agent_id=agent_id)
        (old,) = _seed(agent_id, [f"{agent_id} old"])
        new = store.supersede_fact(identity, old, f"{agent_id} new", safety_flags=["x"])["id"]
        _seed_points(agent_id, [new])
        chains[agent_id] = (old, new)
    other_before = store.query_facts(AgentIdentity(agent_id=other), include_archived=True)
    await P.purge_memory(target, _open)
    assert store.count_agent_facts(AgentIdentity(agent_id=target)) == 0
    assert not _points(target) & {cn.fact_point_id(f) for f in chains[target]}
    delete_guard.assert_memory_cleared(AgentIdentity(agent_id=target))
    assert store.query_facts(AgentIdentity(agent_id=other), include_archived=True) == other_before
    assert {cn.fact_point_id(f) for f in chains[other]} <= _points(other)


# --- 成果審修正：改名只排除名稱來源（R21）、不卡事件迴圈（R25）、registry 讀不到就拒絕（R26） ---


async def test_g08b_rename_still_collides_with_its_own_old_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(custom_router, "_validate_model_access", lambda *_a: None)
    # 改名不搬記憶：舊 ID 在事實庫裡的記憶會和新 ID 共用 collection
    _sources(monkeypatch, customs=["zz g08 old"], facts=["zz g08 old"])
    with pytest.raises(ToukaException) as exc:
        await custom_router.rename_custom_model(
            "zz g08 old", CustomModelRenameRequest(new_id="zz-g08-old"), ADMIN
        )
    assert exc.value.code is ErrorCode.AGENT_ID_COLLECTION_COLLISION
    assert exc.value.detail["collides_with"] == "zz g08 old"


async def test_g11_creation_checks_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread, seen = threading.get_ident(), []

    def spy(identity: AgentIdentity, **_kwargs: object) -> None:
        seen.append(threading.get_ident())
        raise ToukaException(ErrorCode.AGENT_ID_CHECK_FAILED)

    monkeypatch.setattr(guard, "assert_id_free", spy)
    with pytest.raises(ToukaException):
        await _create_soul(f"zz-g11-{next(_SEQ)}")
    _sources(monkeypatch)
    with pytest.raises(ToukaException):
        await custom_router.update_custom_model(
            f"zz-g11-{next(_SEQ)}", CustomModelUpdateRequest(name="x", systemPrompt="p"), ADMIN
        )
    assert len(seen) == 2
    assert loop_thread not in seen


async def test_g12_unreadable_registry_refuses_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(soul_io, "list_soul_ids", list)
    monkeypatch.setattr(custom_io, "list_custom_ids", list)
    monkeypatch.setattr(fm.FactualStore, "list_agent_ids", lambda self: set())
    monkeypatch.setattr(
        agent_service_module.config_utils, "read_config_json_with_fallback", lambda _n: None
    )
    monkeypatch.setattr(agent_service_module.config_utils, "config_file_exists", lambda _n: True)
    with pytest.raises(ValueError):
        agent_service.registered_ids()
    with pytest.raises(ToukaException) as exc:
        await _create_soul(f"zz-g12-{next(_SEQ)}")
    assert exc.value.code is ErrorCode.AGENT_ID_CHECK_FAILED
    monkeypatch.setattr(agent_service_module.config_utils, "config_file_exists", lambda _n: False)
    assert agent_service.registered_ids() == set()  # 還沒有 registry（新安裝）不算讀不到

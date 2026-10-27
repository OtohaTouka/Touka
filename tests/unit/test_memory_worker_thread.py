"""舊審查報告乙-3：記憶讀寫不在主迴圈上跑，排在同一條專用的工作執行緒上（ADR-0288）.

做法：假的向量庫、舊對話庫（與只讀事實庫的那幾個呼叫）被呼叫時記下所在的執行緒，並等主迴圈
放行（``call_later``）。在主迴圈上跑的話主迴圈被它卡住、放行排不到，等待逾時（記成沒放行）；
放行之後才在主迴圈上跑的，由執行緒不只一條抓到。所有等待都有逾時，
退化時變紅而不是卡住（ADR-0103）。後半段測工作者本身（巢狀呼叫、誤用、contextvars、取消）
與向量庫的鎖（兩條執行緒同時第一次用，只建一次）。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest
from memory_server import factual as fm
from memory_server import tools as memory_gateway
from memory_server import vector as vector_module
from memory_server.models.conversation_search import ConversationPage
from memory_server.services import manager as manager_module
from memory_server.services.manager import MemoryManager
from memory_server.utils import memory_worker
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.routers import stats as stats_router
from src.services.agent_reset import delete_guard, primitives
from src.tools.builtin import memory as proxy_memory_gateway

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GATE_TIMEOUT_S = 2.0  # 在主迴圈上跑時，最多卡這麼久就放棄（記成沒放行）
RELEASE_AFTER_S = 0.05  # 主迴圈多久後放行
MEET_TIMEOUT_S = 0.5  # 兩條執行緒互等多久；有鎖時第二條進不來，等完各走各的
JOB_TIMEOUT_S = 5.0
SOUL = AgentIdentity(agent_id="WorkerSoul")
_MARK: contextvars.ContextVar[str] = contextvars.ContextVar("memory_worker_mark", default="unset")


class _Gate:
    """被呼叫時記下執行緒與開始、結束，等主迴圈放行."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.events: list[tuple[str, str, threading.Thread]] = []
        self.released: list[bool] = []

    def arm(self) -> None:
        asyncio.get_running_loop().call_later(RELEASE_AFTER_S, self.release.set)

    def hit(self, label: str) -> None:
        thread = threading.current_thread()
        self.events.append(("start", label, thread))
        self.released.append(self.release.wait(GATE_TIMEOUT_S))
        self.events.append(("end", label, thread))

    def threads(self) -> set[threading.Thread]:
        return {thread for _, _, thread in self.events}


@pytest.fixture
def gate() -> _Gate:
    return _Gate()


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> MemoryManager:
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: MemoryManager) -> Any:
    factual = fm.FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", factual)
    return factual


@pytest.fixture
def vector(monkeypatch: pytest.MonkeyPatch, manager: MemoryManager) -> Any:
    stub = create_autospec(VectorStore, instance=True)
    stub.query_vectors.return_value = []
    stub.save_vector.return_value = True
    stub.archive_vector_by_fact_id.return_value = True
    stub.agent_point_count.return_value = 0
    stub.has_agent_collection.return_value = False
    stub.indexed_fact_ids.return_value = set()
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", stub)
    return stub


@pytest.fixture
def legacy(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> Any:
    """舊對話庫：抽樣與查詢都經過 gate."""

    class _Store:
        db_path = "unused"

        def get_random_messages(self, limit: int) -> list[dict[str, Any]]:
            gate.hit("random")
            return []

        def query_messages(self, **_parameters: Any) -> ConversationPage:
            gate.hit("conversations")
            return ConversationPage(total=0, rows=[])

        def get_stats(self) -> dict[str, int]:
            gate.hit("stats")
            return {"message_count": 0, "session_count": 0}

    class _Service:
        def get_store(self, _identity: AgentIdentity) -> _Store:
            gate.hit("get_store")
            return _Store()

        def file_status(self, _identity: AgentIdentity) -> dict[str, int]:
            gate.hit("file_status")
            return {"processed": 0, "pending": 0}

        def sync_all_json_to_db(self, _identity: AgentIdentity) -> dict[str, Any]:
            gate.hit("sync")
            return {"synced": 0, "errors": []}

    service = _Service()
    monkeypatch.setattr(MemoryManager, "get_legacy_sync_service", lambda _self: service)
    return service


def _through(gate: _Gate, label: str, value: Any) -> Callable[..., Any]:
    def call(*_args: Any, **_kwargs: Any) -> Any:
        gate.hit(label)
        return value

    return call


def _gated(gate: _Gate, label: str, method: Callable[..., Any]) -> Callable[..., Any]:
    """真的方法前面先過 gate（事實庫的讀取用）."""

    def call(*args: Any, **kwargs: Any) -> Any:
        gate.hit(label)
        return method(*args, **kwargs)

    return call


async def _memory(action: str, **arguments: Any) -> str:
    return await proxy_memory_gateway.memory_tools(action=action, identity=SOUL, **arguments)


def _assert_off_loop_on_one_thread(gate: _Gate) -> None:
    assert gate.events, "the fake store was never called"
    assert all(gate.released), "the memory call blocked the event loop"
    (thread,) = gate.threads()
    assert thread is not threading.main_thread()


# 每個記憶動作都要碰到被 gate 的那個方法；setup 放好需要的事實並把 gate 接上
CASES: dict[str, tuple[Callable[[Any, Any, _Gate], dict[str, Any]], str]] = {}


def _case(name: str, action: str) -> Callable[..., Any]:
    def register(setup: Callable[[Any, Any, _Gate], dict[str, Any]]) -> Any:
        CASES[name] = (setup, action)
        return setup

    return register


@_case("save", "save")
def _save(_store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    vector.query_vectors.side_effect = _through(gate, "dedup", [])
    return {"content": "I like green tea"}


@_case("query", "query")
def _query(_store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    vector.query_vectors.side_effect = _through(gate, "query", [])
    return {"query": "green tea"}


@_case("scan", "manage_dups")
def _scan(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    store.save_fact(SOUL, "a fact to scan")
    vector.query_vectors.side_effect = _through(gate, "scan", [])
    return {"operation": "scan"}


@_case("supersede", "manage_dups")
def _supersede(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    old = store.save_fact(SOUL, "I live in Taipei")["id"]
    vector.query_vectors.side_effect = _through(gate, "supersede", [])
    return {"operation": "supersede", "target_id": old, "content": "I moved to Tainan"}


@_case("resolve", "manage_dups")
def _resolve(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    fact_id = store.save_fact(SOUL, "a duplicate to resolve")["id"]
    vector.archive_vector_by_fact_id.side_effect = _through(gate, "resolve", True)
    return {"operation": "resolve", "target_id": fact_id}


@_case("forget", "forget")
def _forget(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    fact_id = store.save_fact(SOUL, "a fact to forget")["id"]
    vector.archive_vector_by_fact_id.side_effect = _through(gate, "forget", True)
    return {"fact_id_to_archive": fact_id}


@_case("reindex", "reindex")
def _reindex(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    store.save_fact(SOUL, "a fact to reindex")
    vector.indexed_fact_ids.side_effect = _through(gate, "reindex", set())
    return {}


@_case("diagnostics", "diagnostics")
def _diagnostics(store: Any, vector: Any, gate: _Gate) -> dict[str, Any]:
    vector.agent_point_count.side_effect = _through(gate, "diagnostics", 0)
    for name in ("count_embedding_states", "count_flagged_facts"):
        setattr(store, name, _gated(gate, name, getattr(store, name)))
    return {}


@_case("random", "random")
def _random(store: Any, _vector: Any, gate: _Gate) -> dict[str, Any]:
    store.query_facts = _gated(gate, "random_facts", store.query_facts)
    return {}


@_case("conversations", "conversations")
def _conversations(_store: Any, _vector: Any, _gate: _Gate) -> dict[str, Any]:
    return {"source": "legacy"}


@pytest.mark.parametrize("name", sorted(CASES))
async def test_memory_actions_run_off_the_event_loop(
    name: str, store: Any, vector: Any, legacy: Any, gate: _Gate
) -> None:
    setup, action = CASES[name]
    arguments = setup(store, vector, gate)
    gate.arm()
    reply = await asyncio.wait_for(_memory(action, **arguments), timeout=10)
    assert "failed while handling" not in reply, reply
    _assert_off_loop_on_one_thread(gate)


async def test_two_memory_actions_run_one_at_a_time_on_the_same_thread(
    store: Any, vector: Any, gate: _Gate
) -> None:
    vector.query_vectors.side_effect = _through(gate, "query", [])
    gate.arm()
    await asyncio.wait_for(
        asyncio.gather(_memory("query", query="first"), _memory("query", query="second")),
        timeout=10,
    )
    _assert_off_loop_on_one_thread(gate)
    # 一次只跑一個：第二個在第一個結束之後才開始（改回多執行緒會交錯）
    assert [kind for kind, _, _ in gate.events] == ["start", "end", "start", "end"]


async def test_identical_saves_at_the_same_time_store_one_fact(store: Any, vector: Any) -> None:
    replies = await asyncio.wait_for(
        asyncio.gather(*(_memory("save", content="same words") for _ in range(4))), timeout=10
    )
    assert sum(reply.startswith("Memory successfully saved") for reply in replies) == 1
    assert len(store.query_facts(identity=SOUL)) == 1


async def test_soul_delete_memory_check_runs_off_the_event_loop(
    store: Any, vector: Any, gate: _Gate
) -> None:
    vector.has_agent_collection.side_effect = _through(gate, "delete_check", False)
    gate.arm()
    await asyncio.wait_for(delete_guard.assert_memory_cleared(SOUL), timeout=10)
    _assert_off_loop_on_one_thread(gate)


async def test_reset_purge_and_recount_run_off_the_event_loop(
    store: Any, vector: Any, gate: _Gate
) -> None:
    vector.drop_agent.side_effect = _through(gate, "purge", 0)
    vector.agent_point_count.side_effect = _through(gate, "recount", 0)
    gate.arm()
    purged = await asyncio.wait_for(
        primitives.purge_memory(SOUL.agent_id, lambda name: name), timeout=10
    )
    remaining = await asyncio.wait_for(
        primitives.remaining_memory(SOUL.agent_id, lambda name: name), timeout=10
    )
    assert (purged, remaining) == (0, 0)
    _assert_off_loop_on_one_thread(gate)


async def test_scrub_recount_from_its_own_thread_runs_on_the_memory_thread(
    store: Any, vector: Any, gate: _Gate
) -> None:
    gate.release.set()  # 清除工作自己的執行緒同步等，不需要主迴圈放行
    vector.agent_point_count.side_effect = _through(gate, "scrub_recount", 0)
    caller = threading.Thread(target=lambda: primitives.count_remaining_memory(SOUL))
    caller.start()
    caller.join(timeout=10)
    assert not caller.is_alive()
    (thread,) = gate.threads()
    assert thread is not caller
    assert thread is not threading.main_thread()


async def test_legacy_stats_endpoint_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, legacy: Any, gate: _Gate
) -> None:
    monkeypatch.setattr(stats_router.resource_manager, "is_owner", lambda *_a: True)
    gate.arm()
    result = await asyncio.wait_for(
        stats_router.get_legacy_sync_stats(SOUL.agent_id, SOUL), timeout=10
    )
    assert result["message_count"] == 0
    assert {label for _, label, _ in gate.events} == {"get_store", "stats", "file_status"}
    _assert_off_loop_on_one_thread(gate)


async def test_legacy_sync_endpoint_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, legacy: Any, gate: _Gate
) -> None:
    monkeypatch.setattr(stats_router.resource_manager, "is_owner", lambda *_a: True)
    gate.arm()
    result = await asyncio.wait_for(stats_router.trigger_legacy_sync(SOUL.agent_id, SOUL), 10)
    assert result["status"] == "success"
    _assert_off_loop_on_one_thread(gate)


# --- 記憶工作者本身 ---


def _on_worker(thread: threading.Thread) -> bool:
    return thread.name.startswith(memory_worker.MEMORY_WORKER_THREAD_NAME)


async def test_only_the_worker_thread_counts_as_inside() -> None:
    assert not memory_worker.in_memory_worker()
    assert await asyncio.wait_for(
        memory_worker.run_memory_job(memory_worker.in_memory_worker), timeout=JOB_TIMEOUT_S
    )


async def test_a_blocking_call_from_inside_a_job_runs_in_place() -> None:
    """工作裡再同步叫工作者（例如重算）直接在原地跑；排到自己後面會永遠等不到."""

    def outer() -> tuple[threading.Thread, threading.Thread]:
        inner = memory_worker.run_memory_job_blocking(threading.current_thread)
        return threading.current_thread(), inner

    outer_thread, inner_thread = await asyncio.wait_for(
        memory_worker.run_memory_job(outer), timeout=JOB_TIMEOUT_S
    )
    assert inner_thread is outer_thread
    assert _on_worker(outer_thread)


async def test_an_awaited_call_from_inside_a_job_runs_in_place() -> None:
    def outer() -> tuple[threading.Thread, threading.Thread]:
        inner = asyncio.run(memory_worker.run_memory_job(threading.current_thread))
        return threading.current_thread(), inner

    outer_thread, inner_thread = await asyncio.wait_for(
        memory_worker.run_memory_job(outer), timeout=JOB_TIMEOUT_S
    )
    assert inner_thread is outer_thread


async def test_blocking_on_the_worker_from_the_event_loop_is_refused() -> None:
    ran: list[bool] = []
    with pytest.raises(memory_worker.MemoryWorkerMisuseError):
        memory_worker.run_memory_job_blocking(ran.append, True)
    assert ran == []


def test_a_blocking_call_from_a_plain_thread_runs_on_the_worker() -> None:
    assert _on_worker(memory_worker.run_memory_job_blocking(threading.current_thread))


async def test_jobs_see_the_callers_context_and_cannot_change_it() -> None:
    _MARK.set("caller")

    def job() -> str:
        seen = _MARK.get()
        _MARK.set("job")
        return seen

    first = await asyncio.wait_for(memory_worker.run_memory_job(job), timeout=JOB_TIMEOUT_S)
    assert first == "caller"
    assert _MARK.get() == "caller"
    # 上一個工作改的值不會留給下一個工作
    second = await asyncio.wait_for(memory_worker.run_memory_job(_MARK.get), timeout=JOB_TIMEOUT_S)
    assert second == "caller"


def test_blocking_jobs_see_the_calling_threads_context() -> None:
    def caller() -> str:
        _MARK.set("scrub")
        return memory_worker.run_memory_job_blocking(_MARK.get)

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(caller).result(timeout=JOB_TIMEOUT_S) == "scrub"


async def test_a_cancelled_wait_skips_a_queued_job_but_the_running_one_finishes() -> None:
    started, release = threading.Event(), threading.Event()
    done: list[str] = []

    def running() -> None:
        started.set()
        release.wait(GATE_TIMEOUT_S)
        done.append("running")

    first = asyncio.ensure_future(memory_worker.run_memory_job(running))
    queued = asyncio.ensure_future(memory_worker.run_memory_job(done.append, "queued"))
    assert await asyncio.to_thread(started.wait, GATE_TIMEOUT_S)
    first.cancel()
    queued.cancel()
    release.set()
    for task in (first, queued):
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.wait_for(memory_worker.run_memory_job(lambda: None), timeout=JOB_TIMEOUT_S)
    assert done == ["running"]


# --- 向量庫的鎖（保險：平常只有工作者在用） ---


class _MeetingPoint:
    """兩條執行緒都到才一起走；有鎖時第二條進不來，第一條等到逾時自己走."""

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2, timeout=MEET_TIMEOUT_S)

    def meet(self) -> None:
        with contextlib.suppress(threading.BrokenBarrierError):
            self.barrier.wait()


def _twice_at_once(fn: Callable[[], Any]) -> list[Any]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fn) for _ in range(2)]
        return [future.result(timeout=JOB_TIMEOUT_S) for future in futures]


def _bare_vector_store(client: Any, model: Any = None) -> VectorStore:
    """不開 Qdrant、不載模型的向量庫：只換掉 client 與模型."""
    store = VectorStore.__new__(VectorStore)
    store.model_name = "fake-model"
    store._model = model
    store._embedding_cache = {}
    store.client = client
    return store


class _Vector:
    def tolist(self) -> list[float]:
        return [0.0, 0.0, 1.0]


class _Model:
    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, _text: str) -> _Vector:
        return _Vector()


def test_a_new_collection_is_created_once_when_two_threads_save_at_once() -> None:
    meeting = _MeetingPoint()

    class _Client:
        def __init__(self) -> None:
            self.exists, self.created = False, 0

        def collection_exists(self, _name: str) -> bool:
            meeting.meet()
            return self.exists

        def create_collection(self, **_settings: Any) -> None:
            self.created += 1
            self.exists = True

        def upsert(self, **_points: Any) -> None:
            pass

    client = _Client()
    store = _bare_vector_store(client, _Model())
    assert _twice_at_once(lambda: store.save_vector(SOUL, "fact-a", "text")) == [True, True]
    assert client.created == 1


def test_the_embedding_model_loads_once_when_two_threads_need_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting = _MeetingPoint()
    loads: list[str] = []

    class _Loader:
        def __init__(self, name: str, **_options: Any) -> None:
            meeting.meet()
            loads.append(name)

    monkeypatch.setattr(vector_module, "SentenceTransformer", _Loader)
    store = _bare_vector_store(client=None)
    first, second = _twice_at_once(lambda: store.model)
    assert first is second
    assert loads == ["fake-model"]


def test_the_vector_store_is_built_once_when_two_threads_need_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting = _MeetingPoint()
    built: list[str] = []

    class _Store:
        def __init__(self, model_name: str) -> None:
            meeting.meet()
            built.append(model_name)

    monkeypatch.setattr(manager_module, "VectorStore", _Store)
    monkeypatch.setattr(manager_module, "get_embedding_model_name", lambda: "fake-model")
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", None)
    first, second = _twice_at_once(MemoryManager().get_vector_store)
    assert first is second
    assert built == ["fake-model"]

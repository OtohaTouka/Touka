"""Legacy conversation dates: search, display, random recall and sync agree on one clock.

Rows written before the fix may hold seconds; new rows hold milliseconds. Searching by date
treats a date as one whole Taipei day, and every time shown to the model is Taipei time
labelled with its zone. Model-facing paths are exercised from the Proxy ``memory_tools``
gateway (``random`` reads the legacy database; ``query`` reads facts). Everything runs in
the pytest sandbox: tmp fact store, tmp legacy directory, autospec vector stub (ADR-0258).
"""

from __future__ import annotations

import ast
import contextlib
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from memory_server import factual as factual_module
from memory_server import tools as memory_gateway
from memory_server.factual import FactualStore
from memory_server.services import legacy_store as legacy_store_module
from memory_server.services import legacy_sync as legacy_sync_module
from memory_server.services import manager as manager_module
from memory_server.services.legacy_store import LegacyStore
from memory_server.services.legacy_sync import LegacySyncService
from memory_server.services.manager import MemoryManager
from memory_server.tools import legacy_vault, recall
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.tools.builtin import memory as proxy_memory_gateway

# 2026-05-13 00:00 Asia/Taipei == 2026-05-12 16:00 UTC
DAY_START_S = 1_778_601_600
DAY_S = 86_400
# 2026-05-13 07:30 Asia/Taipei == 2026-05-12 23:30 UTC
MORNING_S = 1_778_628_600


def _identity() -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=f"DateProbe{uuid.uuid4().hex[:8]}", role="Owner")


@pytest.fixture
def factual_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FactualStore:
    store = FactualStore(base_dir=tmp_path / "vault")
    monkeypatch.setattr(MemoryManager, "_factual_store_singleton", store)
    return store


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, factual_store: FactualStore) -> MemoryManager:
    vector = create_autospec(VectorStore, instance=True)
    vector.save_vector.return_value = True
    vector.query_vectors.return_value = []
    monkeypatch.setattr(MemoryManager, "_vector_store_singleton", vector)
    real = MemoryManager()
    monkeypatch.setattr(memory_gateway, "_global_manager", real)
    return real


@pytest.fixture
def legacy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "legacy"
    directory.mkdir()
    monkeypatch.setattr(
        MemoryManager, "get_legacy_dir", lambda _self, _identity, resource_proxy=None: directory
    )
    return directory


def _seed_raw(directory: Path, identity: AgentIdentity, rows: list[tuple[str, Any]]) -> None:
    """Write rows exactly as given, like rows stored before the fix (seconds or ms)."""
    path = str(directory / "legacy_history.db")
    store = LegacyStore(path)
    store.upsert_session("legacy-session", identity, "legacy.json", "legacy", 1, 1)
    with contextlib.closing(legacy_store_module.sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO messages (id, session_id, parent_id, role, content, timestamp, metadata)"
            " VALUES (?, 'legacy-session', NULL, 'user', ?, ?, '{}')",
            [(content, content, timestamp) for content, timestamp in rows],
        )


def _stored_timestamps(directory: Path) -> dict[str, Any]:
    path = str(directory / "legacy_history.db")
    with contextlib.closing(legacy_store_module.sqlite3.connect(path)) as conn:
        return dict(conn.execute("SELECT id, timestamp FROM messages").fetchall())


def _save_fact_at(
    store: FactualStore,
    identity: AgentIdentity,
    content: str,
    epoch_s: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factual_module, "get_current_epoch", lambda: float(epoch_s))
    assert store.save_fact(identity, content)["status"] == "success"


# --- model path A: memory_tools(action="random") reads the legacy database --------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [MORNING_S, MORNING_S * 1000], ids=["seconds", "ms"])
async def test_model_random_recall_shows_taipei_dates(
    manager: MemoryManager,
    factual_store: FactualStore,
    legacy_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored: int,
) -> None:
    identity = _identity()
    _seed_raw(legacy_dir, identity, [("the legacy line about rain", stored)])
    _save_fact_at(factual_store, identity, "the fact about green tea", MORNING_S, monkeypatch)

    result = await proxy_memory_gateway.memory_tools(action="random", identity=identity)

    assert "the legacy line about rain" in result
    assert "the fact about green tea" in result
    assert result.count("(2026-05-13 Asia/Taipei)") == 2  # legacy line and fact
    assert "1970" not in result
    assert "2026-05-12" not in result  # the UTC date of the same instant


@pytest.mark.asyncio
async def test_model_random_recall_unreadable_time_is_unknown(
    manager: MemoryManager, legacy_dir: Path
) -> None:
    identity = _identity()
    _seed_raw(legacy_dir, identity, [("a line without a usable time", "sometime")])

    result = await proxy_memory_gateway.memory_tools(action="random", identity=identity)

    assert "(未知時間) 使用者曾說過" in result


# --- model path B: memory_tools(action="query") filters facts by Taipei days -------------


@pytest.mark.asyncio
async def test_model_query_uses_whole_taipei_day_and_labels_zone(
    manager: MemoryManager, factual_store: FactualStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    for content, epoch_s in [
        ("fact-before", DAY_START_S - 1),  # 2026-05-12 23:59:59 Taipei
        ("fact-first", DAY_START_S),  # 2026-05-13 00:00:00
        ("fact-last", DAY_START_S + DAY_S - 1),  # 2026-05-13 23:59:59
        ("fact-after", DAY_START_S + DAY_S),  # 2026-05-14 00:00:00
    ]:
        _save_fact_at(factual_store, identity, content, epoch_s, monkeypatch)

    result = await proxy_memory_gateway.memory_tools(
        action="query",
        identity=identity,
        date_start="2026-05-13",
        date_end="2026-05-13",
        limit=10,
    )

    assert "fact-first" in result
    assert "fact-last" in result
    assert "fact-before" not in result
    assert "fact-after" not in result
    assert result.count("(2026-05-13 Asia/Taipei)") == 2


@pytest.mark.parametrize(
    ("date_start", "date_end", "expected"),
    [
        ("2026-05-13", "2026-05-13", (DAY_START_S, DAY_START_S + DAY_S - 1)),
        ("2026-05-13", None, (DAY_START_S, None)),
        (None, "2026-05-13", (None, DAY_START_S + DAY_S - 1)),
        ("2026-05-13", "2026-05-14", (DAY_START_S, DAY_START_S + 2 * DAY_S - 1)),
        ("2026/05/13", None, (None, None)),
        (None, None, (None, None)),
    ],
)
def test_normalize_time_range_reads_taipei_days(
    date_start: str | None, date_end: str | None, expected: tuple[int | None, int | None]
) -> None:
    manager = MemoryManager.__new__(MemoryManager)
    assert manager.normalize_time_range(date_start, date_end) == expected


def test_normalize_time_range_recent_keeps_later_start(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MemoryManager.__new__(MemoryManager)
    now_s = DAY_START_S + 10 * DAY_S
    monkeypatch.setattr(manager_module, "get_current_epoch", lambda: float(now_s))

    assert manager.normalize_time_range(None, None, True) == (now_s - 7 * DAY_S, None)
    assert manager.normalize_time_range("2026-05-22", None, True) == (
        DAY_START_S + 9 * DAY_S,
        None,
    )


# --- legacy database search (store layer and memory_tools conversations) ---------------

_DAY_ROWS: list[tuple[str, Any]] = [
    ("row-a-before", DAY_START_S * 1000 - 1),  # 2026-05-12 23:59:59.999, ms
    ("row-b-sec", DAY_START_S + 3_600),  # 2026-05-13 01:00, seconds
    ("row-c-ms", (DAY_START_S + 7_200) * 1000),  # 2026-05-13 02:00, ms
    ("row-d-last", (DAY_START_S + DAY_S - 1) * 1000 + 500),  # 23:59:59.500, ms
    ("row-e-after", (DAY_START_S + DAY_S) * 1000),  # 2026-05-14 00:00, ms
    ("row-f-after-sec", DAY_START_S + DAY_S + 60),  # 2026-05-14 00:01, seconds
]


@pytest.mark.asyncio
async def test_legacy_date_search_covers_seconds_and_ms_rows(
    manager: MemoryManager, legacy_dir: Path
) -> None:
    identity = _identity()
    _seed_raw(legacy_dir, identity, _DAY_ROWS)

    result = await legacy_vault.search_conversations(
        identity, manager, source="legacy", date_start="2026-05-13", date_end="2026-05-13"
    )

    found = [content for content, _ts in _DAY_ROWS if content in result]
    assert found == ["row-b-sec", "row-c-ms", "row-d-last"]
    # a date range reads oldest first, by the real time of each row
    assert result.index("row-b-sec") < result.index("row-c-ms") < result.index("row-d-last")
    assert "(2026-05-13 01:00 Asia/Taipei)" in result
    assert "(2026-05-13 23:59 Asia/Taipei)" in result
    assert "1970" not in result


@pytest.mark.asyncio
async def test_legacy_search_shows_seconds_row_in_taipei(
    manager: MemoryManager, legacy_dir: Path
) -> None:
    identity = _identity()
    _seed_raw(legacy_dir, identity, [("an old seconds row", MORNING_S)])

    result = await legacy_vault.search_conversations(identity, manager, source="legacy")

    assert "(2026-05-13 07:30 Asia/Taipei)" in result
    assert "1970" not in result


def test_legacy_store_orders_mixed_units_by_real_time(tmp_path: Path) -> None:
    identity = _identity()
    _seed_raw(tmp_path, identity, _DAY_ROWS)
    store = LegacyStore(str(tmp_path / "legacy_history.db"))

    page = store.query_messages(limit=10)
    newest_first = [row.message_id for row in page.rows]
    after_start = store.query_messages(date_start=(DAY_START_S + DAY_S) * 1000, limit=10)

    assert newest_first == [content for content, _ts in reversed(_DAY_ROWS)]
    assert after_start.total == 2
    assert [row.message_id for row in after_start.rows] == ["row-f-after-sec", "row-e-after"]


# --- writes: sync and upsert store milliseconds ------------------------------------------


def test_legacy_sync_stores_milliseconds(
    legacy_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_s = DAY_START_S + 30 * DAY_S
    monkeypatch.setattr(legacy_sync_module, "get_current_epoch", lambda: float(now_s))
    (legacy_dir / "chat-a.json").write_text(
        json.dumps(
            {
                "id": "session-a",
                "title": "session a",
                "messages": [
                    {"id": "m-sec", "role": "user", "content": "a", "timestamp": MORNING_S},
                    {"id": "m-ms", "role": "user", "content": "b", "timestamp": MORNING_S * 1000},
                    {"id": "m-created", "role": "user", "content": "c", "createdAt": MORNING_S},
                    {
                        "id": "m-iso",
                        "role": "user",
                        "content": "d",
                        "timestamp": "2026-05-12T23:30:00Z",
                    },
                    {"id": "m-none", "role": "user", "content": "e"},
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = create_autospec(MemoryManager, instance=True)
    manager.get_legacy_dir.return_value = legacy_dir

    result = LegacySyncService(manager).sync_all_json_to_db(_identity())

    assert (result["synced"], result["errors"]) == (1, 0)
    assert _stored_timestamps(legacy_dir) == {
        "m-sec": MORNING_S * 1000,
        "m-ms": MORNING_S * 1000,
        "m-created": MORNING_S * 1000,
        "m-iso": MORNING_S * 1000,
        "m-none": now_s * 1000,
    }


def test_legacy_upsert_stores_milliseconds(tmp_path: Path) -> None:
    identity = _identity()
    store = LegacyStore(str(tmp_path / "legacy_history.db"))
    store.upsert_session("s", identity, "f.json", "T", 1, 1)

    store.upsert_messages(
        [
            {"id": "u-sec", "session_id": "s", "role": "user", "content": "a", "timestamp": 1},
            {
                "id": "u-created",
                "session_id": "s",
                "role": "user",
                "content": "b",
                "createdAt": MORNING_S,
            },
        ]
    )

    assert _stored_timestamps(tmp_path) == {"u-sec": 1000, "u-created": MORNING_S * 1000}


# --- every presentation site renders through the shared Taipei helper -------------------


@pytest.mark.parametrize("module", [recall, manager_module, legacy_vault])
def test_presentation_sites_do_not_use_the_host_time_zone(module: Any) -> None:
    """A naive ``fromtimestamp`` or ``strptime(...).timestamp()`` follows the host zone."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        naive = node.func.attr == "fromtimestamp" and len(node.args) < 2 and not any(
            keyword.arg == "tz" for keyword in node.keywords
        )
        if naive or node.func.attr == "strptime":
            offenders.append(node.lineno)
    assert offenders == []

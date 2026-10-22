"""MCP 隔離設定檔（``mcp_server_isolation.json``）的讀取規則、鎖與寫入（ADR-0275）。

``origin`` 與 ``sandbox_paths`` 不放進 ``server_config.json``，另存一個檔、以 ``bound`` 綁定條目的
程式身分。這裡逐列釘住設計 v2.1 §1.2 的讀取表：

- 檔案不存在＝空的（這個行程沒讀到過時）；讀到過之後不見＝讀不到（LOW-e）。
- 存在卻讀不到、JSON 壞掉、頂層結構不對＝讀不到：不覆寫、全部當自建、寫入丟
  ``PERSISTENCE_ERROR``。壞 JSON 不能被當成「不存在」（MEDIUM-R3-1：``read_json`` 會這樣做）。
- 單筆看不懂的 origin 當自建；壞的 ``sandbox_paths`` 只丟那幾項。
- ``bound`` 不符：自建類照舊，第三方類與 scaffold 當沒有紀錄，``sandbox_paths`` 一律不用。

另釘：``bound`` 不含 env、寫入走 ``write_json``（原子置換）、同一行程內的鎖不掉更新、
持鎖期間不能跑協程也不能重入（``threading.Lock`` 的條件）。
"""

from __future__ import annotations

import ast
import errno
import json
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args
from unittest.mock import patch

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models import mcp_isolation as models_module
from src.models.mcp_isolation import MAX_SANDBOX_PATHS
from src.services import mcp_isolation_store as store_module
from src.services.mcp_isolation_store import (
    MCP_ISOLATION_FILENAME,
    IsolationFile,
    McpIsolationStore,
    assign,
    bound_of,
    changed_programs,
    delete,
    origin_category,
    replace_isolation,
)
from src.services.resource_manager import resource_manager
from src.utils import isolation_policy
from src.utils.bootstrap import ESSENTIAL_CONFIGS, ensure_configs


def test_changed_env_defaults_to_untrusted():
    old = {"s": {"command": "npx", "env": {"X": "old"}}}
    new = {"s": {"command": "npx", "env": {"X": "new"}}}
    assert changed_programs(old, new) == ["s"]
    assert changed_programs(old, new, human=True) == []

if TYPE_CHECKING:
    from src.services.mcp_isolation_store import IsolationRecord

_REAL_OPEN = Path.open
_ENTRY = {"command": "npx", "args": ["-y", "@acme/server"], "env": {"TOKEN": "a"}}


def _write(config_dir: Path, document: Any) -> Path:
    path = config_dir / MCP_ISOLATION_FILENAME
    path.write_text(
        document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
    )
    return path


def _record(origin: str | None, entry: dict[str, Any] = _ENTRY, **extra: Any) -> dict[str, Any]:
    return {"origin": origin, "bound": bound_of(entry), "updated_at": 1.0, **extra}


def _deny_reads(monkeypatch: pytest.MonkeyPatch, target: Path) -> set[Path]:
    """讓檔案「存在但讀不到」（等同 chmod 000）；集合清空即恢復。"""
    denied = {target.resolve()}

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if denied and self.resolve() in denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return _REAL_OPEN(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    return denied


def _snapshot_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _assert_refuses_write(store: McpIsolationStore) -> None:
    with pytest.raises(ToukaException) as raised:
        store.mutate(assign("demo", "manual", bound_of(_ENTRY), fresh=True))
    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR


# ── 三種讀取狀態（MEDIUM-R3-1） ─────────────────────────────────────────────────


def test_missing_file_reads_as_empty_and_first_write_creates_it(tmp_path: Path) -> None:
    store = McpIsolationStore(tmp_path)

    snapshot = store.load()
    assert snapshot.state == "missing"
    assert store.record_for("demo", _ENTRY).state == "none"

    store.mutate(assign("demo", "creation", bound_of(_ENTRY), fresh=True))
    assert store.record_for("demo", _ENTRY).origin == "creation"


def test_unreadable_file_is_unreadable_and_refuses_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, {"version": 1, "servers": {"demo": _record("hub")}})
    before = path.read_bytes()
    store = McpIsolationStore(tmp_path)
    denied = _deny_reads(monkeypatch, path)

    assert store.load().state == "unreadable"
    assert store.load().issue == "mcp_isolation_store_unreadable"
    view = store.record_for("demo", _ENTRY)
    assert view.state == "unreadable"
    assert view.records_unreadable is True
    assert view.effective_origin is None
    assert view.usable_sandbox_paths == ()
    _assert_refuses_write(store)
    denied.clear()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "document",
    [
        "{not json",
        "",
        json.dumps([{"demo": _record("hub")}]),
        json.dumps({"version": 1, "servers": []}),
        json.dumps({"version": 2, "servers": {}}),
        json.dumps({"version": 1, "servers": {}, "extra": True}),
        json.dumps({"version": 1, "servers": {"demo": "hub"}}),
        json.dumps({"version": 1, "servers": {"demo": {"origin": "hub"}}}),
    ],
    ids=[
        "broken-json",
        "empty-file",
        "top-level-list",
        "servers-list",
        "future-version",
        "unknown-top-key",
        "record-not-object",
        "record-without-bound",
    ],
)
def test_broken_file_is_unreadable_not_missing(tmp_path: Path, document: str) -> None:
    """壞檔不可當成「不存在」：否則條目掉成第三方（變寬），下次寫入還會蓋掉壞檔。"""
    path = _write(tmp_path, document)
    before = path.read_bytes()
    store = McpIsolationStore(tmp_path)

    assert store.load().state == "unreadable"
    assert store.record_for("demo", _ENTRY).state == "unreadable"
    _assert_refuses_write(store)
    assert path.read_bytes() == before


def test_file_that_disappears_after_a_good_read_is_unreadable(tmp_path: Path) -> None:
    """LOW-e：這個行程讀到過之後才不見，當讀不到（較嚴），不退回「空的」。"""
    path = _write(tmp_path, {"version": 1, "servers": {"demo": _record("creation")}})
    store = McpIsolationStore(tmp_path)
    assert store.load().state == "ok"

    path.unlink()

    assert store.load().state == "unreadable"
    _assert_refuses_write(store)
    assert _snapshot_bytes(path) is None


def test_empty_object_is_an_empty_store(tmp_path: Path) -> None:
    """沒有範本時 bootstrap 寫的 ``{}`` 也是合法的空檔。"""
    _write(tmp_path, {})
    assert McpIsolationStore(tmp_path).load().state == "ok"


# ── 單筆的規則 ─────────────────────────────────────────────────────────────────


def test_unknown_origin_is_treated_as_self_built(tmp_path: Path) -> None:
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("borrowed")}})
    view = McpIsolationStore(tmp_path).record_for("demo", _ENTRY)

    assert view.state == "ok"
    assert origin_category(view.effective_origin) == "self"


@pytest.mark.parametrize(
    ("origin", "category"),
    [
        ("agent", "self"),
        ("creation", "self"),
        ("evolution", "self"),
        ("hub", "third_party"),
        ("skill", "third_party"),
        ("manual", "third_party"),
        ("scaffold", "scaffold"),
        (None, None),
    ],
)
def test_origin_categories(origin: str | None, category: str | None) -> None:
    assert origin_category(origin) == category


def _assigned_names(module: Any) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    targets = [t for n in tree.body if isinstance(n, ast.Assign) for t in n.targets]
    targets += [n.target for n in tree.body if isinstance(n, ast.AnnAssign | ast.TypeAlias)]
    return {t.id for t in targets if isinstance(t, ast.Name)}


def test_origin_constants_have_one_copy_in_isolation_policy() -> None:
    """來源分類只有一份（``utils/isolation_policy``）：store 從那裡取、不另存；API 合約的值一致."""
    copies = {"SELF_ORIGINS", "THIRD_PARTY_ORIGINS", "SCAFFOLD_ORIGIN", "OriginCategory"}
    assert not copies & _assigned_names(store_module)
    assert get_args(models_module.McpOrigin) == isolation_policy.MCP_ORIGINS
    for origin in isolation_policy.MCP_ORIGINS:
        expected = (
            "third_party"
            if origin in isolation_policy.MCP_THIRD_PARTY_ORIGINS
            else "scaffold"
            if origin == isolation_policy.MCP_SCAFFOLD_ORIGIN
            else "self"
        )
        assert origin_category(origin) == expected, origin


def test_bad_sandbox_paths_items_are_dropped_one_by_one(tmp_path: Path) -> None:
    good = {"path": str(tmp_path / "keep"), "access": "ro"}
    items = [
        good,
        {"path": "relative/dir", "access": "rw"},
        {"path": str(tmp_path / "x"), "access": "rwx"},
        "not-an-object",
        {"path": str(tmp_path / "y")},
        *({"path": str(tmp_path / f"n{i}"), "access": "rw"} for i in range(MAX_SANDBOX_PATHS)),
    ]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("manual", sandbox_paths=items)}})
    view = McpIsolationStore(tmp_path).record_for("demo", _ENTRY)

    assert view.state == "ok"
    paths = [grant.path for grant in view.usable_sandbox_paths]
    assert len(paths) == MAX_SANDBOX_PATHS
    assert paths[0] == good["path"]
    assert "relative/dir" not in paths
    assert str(tmp_path / "x") not in paths


def test_sandbox_paths_not_a_list_drops_them_but_keeps_the_record(tmp_path: Path) -> None:
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("hub", sandbox_paths="/")}})
    view = McpIsolationStore(tmp_path).record_for("demo", _ENTRY)

    assert view.state == "ok"
    assert view.effective_origin == "hub"
    assert view.usable_sandbox_paths == ()


def _stale_view(tmp_path: Path, origin: str) -> Any:
    grants = [{"path": str(tmp_path / "data"), "access": "rw"}]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record(origin, sandbox_paths=grants)}})
    changed = {**_ENTRY, "args": ["-y", "@evil/server"]}
    return McpIsolationStore(tmp_path).record_for("demo", changed)


@pytest.mark.parametrize("origin", ["agent", "creation", "evolution", "who-knows"])
def test_stale_bound_keeps_self_built_origin(tmp_path: Path, origin: str) -> None:
    view = _stale_view(tmp_path, origin)

    assert view.state == "stale"
    assert view.effective_origin == origin
    assert origin_category(view.effective_origin) == "self"
    assert view.usable_sandbox_paths == ()


@pytest.mark.parametrize("origin", ["hub", "skill", "manual", "scaffold"])
def test_stale_bound_third_party_and_scaffold_have_no_record(tmp_path: Path, origin: str) -> None:
    """刪掉再同名加回、手改指令、匯入改了指令：舊的第三方標記與資料夾不能套到新程式上。"""
    view = _stale_view(tmp_path, origin)

    assert view.state == "stale"
    assert view.effective_origin is None
    assert view.usable_sandbox_paths == ()


def test_matching_record_exposes_origin_and_sandbox_paths(tmp_path: Path) -> None:
    grants = [{"path": str(tmp_path / "repo"), "access": "ro"}]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("manual", sandbox_paths=grants)}})
    view = McpIsolationStore(tmp_path).record_for("demo", _ENTRY)

    assert view.state == "ok"
    assert view.effective_origin == "manual"
    assert [g.path for g in view.usable_sandbox_paths] == [grants[0]["path"]]


def test_self_built_record_never_uses_sandbox_paths(tmp_path: Path) -> None:
    grants = [{"path": str(tmp_path / "repo"), "access": "rw"}]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("agent", sandbox_paths=grants)}})

    assert McpIsolationStore(tmp_path).record_for("demo", _ENTRY).usable_sandbox_paths == ()


def test_orphan_record_does_not_apply_to_other_names(tmp_path: Path) -> None:
    _write(tmp_path, {"version": 1, "servers": {"gone": _record("creation")}})

    assert McpIsolationStore(tmp_path).record_for("demo", _ENTRY).state == "none"


# ── bound ──────────────────────────────────────────────────────────────────────


def test_bound_ignores_env_but_binds_command_args_and_url() -> None:
    """人改 token 不會讓紀錄失效；改程式就會。"""
    base = bound_of(_ENTRY)
    assert bound_of({**_ENTRY, "env": {"TOKEN": "rotated"}}) == base
    assert bound_of({**_ENTRY, "enabled": False, "disabled_tools": ["x"]}) == base
    assert bound_of({**_ENTRY, "args": ["-y", "@acme/other"]}) != base
    assert bound_of({**_ENTRY, "command": "uvx"}) != base
    assert bound_of({"url": "https://a.example/mcp"}) != bound_of({"url": "https://b.example/mcp"})
    assert bound_of({"command": "npx"}) == bound_of({"command": "npx", "args": []})
    assert len(base) == 64


# ── 寫入：整筆覆寫、資料夾跟著程式走、刪除 ───────────────────────────────────────


def test_fresh_name_overwrites_an_orphan_record_and_clears_its_folders(tmp_path: Path) -> None:
    """LOW-c：同名的孤兒紀錄（刪掉後還沒清）不能把舊資料夾悄悄帶回來。"""
    grants = [{"path": str(tmp_path / "repo"), "access": "rw"}]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("manual", sandbox_paths=grants)}})
    store = McpIsolationStore(tmp_path)

    store.mutate(assign("demo", "manual", bound_of(_ENTRY), fresh=True))

    assert store.record_for("demo", _ENTRY).usable_sandbox_paths == ()


def test_same_program_keeps_folders_and_new_program_clears_them(tmp_path: Path) -> None:
    grants = [{"path": str(tmp_path / "repo"), "access": "rw"}]
    _write(tmp_path, {"version": 1, "servers": {"demo": _record("hub", sandbox_paths=grants)}})
    store = McpIsolationStore(tmp_path)

    store.mutate(assign("demo", "hub", bound_of(_ENTRY), fresh=False))
    assert len(store.record_for("demo", _ENTRY).usable_sandbox_paths) == 1

    changed = {**_ENTRY, "args": ["-y", "@acme/v2"]}
    store.mutate(assign("demo", "hub", bound_of(changed), fresh=False))
    assert store.record_for("demo", changed).usable_sandbox_paths == ()


def test_delete_removes_only_the_named_records(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"version": 1, "servers": {"a": _record("hub"), "b": _record("creation")}},
    )
    store = McpIsolationStore(tmp_path)

    store.mutate(delete(["a", "missing"]))

    assert set(store.load().records) == {"b"}


def test_writes_go_through_the_atomic_write_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """條件 ③：寫檔走 ``write_json``（mkstemp → fsync → replace，ADR-0078）。"""
    calls: list[Path] = []
    real = resource_manager.write_json

    def _spy(path: Path | str, data: Any, **kwargs: Any) -> None:
        calls.append(Path(path))
        real(path, data, **kwargs)

    monkeypatch.setattr(resource_manager, "write_json", _spy)
    McpIsolationStore(tmp_path).mutate(assign("demo", "hub", bound_of(_ENTRY), fresh=True))

    assert calls == [tmp_path / MCP_ISOLATION_FILENAME]
    saved = json.loads((tmp_path / MCP_ISOLATION_FILENAME).read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["servers"]["demo"]["origin"] == "hub"
    assert saved["servers"]["demo"]["bound"] == bound_of(_ENTRY)
    assert isinstance(saved["servers"]["demo"]["updated_at"], float)


def test_failed_change_writes_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, {"version": 1, "servers": {"demo": _record("hub")}})
    before = path.read_bytes()

    def _boom(records: dict[str, IsolationRecord]) -> None:
        records.clear()
        raise ToukaException(ErrorCode.VALIDATION_ERROR)

    with pytest.raises(ToukaException):
        McpIsolationStore(tmp_path).mutate(_boom)
    assert path.read_bytes() == before


# ── 鎖：同一行程不掉更新；持鎖期間不能 await、不能重入 ──────────────────────────


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path) -> None:
    store = McpIsolationStore(tmp_path)
    names = [f"s{i}" for i in range(16)]
    barrier = threading.Barrier(len(names))

    def _writer(name: str) -> None:
        barrier.wait()
        store.mutate(assign(name, "hub", bound_of(_ENTRY), fresh=True))

    threads = [threading.Thread(target=_writer, args=(n,)) for n in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert set(store.load().records) == set(names)


def test_two_store_objects_for_the_same_file_share_the_lock(tmp_path: Path) -> None:
    """Hub 服務、路由各自建 store 物件時仍是同一把鎖（否則等於兩把鎖）。"""
    first, second = McpIsolationStore(tmp_path), McpIsolationStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def _slow(records: dict[str, IsolationRecord]) -> None:
        entered.set()
        release.wait(5)
        order.append("first")
        assign("a", "hub", bound_of(_ENTRY), fresh=True)(records)

    def _fast(records: dict[str, IsolationRecord]) -> None:
        order.append("second")
        assign("b", "hub", bound_of(_ENTRY), fresh=True)(records)

    holder = threading.Thread(target=first.mutate, args=(_slow,), daemon=True)
    holder.start()
    entered.wait(5)
    waiter = threading.Thread(target=second.mutate, args=(_fast,), daemon=True)
    waiter.start()
    waiter.join(0.2)
    assert waiter.is_alive()
    release.set()
    holder.join()
    waiter.join()

    assert order == ["first", "second"]
    assert set(first.load().records) == {"a", "b"}


def test_coroutine_function_is_refused_before_taking_the_lock(tmp_path: Path) -> None:
    async def _async_change(records: dict[str, IsolationRecord]) -> None:
        records.clear()

    errors: list[TypeError] = []

    def _call() -> None:
        try:
            McpIsolationStore(tmp_path).mutate(_async_change)
        except TypeError as exc:
            errors.append(exc)

    with store_module._LOCK:  # 鎖被別人拿著時也要立刻拒絕，不能先排隊
        thread = threading.Thread(target=_call, daemon=True)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    assert len(errors) == 1
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_change_that_returns_an_awaitable_is_refused_without_writing(tmp_path: Path) -> None:
    async def _later() -> None:
        return None

    def _sneaky(records: dict[str, IsolationRecord]) -> Any:
        assign("demo", "hub", bound_of(_ENTRY), fresh=True)(records)
        return _later()

    with pytest.raises(TypeError):
        McpIsolationStore(tmp_path).mutate(_sneaky)
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


def test_reentrant_mutate_raises_instead_of_deadlocking(tmp_path: Path) -> None:
    store = McpIsolationStore(tmp_path)

    def _nested(records: dict[str, IsolationRecord]) -> None:
        store.mutate(assign("inner", "hub", bound_of(_ENTRY), fresh=True))

    worker_error: list[BaseException] = []

    def _run() -> None:
        try:
            store.mutate(_nested)
        except TypeError as exc:
            worker_error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(5)

    assert not thread.is_alive(), "重入卡死"
    assert len(worker_error) == 1
    assert isinstance(worker_error[0], TypeError)
    assert not (tmp_path / MCP_ISOLATION_FILENAME).exists()


async def test_amutate_runs_the_same_change_off_the_event_loop(tmp_path: Path) -> None:
    store = McpIsolationStore(tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _change(records: dict[str, IsolationRecord]) -> str:
        seen.append(threading.get_ident())
        assign("demo", "evolution", bound_of(_ENTRY), fresh=True)(records)
        return "done"

    assert await store.amutate(_change) == "done"
    assert seen and seen[0] != loop_thread
    assert store.record_for("demo", _ENTRY).effective_origin == "evolution"


def test_lock_is_a_module_level_threading_lock() -> None:
    """鎖旁註解引用前提與測試；這裡只核對型別（不可換成每個物件一把）。"""
    assert isinstance(store_module._LOCK, type(threading.Lock()))


# ── 自癒：缺檔由 .example 補、既有檔不覆寫（LOW-h） ─────────────────────────────

_CONFIG_EXAMPLES = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"
_EXAMPLE = _CONFIG_EXAMPLES / f"{MCP_ISOLATION_FILENAME}.example"


def test_example_is_an_empty_valid_store() -> None:
    document = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert document == {"version": 1, "servers": {}}
    assert IsolationFile.model_validate(document).servers == {}


def test_bootstrap_restores_a_missing_file_but_keeps_an_existing_one(tmp_path: Path) -> None:
    assert MCP_ISOLATION_FILENAME in ESSENTIAL_CONFIGS
    shutil.copyfile(_EXAMPLE, tmp_path / _EXAMPLE.name)
    with patch("src.utils.bootstrap.CONFIG_DIR", str(tmp_path)):
        assert MCP_ISOLATION_FILENAME in ensure_configs()
        assert McpIsolationStore(tmp_path).load().state == "ok"

        owned = {"version": 1, "servers": {"demo": _record("creation")}}
        _write(tmp_path, owned)
        assert MCP_ISOLATION_FILENAME not in ensure_configs()
    assert json.loads((tmp_path / MCP_ISOLATION_FILENAME).read_text(encoding="utf-8")) == owned


def test_replace_refuses_a_record_changed_since_it_was_read(tmp_path: Path) -> None:
    """``PUT /isolation`` 讀完紀錄之後別的寫入點改了它：拒絕，不蓋掉（422 mcp_entry_changed）。"""
    store = McpIsolationStore(tmp_path)
    seen = store.load().records.get("demo")
    store.mutate(assign("demo", "creation", bound_of(_ENTRY), fresh=True))
    before = (tmp_path / MCP_ISOLATION_FILENAME).read_bytes()

    with pytest.raises(ToukaException) as raised:
        store.mutate(replace_isolation("demo", "agent", bound_of(_ENTRY), (), expected=seen))

    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.detail["message"] == "mcp_entry_changed"
    assert (tmp_path / MCP_ISOLATION_FILENAME).read_bytes() == before

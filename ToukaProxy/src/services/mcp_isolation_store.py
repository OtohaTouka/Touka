"""MCP 隔離設定檔（ADR-0275）：每台 MCP 的來源（origin）與額外允許的資料夾。

``server_config.json`` 的格式不變；這兩樣另存 ``mcp_server_isolation.json``（同一個設定目錄），
每筆以 ``bound``（command／args／url 的 sha256，不含 env）綁定條目的程式身分。

讀取有三種狀態（不能用 ``read_json``：壞 JSON 會被當成不存在）：
- ``missing``：檔案不存在，而且這個行程沒讀到過它 → 等同空的；
- ``unreadable``：其他 ``OSError``、JSON 壞掉、結構不對，或讀到過之後才不見 → 不覆寫，
  所有條目當自建、``sandbox_paths`` 一律不用，寫入一律 ``PERSISTENCE_ERROR``；
- ``ok``。

寫入順序由呼叫端負責：新增或改程式時先寫這個檔、再寫 ``server_config.json``；刪除時反過來。
中途失敗只會留下「``bound`` 對不上、被忽略」的紀錄，不會出現「條目已寫、來源沒寫」。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.core.exceptions import ErrorCode, ToukaException
from src.models.mcp_isolation import MAX_SANDBOX_PATHS, SandboxPathGrant
from src.services.resource_manager import resource_manager
from src.utils.isolation_policy import MCP_SCAFFOLD_ORIGIN, MCP_THIRD_PARTY_ORIGINS
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from src.models.mcp_isolation import McpRecordState
    from src.utils.isolation_policy import McpClass

logger = logging.getLogger(__name__)

MCP_ISOLATION_FILENAME = "mcp_server_isolation.json"
STORE_UNREADABLE = "mcp_isolation_store_unreadable"
"""``/isolation`` 的 ``mcp.store_issue`` 代碼。"""
ENTRY_CHANGED = "mcp_entry_changed"
"""條目或紀錄在畫面打開之後被改過（422 的 ``detail_override`` 代碼）。"""

LoadState = Literal["ok", "missing", "unreadable"]

# 鎖的前提（CTO 的四個條件）：
# ① 寫這個檔的只有單一 proxy 行程（ADR-0207：不准 --workers、不准第二個 replica）；與同意帳本、
#   每個 skill 的鎖相同。檔名只出現在本模組與 bootstrap
#   （tests/lint/test_mcp_isolation_store_lock.py），啟動指令由
#   tests/lint/test_single_proxy_process.py 釘住；要改成多行程先換成跨行程的鎖。
# ② 只包同步的「讀 → 改 → 原子寫入」，持鎖期間不 await（同一個 lint 測試掃 AST；協程與重入
#   在執行期丟 TypeError，見 tests/unit/test_mcp_isolation_store_security.py）。
# ③ 寫檔走 write_json（mkstemp → fsync → replace，ADR-0078），所以讀取不必拿鎖。
# ④ 所有 store 物件共用這一把（Hub 服務、路由各自建物件時仍是同一把）。
_LOCK = threading.Lock()
_SEEN: set[Path] = set()  # LOW-e：讀到過的檔案，之後不見就當讀不到
_UNREADABLE: set[Path] = set()
_WARNING_LOCK = threading.Lock()


class _Reentry(threading.local):
    active = False


_REENTRY = _Reentry()


def origin_category(origin: str | None) -> McpClass | None:
    """``None``＝沒有可用紀錄；看不懂的值一律當自建（只會更嚴）；不會回 ``remote``.

    來源分類的常數只有一份（``utils/isolation_policy``）。
    """
    if origin is None:
        return None
    if origin in MCP_THIRD_PARTY_ORIGINS:
        return "third_party"
    if origin == MCP_SCAFFOLD_ORIGIN:
        return "scaffold"
    return "self"


def bound_of(entry: Mapping[str, Any]) -> str:
    """條目「程式身分」的 sha256：command、args、url 的正規 JSON；不含 env（改 token 不失效）。"""
    identity = {
        "command": entry.get("command") or None,
        "args": list(entry.get("args") or []),
        "url": entry.get("url") or None,
    }
    text = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _usable_grants(name: str, value: Any) -> tuple[SandboxPathGrant, ...]:
    """只丟壞的那幾項（不是物件、不是絕對路徑、access 不對、超過上限）；記警告不記路徑。"""
    items = value if isinstance(value, list | tuple) else [value]
    kept: list[SandboxPathGrant] = []
    for item in items:
        try:
            kept.append(SandboxPathGrant.model_validate(item))
        except ValidationError:
            continue
    usable = tuple(kept[:MAX_SANDBOX_PATHS])
    if len(usable) != len(items):
        logger.warning("MCP isolation record %r: dropped bad sandbox_paths item(s)", name)
    return usable


class IsolationRecord(BaseModel):
    """一台伺服器的紀錄；``origin`` 存原字串（看不懂的值在解讀時當自建，不讓整份失效）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str | None = None
    sandbox_paths: tuple[SandboxPathGrant, ...] = ()
    bound: str
    updated_at: float = 0.0


class IsolationFile(BaseModel):
    """整份檔案；頂層結構不對、版本不是 1 都算讀不到。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    servers: dict[str, IsolationRecord] = Field(default_factory=dict)

    @field_validator("servers", mode="before")
    @classmethod
    def drop_bad_sandbox_paths(cls, value: Any) -> Any:
        """單筆的 ``sandbox_paths`` 只丟壞的項目，不讓整份失效。"""
        if not isinstance(value, dict):
            return value
        cleaned: dict[Any, Any] = {}
        for name, record in value.items():
            if isinstance(record, dict) and "sandbox_paths" in record:
                record = {**record, "sandbox_paths": _usable_grants(name, record["sandbox_paths"])}
            cleaned[name] = record
        return cleaned


@dataclass(frozen=True)
class RecordView:
    """某台伺服器對照目前條目的紀錄；``state`` 是 ok／none／stale／unreadable。"""

    state: McpRecordState
    origin: str | None
    sandbox_paths: tuple[SandboxPathGrant, ...]
    bound: str

    @property
    def records_unreadable(self) -> bool:
        """讀不到時呼叫端要把所有 stdio 條目當自建。"""
        return self.state == "unreadable"

    @property
    def effective_origin(self) -> str | None:
        """``bound`` 不符時自建類照舊，第三方類與 scaffold 當沒有紀錄（``None``）。"""
        if self.state == "ok":
            return self.origin
        if self.state == "stale" and origin_category(self.origin) == "self":
            return self.origin
        return None

    @property
    def usable_sandbox_paths(self) -> tuple[SandboxPathGrant, ...]:
        """只有紀錄和條目相符、而且不是自建類時才給；啟動時仍要再過永不授權。"""
        if self.state != "ok" or origin_category(self.origin) == "self":
            return ()
        return self.sandbox_paths


@dataclass(frozen=True)
class IsolationSnapshot:
    """一次讀取的結果；``records`` 唯讀。"""

    state: LoadState
    records: Mapping[str, IsolationRecord]

    @property
    def issue(self) -> str | None:
        """讀不到時給 ``/isolation`` 的原因代碼。"""
        return STORE_UNREADABLE if self.state == "unreadable" else None

    def view(self, name: str, entry: Mapping[str, Any]) -> RecordView:
        """對照目前條目的 ``bound``。"""
        bound = bound_of(entry)
        if self.state == "unreadable":
            return RecordView("unreadable", None, (), bound)
        record = self.records.get(name)
        if record is None:
            return RecordView("none", None, (), bound)
        state: McpRecordState = "ok" if record.bound == bound else "stale"
        return RecordView(state, record.origin, record.sandbox_paths, bound)


def _unreadable(path: Path, why: str) -> IsolationSnapshot:
    with _WARNING_LOCK:
        if path not in _UNREADABLE:
            logger.warning(
                "AUDIT: %s is unreadable (%s); every MCP is treated as self-built", path, why,
            )
            _UNREADABLE.add(path)
    return IsolationSnapshot("unreadable", MappingProxyType({}))


def _read(path: Path) -> IsolationSnapshot:
    try:
        text = resource_manager.read_file_text(path)
    except FileNotFoundError:
        if path in _SEEN:
            return _unreadable(path, "missing after a good read")
        with _WARNING_LOCK:
            _UNREADABLE.discard(path)
        return IsolationSnapshot("missing", MappingProxyType({}))
    except (OSError, UnicodeDecodeError) as exc:
        return _unreadable(path, type(exc).__name__)
    try:
        document = IsolationFile.model_validate(json.loads(text))
    except (ValueError, RecursionError) as exc:  # JSONDecodeError、ValidationError 都是 ValueError
        return _unreadable(path, type(exc).__name__)
    _SEEN.add(path)
    with _WARNING_LOCK:
        _UNREADABLE.discard(path)
    return IsolationSnapshot("ok", MappingProxyType(dict(document.servers)))


def _apply[T](path: Path, fn: Callable[[dict[str, IsolationRecord]], T]) -> T:
    snapshot = _read(path)
    if snapshot.state == "unreadable":
        logger.warning("AUDIT: refusing to rewrite unreadable %s", path)
        raise ToukaException(ErrorCode.PERSISTENCE_ERROR)
    records = dict(snapshot.records)
    result = fn(records)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError(fn)
    document = IsolationFile(servers=records)
    resource_manager.write_json(path, document.model_dump(mode="json"), indent=2)
    _SEEN.add(path)
    return result


class McpIsolationStore:
    """某個設定目錄的隔離設定檔；寫入經同一把行程內的鎖（見 ``_LOCK`` 旁的前提）。"""

    def __init__(self, config_dir: Path | str) -> None:
        self.path = Path(config_dir) / MCP_ISOLATION_FILENAME

    def load(self) -> IsolationSnapshot:
        """讀整份（不拿鎖：寫入是原子置換）。"""
        return _read(self.path)

    def record_for(self, name: str, entry: Mapping[str, Any]) -> RecordView:
        """某台伺服器對照目前條目的紀錄。"""
        return self.load().view(name, entry)

    def mutate[T](self, fn: Callable[[dict[str, IsolationRecord]], T]) -> T:
        """持鎖「讀 → ``fn`` 改 dict → 原子寫入」；``fn`` 必須是同步函式、不可再呼叫本方法。"""
        if inspect.iscoroutinefunction(fn) or _REENTRY.active:
            raise TypeError(fn)
        with _LOCK:
            _REENTRY.active = True
            try:
                return _apply(self.path, fn)
            finally:
                _REENTRY.active = False

    async def amutate[T](self, fn: Callable[[dict[str, IsolationRecord]], T]) -> T:
        """給 async 呼叫端：在執行緒裡跑 ``mutate``，不讓鎖卡住事件迴圈。"""
        return await asyncio.to_thread(self.mutate, fn)


def _new_record(
    origin: str | None, bound: str, grants: tuple[SandboxPathGrant, ...] = ()
) -> IsolationRecord:
    return IsolationRecord(
        origin=origin, sandbox_paths=grants, bound=bound, updated_at=get_current_epoch()
    )


def assign(
    name: str, origin: str, bound: str, *, fresh: bool
) -> Callable[[dict[str, IsolationRecord]], None]:
    """寫入點用：新名稱（``fresh``）整筆覆寫並清空資料夾（LOW-c）；同名時程式變了也清空。"""

    def apply(records: dict[str, IsolationRecord]) -> None:
        old = records.get(name)
        keep = () if fresh or old is None or old.bound != bound else old.sandbox_paths
        records[name] = _new_record(origin, bound, keep)

    return apply


def delete(names: Iterable[str]) -> Callable[[dict[str, IsolationRecord]], None]:
    """刪掉指定名稱的紀錄（刪除條目之後、匯入新名稱之前）。"""
    targets = frozenset(names)

    def apply(records: dict[str, IsolationRecord]) -> None:
        for name in targets:
            records.pop(name, None)

    return apply


def copy_to(renames: Iterable[tuple[str, str]]) -> Callable[[dict[str, IsolationRecord]], None]:
    """改名（條目整筆搬過去、``bound`` 不變）之前把紀錄複製到新名稱。

    舊名稱沒有紀錄時清掉新名稱上的（孤兒紀錄不能套到搬過來的條目上）。
    """
    pairs = tuple(renames)

    def apply(records: dict[str, IsolationRecord]) -> None:
        for old, new in pairs:
            if old in records:
                records[new] = records[old]
            else:
                records.pop(new, None)

    return apply


def changed_programs(
    existing: Mapping[str, Mapping[str, Any]], incoming: Mapping[str, Mapping[str, Any]],
    *, human: bool = False,
) -> list[str]:
    """新名稱、程式變動，或非人類改動 env；呼叫前須完成遮罩回填。"""
    return [
        name
        for name, entry in incoming.items()
        if name not in existing or bound_of(existing[name]) != bound_of(entry)
        or (not human and env_changed(existing[name], entry))
    ]


def env_changed(existing: object, incoming: object) -> bool:
    """環境也能選擇執行程式；名稱、刪除與值的變更都算。"""
    if not isinstance(existing, Mapping) or not isinstance(incoming, Mapping):
        return True
    return (existing.get("env") or {}) != (incoming.get("env") or {})


def _rebound_origin(prior: str | None, *, human: bool) -> str | None:
    """程式變了：自建類照舊；第三方類與 scaffold 只有人類才保留，否則改成 agent。"""
    if origin_category(prior) == "self" or (human and prior is not None):
        return prior
    return None if human else "agent"


def reconcile_saved_entries(
    existing: Mapping[str, Mapping[str, Any]],
    incoming: Mapping[str, Mapping[str, Any]],
    names: Iterable[str],
    *,
    human: bool,
) -> Callable[[dict[str, IsolationRecord]], list[str]]:
    """設定頁存檔（寫 ``server_config.json`` 之前）；回傳資料夾被清掉的名稱。

    新名稱整筆覆寫成 ``manual``（人類）或 ``agent``；既有名稱的程式變了就依
    ``_rebound_origin`` 重綁 ``bound``，``sandbox_paths`` 一律清掉（人給的資料夾不能套到
    另一個程式上）。沒有紀錄而人類改程式時不寫（照舊由啟發式判斷）。
    """
    targets = tuple(names)

    def apply(records: dict[str, IsolationRecord]) -> list[str]:
        cleared: list[str] = []
        for name in targets:
            bound = bound_of(incoming[name])
            if name not in existing or not isinstance(existing[name], Mapping):
                records[name] = _new_record("manual" if human else "agent", bound)
                continue
            old = records.get(name)
            prior = IsolationSnapshot("ok", {name: old} if old else {}).view(name, existing[name])
            if prior.usable_sandbox_paths:
                cleared.append(name)
            origin = _rebound_origin(prior.effective_origin, human=human)
            if origin is None:
                records.pop(name, None)
            else:
                records[name] = _new_record(origin, bound)
        return cleared

    return apply


def replace_isolation(
    name: str,
    origin: str | None,
    bound: str,
    grants: Iterable[SandboxPathGrant],
    *,
    expected: IsolationRecord | None,
) -> Callable[[dict[str, IsolationRecord]], None]:
    """設定頁改來源或資料夾：紀錄在讀取之後被別的寫入改過就拒絕（422 ``mcp_entry_changed``）。"""
    kept = tuple(grants)

    def apply(records: dict[str, IsolationRecord]) -> None:
        if records.get(name) != expected:
            raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=ENTRY_CHANGED)
        if origin is None and not kept:
            records.pop(name, None)
        else:
            records[name] = _new_record(origin, bound, kept)

    return apply


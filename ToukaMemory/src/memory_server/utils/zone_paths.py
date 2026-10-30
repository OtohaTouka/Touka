"""代理人寫得到的資料夾（區）：唯一的區內判斷與路徑物件（ADR-0293）。

代理人能在自己的資料夾裡放連結（symlink、junction）。Touka 自己替代理人讀寫時若跟著連結走，
就會讀寫或刪到設定（``config/hooks.json``、``users.json`` 等）。所以：

- 「什麼算區」只寫在這裡：錨點（``Anchor``）與 ``zone_of``／``contains_zone``；RM 的字串方法、
  測試絆線、備份腳本、權限自癒與隔離層的形狀判斷都呼叫這兩個函式（R3）。
- 區裡的路徑只用 ``ZonePath`` 表示，只能交回 ``ResourceManager`` 的區內方法；拿去開檔、
  接路徑或轉字串都會先通知觀察者再丟錯（R1），漏改的地方不會默默跟隨。
- 錨點底下的資料夾名稱只在這裡的私有對照表（``anchor_dirname`` 給備份用）。

只用標準函式庫；``no_follow`` 的不跟隨原語與 ``ResourceManager`` 都用這裡的判斷。
"""

from __future__ import annotations

import contextlib
import enum
import errno
import logging
import ntpath
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"


class Anchor(enum.Enum):
    """錨點代號（不是資料夾名稱；名稱只在 ``_DATA_DIRS``）。"""

    PERSONAL_DRIVE = "personal_drive"
    CUSTOM_DRIVE = "custom_drive"
    MCP_SERVERS = "mcp_servers"
    DOCGEN_OUTPUT = "docgen_output"
    SANDBOX_STATE = "sandbox_state"


ALL_ANCHORS = frozenset(Anchor)
# PR 1 只對個人空間執法；其他三個錨點在 PR 2 加進來（Z10 與隔離層已用 ALL_ANCHORS）
ENFORCED_ANCHORS = frozenset({Anchor.PERSONAL_DRIVE, Anchor.CUSTOM_DRIVE})

# 錨點相對 data 根的資料夾；SANDBOX_STATE 不在 data 根底下（由 ZoneRoots 的 state_root 給）
_DATA_DIRS: dict[Anchor, tuple[str, ...]] = {
    Anchor.PERSONAL_DRIVE: ("personal_drive",),
    Anchor.CUSTOM_DRIVE: ("custom",),
    Anchor.MCP_SERVERS: ("mcp_servers", "managed"),
    Anchor.DOCGEN_OUTPUT: ("temp", "docgen_output"),
}
# 自訂模型的個人空間是 custom/<id>/drive：custom/<id> 本身與裡面的設定檔不算區
_CUSTOM_DRIVE_DIR = "drive"
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in "123456789¹²³"}
)


def anchor_dirname(anchor: Anchor) -> str | None:
    """錨點相對 data 根的資料夾（以 ``/`` 分隔，備份用）；不在 data 根底下的錨點回 None。"""
    parts = _DATA_DIRS.get(anchor)
    return None if parts is None else "/".join(parts)


# --- 例外與觀察者（R12） -------------------------------------------------


@dataclass(frozen=True)
class ZoneEvent:
    """觀察者收到的一筆紀錄。

    ``error`` 是例外類別（R1 的 Pathlib 名稱誤用是 ``AttributeError``），``detail`` 是誤用的方法。
    """

    error: type[BaseException]
    display: str | None
    detail: str = ""


_observers: list[Callable[[ZoneEvent], None]] = []
_observers_lock = threading.Lock()


def add_observer(callback: Callable[[ZoneEvent], None]) -> None:
    """登記觀察者：建立區的例外、R1 的誤用時呼叫。"""
    with _observers_lock:
        _observers.append(callback)


def remove_observer(callback: Callable[[ZoneEvent], None]) -> None:
    """取消登記（沒登記過時不做事）。"""
    with _observers_lock:
        if callback in _observers:
            _observers.remove(callback)


def report(event: ZoneEvent) -> None:
    """通知所有觀察者；觀察者自己的錯誤吞掉、不影響產品。"""
    with _observers_lock:
        callbacks = list(_observers)
    for callback in callbacks:
        # 觀察者只負責記錄，不能讓產品失敗
        with contextlib.suppress(Exception):
            callback(event)


def _log_observer(event: ZoneEvent) -> None:
    logger.warning("Zone path refused: %s %s %s", event.error.__name__, event.display, event.detail)


add_observer(_log_observer)


class ZonePathRequiredError(TypeError):
    """區裡的路徑必須以 ``ZonePath`` 交給 RM 的區內方法（程式錯誤，``except OSError`` 接不到）。"""

    MESSAGE = "a path inside an agent folder must be handled as ZonePath by ResourceManager"

    def __init__(self, display: str | None = None, detail: str = "") -> None:
        super().__init__(self.MESSAGE)
        self.display = display
        report(ZoneEvent(type(self), display, detail))


class ZoneSegmentError(ValueError):
    """名稱不能當成區裡的一段（``..``、分隔符、Windows 保留名等）。"""

    MESSAGE = "the name cannot be used as a path segment inside an agent folder"

    def __init__(self, display: str | None = None) -> None:
        super().__init__(self.MESSAGE)
        self.display = display
        report(ZoneEvent(type(self), display))


class ZoneRootsUnavailableError(RuntimeError):
    """還沒有人登記「目前的 data 根」，或錨點需要的根沒有設定（不猜）。"""

    MESSAGE = "the data root for agent folders is not configured"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


# --- 段落 ----------------------------------------------------------------


def is_zone_segment(name: object) -> bool:
    r"""能不能當成區裡的一段：拒 ``..``、``.``、空、``/``、``\``、NUL。

    Windows 另拒 ``:``、保留裝置名（含副檔名，例如 ``con.txt``）、結尾的點與半形空白（系統會
    去掉，變成另一個名稱的別名）。其他空白（開頭的、全形的）是合法而不同的名稱，照收。
    """
    if not isinstance(name, str):
        return False
    if not name or name in {".", ".."}:
        return False
    if any(ch in name for ch in ("/", "\\", "\x00")):
        return False
    if _WINDOWS:
        if ":" in name or name.endswith((".", " ")):
            return False
        if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED:
            return False
    return True


def _norm(segment: str) -> str:
    return os.path.normcase(segment)


def _key(parts: Iterable[str]) -> tuple[str, ...]:
    return tuple(_norm(p) for p in parts)


# --- ZonePath（R1、R2、R8） ----------------------------------------------

# 每個錨點裡「代理人」佔幾段：parent 最多回到這裡，same_agent 比這幾段
_AGENT_DEPTH = {Anchor.CUSTOM_DRIVE: 2}
# Pathlib 的方法名稱：ZonePath 被當成 Path 用時先通知再丟 AttributeError
_PATHLIB_NAMES = frozenset(n for n in dir(Path) if not n.startswith("_"))


class ZonePath:
    """區裡的一個路徑：錨點加上逐段驗證過的名稱；只能交回 RM 的區內方法。

    不可變；段落保留原本的大小寫，``normcase`` 只用於比對。``CUSTOM_DRIVE`` 至少是
    ``<id>/drive``。整個錨點（沒有段落）只有 RM 的 ``anchor_root`` 會建立。
    """

    __slots__ = ("_anchor", "_parts")

    def __init__(self, anchor: Anchor, parts: Iterable[str]) -> None:
        """任何一段不合格，或自訂模型的個人空間不是 ``<id>/drive`` 時丟 ``ZoneSegmentError``。"""
        segments = tuple(parts)
        if not isinstance(anchor, Anchor) or not all(is_zone_segment(s) for s in segments):
            raise ZoneSegmentError
        if anchor is Anchor.CUSTOM_DRIVE and (
            len(segments) < 2 or segments[1] != _CUSTOM_DRIVE_DIR
        ):
            raise ZoneSegmentError
        object.__setattr__(self, "_anchor", anchor)
        object.__setattr__(self, "_parts", segments)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(name)

    def __delattr__(self, name: str) -> None:
        raise AttributeError(name)

    @property
    def anchor(self) -> Anchor:
        """錨點。"""
        return self._anchor

    @property
    def parts(self) -> tuple[str, ...]:
        """錨點底下的段落（保留原本的大小寫）。"""
        return self._parts

    @property
    def name(self) -> str:
        """最後一段；整個錨點時是空字串。"""
        return self._parts[-1] if self._parts else ""

    def _agent_depth(self) -> int:
        return min(len(self._parts), _AGENT_DEPTH.get(self._anchor, 1))

    @property
    def parent(self) -> ZonePath:
        """上一層；最多回到代理人那一層（自訂模型是 ``<id>/drive``），到了就回自己。"""
        if len(self._parts) <= self._agent_depth():
            return self
        return ZonePath(self._anchor, self._parts[:-1])

    def child(self, *segments: str) -> ZonePath:
        """程式給的名稱：每一段都要合格。"""
        return ZonePath(self._anchor, self._parts + segments)

    def join_rel(self, text: str) -> ZonePath:
        r"""模型或使用者給的相對路徑；``..`` 最多回到自己（接收者），不能往上。

        開頭是 ``/``、``\`` 或磁碟機時丟 ``ZoneSegmentError``（絕對路徑由呼叫端另外處理）。
        Windows 上 ``\`` 也是分隔符；POSIX 上含 ``\`` 的名稱不合格。
        """
        if not isinstance(text, str) or text.startswith(("/", "\\")):
            raise ZoneSegmentError(self.display())
        if _WINDOWS:
            if ntpath.splitdrive(text)[0]:
                raise ZoneSegmentError(self.display())
            text = text.replace("\\", "/")
        stack: list[str] = []
        for segment in text.split("/"):
            if segment in {"", "."}:
                continue
            if segment == "..":
                if not stack:
                    raise ZoneSegmentError(self.display())
                stack.pop()
                continue
            stack.append(segment)
        return self.child(*stack)

    def is_under(self, base: ZonePath) -> bool:
        """自己是 ``base`` 本身或在它底下（同一個錨點）。"""
        n = len(base._parts)
        return (
            self._anchor is base._anchor
            and len(self._parts) >= n
            and _key(self._parts[:n]) == _key(base._parts)
        )

    def relative_parts(self, base: ZonePath) -> tuple[str, ...]:
        """相對 ``base`` 的段落；不在 ``base`` 底下時丟 ``ZoneSegmentError``。"""
        if not self.is_under(base):
            raise ZoneSegmentError(self.display())
        return self._parts[len(base._parts) :]

    def same_agent(self, other: ZonePath) -> bool:
        """同一個錨點的同一個代理人（自訂模型比 ``<id>/drive``）。"""
        n = _AGENT_DEPTH.get(self._anchor, 1)
        return (
            self._anchor is other._anchor
            and len(self._parts) >= n
            and len(other._parts) >= n
            and _key(self._parts[:n]) == _key(other._parts[:n])
        )

    def display(self) -> str:
        """給 log 與例外的形式（``zone_display``）。"""
        return zone_display(self._anchor, self._parts)

    def __repr__(self) -> str:
        return self.display()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ZonePath):
            return NotImplemented
        return self._anchor is other._anchor and _key(self._parts) == _key(other._parts)

    def __hash__(self) -> int:
        return hash((self._anchor, _key(self._parts)))

    # R1：每一種「當成路徑用」的寫法都先經過這裡、通知觀察者再丟錯
    def _misuse(self, method: str) -> ZonePathRequiredError:
        return ZonePathRequiredError(self.display(), method)

    def __fspath__(self) -> str:
        raise self._misuse("__fspath__")

    def __truediv__(self, other: object) -> ZonePath:
        raise self._misuse("__truediv__")

    def __rtruediv__(self, other: object) -> ZonePath:
        raise self._misuse("__rtruediv__")

    def __add__(self, other: object) -> str:
        raise self._misuse("__add__")

    def __radd__(self, other: object) -> str:
        raise self._misuse("__radd__")

    def __str__(self) -> str:
        raise self._misuse("__str__")

    def __format__(self, spec: str) -> str:
        raise self._misuse("__format__")

    def __bytes__(self) -> bytes:
        raise self._misuse("__bytes__")

    def __getattr__(self, name: str) -> object:
        if name in _PATHLIB_NAMES:
            report(ZoneEvent(AttributeError, self.display(), name))
        raise AttributeError(name)


def agent_root(anchor: Anchor, folder: str) -> ZonePath:
    """代理人在錨點裡的根：``folder`` 是代理人的資料夾名稱（一段），自訂模型再接 ``drive``。

    名稱不合格丟 ``ZoneSegmentError``。
    """
    if anchor is Anchor.CUSTOM_DRIVE:
        return ZonePath(anchor, (folder, _CUSTOM_DRIVE_DIR))
    return ZonePath(anchor, (folder,))


def zone_display(anchor: Anchor, parts: Iterable[str]) -> str:
    """區內位置給 log 與例外的形式（``ZonePath.display`` 與字串方法的拒絕共用）。

    ``zone://`` 拼回路徑一定失敗：POSIX 上沒有這個資料夾，Windows 上 ``:`` 不合法。
    """
    return "/".join(("zone:/", anchor.value, *parts))


# --- 根（R9、R14） -------------------------------------------------------


def absolute_text(path: str | os.PathLike[str]) -> str:
    """絕對路徑並以字面消掉 ``..``：判斷與真正的 I/O 用同一個字串（R4；RM 的字串方法也用它）。"""
    return os.path.normpath(Path(os.fspath(path)).absolute())


def _real(path: str) -> str:
    """跟隨連結解析；不存在的部分照字面接上。

    連結迴圈時解析到迴圈前一段、後面照字面接上（之後真正的 I/O 會因 ELOOP 失敗）。
    """
    try:
        return str(Path(path).resolve())
    except RuntimeError:
        parts = _parts_of(path)
        for i in range(len(parts) - 1, 0, -1):
            try:
                head = Path(*parts[:i]).resolve()
            except RuntimeError:
                continue
            return str(head.joinpath(*parts[i:]))
        return path


def _identity_of(info: object) -> tuple[int, int] | None:
    """檔案身分 ``(st_dev, st_ino)``；``st_ino == 0`` 的檔案系統上身分未知。"""
    ino = getattr(info, "st_ino", 0)
    return None if not ino else (getattr(info, "st_dev", 0), ino)


def _identity(path: str | Path) -> tuple[int, int] | None:
    try:
        return _identity_of(Path(path).stat())
    except (OSError, ValueError):
        return None


class ZoneRoots:
    """各錨點的字面絕對路徑；解析後的路徑與身分每次判斷時現算（錨點之後才建也認得）。"""

    __slots__ = ("_literals",)

    def __init__(
        self, data_root: str | os.PathLike[str], state_root: str | os.PathLike[str] | None
    ) -> None:
        """``state_root`` 沒設定時，``SANDBOX_STATE`` 不參與判斷。"""
        data = Path(absolute_text(data_root))
        literals = {anchor: str(data.joinpath(*parts)) for anchor, parts in _DATA_DIRS.items()}
        if state_root is not None and os.fspath(state_root):
            literals[Anchor.SANDBOX_STATE] = absolute_text(state_root)
        self._literals = literals

    def literal(self, anchor: Anchor) -> str | None:
        """錨點的字面絕對路徑（不解析）。"""
        return self._literals.get(anchor)

    def resolved(self, anchor: Anchor) -> str | None:
        """錨點解析後的路徑（人做的連結照跟隨，R9）。"""
        literal = self._literals.get(anchor)
        return None if literal is None else _real(literal)


_roots_provider: Callable[[], ZoneRoots] | None = None


def set_roots_provider(provider: Callable[[], ZoneRoots]) -> None:
    """登記「目前的 data 根」的唯一來源（RM 在模組載入時登記一次）。"""
    global _roots_provider
    _roots_provider = provider


def current_roots() -> ZoneRoots:
    """目前的 data 根；沒有登記時丟 ``ZoneRootsUnavailableError``（不猜）。"""
    if _roots_provider is None:
        raise ZoneRootsUnavailableError
    return _roots_provider()


def ensure_anchor(roots: ZoneRoots, anchor: Anchor) -> str:
    """錨點不存在時建立；存在時確認是資料夾（可以是人做的連結）。回解析後的路徑。"""
    literal = roots.literal(anchor)
    if literal is None:
        raise ZoneRootsUnavailableError
    with contextlib.suppress(FileExistsError):
        Path(literal).mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(Path(literal).stat().st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), literal)
    return _real(literal)


# --- 區內判斷（R3） ------------------------------------------------------


def _parts_of(path: str) -> tuple[str, ...]:
    return Path(path).parts


_SEPS = tuple(s for s in (os.sep, os.altsep) if s)


def _prefixes(parts: tuple[str, ...]) -> Iterator[str]:
    """``str(Path(*parts[:1]))``、``str(Path(*parts[:2]))`` …（不每段重建 Path）。"""
    current = ""
    for part in parts:
        if not current or current.endswith(_SEPS):
            current += part
        else:
            current = current + os.sep + part
        yield current


def _shape(anchor: Anchor, rest: tuple[str, ...], fold: Callable[[str], str]) -> bool:
    """錨點底下的段落是不是區：自訂模型要對上 ``<id>/drive``，其他至少一段。"""
    if anchor is Anchor.CUSTOM_DRIVE:
        return len(rest) >= 2 and fold(rest[1]) == fold(_CUSTOM_DRIVE_DIR)
    return len(rest) >= 1


@dataclass(frozen=True)
class _Base:
    anchor: Anchor
    parts: tuple[str, ...]
    key: tuple[str, ...]


def _anchor_bases(
    roots: ZoneRoots, anchors: Iterable[Anchor], *, follow: bool = True
) -> tuple[list[_Base], dict[Anchor, str]]:
    """這次判斷用的錨點：字面與解析後的段落（錨點順序、字面在前），和解析後的路徑。

    每次呼叫重算、不快取（錨點之後才建、之後才換成連結都認得）；同一次呼叫裡每個錨點只解析一次。
    """
    wanted = set(anchors)
    bases: list[_Base] = []
    resolved: dict[Anchor, str] = {}
    for anchor in Anchor:
        literal = roots.literal(anchor) if anchor in wanted else None
        if literal is None:
            continue
        texts = [literal]
        if follow:
            resolved[anchor] = _real(literal)
            if resolved[anchor] != literal:
                texts.append(resolved[anchor])
        for anchor_text in texts:
            parts = _parts_of(anchor_text)
            bases.append(_Base(anchor, parts, _key(parts)))
    return bases, resolved


def zone_of(
    path: str | os.PathLike[str],
    roots: ZoneRoots,
    *,
    anchors: Iterable[Anchor] = ENFORCED_ANCHORS,
    use_identity: bool = True,
    follow: bool = True,
) -> tuple[Anchor, tuple[str, ...]] | None:
    """``path`` 在某個區裡時回（錨點, 段落），否則 None；錨點本身不算區內。

    三項任一成立：字面在錨點的字面或解析後路徑底下；解析後在錨點解析後的路徑底下；
    從根往下某個存在的前綴，檔案身分等於某錨點，後面還有段落（``use_identity``，只有測試
    絆線關掉）。自訂模型三項都要再對上 ``<id>/drive``。``follow=False`` 只看字面：路徑與
    錨點都不解析、不比身分（權限自癒自己比對解析前後）。
    """
    text = absolute_text(path)
    literal = _parts_of(text)
    candidates = [literal, _parts_of(_real(text))] if follow else [literal]
    bases, resolved = _anchor_bases(roots, anchors, follow=follow)
    for candidate in candidates:
        key = _key(candidate)
        for base in bases:
            n = len(base.key)
            if key[:n] == base.key and _shape(base.anchor, candidate[n:], _norm):
                return base.anchor, candidate[n:]
    if use_identity and follow:
        return _zone_by_identity(literal, resolved)
    return None


def anchor_at(
    path: str | os.PathLike[str], roots: ZoneRoots, *, anchors: Iterable[Anchor] = ENFORCED_ANCHORS
) -> Anchor | None:
    """``path``（字面或解析後）恰好是某個錨點本身時回那個錨點，否則 None。"""
    text = absolute_text(path)
    bases, _resolved = _anchor_bases(roots, anchors)
    for candidate in (_parts_of(text), _parts_of(_real(text))):
        key = _key(candidate)
        for base in bases:
            if key == base.key:
                return base.anchor
    return None


def _zone_by_identity(
    parts: tuple[str, ...], resolved: dict[Anchor, str]
) -> tuple[Anchor, tuple[str, ...]] | None:
    ids = {}
    for anchor, anchor_path in resolved.items():
        ident = _identity(anchor_path)
        if ident is not None:
            ids[anchor] = ident
    if not ids:
        return None
    for i, prefix in enumerate(_prefixes(parts[:-1]), start=1):
        ident = _identity(prefix)
        if ident is None:
            return None
        for anchor, anchor_id in ids.items():
            if ident == anchor_id and _shape(anchor, parts[i:], str.casefold):
                return anchor, parts[i:]
    return None


def contains_zone(
    path: str | os.PathLike[str], roots: ZoneRoots, *, anchors: Iterable[Anchor] = ENFORCED_ANCHORS
) -> bool:
    """``path`` 是某個錨點本身或它的上層，或是 ``custom/<id>``（形狀上包含 ``custom/<id>/drive``）。

    身分只比對錨點自己的上層鏈；不在這條鏈上的別名上層靠逐層走訪補上（R4、R5）。
    """
    text = absolute_text(path)
    bases, resolved = _anchor_bases(roots, anchors)
    for candidate in (_parts_of(text), _parts_of(_real(text))):
        key = _key(candidate)
        m = len(key)
        for base in bases:
            if base.key[:m] == key:
                return True
            n = len(base.key)
            if base.anchor is Anchor.CUSTOM_DRIVE and m == n + 1 and key[:n] == base.key:
                return True
    return _contains_by_identity(text, resolved)


def _contains_by_identity(text: str, resolved: dict[Anchor, str]) -> bool:
    known: dict[str, tuple[int, int] | None] = {}

    def identity(prefix: str) -> tuple[int, int] | None:
        # 兩個錨點的上層鏈大多相同：同一次呼叫裡每個前綴只 stat 一次
        if prefix not in known:
            known[prefix] = _identity(prefix)
        return known[prefix]

    ident = _identity(text)
    if ident is None:
        return False
    parent_id = _identity(Path(text).parent)
    for anchor, anchor_path in resolved.items():
        if any(identity(prefix) == ident for prefix in _prefixes(_parts_of(anchor_path))):
            return True
        custom = anchor is Anchor.CUSTOM_DRIVE and parent_id is not None
        if custom and parent_id == identity(anchor_path):
            return True
    return False


@dataclass(frozen=True)
class MarkerState:
    """走訪時一個資料夾的位置（一般資料夾用 None 表示）。

    ``kind``：``anchor``（自訂模型的錨點本身，繼續往下）、``custom_id``（``custom/<id>``，
    ``parts`` 是 ``(<id>,)``）、``zone``（區根或整個其他錨點，呼叫端剪掉；``parts`` 是錨點底下
    的段落，整個錨點時是空的）。
    """

    kind: str
    anchor: Anchor
    parts: tuple[str, ...] = ()


class ZoneMarkers:
    """走訪時逐層判斷子資料夾是不是區根（R5）；和 ``zone_of`` 同一套形狀規則。

    身分用走訪時已有的 lstat 結果比對錨點，不另外 stat，所以別名路徑（例如另一個掛載路徑底下
    的同一個 data）也認得；身分未知（``st_ino == 0``）時只比對字面與解析後的錨點路徑。
    """

    __slots__ = ("_anchors", "_by_id", "_by_path", "_roots")

    def __init__(self, roots: ZoneRoots, anchors: Iterable[Anchor] = ENFORCED_ANCHORS) -> None:
        """錨點的身分在這裡算一次（走訪期間錨點本身由 Touka 管，不會被換）。"""
        self._roots = roots
        self._anchors = [a for a in Anchor if a in set(anchors)]
        self._by_id: dict[tuple[int, int], Anchor] = {}
        self._by_path: dict[tuple[str, ...], Anchor] = {}
        bases, resolved = _anchor_bases(roots, self._anchors)
        for base in bases:
            self._by_path[base.key] = base.anchor
        for anchor, anchor_path in resolved.items():
            ident = _identity(anchor_path)
            if ident is not None:
                self._by_id[ident] = anchor

    def _anchor_at(self, path: str, info: object) -> Anchor | None:
        ident = _identity_of(info)
        if ident is not None and ident in self._by_id:
            return self._by_id[ident]
        return self._by_path.get(_key(_parts_of(path)))

    @staticmethod
    def _as_anchor(anchor: Anchor) -> MarkerState:
        # 自訂模型的錨點底下 custom/<id> 不是區，要繼續往下；其他錨點整個都是區
        if anchor is Anchor.CUSTOM_DRIVE:
            return MarkerState("anchor", anchor)
        return MarkerState("zone", anchor)

    def start(self, path: str) -> MarkerState | None:
        """走訪起點的位置。"""
        found = zone_of(path, self._roots, anchors=self._anchors)
        if found is not None:
            return MarkerState("zone", *found)
        text = absolute_text(path)
        anchor = self._anchor_at(text, _stat_or_none(text))
        if anchor is not None:
            return self._as_anchor(anchor)
        parent = str(Path(text).parent)
        if self._anchor_at(parent, _stat_or_none(parent)) is Anchor.CUSTOM_DRIVE:
            return MarkerState("custom_id", Anchor.CUSTOM_DRIVE, (Path(text).name,))
        return None

    def child(
        self, parent: MarkerState | None, path: str, name: str, info: object
    ) -> MarkerState | None:
        """``parent`` 位置底下名為 ``name`` 的子資料夾的位置.

        ``path`` 是它的字面路徑、``info`` 是走訪時的 lstat。
        """
        anchor = self._anchor_at(path, info)
        if anchor is not None:
            return self._as_anchor(anchor)
        if parent is None:
            return None
        if parent.kind == "anchor":
            return MarkerState("custom_id", parent.anchor, (name,))
        rest = (*parent.parts, name)
        if parent.kind == "custom_id" and _shape(parent.anchor, rest, str.casefold):
            return MarkerState("zone", parent.anchor, rest)
        return None


def _stat_or_none(path: str) -> os.stat_result | None:
    try:
        return Path(path).stat()
    except (OSError, ValueError):
        return None


# --- 交接登記表（R7） ----------------------------------------------------


class HandoffKind(enum.Enum):
    """區內路徑轉成字串交出去的種類；測試絆線只放行該種類允許的事件。"""

    SUBPROCESS_CWD = "subprocess_cwd"
    PROC_FD = "proc_fd"
    WINDOWS_FILE_RESPONSE = "windows_file_response"


class _HandoffToken:
    __slots__ = ("key", "released")

    def __init__(self, key: tuple[str, HandoffKind]) -> None:
        self.key = key
        self.released = False


_handoffs: dict[tuple[str, HandoffKind], int] = {}
_handoffs_lock = threading.Lock()


def _handoff_key(text: str, kind: HandoffKind) -> tuple[str, HandoffKind]:
    return os.path.normcase(absolute_text(text)), kind


def _register_handoff(text: str, kind: HandoffKind) -> _HandoffToken:
    """登記一個交出去的字串。

    私有：只有 RM 的 ``zone_handoff`` 與 ``ZoneFileResponse`` 能呼叫（Z8 斷言），否則等於替
    任意字串申請絆線放行。
    """
    key = _handoff_key(text, kind)
    with _handoffs_lock:
        _handoffs[key] = _handoffs.get(key, 0) + 1
    return _HandoffToken(key)


def _release_handoff(token: _HandoffToken) -> None:
    """取消登記；同一個 token 重複取消不會多扣。"""
    with _handoffs_lock:
        if token.released:
            return
        token.released = True
        count = _handoffs.get(token.key, 0) - 1
        if count > 0:
            _handoffs[token.key] = count
        else:
            _handoffs.pop(token.key, None)


def is_handed_off(text: str, kind: HandoffKind) -> bool:
    """這個字串現在以這個種類登記中（測試絆線用的唯讀查詢）。"""
    with _handoffs_lock:
        return _handoff_key(text, kind) in _handoffs

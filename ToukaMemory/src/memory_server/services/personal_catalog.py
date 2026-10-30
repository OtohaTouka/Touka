"""個人空間目錄（`.touka/catalog.json`）.

系統替每個個人空間維護「我的檔案放在哪」：只記能從檔案系統重建的 metadata
（相對路徑、類型、大小、修改時間、內容 SHA-256、Markdown 標題），沒有摘要。

目錄只當快取，每次讀都以實際檔案為準：
- 只信任本行程最後一次自己寫出的內容；目錄檔被終端機等其他方式改過、壞掉、不存在，
  或行程剛啟動時，整份不信任、全部重算。
- 信任時，大小與修改時間都對得上的項目才沿用 hash 與標題，其餘重算；實際不存在的
  項目丟掉；不認得的欄位忽略。

檔案動詞的寫入類動作不能碰 `.touka/`；list／find／grep 看不到它。所有 I/O 經 resource proxy
的區內方法（個人空間的根是 ``ZonePath``，不跟隨連結；ADR-0293）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import weakref
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from memory_server.config_utils import get_resource_proxy
from memory_server.utils.no_follow import LinkRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from memory_server.protocols import IResourceProxy
    from memory_server.utils.zone_paths import ZonePath

logger = logging.getLogger(__name__)

CATALOG_DIR_NAME = ".touka"
CATALOG_FILE_NAME = "catalog.json"
CATALOG_VERSION = 1
# 目錄最多收幾個檔（超過就標 truncated）；hash 與標題只讀這個大小以內的檔。
CATALOG_MAX_ENTRIES = 2_000
CATALOG_READ_MAX_BYTES = 1024 * 1024
CATALOG_TITLE_MAX_CHARS = 200
CATALOG_HINT = "目錄可用：{count} 個檔，read .touka/catalog.json"
# list／find／grep 隱藏的名稱。
CATALOG_HIDDEN_NAMES = frozenset({CATALOG_DIR_NAME})
# 目錄掃描略過：任何深度的系統與雜訊目錄、根目錄的任務板與派工結果、寫檔暫存與鎖檔。
CATALOG_SKIP_DIR_NAMES = frozenset({CATALOG_DIR_NAME, ".git", "node_modules", "__pycache__"})
CATALOG_SKIP_ROOT_DIRS = frozenset({"missions", "dispatch_results"})
CATALOG_SKIP_FILE_GLOBS = (".*.tmp", "*.lock")

_CATALOG_NOTE = "System-maintained cache, rebuilt from the actual files on every read."
_WRITE_ACTIONS = frozenset({"write", "append", "str_replace", "delete"})
_SEARCH_ACTIONS = frozenset({"list", "find", "grep"})
_MARKDOWN = "markdown"
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_TYPE_BY_SUFFIX = {
    **dict.fromkeys((".md", ".markdown"), _MARKDOWN),
    **dict.fromkeys((".txt", ".log"), "text"),
    **dict.fromkeys((".json", ".csv", ".yaml", ".yml", ".toml", ".xml"), "data"),
    **dict.fromkeys((".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".html", ".css", ".sql"), "code"),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"), "image"),
    ".pdf": "pdf",
    **dict.fromkeys((".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"), "document"),
    **dict.fromkeys((".mp3", ".wav", ".ogg", ".m4a"), "audio"),
    **dict.fromkeys((".mp4", ".webm", ".mov"), "video"),
    **dict.fromkeys((".zip", ".tar", ".gz", ".7z"), "archive"),
}

# 每個 event loop、每個個人空間一把鎖（同回合並行的工具呼叫依序更新目錄）。
_catalog_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
# 本行程最後一次寫出的目錄內容 SHA-256（依個人空間）；對不上的目錄檔一律不信任。
_trusted_digests: dict[str, str] = {}


class CatalogPathDeniedError(PermissionError):
    """檔案動詞的寫入類動作碰到 `.touka/`；訊息固定、不含實體路徑。"""

    def __init__(self) -> None:
        super().__init__("'.touka/' is maintained by the system; file verbs cannot change it.")


class CatalogEntry(BaseModel):
    """一個檔案的 metadata（都能從檔案系統重算）。"""

    model_config = ConfigDict(extra="ignore")

    path: str
    type: str
    size: int = Field(ge=0)
    mtime_ns: int
    sha256: str | None = None
    title: str | None = None

    @field_validator("path")
    @classmethod
    def _relative_inside(cls, value: str) -> str:
        """只收留在個人空間內的相對 POSIX 路徑。"""
        pure = PurePosixPath(value)
        if not value or "\\" in value or ":" in value or pure.is_absolute() or ".." in pure.parts:
            msg = "catalog path must stay inside the personal drive"
            raise ValueError(msg)
        return value


class PersonalCatalog(BaseModel):
    """`.touka/catalog.json` 的內容。"""

    model_config = ConfigDict(extra="ignore")

    version: int = CATALOG_VERSION
    note: str = _CATALOG_NOTE
    file_count: int = 0
    truncated: bool = False
    files: list[CatalogEntry] = Field(default_factory=list)


def _root_key(root: ZonePath) -> str:
    return root.display()


def _lock_for(key: str) -> asyncio.Lock:
    locks = _catalog_locks.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


def _markdown_title(data: bytes) -> str | None:
    """第一個 Markdown ATX 標題（`# ...`），截到上限長度。"""
    for line in data.decode("utf-8", errors="replace").splitlines():
        match = _HEADING.match(line)
        if match:
            return match.group(1)[:CATALOG_TITLE_MAX_CHARS]
    return None


def _describe(
    proxy: IResourceProxy, root: ZonePath, rel: str, size: int, mtime_ns: int
) -> CatalogEntry:
    """由實際檔案算出一個項目；超過讀取上限或讀不到時不填 hash 與標題.

    ``rel`` 來自區內走訪，名稱不合格的項目走訪時已略過。
    """
    file_type = _TYPE_BY_SUFFIX.get(PurePosixPath(rel).suffix.lower(), "other")
    sha256 = title = None
    if size <= CATALOG_READ_MAX_BYTES:
        try:
            data = proxy.zone_read_binary(root.child(*rel.split("/")), CATALOG_READ_MAX_BYTES)
        except OSError:
            logger.warning("Personal catalog could not read a file; hash skipped")
        else:
            sha256 = hashlib.sha256(data).hexdigest()
            title = _markdown_title(data) if file_type == _MARKDOWN else None
    return CatalogEntry(
        path=rel, type=file_type, size=size, mtime_ns=mtime_ns, sha256=sha256, title=title
    )


def _load_trusted(
    proxy: IResourceProxy, catalog_path: ZonePath, key: str
) -> tuple[str | None, dict[str, CatalogEntry]]:
    """回（目前目錄檔文字, 可沿用的項目）；只沿用本行程自己寫出、格式正確的內容.

    讀不到（不存在、壞掉、路上有連結）都當沒有；連結由之後的寫回拒絕。
    """
    try:
        text = proxy.zone_read_text(catalog_path)
    except (OSError, UnicodeDecodeError):
        return None, {}
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != _trusted_digests.get(key):
        return text, {}
    try:
        cached = PersonalCatalog.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        return text, {}
    return text, {entry.path: entry for entry in cached.files}


def _rebuild(proxy: IResourceProxy, root: ZonePath) -> PersonalCatalog:
    """掃實際檔案、比對快取、內容有變才原子寫回（同步；呼叫端持鎖）.

    讀寫都不跟隨連結；根本身是連結就拒絕（``ZoneLinkRefusedError``）。
    """
    if proxy.zone_kind(root) == "missing":
        return PersonalCatalog()
    key = _root_key(root)
    catalog_path = root.child(CATALOG_DIR_NAME, CATALOG_FILE_NAME)
    old_text, trusted = _load_trusted(proxy, catalog_path, key)
    rows = proxy.zone_stat_files(
        root,
        exclude_names=CATALOG_SKIP_DIR_NAMES,
        exclude_paths=CATALOG_SKIP_ROOT_DIRS,
        exclude_globs=CATALOG_SKIP_FILE_GLOBS,
        max_files=CATALOG_MAX_ENTRIES + 1,
    )
    entries: list[CatalogEntry] = []
    for rel, size, mtime_ns in rows[:CATALOG_MAX_ENTRIES]:
        cached = trusted.get(rel)
        if cached is not None and cached.size == size and cached.mtime_ns == mtime_ns:
            entries.append(cached)
        else:
            entries.append(_describe(proxy, root, rel, size, mtime_ns))
    entries.sort(key=lambda entry: entry.path)
    catalog = PersonalCatalog(
        file_count=len(entries), truncated=len(rows) > CATALOG_MAX_ENTRIES, files=entries
    )
    text = json.dumps(catalog.model_dump(), ensure_ascii=False, indent=2)
    if text != old_text:
        proxy.zone_write_text(catalog_path, text)
    _trusted_digests[key] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return catalog


async def refresh(root: ZonePath, proxy: IResourceProxy | None = None) -> PersonalCatalog:
    """以實際檔案重建並回傳個人空間目錄；同一個個人空間一次只跑一個。"""
    io = proxy or get_resource_proxy()
    async with _lock_for(_root_key(root)):
        return await asyncio.to_thread(_rebuild, io, root)


async def _refresh_logged(root: ZonePath, proxy: IResourceProxy) -> PersonalCatalog | None:
    """更新目錄；失敗只記 log（檔案動作本身已完成，下次讀目錄時會再比對）。"""
    try:
        return await refresh(root, proxy)
    except Exception:
        logger.exception("Personal catalog refresh failed")
        return None


async def run_vault_action(
    handler: Callable[..., Awaitable[Any]],
    action: str,
    root: ZonePath,
    target: ZonePath | None,
    kwargs: dict[str, Any],
) -> Any:
    """個人空間的檔案動作：拒寫 `.touka/`、隱藏 `.touka/`、寫入後更新並在讀時比對目錄。

    ``target`` 是檔案動詞放好的目標（``core_file_ops.drive_target``）；越界或無效時是 None，
    交給檔案動詞報錯。

    Raises:
        CatalogPathDeniedError: 寫入類動作的目標落在 `.touka/` 底下（不分大小寫）。
    """
    proxy = kwargs.get("resource_proxy") or get_resource_proxy()
    kwargs["resource_proxy"] = proxy
    if (
        action in _WRITE_ACTIONS
        and target is not None
        and any(part.casefold() == CATALOG_DIR_NAME for part in target.relative_parts(root))
    ):
        raise CatalogPathDeniedError
    if action in _SEARCH_ACTIONS:
        kwargs["exclude"] = CATALOG_HIDDEN_NAMES
    if action == "read" and target == root.child(CATALOG_DIR_NAME, CATALOG_FILE_NAME):
        async with _lock_for(_root_key(root)):
            try:
                await asyncio.to_thread(_rebuild, proxy, root)
            except LinkRefusedError:
                # 個人空間或 `.touka` 是連結：不重建，讀檔交給 core ops 回固定的連結訊息
                logger.warning("Personal catalog not rebuilt: its folder went through a link")
            return await handler(root_bound=root, **kwargs)

    result = await handler(root_bound=root, **kwargs)
    if action in _WRITE_ACTIONS and str(result).startswith("Success"):
        await _refresh_logged(root, proxy)
    elif action == "list" and target == root and not str(result).startswith("Error"):
        catalog = await _refresh_logged(root, proxy)
        if catalog is not None:
            result = f"{result}\n{CATALOG_HINT.format(count=catalog.file_count)}"
    return result

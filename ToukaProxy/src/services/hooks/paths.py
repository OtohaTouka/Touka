"""Hook 指令的路徑界線（ADR-0292）.

hook 以 Touka 自己的權限、不經隔離執行。代理人改得到的程式或腳本若能被 hook 執行，就等於
不經審批跑程式，所以存檔與每次執行前都查指令與參數裡的路徑：

- 參數：拆成字詞逐一檢查（``--script=x``、``-c "… /path …"``、POSIX 用 ``:`` 串的路徑清單也算），
  每個看起來像路徑的字詞都必須在設定資料夾的 ``hooks/`` 裡（相對路徑以 ``hooks/`` 為準，那是 hook
  的工作目錄）；``file:`` 網址一律拒收。這個判斷只看 ``hooks/`` 本身，不看專案清單一類會變動的資料。
- 指令：在 ``hooks/`` 裡時照參數的規則；在別處（例如系統的直譯器）時不能落在代理人可能寫得到
  的地方：資料根、隔離暫存區、執行器資料夾、工作區根（使用者能在底下自己登記專案）、每個專案根、
  MCP 伺服器的可寫資料夾，容器裡另加鏡像掛載根（同一批檔案的另一個路徑）；設定資料夾裡
  ``hooks/`` 以外的地方也不行（``skills``、``mcp_servers``、``evals`` 由創建與演化寫入）。
- ``hooks/`` 裡：字面與解析後都要在 ``hooks/`` 裡，``hooks/`` 本身與底下經過的每一層都不能是
  連結（symlink、junction），檔案不能有其他硬連結（另一個名字可能在代理人資料夾裡）。
- ``hooks/`` 以外：比對原樣的絕對路徑、解析連結後的路徑、以及每一層上層資料夾的檔案識別碼
  （``st_dev``＋``st_ino``，抓大小寫、8.3 短名與 bind mount 別名）；路徑上每一個連結的所在位置
  與它直接指向的地方都要查。
- 任何 ``..`` 一律拒收；指令必須是存在的絕對路徑一般檔。

hook 程式自己執行時再去讀哪裡，Touka 管不到；這裡只擋設定裡寫得出來的路徑。
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from src.core.exceptions import ErrorCode, ToukaException
from src.services.isolation import profiles
from src.services.mcp_isolation_store import McpIsolationStore
from src.services.resource_manager import resource_manager
from src.utils.async_db import list_project_root_paths

if TYPE_CHECKING:
    from src.models.hooks import CommandHook

HOOKS_DIR_NAME: Final[str] = "hooks"
MAX_LINK_HOPS: Final[int] = 40

PathReason = Literal[
    "not_absolute",
    "missing",
    "not_file",
    "parent_segment",
    "file_url",
    "unc_path",
    "agent_writable",
    "outside_hooks_dir",
    "linked_path",
    "hard_link",
    "link_loop",
    "unverifiable",
]

# 參數拆字詞：空白、引號與 shell／程式碼常見的分隔符號（``=`` 讓 ``--opt=/abs`` 也拆得開）。
_TOKEN_SPLIT = re.compile(r"[\s\"'`;&|()<>,=\[\]{}]+")
# 路徑清單的分隔符號：POSIX 是 ``:``（``-cp a.jar:/data/x.jar``）；Windows 是 ``;``，上面已經拆了，
# 而 Windows 的 ``:`` 是磁碟代號，不能拆。
_PATH_LIST_SEPARATOR: Final[str] = os.pathsep
# ``file:`` 網址看不出最後讀哪個檔，一律拒收（不分大小寫）；字詞中間的也算（``jar:file:/x``），
# 前面緊接英數字的不算（``--profile:dev``）。
_FILE_URL = re.compile(r"(?<![a-z0-9])file:", re.IGNORECASE)
# 黏在單字母選項後面的路徑（``-I/data/lib``、``-oD:\data\x``、``-oD:x``），以及 ``@`` 後面的
# 參數檔（``@/data/args.txt``）。只認單字母選項，``-Ilib/sub`` 這種相對路徑不拆。
_GLUED_PATH = re.compile(r"^(?:-[A-Za-z](?=[A-Za-z]:|[\\/])|@)(?P<path>.+)$")
# 字詞中間 ``:``／``@`` 後面接著的路徑（``-javaagent:D:/x.jar``、``--rcfile:/x``、``pkg@/x``）：
# Windows 的 ``:`` 是磁碟代號、上面不拆，所以另外取出來。磁碟代號開頭（含 ``D:x`` 這種相對於
# 磁碟目前目錄、之後判成看不出指哪裡的寫法）、反斜線與斜線開頭都算。
_ABSOLUTE_START = re.compile(r"[A-Za-z]:|[\\/]")
# 網址的 scheme（可以串接：``jdbc:postgresql://``）。``:`` 前面整段是 scheme、後面接 ``//`` 才當
# 網址略過；``-javaagent://host/share/x``、``pkg@//host/x`` 照路徑檢查（Windows 上是 UNC）。
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*(?::[A-Za-z][A-Za-z0-9+.\-]*)*")
_DRIVE_LETTER = re.compile(r"[A-Za-z]")
_SEPARATORS = re.compile(r"[\\/]")
_VERBATIM_PREFIX = "\\\\?\\"
_VERBATIM_UNC_PREFIX = "\\\\?\\UNC\\"
# 檔名不合法、太長一類的錯誤當成「不存在」；權限錯誤才算查不了。
_NONEXISTENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EINVAL, errno.ENAMETOOLONG})


@dataclass(frozen=True)
class HookZones:
    """路徑界線需要的資料夾：設定資料夾與 ``hooks/`` 以外的指令不能落在的地方。"""

    config_dir: Path
    forbidden: tuple[Path, ...]

    @property
    def hooks_dir(self) -> Path:
        """Hook 腳本放的地方，也是 hook 的工作目錄。"""
        return self.config_dir / HOOKS_DIR_NAME


@dataclass(frozen=True)
class PathProblem:
    """一個被拒收的路徑；``where`` 是 ``command`` 或 ``args[i]``。"""

    reason: PathReason
    where: str
    value: str


def _norm(path: Path | str) -> Path:
    return Path(os.path.normcase(Path(path).absolute()))


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _strip_verbatim(text: str) -> str:
    """去掉 Windows 的 verbatim 前綴（junction 的 readlink 會帶）；UNC 版本保留。"""
    if text.startswith(_VERBATIM_PREFIX) and not text.startswith(_VERBATIM_UNC_PREFIX):
        return text[len(_VERBATIM_PREFIX) :]
    return text


def _is_unc(text: str) -> bool:
    if sys.platform != "win32":
        return False
    return text.startswith(("\\\\", "//")) or Path(text).drive.startswith(("\\\\", "//"))


def _has_parent_segment(text: str) -> bool:
    return ".." in _SEPARATORS.split(text)


def _stat_or_none(path: Path, *, follow: bool) -> os.stat_result | None:
    """不存在（含檔名不合法）回 None；權限錯誤與其他錯誤照丟，呼叫端當成查不了。"""
    try:
        return path.stat() if follow else path.lstat()
    except PermissionError:
        raise
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno in _NONEXISTENT_ERRNOS:
            return None
        raise


def _lstat(path: Path) -> os.stat_result | None:
    return _stat_or_none(path, follow=False)


def _stat(path: Path) -> os.stat_result | None:
    return _stat_or_none(path, follow=True)


def _identity(info: os.stat_result | None) -> tuple[int, int] | None:
    if info is None or info.st_ino == 0:
        return None  # 檔案系統不給識別碼時不比對（另外兩種比對照做）
    return (info.st_dev, info.st_ino)


def _is_link(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return sys.platform == "win32" and path.is_junction()


class _ZoneChecker:
    """對一份 ``HookZones`` 判斷路徑是否落在代理人寫得到的地方。"""

    def __init__(self, zones: HookZones) -> None:
        self._hooks_path = Path(zones.hooks_dir).absolute()
        self._hooks_dir = _norm(zones.hooks_dir)
        self._config_dir = _norm(zones.config_dir)
        lexical: set[Path] = set()
        identities: set[tuple[int, int]] = set()
        for root in zones.forbidden:
            lexical.add(_norm(root))
            lexical.add(_norm(Path(root).resolve()))
            if (ident := _identity(_stat(Path(root)))) is not None:
                identities.add(ident)
        self._forbidden = tuple(lexical)
        self._forbidden_ids = frozenset(identities)
        self._config_id = _identity(_stat(Path(zones.config_dir)))
        self._hooks_id = _identity(_stat(Path(zones.hooks_dir)))

    def _lexically_forbidden(self, path: Path) -> bool:
        norm = _norm(path)
        if any(_within(norm, root) for root in self._forbidden):
            return True
        return _within(norm, self._config_dir) and not _within(norm, self._hooks_dir)

    def _identity_forbidden(self, path: Path) -> bool:
        """往上每一層存在的資料夾：識別碼是代理人資料夾、或設定資料夾裡 hooks 以外的子項就擋。"""
        child_id: tuple[int, int] | None = None
        child_exists = False
        for ancestor in (path, *path.parents):
            ident = _identity(_stat(ancestor))
            if ident is None:
                child_id, child_exists = None, False
                continue
            if ident in self._forbidden_ids:
                return True
            in_config = self._config_id is not None and ident == self._config_id
            if in_config and (not child_exists or child_id != self._hooks_id):
                return True
            child_id, child_exists = ident, True
        return False

    def reason(self, path: Path, depth: int = 0) -> PathReason | None:
        """``path`` 是絕對路徑；回傳拒收原因或 None。"""
        if depth > MAX_LINK_HOPS:
            return "link_loop"
        text = str(path)
        if _is_unc(text):
            return "unc_path"
        lexical = path.absolute()
        if self._lexically_forbidden(lexical):
            return "agent_writable"
        for index in range(1, len(lexical.parts) + 1):
            prefix = Path(*lexical.parts[:index])
            info = _lstat(prefix)
            if info is None:
                break
            if not _is_link(prefix, info):
                continue
            target = Path(_strip_verbatim(str(prefix.readlink())))
            if not target.is_absolute():
                target = prefix.parent / target
            if _has_parent_segment(str(target)):
                target = Path(os.path.normpath(target))
            hop = self.reason(target.joinpath(*lexical.parts[index:]), depth + 1)
            if hop is not None:
                return hop
            break  # 後半段已由那一跳檢查過
        resolved = lexical.resolve()
        if self._lexically_forbidden(resolved):
            return "agent_writable"
        if self._identity_forbidden(lexical) or self._identity_forbidden(resolved):
            return "agent_writable"
        return None

    def in_hooks(self, path: Path) -> bool:
        """字面上在 ``hooks/`` 裡（大小寫依平台）。"""
        return _within(_norm(path), self._hooks_dir)

    def resolves_into_hooks(self, path: Path) -> bool:
        """其實在 ``hooks/`` 裡（字面不在時用的是別名）。

        解析後在裡面（連結、短檔名、結尾的點），或上層有一層的檔案識別碼就是 ``hooks/``
        （bind mount 這類解析不出來的別名）。
        """
        lexical = path.absolute()
        if self.in_hooks(lexical.resolve()):
            return True
        if self._hooks_id is None:
            return False
        return any(_identity(_stat(ancestor)) == self._hooks_id for ancestor in lexical.parents)

    def hooks_reason(self, path: Path) -> PathReason | None:
        """``path`` 必須在 ``hooks/`` 裡：字面與解析後都在、一路沒有連結、沒有其他硬連結。"""
        lexical = path.absolute()
        if not self.in_hooks(lexical):
            return "outside_hooks_dir"
        current = self._hooks_path
        for part in (None, *lexical.parts[len(self._hooks_dir.parts) :]):
            if part is not None:
                current = current / part
            info = _lstat(current)
            if info is None:
                break  # 還不存在的部分（例如 ``-c`` 這類不是檔案的字詞）
            if _is_link(current, info):
                return "linked_path"
        if not self.in_hooks(lexical.resolve()):
            return "outside_hooks_dir"
        info = _stat(lexical)
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return "hard_link"
        return None


def _safe_reason(checker: _ZoneChecker, path: Path, *, hooks_only: bool) -> PathReason | None:
    try:
        return checker.hooks_reason(path) if hooks_only else checker.reason(path)
    except OSError:
        return "unverifiable"


def _command_problems(checker: _ZoneChecker, command: str) -> list[PathProblem]:
    def problem(reason: PathReason) -> list[PathProblem]:
        return [PathProblem(reason=reason, where="command", value=command)]

    if _has_parent_segment(command):
        return problem("parent_segment")
    if _is_unc(command):
        return problem("unc_path")
    path = Path(command)
    if not path.is_absolute():
        return problem("not_absolute")
    try:
        # hooks/ 裡的程式要照字面寫 hooks/ 裡的路徑：用別名指進去的也走 hooks/ 的檢查（會被拒），
        # 才查得到硬連結
        hooks_only = checker.in_hooks(path) or checker.resolves_into_hooks(path)
    except OSError:
        return problem("unverifiable")
    if (reason := _safe_reason(checker, path, hooks_only=hooks_only)) is not None:
        return problem(reason)
    try:
        info = _stat(path)
    except OSError:
        return problem("unverifiable")
    if info is None:
        return problem("missing")
    if not stat.S_ISREG(info.st_mode):
        return problem("not_file")
    return []


def _is_drive_colon(word: str, index: int) -> bool:
    """``word[index]`` 是磁碟代號的 ``:``（``D:`` 在字詞開頭，或緊接在 ``:``／``@`` 後面）。"""
    return (
        word[index] == ":"
        and index >= 1
        and _DRIVE_LETTER.fullmatch(word[index - 1]) is not None
        and (index == 1 or word[index - 2] in ":@")
    )


def _embedded_paths(word: str) -> list[str]:
    found: list[str] = []
    for index, char in enumerate(word):
        if char not in ":@" or _is_drive_colon(word, index):
            continue
        rest = word[index + 1 :]
        if not _ABSOLUTE_START.match(rest):
            continue
        if char == ":" and rest.startswith("//") and _URL_SCHEME.fullmatch(word[:index]):
            continue  # 網址
        found.append(rest)
    return found


def _argument_tokens(arg: str) -> list[str]:
    words = [word for word in _TOKEN_SPLIT.split(arg) if word]
    glued = [match["path"] for word in words if (match := _GLUED_PATH.match(word))]
    glued += [path for word in words for path in _embedded_paths(word)]
    words += [path for path in glued if path not in words]
    parts = [part for word in words for part in word.split(_PATH_LIST_SEPARATOR) if part]
    return words + [part for part in parts if part not in words]


def _argument_problems(
    checker: _ZoneChecker, zones: HookZones, arg: str, where: str
) -> list[PathProblem]:
    def problem(reason: PathReason) -> list[PathProblem]:
        return [PathProblem(reason=reason, where=where, value=arg)]

    tokens = (arg, *_argument_tokens(arg))
    if any(_FILE_URL.search(token) for token in tokens):
        return problem("file_url")
    for token in tokens:
        if _has_parent_segment(token):
            return problem("parent_segment")
        if _is_unc(token):
            return problem("unc_path")
        candidate = Path(token)
        if candidate.drive and not candidate.is_absolute():
            return problem("not_absolute")  # ``C:foo``：相對於某個磁碟的目前目錄，看不出指哪裡
        if not candidate.is_absolute():
            candidate = zones.hooks_dir / candidate
        if (reason := _safe_reason(checker, candidate, hooks_only=True)) is not None:
            return problem(reason)
    return []


def command_path_problems(hook: CommandHook, zones: HookZones) -> list[PathProblem]:
    """``hook`` 的指令與參數中被拒收的路徑（空清單＝可以執行）。"""
    checker = _ZoneChecker(zones)
    problems = _command_problems(checker, hook.command)
    for index, arg in enumerate(hook.args):
        problems.extend(_argument_problems(checker, zones, arg, f"args[{index}]"))
    return problems


def _mcp_writable_folders(config_dir: Path) -> list[Path]:
    """MCP 伺服器的可寫資料夾（不論紀錄是否還對得上條目）；隔離設定檔讀不到就丟例外。"""
    snapshot = McpIsolationStore(config_dir).load()
    if snapshot.state == "unreadable":
        raise ToukaException(ErrorCode.PERSISTENCE_ERROR)
    return [
        Path(grant.path)
        for record in snapshot.records.values()
        for grant in record.sandbox_paths
        if grant.access == "rw"
    ]


async def current_zones() -> HookZones:
    """依目前設定組出 ``hooks/`` 以外的指令不能落在的地方；讀不到就丟例外（呼叫端當成不能執行）。

    專案根只影響 ``hooks/`` 以外的指令；一般使用者只能把專案登記在工作區根底下，工作區根整個
    已在禁區裡，所以他們登記或撤掉專案都不會改變結果。
    """
    layout = profiles.current_layout()
    forbidden: list[Path] = [layout.data_root]
    forbidden.extend(p for p in (layout.state_root, layout.executor_dir) if p is not None)
    if layout.in_container:
        forbidden.extend(layout.mirror_roots)
    workspace = await resource_manager.get_workspace_jail_root()
    for root in (str(workspace), *await list_project_root_paths()):
        forbidden.append(Path(root))
        translated = Path(await resource_manager.translate_path(root))
        if translated != Path(root):
            forbidden.append(translated)
    forbidden.extend(_mcp_writable_folders(layout.config_dir))
    return HookZones(
        config_dir=layout.config_dir,
        forbidden=tuple(p for p in forbidden if p.is_absolute()),
    )

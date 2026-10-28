"""Hook 指令的路徑界線（ADR-0292）.

hook 以 Touka 自己的權限、不經隔離執行。代理人改得到的程式或腳本若能被 hook 執行，就等於
不經審批跑程式，所以指令與參數裡的路徑只要落在代理人寫得到的地方就拒收，存檔與每次執行前都查：

- 代理人寫得到的地方：資料根、隔離暫存區、執行器資料夾、所有已登記的專案根目錄；容器裡另加
  鏡像掛載根（同一批檔案的另一個路徑）。設定資料夾裡只有 ``hooks/`` 可以放（``skills``、
  ``mcp_servers``、``evals`` 由創建與演化寫入）。
- 比對三種寫法：原樣的絕對路徑、解析連結後的路徑、以及每一層上層資料夾的檔案識別碼
  （``st_dev``＋``st_ino``，抓大小寫、8.3 短名與 bind mount 別名）。
- 連結：路徑上每一個連結（symlink、junction）的所在位置與它直接指向的地方都要查，所以經過
  代理人資料夾裡的連結再指回外面也會被擋。
- 指令必須是存在的絕對路徑一般檔；參數裡任何 ``..`` 一律拒收；``hooks/`` 裡的檔案有多個硬連結
  時拒收（另一個名字可能在代理人資料夾裡）。
- 參數會拆成字詞逐一檢查（``--script=x``、``-c "… /path …"`` 也算）；相對路徑以 ``hooks/`` 為準
  （hook 的工作目錄）。

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

from src.services.isolation import profiles
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
    "unc_path",
    "agent_writable",
    "hard_link",
    "link_loop",
    "unverifiable",
]

# 參數拆字詞：空白、引號與 shell／程式碼常見的分隔符號（``=`` 讓 ``--opt=/abs`` 也拆得開）。
_TOKEN_SPLIT = re.compile(r"[\s\"'`;&|()<>,=\[\]{}]+")
_SEPARATORS = re.compile(r"[\\/]")
_VERBATIM_PREFIX = "\\\\?\\"
_VERBATIM_UNC_PREFIX = "\\\\?\\UNC\\"
# 檔名不合法、太長一類的錯誤當成「不存在」；權限錯誤才算查不了。
_NONEXISTENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EINVAL, errno.ENAMETOOLONG})


@dataclass(frozen=True)
class HookZones:
    """路徑界線需要的資料夾：設定資料夾與代理人寫得到的地方。"""

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

    def hard_linked_in_hooks(self, path: Path) -> bool:
        resolved = path.resolve()
        info = _stat(resolved)
        return (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and info.st_nlink > 1
            and _within(_norm(resolved), self._hooks_dir)
        )


def _safe_reason(checker: _ZoneChecker, path: Path) -> PathReason | None:
    try:
        return checker.reason(path)
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
    if (reason := _safe_reason(checker, path)) is not None:
        return problem(reason)
    try:
        info = _stat(path)
    except OSError:
        return problem("unverifiable")
    if info is None:
        return problem("missing")
    if not stat.S_ISREG(info.st_mode):
        return problem("not_file")
    if checker.hard_linked_in_hooks(path):
        return problem("hard_link")
    return []


def _argument_tokens(arg: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(arg) if token]


def _argument_problems(
    checker: _ZoneChecker, zones: HookZones, arg: str, where: str
) -> list[PathProblem]:
    def problem(reason: PathReason) -> list[PathProblem]:
        return [PathProblem(reason=reason, where=where, value=arg)]

    for token in (arg, *_argument_tokens(arg)):
        if _has_parent_segment(token):
            return problem("parent_segment")
        if _is_unc(token):
            return problem("unc_path")
        candidate = Path(token)
        if candidate.drive and not candidate.is_absolute():
            return problem("not_absolute")  # ``C:foo``：相對於某個磁碟的目前目錄，看不出指哪裡
        if not candidate.is_absolute():
            candidate = zones.hooks_dir / candidate
        if (reason := _safe_reason(checker, candidate)) is not None:
            return problem(reason)
        try:
            if checker.hard_linked_in_hooks(candidate):
                return problem("hard_link")
        except OSError:
            return problem("unverifiable")
    return []


def command_path_problems(hook: CommandHook, zones: HookZones) -> list[PathProblem]:
    """``hook`` 的指令與參數中被拒收的路徑（空清單＝可以執行）。"""
    checker = _ZoneChecker(zones)
    problems = _command_problems(checker, hook.command)
    for index, arg in enumerate(hook.args):
        problems.extend(_argument_problems(checker, zones, arg, f"args[{index}]"))
    return problems


async def current_zones() -> HookZones:
    """依目前設定組出路徑界線；專案清單讀不到就丟例外（呼叫端當成不能執行）。"""
    layout = profiles.current_layout()
    forbidden: list[Path] = [layout.data_root]
    forbidden.extend(p for p in (layout.state_root, layout.executor_dir) if p is not None)
    if layout.in_container:
        forbidden.extend(layout.mirror_roots)
    for root in await list_project_root_paths():
        forbidden.append(Path(root))
        translated = Path(await resource_manager.translate_path(root))
        if translated != Path(root):
            forbidden.append(translated)
    return HookZones(
        config_dir=layout.config_dir,
        forbidden=tuple(p for p in forbidden if p.is_absolute()),
    )

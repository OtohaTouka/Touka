"""MCP 伺服器的來源分類（C2-3，純函式；禁令 18）.

分類只看兩樣東西：呼叫端交進來的紀錄狀態（origin 原值、讀不到等，本模組不讀任何存放位置），
與條目自己引用的路徑。引用路徑只讀檔案系統的中繼資料（lstat、resolve、往上檢查 Touka 目錄時
列幾個上層資料夾），不讀內容、不寫入；呼叫端在事件迴圈上請放進執行緒。

啟發式對所有條目都套用、只會更嚴：任一參數或環境路徑引用到代理人影響得到的位置，
就當自建，不論 origin、用途或路徑是否已存在。解析結果不改寫啟動參數。
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import unquote, urlsplit

from memory_server.utils.zone_paths import (
    ALL_ANCHORS,
    Anchor,
    ZoneRoots,
    anchor_at,
    contains_zone,
    zone_of,
)

from src.services.isolation.project_scan import layout_anchors
from src.utils.isolation_policy import (
    MCP_ORIGINS,
    MCP_SCAFFOLD_ORIGIN,
    MCP_SELF_ORIGINS,
    is_safe_segment,
)
from src.utils.mcp_sentinels import APPDATA_SENTINEL, OUTPUT_DIR_SENTINEL, resolve_env_sentinel
from src.utils.touka_dirs import upward_verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from src.services.isolation.profiles import SandboxLayout
    from src.utils.isolation_policy import McpClass, McpOrigin

# 會載入程式碼的環境變數（名稱比對不分大小寫）：值的每一項落在代理人影響得到的地方就是自建，
# 資料夾也算；值先以空白與 ``os.pathsep`` 切開（``NODE_OPTIONS="--require x"`` 這類）
CODE_LOADING_ENV_NAMES: frozenset[str] = frozenset(
    name.casefold()
    for name in (
        "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
        "PYTHONPYCACHEPREFIX", "NODE_PATH", "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH",
        "LD_AUDIT", "PERL5LIB", "PERL5OPT", "RUBYLIB", "RUBYOPT", "GEM_PATH", "CLASSPATH",
        "JAVA_TOOL_OPTIONS", "UV_TOOL_DIR", "npm_config_prefix", "BASH_ENV", "ENV",
    )
)  # fmt: skip
_SEPARATORS = ("/", "\\")
_URL_MARK = "://"
_FILE_URL = "file://"
_PACKAGE_RUNNERS = frozenset({"npx", "uvx", "bunx"})


def _comparison_path(path: Path) -> Path:
    return Path(str(path).casefold()) if sys.platform == "darwin" else path


def _code_env(name: str) -> bool:
    name = name.casefold()
    return (name in CODE_LOADING_ENV_NAMES or name == "git_exec_path"
            or any(part in name for part in ("find_links", "index", "registry"))
            or name.endswith(("_command", "_cache_dir", "_install_dir")))

RecordState = Literal["absent", "present", "unreadable"]


class McpEntry(Protocol):
    """分類會看的四個欄位（``ProtocolServerConfig`` 的形狀）。"""

    @property
    def command(self) -> str | None:
        """Stdio 的指令（遠端條目是 None）。"""
        ...

    @property
    def args(self) -> Sequence[str]:
        """啟動參數。"""
        ...

    @property
    def env(self) -> Mapping[str, str] | None:
        """條目自己的環境變數（可含哨兵）。"""
        ...

    @property
    def url(self) -> str | None:
        """遠端條目的網址。"""
        ...


@dataclass(frozen=True)
class OriginRecord:
    """呼叫端讀隔離設定檔、核對 ``bound`` 之後交進來的紀錄狀態。

    ``absent``：沒有紀錄（舊條目、``bound`` 不符的第三方類）；``present``：``origin`` 是紀錄裡的
    原值（可能看不懂）；``unreadable``：隔離設定檔存在卻讀不到（一律當自建）。
    """

    state: RecordState
    origin: object = None


NO_RECORD = OriginRecord("absent")
UNREADABLE_RECORD = OriginRecord("unreadable")


@dataclass(frozen=True)
class McpClassification:
    """分類結果；``reason`` 是判定依據的代碼（只有代碼，沒有路徑）。

    ``managed_skills``：條目引用到的 ``managed/<skill>``（演化部署的程式資料夾，唯讀＋執行）；
    ``scaffold_dir``：scaffold 自己的 ``config/mcp_servers/<name>``（只有 scaffold 有）。
    """

    kind: McpClass
    origin: McpOrigin | None
    reason: str
    managed_skills: frozenset[str] = frozenset()
    scaffold_dir: Path | None = None


@dataclass(frozen=True)
class PathReference:
    """引用路徑；``code_dir`` 用於來源分類，``freeze_ancestor`` 決定是否凍結下層技能。"""

    path: Path
    code_dir: bool = False
    program: bool = False
    freeze_ancestor: bool = False


def _resolve(path: Path) -> Path:
    return path.resolve()


def proxy_base_dir() -> Path:
    """``classify``／``referenced_paths`` 的 ``base_dir``（全專案只此一份）.

    ＝未隔離 MCP 子行程繼承的工作目錄：proxy 行程的 cwd（``StdioServerParameters`` 不給 cwd）。
    不要改成固定路徑，除非同時把子行程的 cwd 明確設成同一個值。會讀行程狀態，在事件迴圈上
    和 ``classify`` 一起放進 ``asyncio.to_thread``。
    """
    return Path.cwd().resolve()


def _under(path: Path, root: Path) -> bool:
    path, root = _comparison_path(path), _comparison_path(root)
    return path == root or path.is_relative_to(root)


def _docgen_relative() -> Path:
    """Docgen 輸出夾相對 data 根的位置（SSOT 是 ``mcp_sentinels``；純路徑運算）。"""
    return Path(resolve_env_sentinel(OUTPUT_DIR_SENTINEL)).relative_to(
        resolve_env_sentinel(APPDATA_SENTINEL)
    )


def docgen_output_root(layout: SandboxLayout) -> Path:
    """第三方共用、代理人可經 docgen 影響內容的輸出夾（``@TOUKA_OUTPUT@``）。"""
    return _resolve(layout.data_root) / _docgen_relative()


def personal_drive_root(layout: SandboxLayout) -> Path:
    """所有代理人個人空間的上層（第三方的 APPDATA 對映到這裡）。"""
    return _resolve(layout.data_root) / "personal_drive"


def managed_root(layout: SandboxLayout) -> Path:
    """演化部署的程式資料夾的上層（``managed/<skill>``）。"""
    return _resolve(layout.data_root) / "mcp_servers" / "managed"


def resolve_reference_sentinel(value: str, layout: SandboxLayout) -> str:
    """哨兵換成版面上的路徑（APPDATA → data 根、OUTPUT → docgen 輸出夾）；其他原樣。"""
    if value == APPDATA_SENTINEL:
        return str(_resolve(layout.data_root))
    if value == OUTPUT_DIR_SENTINEL:
        return str(docgen_output_root(layout))
    return value


def _item_paths(item: str, base_dir: Path, *, code: bool = False) -> Iterator[Path]:
    """字串裡的路徑：``os.pathsep`` 切開；``file://`` 取路徑、其他網址略過；相對的要有分隔符。"""
    pieces = (item,) if _URL_MARK in item else item.split(os.pathsep)
    for raw in pieces:
        piece = raw
        if _URL_MARK in piece:
            if _FILE_URL not in piece:
                continue
            url = urlsplit(piece)
            piece = unquote(url.path)
            if url.netloc:
                piece = "//" + url.netloc + piece
            elif os.name == "nt" and re.match(r"^/[A-Za-z]:", piece):
                piece = piece[1:]
        if not piece or "\x00" in piece:
            continue
        path = Path(piece)
        if path.is_absolute():
            yield path
        elif (any(sep in piece for sep in _SEPARATORS) or (base_dir / path).exists()
              or (code and path.suffix)):
            yield base_dir / path


def _token_words(token: str) -> tuple[str, ...]:
    words = re.findall(r'''(?:"[^"]*"|'[^']*'|[^\s;|&()"'])+''', token)
    return tuple(dict.fromkeys((token, *words)))


def _expandable_reference(entry: McpEntry) -> bool:
    """不猜 shell／目標程式如何展開；無法確定位置的值不取得第三方待遇。"""
    values = (entry.command or "", *entry.args, *(entry.env or {}).values())
    for token in values:
        for word in _token_words(token):
            value = re.sub(r"^(?:--?[\w-]+|[A-Za-z_]\w*)=", "", word.strip("\"'")).strip("\"'")
            if re.search(r"\$|%[^%\s]+%|![^!\s]+!", value):
                return True
            if (re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", value)
                    and not value.startswith(_FILE_URL)):
                continue
            if re.search(r'''[`*?\[{"']''', value):
                return True
            if re.search(r'''(?:^|[\s=:"'/\\])~''', value):
                return True
    return False


def _token_paths(token: str, base_dir: Path, *, code: bool = False) -> Iterator[Path]:
    """保留引號內的空白與 Windows 分隔符；選項或賦值另檢查 ``=`` 後面的路徑。"""
    for word in _token_words(token):
        word = word.strip("\"'")
        if not word.startswith("-"):
            yield from _item_paths(word, base_dir, code=code)
        if "=" in word:
            value = word.split("=", 1)[1].strip("\"'")
            yield from _item_paths(value, base_dir, code=code)


def referenced_paths(
    entry: McpEntry, layout: SandboxLayout, *, base_dir: Path,
    scaffold_appdata: bool = False, infer_suffix: bool = True,
) -> tuple[PathReference, ...]:
    """條目引用的路徑（哨兵解析後）：``command``、每個參數、每個 env 值。

    ``base_dir`` 是要檢查的 cwd；授權不以副檔名推測普通名稱，使用 ``infer_suffix=False``。
    """
    refs: list[PathReference] = []
    if entry.command:
        command = resolve_reference_sentinel(entry.command, layout)
        refs += [PathReference(p, code_dir=True, program=True, freeze_ancestor=True)
                 for p in _item_paths(command, base_dir, code=infer_suffix)]
    for index, argument in enumerate(entry.args):
        if scaffold_appdata and argument == APPDATA_SENTINEL:
            continue  # 合法 scaffold 的資料參數由 profile 對映，不是程式位置。
        # Scoped package 規格不是相對檔名；其他啟動器的同形字串仍按路徑檢查。
        if Path(entry.command or "").stem.casefold() in _PACKAGE_RUNNERS and re.fullmatch(
            r"@[^/\\\s]+/[^/\\\s]+", argument
        ) and not (base_dir / argument).exists():
            continue
        token = resolve_reference_sentinel(argument, layout)
        if "=" in token:
            prefix, value = token.split("=", 1)
            token = f"{prefix}={resolve_reference_sentinel(value, layout)}"
        option_value = argument.startswith("-") or "=" in argument or (
            index > 0 and entry.args[index - 1].startswith("-") and entry.args[index - 1] != "--"
        )
        # 未知包裝器也可能把任何位置參數當程式；不依啟動器名稱放寬。
        # 只有普通 data 根資料參數不代表載入全部技能；command／env／選項仍從嚴凍結。
        for path in _token_paths(token, base_dir, code=infer_suffix):
            plain_data_root = not option_value and _resolve(layout.data_root) in _candidates(path)
            refs.append(PathReference(
                path, code_dir=True, program=True, freeze_ancestor=not plain_data_root,
            ))
    for name, raw in (entry.env or {}).items():
        if scaffold_appdata and raw == APPDATA_SENTINEL and not _code_env(name):
            continue
        value = resolve_reference_sentinel(raw, layout)
        code = _code_env(name) and not (
            name.casefold() == "mcp_docgen_output_dir" and raw == OUTPUT_DIR_SENTINEL
        )
        refs += [PathReference(p, code_dir=code, freeze_ancestor=code)
                 for p in _token_paths(value, base_dir, code=infer_suffix)]
    return tuple(refs)


def _candidates(path: Path) -> tuple[Path, ...]:
    """字面（去掉 ``..``）與 resolve 後兩個位置都要看：連結的位置與指向都算。"""
    lexical = Path(os.path.normpath(path))
    if PureWindowsPath(lexical).drive[:2] == "\\\\":
        return (lexical,)
    try:
        resolved = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        return (lexical,)
    return (lexical, resolved) if resolved != lexical else (lexical,)


def is_file_reference(path: Path) -> bool:
    """「檔案」：lstat 不是資料夾（連結與 junction 一律算）；不存在時看最後一段有沒有副檔名。"""
    lexical = Path(os.path.normpath(path))
    try:
        info = os.lstat(lexical)
    except (OSError, ValueError):
        return bool(lexical.suffix)
    return not stat.S_ISDIR(info.st_mode) or lexical.is_junction()


def _zone_roots(layout: SandboxLayout) -> ZoneRoots:
    """版面的區根；大小寫比較同 ``_comparison_path``（macOS 不分大小寫）."""
    state = layout.state_root
    return ZoneRoots(
        _comparison_path(_resolve(layout.data_root)),
        None if state is None else _comparison_path(_resolve(state)),
    )


def _in_agent_zone(path: Path, zones: ZoneRoots, *, code_dir: bool) -> bool:
    """在某個區裡，或就是某個錨點（``custom`` 本身除外）；形狀經 ``zone_of``（ADR-0293 R3）.

    ``code_dir`` 另算錨點的上層與 ``custom/<x>``（``contains_zone``）。
    """
    if zone_of(path, zones, anchors=ALL_ANCHORS) is not None:
        return True
    if code_dir:
        return contains_zone(path, zones, anchors=ALL_ANCHORS)
    return anchor_at(path, zones, anchors=ALL_ANCHORS) not in (None, Anchor.CUSTOM_DRIVE)


def _mirror_hit(path: Path, layout: SandboxLayout, *, code_dir: bool) -> bool:
    """鏡像根底下、代理人可能拿到的專案裡（不在 Touka 目錄裡）；查不了的當作寫得到。"""
    for mirror in (_comparison_path(_resolve(m)) for m in layout.mirror_roots):
        if code_dir and _under(mirror, path):
            return True
        if path == mirror or not path.is_relative_to(mirror):
            continue
        folder = path if code_dir and not is_file_reference(path) else path.parent
        if folder == mirror:
            continue  # 鏡像根本身不會授權給代理人，直接放在那裡的檔案代理人寫不到
        if not folder.is_dir():
            return True
        if upward_verdict(folder.resolve(), layout_anchors(layout)) is None:
            return True
    return False


def agent_influenced(path: Path, layout: SandboxLayout, *, code_dir: bool) -> bool:
    """代理人影響得到的位置：個人空間、custom drive、演化的程式、ST、docgen 輸出夾、鏡像根裡的專案.

    ``code_dir``（載入程式碼的位置）另算這些根的上層（namespace package 也能 import 進去）。
    """
    zones = _zone_roots(layout)
    for candidate in _candidates(path):
        candidate = _comparison_path(candidate)
        if _in_agent_zone(candidate, zones, code_dir=code_dir):
            return True
        if _mirror_hit(candidate, layout, code_dir=code_dir):
            return True
    return False


def heuristic_hit(refs: Sequence[PathReference], layout: SandboxLayout) -> bool:
    """所有路徑引用都檢查；參數中的現存資料夾亦可能是程式，不因未知語法放寬。"""
    for ref in refs:
        if PureWindowsPath(ref.path).drive[:2] == "\\\\":
            return True  # 裝置與 UNC 名稱不授予第三方來源，避免根路徑別名。
        if agent_influenced(ref.path, layout, code_dir=ref.code_dir):
            return True
        if ref.program and ref.path.is_dir():
            return True
    return False


def referenced_managed_skills(refs: Sequence[PathReference], layout: SandboxLayout) -> set[str]:
    """引用到的 ``managed/<skill>``（字面或 resolve 後）；看路徑不看條目名稱。"""
    root = managed_root(layout)
    skills: set[str] = set()
    for ref in refs:
        for candidate in _candidates(ref.path):
            if candidate != root and candidate.is_relative_to(root):
                skills.add(candidate.relative_to(root).parts[0])
    return skills


def scaffold_dir_for(
    name: str, entry: McpEntry, layout: SandboxLayout, *, base_dir: Path
) -> Path | None:
    """Scaffold 的形狀：某個參數是直接放在 ``config/mcp_servers/<name>/`` 裡、不是連結的檔案."""
    if not is_safe_segment(name):
        return None
    folder = _resolve(layout.config_dir) / "mcp_servers" / name
    for token in entry.args:
        for path in _token_paths(token, base_dir):
            lexical = Path(os.path.normpath(path))
            if lexical.parent == folder and is_file_reference(lexical) and _candidates(
                lexical
            ) == (lexical,):
                return folder
    return None


@dataclass(frozen=True)
class DeployedSkills:
    """已部署的演化 skill（演化的入口與 profile 拒絕再演化）；``every`` 是全部都算已部署。"""

    skills: frozenset[str]
    every: bool = False

    def covers(self, skill_id: str) -> bool:
        """這個 skill 算不算已部署。"""
        return self.every or skill_id in self.skills


# 設定讀不到、不能解析或結構不對：一律當已部署（fail-closed）
ALL_SKILLS_DEPLOYED = DeployedSkills(frozenset(), every=True)


@dataclass(frozen=True)
class RawMcpEntry:
    """``server_config.json`` 原文的一筆（不經 registry，所以停用的條目也在）。"""

    command: str | None
    args: tuple[str, ...]
    env: Mapping[str, str] | None
    url: str | None


def _str_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def raw_entry(value: object) -> RawMcpEntry | None:
    """原文一筆的四個欄位；型別不對回 None（呼叫端當結構不對）。"""
    if not isinstance(value, dict):
        return None
    command, url, env = value.get("command"), value.get("url"), value.get("env")
    args = value.get("args", [])
    texts_ok = all(v is None or isinstance(v, str) for v in (command, url))
    args_ok = isinstance(args, list) and all(isinstance(a, str) for a in args)
    if not (texts_ok and args_ok and (env is None or _str_map(env))):
        return None
    return RawMcpEntry(command, tuple(args), env, url)


def deployed_skills(
    document: object, layout: SandboxLayout, *, base_dir: Path,
    bases_for: Callable[[str], Sequence[Path]] | None = None,
) -> DeployedSkills:
    """``server_config.json`` 原文裡任何條目（不看名稱、origin、enabled）引用的 ``managed/<skill>``.

    檔案不存在時傳 ``{}``；讀不到、結構不對或程式載入位置涵蓋 managed 根時當全部已部署。
    ``bases_for`` 由啟動 profile 提供相同的 cwd／state home；普通 data 根參數不代表部署。
    """
    servers = document.get("mcpServers", {}) if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return ALL_SKILLS_DEPLOYED
    skills: set[str] = set()
    for name, value in servers.items():
        entry = raw_entry(value)
        if entry is None:
            return ALL_SKILLS_DEPLOYED
        if entry.url:
            continue
        refs = tuple(
            ref for base in (bases_for(name) if bases_for is not None else (base_dir,))
            for ref in referenced_paths(entry, layout, base_dir=base)
        )
        if any(
            ref.freeze_ancestor and _under(managed_root(layout), candidate)
            for ref in refs for candidate in _candidates(ref.path)
        ):
            return ALL_SKILLS_DEPLOYED
        skills |= referenced_managed_skills(refs, layout)
    return DeployedSkills(frozenset(skills))


def _known_origin(value: object) -> McpOrigin | None:
    return cast("McpOrigin", value) if isinstance(value, str) and value in MCP_ORIGINS else None


def classify(
    name: str,
    entry: McpEntry,
    record: OriginRecord,
    layout: SandboxLayout,
    *,
    base_dir: Path,
    infer_suffix: bool = True,
) -> McpClassification:
    """依序：遠端 → 啟發式（不論 origin）→ 紀錄讀不到 → origin → 未標示（scaffold 形狀或第三方）."""
    origin = _known_origin(record.origin) if record.state == "present" else None
    if entry.url:
        return McpClassification("remote", origin, "remote")
    scaffold = scaffold_dir_for(name, entry, layout, base_dir=base_dir)
    refs = referenced_paths(
        entry, layout, base_dir=base_dir, infer_suffix=infer_suffix,
        scaffold_appdata=scaffold is not None and (
            record.state == "absent" or origin == MCP_SCAFFOLD_ORIGIN
        ),
    )
    skills = frozenset(referenced_managed_skills(refs, layout))
    if _expandable_reference(entry):
        return McpClassification("self", origin, "expandable_reference", skills)
    if heuristic_hit(refs, layout):
        return McpClassification("self", origin, "agent_reference", skills)
    if record.state == "unreadable":
        return McpClassification("self", None, "record_unreadable", skills)
    if record.state == "absent":
        if scaffold is not None:
            return McpClassification(
                "scaffold", None, "unlabeled_scaffold_shape", skills, scaffold
            )
        return McpClassification("third_party", None, "unlabeled", skills)
    if origin is None:
        return McpClassification("self", None, "origin_unknown", skills)
    if origin in MCP_SELF_ORIGINS:
        return McpClassification("self", origin, "origin", skills)
    if origin == MCP_SCAFFOLD_ORIGIN:
        if scaffold is None:
            return McpClassification("self", origin, "scaffold_shape_mismatch", skills)
        return McpClassification("scaffold", origin, "origin", skills, scaffold)
    return McpClassification("third_party", origin, "origin", skills)

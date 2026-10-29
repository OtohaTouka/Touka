"""硬地板（ADR-0246 底線）：設定檔關不掉的程式常數判斷，純函式（禁令 18）。

``AuthPoliciesConfig.evaluate`` 最前面呼叫 ``classify_hard_floor``：

- F1 資料、F2 金鑰、F3 權限設定、F4 原始碼、F7 系統破壞：三種模式都只有人能核准
  （``HUMAN_ONLY_CATEGORIES``）；黑名單的 DENY 照舊優先。
- F5 能力擴張與身分、F6 對外發言：Auto 下只有人能核准（``AUTO_HUMAN_ONLY_CATEGORIES``，
  ADR-0246 決定 6）；Manual 照常 CONFIRM，YOLO 放行。

工具（F5、F6）依原生工具名＋動詞判斷，是確定性的。shell 文字只能盡力而為：路徑／檔名
token 加危險動詞（受保護根目錄動態解析）；變數、萬用字元、編碼、``cd`` 後的相對路徑、
先寫腳本再執行都能繞過，真正的地板是隔離執行環境（第 5 段）。
"""

from __future__ import annotations

import functools
import posixpath
import re
from itertools import pairwise, takewhile
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, NamedTuple

from src.config import (
    get_config_dir,
    resolve_env_file_path,
)
from src.services.resource_manager import resource_manager

FloorCategory = Literal["F1", "F2", "F3", "F4", "F5", "F6", "F7"]

HUMAN_ONLY_CATEGORIES: Final[frozenset[str]] = frozenset({"F1", "F2", "F3", "F4", "F7"})
AUTO_HUMAN_ONLY_CATEGORIES: Final[frozenset[str]] = frozenset({"F5", "F6"})
FLOOR_LABELS: Final[dict[str, str]] = {
    "F1": "Touka data",
    "F2": "secrets",
    "F3": "permission settings",
    "F4": "Touka source code",
    "F5": "capabilities or identity",
    "F6": "external messages",
    "F7": "system damage",
}

# --- 工具（F5、F6）：原生工具名＋動詞 ---
_F5_TOOLS: Final = frozenset(
    {
        "request_skill_install", "create_skill", "create_mcp", "scaffold_mcp",
        "request_mcp_creation", "connect_mcp_server", "evolution_implement_and_verify",
        "write_mcp_implementation", "verify_mcp_locally",  # 演化的兩個舊別名（shim）
        "edit_soul", "update_my_soul",
    }
)  # fmt: skip
_F5_TOOL_ACTIONS: Final = frozenset({("agent_control", "switch_brain")})
_F6_TOOLS: Final = frozenset({"send_message", "sns_control"})

# --- shell 文字 ---
_SEGMENT_RE: Final = re.compile(r"[;&|\n\r`(){}]|\$\(")
_STAGE_RE: Final = re.compile(r"\||`|\$\(")  # 一句裡另一個指令的開頭（管線、子指令）
_OCTAL_RE: Final = re.compile(r"\\([0-7]{3})")  # mountinfo 的 \040 這類跳脫
_REMOVE_CALL_RE: Final = re.compile(  # 直譯器裡的刪除、搬移呼叫
    r"\b(?:shutil\.)?rmtree\s*\(|\bos\.(?:remove|unlink|rmdir|removedirs|rename|replace)\s*\("
    r"|\bshutil\.move\s*\(|\bfs\.(?:rm|rmdir|unlink)\s*\(|\b(?:rm|rmdir|unlink)sync\s*\("
    r"|\bremove_tree\b|\brimraf\b",
    re.I,
)
_SEGMENT_OR_QUOTE_RE: Final = re.compile(r"[;&|\n\r`(){}\"']|\$\(")
_STATEMENT_RE: Final = re.compile(r"\|\||&&|[;&\n\r]")  # 切完剩下的 | 是管線
_PATH_SPLIT_RE: Final = re.compile(r"[\s\"'=,;&|<>(){}`]+")
_REDIRECT_RE: Final = re.compile(r">(?!&)")
_DRIVE_RE: Final = re.compile(r"[a-z]:")
_DEVICE_PREFIX_RE: Final = re.compile(r"^//[?.]/")  # \\?\、\\.\
_NUMERIC_RE: Final = re.compile(r"\+?\d+(?:\.\d+)?[smhd]?|0x[0-9a-f]+|\d+(?:[-,]\d+)+", re.I)
_ENV_FILE_RE: Final = re.compile(r"\.env(?:\.(?!(?:example|sample|template)$)[\w.-]+)?")
_POLICY_FILE_RE: Final = re.compile(
    r"(?:auth_policies|identity_map|autonomy|dispatch_hooks|server_config|group_whitelist"
    r"|permissions|hooks)\.json(?:\.example)?|auth_policies|identity_map"
)
_RECURSIVE_FLAG_RE: Final = re.compile(r"-[a-z]*r[a-z]*|--recursive|/s")
_UNIX_RECURSE_RE: Final = re.compile(r"-[A-Za-z]*[rRa][A-Za-z]*|--recursive|--archive")
_TAR_CREATE_RE: Final = re.compile(r"-?[A-Za-z]*c[A-Za-z]*|--create")
_CMD_FLAG_RE: Final = re.compile(r"-.*|/[a-z?]")  # 旗標（含 cmd 的 /s、/q），不是刪除目標

_PS_WORDS: Final = frozenset({"powershell", "pwsh"})
# 前綴指令 → 需要值的選項（其他選項當作不帶值；PowerShell 的選項一律可能帶值）
_PREFIX_VALUE_OPTIONS: Final = {
    name: frozenset(options.split())
    for name, options in {
        "sudo": "-u -g -h -p -C -D -r -t -U -T --user --group --host --prompt --chdir",
        "doas": "-u -C",
        "env": "-u -C -S --unset --chdir --split-string",
        "nice": "-n --adjustment",
        "ionice": "-c -n -p -P -u --class --classdata",
        "stdbuf": "-i -o -e --input --output --error",
        "timeout": "-s -k --signal --kill-after",
        "time": "-f -o --format --output",
        "xargs": "-a -d -E -I -L -n -P -s --arg-file --delimiter --max-args --max-procs",
    }.items()
}
_PREFIX_WORDS: Final = frozenset(
    {*_PREFIX_VALUE_OPTIONS, *_PS_WORDS, "nohup", "exec", "command", "busybox", "setsid"}
    | {"chrt", "taskset"}
)
_SHELL_WORDS: Final = frozenset({"bash", "sh", "zsh", "cmd", "powershell", "pwsh"})
_INLINE_FLAGS: Final = frozenset({"-c", "/c", "/k", "-command"})  # 後面整段是另一條指令
_POWER_WORDS: Final = frozenset(
    {
        "shutdown", "reboot", "halt", "poweroff", "dd", "diskpart", "stop-computer",
        "restart-computer", "format-volume", "clear-disk", "initialize-disk",
        "pkill", "killall", "taskkill", "stop-process",
    }
)  # fmt: skip
_POWER_ARGS: Final = frozenset({"poweroff", "reboot", "halt", "--shutdown", "kexec"})
_CONTAINER_WORDS: Final = frozenset({"docker", "docker-compose", "podman"})
_CONTAINER_STOP: Final = frozenset({"up", "down", "stop", "restart", "kill", "rm", "prune"})
_DELETE_WORDS: Final = frozenset({"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri"})
_MOVE_WORDS: Final = frozenset({"mv", "move", "move-item", "mi", "ren", "rename", "rename-item"})
_HOME_WORDS: Final = frozenset({"~", "$home", "${home}", "%userprofile%", "$env:userprofile"})
_FIND_NAME_FILTERS: Final = frozenset({"-name", "-iname", "-path", "-ipath", "-regex", "-iregex"})
_GIT_WRITE: Final = frozenset(
    {"checkout", "restore", "apply", "clean", "reset", "rm", "mv", "am", "stash", "switch"}
)
_WRITE_WORDS: Final = _DELETE_WORDS | frozenset(
    {
        "mv", "move", "move-item", "mi", "ren", "rename", "rename-item",
        "cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "rsync", "scp",
        "tee", "truncate", "shred", "ln", "mklink", "chmod", "chown", "icacls", "attrib",
        "set-content", "sc", "add-content", "ac", "out-file", "clear-content", "new-item",
        "sed", "perl", "python", "python3", "py", "node", "ruby", "php", "deno", "bun",
        "bash", "sh", "zsh", "powershell", "pwsh", "cmd", "sqlite3", "patch", "invoke-webrequest",
        "tar", "unzip", "7z", "zip", "expand-archive", "compress-archive", "curl", "wget", "iwr",
    }
)  # fmt: skip
# 遞迴讀取：一定遞迴的；要帶遞迴旗標的（grep -r、cp -r、gci -Recurse、xcopy /s…）
_ALWAYS_RECURSIVE: Final = frozenset({"find", "rg", "ag", "ack", "compress-archive"})
_UNIX_RECURSIVE: Final = frozenset({"grep", "egrep", "zgrep", "cp", "scp", "rsync", "zip"})
_PS_RECURSIVE: Final = frozenset({"get-childitem", "gci", "copy-item", "cpi"})
_CMD_RECURSIVE: Final = frozenset({"findstr", "xcopy", "robocopy"})
_COPY_WORDS: Final = frozenset({"cp", "scp", "rsync", "copy-item", "cpi", "xcopy", "robocopy"})
# 路徑元件（小寫）：資料、金鑰、政策目錄、原始碼
_DATA_PARTS: Final = frozenset({"data", "soul", "legacy"})
# hooks.json：http hook 的標頭照字面存，可能有權杖（ADR-0292），讀和寫都算金鑰
_SECRET_FILES: Final = frozenset({"users.json", "connections.json", "hooks.json"})
# 心跳存 heartbeat_token 的 config.json：awaken._resolve_config_file 的預設位置
# ToukaHeartbeat/config.json，與 compose 掛進心跳容器的 ToukaHeartbeat/config/
_HB: Final = "toukaheartbeat"
_SECRET_TAILS: Final = ((_HB, "config.json"), (_HB, "config", "config.json"))
_SECRET_DIR_TAILS: Final = ((_HB,), (_HB, "config"))
_CONFIG_DIRS: Final = (("app", "config"), ("toukaproxy", "config"))
_SOURCE_DIRS: Final = (("toukaproxy", "src"),)
_SOURCE_PARTS: Final = frozenset(
    {"toukamemory", "toukadispatch", "toukaheartbeat", "toukachat", "toukaide", "host_root"}
)
_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_MOUNTINFO: Final = Path("/proc") / "self" / "mountinfo"  # Linux 核心介面，不是部署路徑
# Docker named volume 在宿主上的資料佈局慣例 …/volumes/<名稱>/_data（只比 mountinfo root 欄位
# 的片段；不是部署路徑，也不寫死 Docker 的資料根目錄）
_DOCKER_VOLUME_LAYOUT: Final = ("volumes", "_data")


class _AbsPath(NamedTuple):
    drive: str  # 空字串＝沒寫磁碟代號
    path: PurePosixPath


def classify_hard_floor(
    action: str,
    resource: str,
    *,
    tool_action: str | None = None,
    is_mcp: bool = False,
) -> FloorCategory | None:
    """回傳命中的地板類別；``terminal_execute`` 看指令文字，其他看原生工具名＋動詞。"""
    if action == "terminal_execute":
        return _classify_shell(resource)
    if is_mcp:
        return None
    if resource in _F5_TOOLS or (resource, tool_action) in _F5_TOOL_ACTIONS:
        return "F5"
    if resource in _F6_TOOLS:
        return "F6"
    return None


def floor_tool_action(tool: str, args: dict[str, Any]) -> str | None:
    """F5 工具的動詞：executor 直接從 ``args["action"]`` 取，不依賴設定檔的 action_param。"""
    verb = args.get("action") if any(tool == n for n, _v in _F5_TOOL_ACTIONS) else None
    return verb if isinstance(verb, str) else None


def _classify_shell(command: str) -> FloorCategory | None:
    segments = [*_SEGMENT_RE.split(command), *_SEGMENT_OR_QUOTE_RE.split(command)]
    words = [w for segment in segments for w in _command_candidates(segment)]
    if any(_is_system_damage(w[0], w[1:]) for w in words) or _piped_delete_wipes(command):
        return "F7"
    tokens = [t for t in _PATH_SPLIT_RE.split(command) if t]
    paths = [p for p in map(_path_parts, tokens) if p]
    data, source, plain, secret, aliases = _protected_roots()
    reads = any(_reads_secret_dir(w, secret, aliases) for w in words)
    if any(_is_secret(p) for p in paths) or reads:
        return "F2"
    writes = bool(_REDIRECT_RE.search(command) or _REMOVE_CALL_RE.search(command)) or any(
        _is_write(w[0], w[1:]) for w in words
    )
    if not writes:
        return None
    if any(_is_policy(p) for p in paths):
        return "F3"
    if any(_is_source(p) for p in paths):
        return "F4"
    if any(_is_data(p) for p in paths):
        return "F1"
    targets = [(a, r) for t, r in _root_targets(command) for a in _alias_paths(t, aliases)]
    return _root_category(targets, data, source, plain)


def _command_candidates(segment: str) -> list[list[str]]:
    tokens = [t.strip("\"'") for t in segment.split()]
    return _candidates([t for t in tokens if t])


def _candidates(tokens: list[str]) -> list[list[str]]:
    """去掉 VAR=x 與 sudo／timeout 等前綴（連同參數）；帶值選項後面那個字是值或指令都試。"""
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return []
    name, rest = _command_name(tokens[0]), tokens[1:]
    inline = [i for i, t in enumerate(rest) if _is_inline_flag(name, t)]
    if name in _SHELL_WORDS and inline:
        return _candidates(rest[inline[0] + 1 :])
    script = name in _PS_WORDS and any(_ps_abbrev(t, "-file") for t in rest)
    if name not in _PREFIX_WORDS or script:
        return [[name, *rest]]
    found: list[list[str]] = []
    maybe_value = False
    for i, token in enumerate(rest):
        if token.startswith("-") or "=" in token or _NUMERIC_RE.fullmatch(token):
            maybe_value = name in _PS_WORDS or token in _PREFIX_VALUE_OPTIONS.get(name, ())
            continue
        found.extend(_candidates(rest[i:]))
        if not maybe_value:
            break
        maybe_value = False
    return found


def _command_name(token: str) -> str:
    return posixpath.basename(token.replace("\\", "/")).lower().removesuffix(".exe")


def _ps_abbrev(token: str, option: str) -> bool:
    return len(token) > 1 and option.startswith(token.lower())


def _is_inline_flag(name: str, token: str) -> bool:
    return token.lower() in _INLINE_FLAGS or (name in _PS_WORDS and _ps_abbrev(token, "-command"))


def _is_system_damage(word: str, args: list[str]) -> bool:
    lowered = [a.lower() for a in args]
    if word in _POWER_WORDS or word.startswith("mkfs"):
        return True
    if word == "format":
        return bool(lowered) and _DRIVE_RE.fullmatch(lowered[0].rstrip("\\/")) is not None
    if word in {"systemctl", "wsl"}:
        return any(a in _POWER_ARGS for a in lowered)
    if word in _CONTAINER_WORDS:
        return any(a in _CONTAINER_STOP for a in lowered)
    if word == "kill":
        return any(a in {"1", "-1"} for a in lowered)
    if word == "find":
        deletes = _find_runs(lowered, _DELETE_WORDS)
        return deletes and any(_is_wipe_target(t) for t in _find_starts(lowered))
    if word in _DELETE_WORDS:
        recursive = any(_RECURSIVE_FLAG_RE.fullmatch(a) for a in lowered)
        targets = [a for a in lowered if not _CMD_FLAG_RE.fullmatch(a)]
        return recursive and any(_is_wipe_target(a) for a in targets)
    return False


def _find_runs(lowered: list[str], words: frozenset[str]) -> bool:
    """``find`` 帶 ``-delete``，或 ``-exec``／``-ok`` 接 ``words`` 裡的指令。"""
    return "-delete" in lowered or any(
        flag in {"-exec", "-execdir", "-ok", "-okdir"} and _command_name(command) in words
        for flag, command in pairwise(lowered)
    )


def _find_starts(lowered: list[str]) -> list[str]:
    """``find`` 的起點（預設 ``.``）；有檔名篩選時 ``.``／``*`` 不算整片刪除。"""
    starts = list(takewhile(lambda a: not a.startswith(("-", "(", "!")), lowered)) or ["."]
    if any(a in _FIND_NAME_FILTERS for a in lowered):
        return [s for s in starts if any(p != "*" for p in _path_parts(s))]
    return starts


def _piped_delete_wipes(command: str) -> bool:
    """``A | xargs rm -rf``、``A | Remove-Item``：刪除指令沿用左邊的目標。"""
    pairs = (
        (source, words)
        for statement in _STATEMENT_RE.split(command)
        for left, right in pairwise(statement.split("|"))
        for words in _command_candidates(right)
        if words[0] in _DELETE_WORDS
        for source in _command_candidates(left)
    )
    for source, words in pairs:
        flags = [a.lower() for a in [*source[1:], *words[1:]]]
        targets = [a for a in flags[: len(source) - 1] if not _CMD_FLAG_RE.fullmatch(a)]
        recursive = source[0] == "find" or any(_RECURSIVE_FLAG_RE.fullmatch(a) for a in flags)
        if recursive and any(_is_wipe_target(t) for t in targets):
            return True
    return False


def _is_wipe_target(arg: str) -> bool:
    """根目錄、第一層系統目錄、家目錄、``.``／``*``，或往上跳出工作目錄的路徑。"""
    parts = [p for p in _path_parts(arg) if p != "*"]
    if parts[:1] == ["/"]:
        return len(parts) <= 2
    return not parts or parts[0] == ".." or (len(parts) == 1 and parts[0] in _HOME_WORDS)


def _is_write(word: str, args: list[str]) -> bool:
    if word == "git":
        return any(a.lower() in _GIT_WRITE for a in args)
    if word == "find":
        return _find_runs([a.lower() for a in args], _WRITE_WORDS)
    return word in _WRITE_WORDS


def _is_recursive_read(word: str, args: list[str]) -> bool:
    if word == "tar":  # 建立封存：模式寫在第一個參數或 - 開頭的旗標
        return any(
            _TAR_CREATE_RE.fullmatch(a) for i, a in enumerate(args) if i == 0 or a[:1] == "-"
        )
    if word in _PS_RECURSIVE:
        return any(_ps_abbrev(a, "-recurse") for a in args)
    if word in _CMD_RECURSIVE:
        return any(a.lower() in {"/s", "/e", "/mir"} for a in args)
    flagged = word in _UNIX_RECURSIVE and any(_UNIX_RECURSE_RE.fullmatch(a) for a in args)
    return flagged or word in _ALWAYS_RECURSIVE


def _reads_secret_dir(words: list[str], secret: tuple, aliases: tuple) -> bool:
    """遞迴讀取放金鑰的目錄或它的上層，或往上跳出工作目錄（終端在 Touka 資料夾底下）。"""
    if not _is_recursive_read(words[0], words[1:]):
        return False
    targets = [a for a in words[1:] if not _CMD_FLAG_RE.fullmatch(a.lower())]
    for arg in targets[:-1] if words[0] in _COPY_WORDS else targets:  # 複製的目的地不算讀
        parts, target = _path_parts(arg), _abs_path(arg)
        if parts[:1] == ("..",) or _ends_with(parts, _SECRET_DIR_TAILS):
            return True
        if target and any(
            _related(a, root, above=True) for a in _alias_paths(target, aliases) for root in secret
        ):
            return True
    return False


def _normalize(token: str) -> str:
    """小寫、斜線統一、去裝置前綴；每段去 NTFS 資料串流與結尾的點和空白（Win32 會忽略）。"""
    text = _DEVICE_PREFIX_RE.sub("", token.replace("\\", "/").lower())
    drive = text[:2] if _DRIVE_RE.match(text) else ""
    cleaned = [
        p if p in {".", ".."} or p.startswith("$") else p.split(":")[0].rstrip(". ")
        for p in text[len(drive) :].split("/")
    ]
    return drive + "/".join(cleaned)


def _abs_path(token: str) -> _AbsPath | None:
    text = _normalize(token)
    drive = text[:2] if _DRIVE_RE.match(text) else ""
    rest = text[len(drive) :]
    if not (drive or rest.startswith("/")):
        return None
    path = posixpath.normpath("/" + rest.lstrip("/"))
    while path != "/" and posixpath.basename(path) in {"*", "**", "*.*"}:  # 裡面全部＝清空它本身
        path = posixpath.dirname(path)
    return _AbsPath(drive, PurePosixPath(path))


def _path_parts(token: str) -> tuple[str, ...]:
    absolute = _abs_path(token)
    if absolute is not None:
        return ("/", *absolute.path.parts[1:])
    return PurePosixPath(posixpath.normpath(_normalize(token))).parts


def _related(token: _AbsPath, root: _AbsPath, *, above: bool = False) -> bool:
    """路徑在根目錄底下（``above``：是根目錄本身或上層）；路徑寫了磁碟代號就要相同。"""
    if token.drive and token.drive != root.drive:
        return False
    inner, outer = (root.path, token.path) if above else (token.path, root.path)
    return inner.is_relative_to(outer)


def _as_roots(paths: list[Any]) -> tuple[_AbsPath, ...]:
    found = (_abs_path(str(p)) or _abs_path(str(Path(p).absolute())) for p in paths if str(p))
    return tuple(root for root in found if root is not None)


@functools.cache
def _read_mountinfo() -> str:
    """Linux 掛載表（經 ResourceManager 讀一次並快取）；讀不到回空字串＝證明不了。"""
    try:
        return resource_manager.read_file_text(_MOUNTINFO)
    except (OSError, UnicodeDecodeError):
        return ""


def mounts_separate(mountinfo: str, workspace: str, anchors: tuple[str, ...]) -> bool:
    """純函式：mountinfo 能不能證明工作區的宿主來源和每個 anchor 都不重疊。

    工作區掛載項和它底下的子掛載都要和每個 anchor 證明分開（見 ``_apart``）。解析失敗、
    工作區不是一個獨立掛載項、anchor 找不到掛載項，都算證明不了（False）。
    """
    mounts: list[tuple[str, PurePosixPath, _AbsPath | None]] = []
    for line in mountinfo.splitlines():
        fields = [_OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), f) for f in line.split()]
        if len(fields) < 7 or "-" not in fields[6:]:
            return False
        mounts.append((fields[2], PurePosixPath(fields[3].casefold()), _abs_path(fields[4])))
    ws, point = _mount_source(mounts, workspace, exact=True), _abs_path(workspace)
    if ws is None or point is None:
        return False
    parts = [ws, *((d, r) for d, r, p in mounts if p and p != point and _related(p, point))]
    found = [_mount_source(mounts, a) for a in anchors]
    return all(a is not None and all(_apart(w, a) for w in parts) for a in found)


def _apart(one: tuple[str, PurePosixPath], other: tuple[str, PurePosixPath]) -> bool:
    """兩個掛載來源能不能證明分開。

    同裝置：互不為本身或上下層（root 已 casefold）。不同裝置：兩邊都要是實體區塊裝置
    （major≠0）才算分開，9p／virtiofs／FUSE／overlay（major 0）證明不了；唯一例外是錨點
    （``other``）為實體區塊裝置上的 Docker named volume——主機共享不可能包含 VM 磁碟裡的
    volume（ghcr compose 跑在 Docker Desktop 時的設定目錄）。
    """
    if one[0] != other[0]:
        return "0" not in {one[0].split(":")[0], other[0].split(":")[0]} or _docker_volume(other)
    return not (one[1].is_relative_to(other[1]) or other[1].is_relative_to(one[1]))


def _docker_volume(source: tuple[str, PurePosixPath]) -> bool:
    """實體區塊裝置（major≠0）上、宿主來源符合 Docker volume 佈局 …/volumes/<名稱>/_data。"""
    parts, (volumes, data) = source[1].parts, _DOCKER_VOLUME_LAYOUT
    return source[0].split(":")[0] != "0" and any(
        parts[i] == volumes and parts[i + 2] == data for i in range(len(parts) - 2)
    )


def _mount_source(
    mounts: list, path: str, *, exact: bool = False
) -> tuple[str, PurePosixPath] | None:
    """路徑所在的掛載項（最深的掛載點，同深度取後掛的）換算成（裝置, 宿主來源路徑）。"""
    target, best = _abs_path(path), None
    for device, root, point in mounts:
        deeper = best is None or (point and len(point.path.parts) >= len(best[2].path.parts))
        if target and point and _related(target, point) and deeper:
            best = (device, root, point)
    if target is None or best is None or (exact and best[2] != target):
        return None
    return best[0], best[1] / target.path.relative_to(best[2].path)


def _protected_roots() -> tuple[tuple, ...]:
    """甲的受保護根目錄與乙的使用者工作區（SSOT 動態解析，禁令 41）。

    依序：資料（F1）、其他（F4）、乙的工作區、放金鑰的目錄（F2：遞迴讀取它或它的上層）、
    別名（不在 Docker 時工作區的 symlink／junction → 真實位置）。工作區和受保護根目錄重疊
    時交給甲；Docker 掛載點要 mountinfo 證明不重疊才算乙，證明不了就當作是受保護根目錄的
    上層（不豁免、遞迴讀取算 F2）。不在 Docker 時兩邊都 resolve 再比，解析失敗就整片保護。
    """
    data, env_dir, config_dir = (
        resource_manager.get_base_path(),
        resolve_env_file_path().parent,
        get_config_dir(),
    )
    groups = ([data], [_REPO_ROOT, env_dir, config_dir], [env_dir, config_dir])
    roots = [_as_roots(group) for group in groups]  # 資料、其他、金鑰目錄
    mount, container = (
        resource_manager.get_workspace_mount(),
        resource_manager.get_container_mount(),
    )
    space, aliases = _as_roots([mount] if mount is not None else []), ()
    if container is not None:
        anchors = tuple(map(str, (data, env_dir, config_dir)))
        if not mounts_separate(_read_mountinfo(), str(container), anchors):
            space, roots[2] = (), (*roots[2], *_as_roots([container]))
    elif mount is not None:
        try:
            roots = [(*r, *_resolved(g)) for r, g in zip(roots, groups, strict=True)]
            aliases = tuple((w, real) for w in space for real in _resolved([mount]) if real != w)
        except OSError:  # 解析不了：當作重疊，整片保護
            roots[1], space = (*roots[1], *space), ()
        reals = (*space, *(real for _w, real in aliases))
        if any(
            _related(w, r) or _related(w, r, above=True) for w in reals for r in roots[0] + roots[1]
        ):
            space = ()  # 和受保護根目錄重疊（本身、底下或上層）：交給甲
    return roots[0], roots[1], space, roots[2], aliases


def _resolved(paths: list[Any]) -> tuple[_AbsPath, ...]:
    """跟著 symlink／junction 解析成真實位置（只讀 metadata，不開檔）。"""
    return _as_roots([Path(path).resolve() for path in paths])


def _alias_paths(target: _AbsPath, aliases: tuple) -> list[_AbsPath]:
    """原路徑，加上經過別名工作區換成真實位置的路徑。"""
    return [target] + [
        _AbsPath(real.drive, real.path / target.path.relative_to(alias.path))
        for alias, real in aliases
        if _related(target, alias)
    ]


def _root_targets(command: str) -> list[tuple[_AbsPath, bool]]:
    """寫入檢查用的絕對路徑＋所在這一句（含整條管線）會不會刪除、搬走或清空。

    每個指令開頭要執行的程式（如 .venv 裡的 python）不是寫入目標。
    """
    targets: list[tuple[_AbsPath, bool]] = []
    for statement in _STATEMENT_RE.split(command):
        removes = bool(_REMOVE_CALL_RE.search(statement)) or any(
            _removes(w) for part in _SEGMENT_RE.split(statement) for w in _command_candidates(part)
        )
        for stage in _STAGE_RE.split(statement):
            tokens = [t for t in _PATH_SPLIT_RE.split(stage) if t]
            if tokens and stage.split()[0].strip("\"'") == tokens[0]:
                tokens = tokens[1:]
            targets += [(a, removes) for a in map(_abs_path, tokens) if a]
    return targets


def _removes(words: list[str]) -> bool:
    """刪除、搬走或清空：rm、mv 類、find -delete、rsync --delete、robocopy /MIR、git clean。"""
    name, args = words[0], [a.lower() for a in words[1:]]
    return (
        name in _DELETE_WORDS | _MOVE_WORDS
        or (name == "find" and _find_runs(args, _DELETE_WORDS))
        or (name == "rsync" and any(a.startswith("--delete") for a in args))
        or (name == "robocopy" and any(a in {"/mir", "/purge"} for a in args))
        or (name == "git" and "clean" in args)
    )


def _root_category(
    targets: list[tuple[_AbsPath, bool]], data: tuple, source: tuple, plain: tuple
) -> FloorCategory | None:
    """甲：碰到受保護根目錄本身、底下或上層（資料 F1、其他 F4）。

    乙的工作區（plain）在 repo 根底下時不因此算 F4；它本身與上層只擋刪除、搬走或清空。
    單獨的 ``/`` 不算上層（分不出除號；刪根目錄由 F7 擋）。
    """
    paths = [t for t, _removes in targets]
    above = [(t, removes) for t, removes in targets if t.path.parts[1:]]
    if any(_related(t, root) for t in paths for root in data):
        return "F1"
    if (
        any(_related(t, r) and not _in_plain(t, r, plain) for t in paths for r in source)
        or any(_related(t, r, above=True) for t, _x in above for r in source)
        or any(removes and _related(t, w, above=True) for t, removes in above for w in plain)
    ):
        return "F4"
    return "F1" if any(_related(t, r, above=True) for t, _x in above for r in data) else None


def _in_plain(token: _AbsPath, root: _AbsPath, plain: tuple) -> bool:
    """路徑落在 root 底下的乙工作區裡（例如 Docker 在 repo 根底下掛的使用者專案）。"""
    return any(_related(token, w) and _related(w, root) for w in plain)


def _ends_with(parts: tuple[str, ...], tails: tuple[tuple[str, ...], ...]) -> bool:
    return any(parts[-len(tail) :] == tail for tail in tails)


def _has_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    size = len(sequence)
    return any(parts[i : i + size] == sequence for i in range(len(parts) - size + 1))


def _is_secret(parts: tuple[str, ...]) -> bool:
    name = parts[-1]
    return (
        _ENV_FILE_RE.fullmatch(name) is not None
        or name in _SECRET_FILES
        or ("proc" in parts and name == "environ")
        or _ends_with(parts, _SECRET_TAILS)
    )


def _is_policy(parts: tuple[str, ...]) -> bool:
    return _POLICY_FILE_RE.fullmatch(parts[-1]) is not None or any(
        _has_sequence(parts, seq) for seq in _CONFIG_DIRS
    )


def _is_source(parts: tuple[str, ...]) -> bool:
    return any(p in _SOURCE_PARTS for p in parts) or any(
        _has_sequence(parts, seq) for seq in _SOURCE_DIRS
    )


def _is_data(parts: tuple[str, ...]) -> bool:
    """絕對路徑碰到 data／soul／legacy；相對路徑碰到 soul，或往上跳出工作目錄。

    終端的工作目錄是靈魂自己的硬碟（位在 Touka 資料夾底下），所以往上跳就可能碰到資料。
    """
    if parts[0] == "/":
        return any(p in _DATA_PARTS for p in parts)
    return parts[0] == ".." or "soul" in parts

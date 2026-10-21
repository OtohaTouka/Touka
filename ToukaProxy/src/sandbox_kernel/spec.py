"""啟動器 spec 的版本、欄位與驗證（SSOT；只用標準函式庫）.

proxy 端組 spec，啟動器（``launcher.py``）從繼承來的 pipe fd 讀入後用 ``parse_spec`` 驗證。
看不懂的版本、多出來的欄位、型別不對一律拒絕（fail-closed）。

spec 第 1 版的欄位（JSON 物件）：

- ``version``（必要）：整數，等於 ``SPEC_VERSION``。
- ``profile``：標籤，``[a-z0-9_]{1,64}``，只用在診斷。
- ``rules``：路徑規則清單（最多 ``MAX_RULES`` 條），每條是
  ``{"path", "access", "optional", "skip_symlink"}``：

  - ``path``：絕對路徑，不能是 ``/``，不能有空段、``.``、``..`` 或 NUL；
  - ``access``：``ro``／``rox``／``rwx``／``dev_rw``；
  - ``optional``：目標不存在、上層不是一般資料夾、目標是符號連結，或型別／連結數不能授權時
    略過（不授權），不讓整條指令失敗；
  - ``skip_symlink``：只在目標是符號連結時略過。
- ``block_ports``：要封鎖連線的 TCP 埠（1～65535、不重複、最多 ``MAX_BLOCK_PORTS`` 個）。
  沒有共用網路規則集時，啟動器自己建（ABI≥4）。
- ``rlimits``：``cpu``／``fsize``／``as`` 的上限（非負整數，soft＝hard）。
- ``pin``：預設 ``True``（規則路徑的 O_PATH fd 帶過 exec；限制見 ``launcher.py``）。
- ``parent_pid``：spawn 端的 pid（非負整數，0＝不檢查）；設好 PDEATHSIG 後父行程不是它就不執行。
- ``net_strict``：預設 ``True``；有 TCP 規則時啟動器每次都讀 Fast Open／MPTCP 設定，不安全就以
  ``net_*`` 拒絕執行。只有人核准、features 不含 tcp 的降級才送 ``False``（依 runner 的 features）。
- ``exec_env``（C2-3）：條目自己的環境變數（物件，名稱→字串），啟動器套完限制後 ``execvpe`` 時
  才合併進交給目標的環境，啟動器自己的環境不變。名稱 ``EXEC_ENV_NAME_PATTERN``、值沒有 NUL、
  最多 ``MAX_EXEC_ENV_ITEMS`` 個、合計 ``MAX_EXEC_ENV_BYTES``；profile 管的名稱
  （``exec_env_reserved``）拒收；直譯器控制與動態連結 loader 的變數
  （``exec_env_stripped``，禁令 19）移除、目標拿不到。spec 版本不變：不認得這個鍵的舊啟動器
  以 ``spec_invalid`` 拒絕。

單檔規則只授權 ``PUBLIC_CONFIG_NAMES`` 這九個檔名（唯讀、一般檔、非硬連結），由啟動器依
實際的檔案型別判定，見 ``launcher.rule_rights``。

proxy 端也從這裡取 spec 上限、公開設定檔名、``kernel_digest`` 與自測輸出解析
（``parse_selftest_output``），不另存副本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import PurePath

SPEC_VERSION = 1
MAX_SPEC_BYTES = 1024 * 1024
MAX_RULES = 512
MAX_PATH_BYTES = 4096
MAX_BLOCK_PORTS = 1024  # 含從設定推導的上游埠；超過就拒絕，不截斷
MAX_PORT = 65535
MAX_RLIMIT = (1 << 63) - 1

AccessName = Literal["ro", "rox", "rwx", "dev_rw"]
RlimitName = Literal["cpu", "fsize", "as"]
ACCESS_NAMES = frozenset(get_args(AccessName))
RLIMIT_NAMES = frozenset(get_args(RlimitName))
# 專案根與 cwd 上層裡可給唯讀的公開設定檔；`.env*` 永遠不在內
PUBLIC_CONFIG_NAMES = frozenset(
    {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "ruff.toml", ".ruff.toml"}
    | {".python-version", "uv.lock", "package.json"}
)
# Touka 內部服務與 Docker 遠端控制的固定埠（proxy 另加設定改過的埠與推導出的上游埠）
DEFAULT_BLOCK_PORTS = frozenset({8000, 8082, 8083, 3001, 2375, 2376})

# 執行器 START 的 env 允許清單（只收精確名稱；proxy 端先過濾、伺服器再驗）。帶網址或憑證的 pip、
# 代理伺服器與自訂 CA 的變數刻意不收（網址裡可能有帳號密碼）；TOUKA_EXEC_ID 由伺服器自己給
EXECUTOR_ENV_ALLOWED = frozenset(
    {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "TZ", "TERM", "NO_COLOR"}
    | {"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC"}
    | {"LC_TIME", "LC_MONETARY"}
    | {"XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"}
    | {"GIT_CEILING_DIRECTORIES", "PYTEST_ADDOPTS"}
    | {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONIOENCODING"}
    | {"PIP_NO_INPUT", "PIP_DISABLE_PIP_VERSION_CHECK", "PIP_NO_CACHE_DIR", "PIP_PROGRESS_BAR"}
    | {"UV_PYTHON_DOWNLOADS"}  # 執行器裡的 uvx 用系統 Python，不自己下載
)
# GIT_CONFIG_* 只收 profile 自己設的這一組（繼承來的一律拿掉；其他值或其他編號拒絕 START）
EXECUTOR_GIT_CONFIG = (
    ("GIT_CONFIG_COUNT", "1"),
    ("GIT_CONFIG_KEY_0", "safe.directory"),
    ("GIT_CONFIG_VALUE_0", "*"),
)
MAX_EXECUTOR_ENV_BYTES = 64 * 1024
_GIT_CONFIG_PREFIX = "GIT_CONFIG_"

# spec 的 ``exec_env``（條目自己的環境變數，只給目標程式）
EXEC_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_EXEC_ENV_ITEMS = 256
MAX_EXEC_ENV_BYTES = 64 * 1024
# profile 拿掉的快取變數（指到 proxy 的快取時，伺服器會去寫 Landlock 沒給的地方）；proxy 的
# ``profiles`` 從這裡匯入，和下面的保留名稱只有這一份
SANDBOX_ENV_UNSET = frozenset(
    {"HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "TORCH_HOME", "PIP_CACHE_DIR"}
    | {"UV_CACHE_DIR", "UV_TOOL_DIR", "UV_PYTHON_INSTALL_DIR"}
    | {"NPM_CONFIG_CACHE", "NPM_CONFIG_PREFIX", "npm_config_cache"}
)
# profile 管的名稱（``profiles.sandbox_env`` 設的、``SANDBOX_ENV_UNSET``、執行器給的 exec id）：
# 條目不能蓋，否則伺服器會去寫 Landlock 沒給的地方。不分大小寫比對（npm 讀 ``npm_config_*``
# 不分大小寫）；和 ``sandbox_env`` 的對照由測試釘住
EXEC_ENV_RESERVED = frozenset(
    {"HOME", "TMPDIR", "TMP", "TEMP", "GIT_CEILING_DIRECTORIES", "PYTEST_ADDOPTS", "TOUKA_EXEC_ID"}
    | {name.upper() for name in SANDBOX_ENV_UNSET}
)
EXEC_ENV_RESERVED_PREFIXES = ("XDG_", _GIT_CONFIG_PREFIX)
# 禁令 19：和 ``EvolutionPathGuard.strip_sensitive_env`` 同一套直譯器控制與動態連結 loader 的黑名單
# （子字串、不分大小寫）加顯式移除清單；``exec_env`` 裡命中的一律移除。密鑰名稱那一組不套用：條目的
# env 本來就要把伺服器自己的 token 交給它（ADR-0254）。和 evolution_security 的對照由測試釘住
EXEC_ENV_STRIP_PATTERNS = ("PYTHON", "LD_", "DYLD_", "NODE_", "GEM_", "JAVA_", "PERL")
EXEC_ENV_STRIP_NAMES = frozenset(
    {"PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "LD_PRELOAD", "LD_LIBRARY_PATH"}
    | {"DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"}
)

# Touka 目錄的判定（名稱一律 ``fold_name`` 後比對）；proxy 的 ``utils/touka_dirs`` 與執行器伺服器
# 共用這一份。資料根形狀：任一個就算；legacy 用到才建、名稱也太常見，不列；ui_sessions.db 是啟動時
# 一定會建的
TOUKA_MARKERS = frozenset(
    {"toukaproxy", "toukamemory", "toukaheartbeat", "toukadispatch", "heartbeat-config"}
    | {"docker-compose.ghcr.yml"}
)
DATA_ROOT_NAMES = frozenset({"soul", "personal_drive", "ui_sessions.db"})
DirKind = Literal["touka", "sealed"]

# 失敗時：exit 125，stderr 一行 ``TOUKA_SANDBOX_ERROR:<code>[ <detail>]``（給人看）；有
# ``--status-fd`` 時另把代碼寫進那條通道（``encode_status``），runner 只認那條通道
FAILURE_EXIT = 125
FAILURE_MARKER = "TOUKA_SANDBOX_ERROR:"
FAILURE_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
STATUS_MAX_BYTES = 128
# Landlock 擋不到的網路設定（TCP Fast Open、MPTCP）：自測另外回報；``net_strict`` 的啟動也以它拒絕
NET_TCP_FASTOPEN, NET_MPTCP = "net_tcp_fastopen", "net_mptcp"
NET_SYSCTL_CODES = frozenset({NET_TCP_FASTOPEN, NET_MPTCP})
# usage：命令列；spec_*：spec；landlock_unavailable／abi_too_old：核心；process_setup：
# NO_NEW_PRIVS／PDEATHSIG；path_open／path_type：必要路徑開不了或型別不能授權；
# landlock_rule：建規則集或 restrict_self 失敗；rlimit：setrlimit 失敗；net_*：見上
FAILURE_CODES = frozenset(
    {"usage", "spec_read", "spec_too_large", "spec_invalid", "spec_version", "spec_path"}
    | {"spec_ports", "landlock_unavailable", "abi_too_old", "process_setup", "path_open"}
    | {"path_type", "landlock_rule", "rlimit"}
    | NET_SYSCTL_CODES
)
# relay 自己的失敗（遠端啟動器的失敗照原代碼轉回）：連不上或連線在 EXIT 前斷掉、執行器重啟過
# （boot_id 不同，沒送 START）、權杖讀不到或被拒、START 被拒
RELAY_FAILURE_CODES = frozenset(
    {"executor_unreachable", "executor_restarted", "executor_auth_failed", "executor_rejected"}
)
# 可信直譯器不合格（proxy 判 none、降級只能 direct）
INTERPRETER_UNTRUSTED = "interpreter_untrusted"
_GROUP_OTHER_WRITE = 0o022

_TOP_KEYS = frozenset(
    {"version", "profile", "rules", "block_ports", "rlimits", "pin", "parent_pid", "net_strict"}
    | {"exec_env"}
)
_RULE_KEYS = frozenset({"path", "access", "optional", "skip_symlink"})
_PROFILE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


class SpecError(Exception):
    """spec 或命令列不合格式；``code`` 是失敗標記用的代碼。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RuleSpec:
    """一條路徑規則（已驗證）。"""

    path: str
    access: str
    optional: bool
    skip_symlink: bool


@dataclass(frozen=True)
class LaunchSpec:
    """驗證過的 spec。"""

    version: int
    profile: str
    rules: tuple[RuleSpec, ...]
    block_ports: tuple[int, ...]
    rlimits: tuple[tuple[str, int], ...]
    pin: bool
    parent_pid: int = 0
    net_strict: bool = True
    exec_env: tuple[tuple[str, str], ...] = ()


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SpecError("spec_invalid", "duplicate_key")
        result[key] = value
    return result


def _is_int(value: object) -> bool:
    return type(value) is int


def _flag(document: dict, key: str, default: bool) -> bool:
    value = document.get(key, default)
    if type(value) is not bool:
        raise SpecError("spec_invalid", key)
    return value


def _checked_path(path: object) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise SpecError("spec_path")
    if len(path.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise SpecError("spec_path")
    segments = path.split("/")[1:]
    if not all(seg and seg not in {".", ".."} for seg in segments):
        raise SpecError("spec_path")
    return path


def _rule(item: object) -> RuleSpec:
    if not isinstance(item, dict) or not set(item) <= _RULE_KEYS:
        raise SpecError("spec_invalid", "rule")
    path = _checked_path(item.get("path"))
    access = item.get("access")
    if access not in ACCESS_NAMES:
        raise SpecError("spec_invalid", "access")
    optional, skip_symlink = _flag(item, "optional", False), _flag(item, "skip_symlink", False)
    return RuleSpec(path, access, optional, skip_symlink)


def _ports(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > MAX_BLOCK_PORTS:
        raise SpecError("spec_ports")
    if not all(_is_int(p) and 1 <= p <= MAX_PORT for p in value) or len(set(value)) != len(value):
        raise SpecError("spec_ports")
    return tuple(value)


def _rlimits(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict) or not set(value) <= RLIMIT_NAMES:
        raise SpecError("spec_invalid", "rlimits")
    if not all(_is_int(v) and 0 <= v <= MAX_RLIMIT for v in value.values()):
        raise SpecError("spec_invalid", "rlimits")
    return tuple(sorted(value.items()))


def exec_env_reserved(name: str) -> bool:
    """``exec_env`` 不能用的名稱（profile 管的；不分大小寫）；proxy 端也用它先濾掉."""
    upper = name.upper()
    return upper in EXEC_ENV_RESERVED or upper.startswith(EXEC_ENV_RESERVED_PREFIXES)


def exec_env_stripped(name: str) -> bool:
    """``exec_env`` 裡要移除的名稱（直譯器控制、動態連結 loader；禁令 19）；proxy 端也用它先濾掉."""
    upper = name.upper()
    return name in EXEC_ENV_STRIP_NAMES or any(part in upper for part in EXEC_ENV_STRIP_PATTERNS)


def _exec_env(value: object) -> tuple[tuple[str, str], ...]:
    """驗證整份（格式、保留名稱、上限都算上要移除的那些）後，回傳移除禁令 19 名稱的結果."""
    if not isinstance(value, dict) or len(value) > MAX_EXEC_ENV_ITEMS:
        raise SpecError("spec_invalid", "exec_env")
    total = 0
    for name, text in value.items():
        if not EXEC_ENV_NAME_PATTERN.fullmatch(name) or not isinstance(text, str) or "\x00" in text:
            raise SpecError("spec_invalid", "exec_env")
        if exec_env_reserved(name):
            raise SpecError("spec_invalid", "exec_env_reserved")
        try:  # execvpe 用的就是這個編碼；編不出來（單獨的 surrogate）就在這裡拒絕
            total += len(name) + len(text.encode("utf-8", "surrogateescape")) + 2
        except UnicodeEncodeError as exc:
            raise SpecError("spec_invalid", "exec_env") from exc
    if total > MAX_EXEC_ENV_BYTES:
        raise SpecError("spec_invalid", "exec_env_size")
    return tuple(sorted((k, v) for k, v in value.items() if not exec_env_stripped(k)))


def parse_spec(raw: bytes) -> LaunchSpec:
    """驗證 spec 原文（UTF-8 JSON）；不合格拋 ``SpecError``。"""
    if len(raw) > MAX_SPEC_BYTES:
        raise SpecError("spec_too_large")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SpecError("spec_invalid", "json") from exc
    if not isinstance(document, dict):
        raise SpecError("spec_invalid", "document")
    version = document.get("version")
    if not _is_int(version) or version != SPEC_VERSION:
        raise SpecError("spec_version")
    if not set(document) <= _TOP_KEYS:
        raise SpecError("spec_invalid", "keys")
    profile = document.get("profile", "unnamed")
    if not isinstance(profile, str) or not _PROFILE_PATTERN.fullmatch(profile):
        raise SpecError("spec_invalid", "profile")
    rules = document.get("rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise SpecError("spec_invalid", "rules")
    parent_pid = document.get("parent_pid", 0)
    if not _is_int(parent_pid) or parent_pid < 0:
        raise SpecError("spec_invalid", "parent_pid")
    return LaunchSpec(
        version=version,
        profile=profile,
        rules=tuple(_rule(item) for item in rules),
        block_ports=_ports(document.get("block_ports", [])),
        rlimits=_rlimits(document.get("rlimits", {})),
        pin=_flag(document, "pin", True),
        parent_pid=parent_pid,
        net_strict=_flag(document, "net_strict", True),
        exec_env=_exec_env(document.get("exec_env", {})),
    )


def encode_status(code: str) -> bytes:
    """失敗代碼通道的內容：一行 ASCII 代碼。"""
    return code.encode("ascii") + b"\n"


def parse_status(raw: bytes, known: frozenset[str]) -> str | None:
    """讀失敗代碼通道的內容：恰好一個 ``known`` 裡的代碼才算；其他（含空的）一律 None。"""
    if len(raw) > STATUS_MAX_BYTES:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    code = text[:-1] if text.endswith("\n") else text
    if not FAILURE_CODE_PATTERN.fullmatch(code):
        return None
    return code if code in known else None


class InterpreterUntrustedError(Exception):
    """可信直譯器不合格；``code`` 一律是 ``INTERPRETER_UNTRUSTED``，``detail`` 給 log。"""

    def __init__(self, detail: str) -> None:
        super().__init__(INTERPRETER_UNTRUSTED)
        self.code = INTERPRETER_UNTRUSTED
        self.detail = detail


def owner_chain_issue(path: PurePath, lstat: Callable[[PurePath], os.stat_result]) -> str | None:
    """檔案與每一層上層（到 ``/``）都要 root 擁有、群組與其他人不可寫、不是連結；不符回原因。"""
    for node in (path, *path.parents):
        try:
            info = lstat(node)
        except OSError:
            return f"unreadable {node}"
        if stat.S_ISLNK(info.st_mode):
            return f"symlink {node}"
        if info.st_uid != 0:
            return f"owner {node}"
        if info.st_mode & _GROUP_OTHER_WRITE:
            return f"writable {node}"
        if node == path and not stat.S_ISREG(info.st_mode):
            return f"not_file {node}"
    return None


def trusted_interpreter(*, require_root_owned: bool) -> str:
    """啟動器、relay、自測與執行器用的直譯器：``sys._base_executable`` 解析後的實體路徑.

    venv 的 python（``sys.executable``）可能放在服務帳號改得到的位置（容器的 ``/app`` 由 appuser
    擁有），所以不用。``require_root_owned``（容器裡）時整條擁有者鏈要過 ``owner_chain_issue``；
    原生環境沒有信任根，只解析路徑。不合格拋 ``InterpreterUntrustedError``。
    """
    base = getattr(sys, "_base_executable", None)
    if not base:
        raise InterpreterUntrustedError("missing")
    try:
        path = Path(base).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InterpreterUntrustedError("unresolvable") from exc
    if require_root_owned:
        issue = owner_chain_issue(path, os.lstat)
        if issue:
            raise InterpreterUntrustedError(issue)
    return str(path)


def filter_executor_env(env: Mapping[str, str]) -> dict[str, str]:
    """Proxy 端：只留允許清單裡的精確名稱（GIT_CONFIG_* 一律拿掉，之後再套 profile 那一組）。"""
    return {name: value for name, value in env.items() if name in EXECUTOR_ENV_ALLOWED}


def executor_env_issue(env: object) -> str | None:
    """伺服器端驗證 START 的 env；不合格回原因（拒絕 START），合格回 None。"""
    if not isinstance(env, dict):
        return "env_type"
    git = {k: v for k, v in env.items() if isinstance(k, str) and k.startswith(_GIT_CONFIG_PREFIX)}
    if git and git != dict(EXECUTOR_GIT_CONFIG):
        return "env_git_config"
    total = 0
    for name, value in env.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return "env_type"
        if name not in EXECUTOR_ENV_ALLOWED and name not in git:
            return "env_name"
        if "\x00" in value:
            return "env_nul"
        total += len(name.encode("utf-8")) + len(value.encode("utf-8", "surrogatepass")) + 2
    return "env_too_large" if total > MAX_EXECUTOR_ENV_BYTES else None


def fold_name(name: str) -> str:
    """比對用的名稱：NFC 正規化後 casefold（9p 與 NTFS 不分大小寫）。"""
    return unicodedata.normalize("NFC", name).casefold()


def listing_kind(names: Iterable[str]) -> DirKind | None:
    """只看資料夾內容（名稱已 fold）：資料根形狀是 ``sealed``，有標記是 ``touka``。"""
    folded = frozenset(names)
    if folded & DATA_ROOT_NAMES:
        return "sealed"
    return "touka" if folded & TOUKA_MARKERS else None


def kernel_digest(kernel_dir: Path | None) -> str | None:
    """回傳 kernel 目錄裡 ``*.py`` 的 sha256（依檔名排序，逐檔「檔名＋NUL＋內容＋NUL」）。

    proxy 比對 live src 與 ``/opt`` 這份、執行器回報 READY 都用這個演算法；沒有目錄或檔案回 None。
    """
    if kernel_dir is None or not kernel_dir.is_dir():
        return None
    files = sorted((p for p in kernel_dir.glob("*.py") if p.is_file()), key=lambda p: p.name)
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class SelfTestResult:
    """``launcher.py --self-test`` 的結果（proxy 端解析）。"""

    ok: bool
    abi: int = 0
    error: str | None = None
    net: str | None = None  # TCP 規則擋不住的網路設定（另外回報，不算自測失敗）


def parse_selftest_output(returncode: int | None, stdout: bytes) -> SelfTestResult:
    """解析 ``--self-test`` 最後一行 JSON ``{"ok", "abi", "error", "pin", "failed", "net"}``.

    通過 exit 0；檢查失敗 exit 1；Landlock 不可用或 ABI 太舊 exit 125（``abi`` 照實填）。
    ``error`` 保留啟動器給的代碼，沒有才用 ``exit_<rc>``；exit 不是 0 或帶 error 一律不算通過。
    """
    lines = [line for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    try:
        report = json.loads(lines[-1]) if lines else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return SelfTestResult(ok=False, error="bad_output")
    abi = report.get("abi") if isinstance(report.get("abi"), int) else 0
    error = report.get("error")
    error = str(error) if error else (None if returncode == 0 else f"exit_{returncode}")
    ok = returncode == 0 and report.get("ok") is True and error is None
    net = str(report["net"]) if report.get("net") else None
    return SelfTestResult(ok=ok, abi=abi, error=error, net=net)

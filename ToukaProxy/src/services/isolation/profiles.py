"""隔離 profile：路徑權利、永不授權規則、環境變數改寫與封鎖埠（iso-c2）.

判定順序（先命中的生效）：
1. Touka 自選路徑（代理人資料夾、ST 底下自己的子資料夾、系統路徑等）——由本模組依形狀組出；
2. 專案根與 cwd 每一層上層的九個公開設定檔——唯讀，位在 D、ST、受保護的 E 底下則不給；
3. 推導路徑（專案根等）：經過連結的拒絕；可給根目錄 G（代理人自己的資料夾）直接放行；固定
   受保護集合 P、鏡像根本身拒絕；其餘交給 Touka 目錄偵測（``touka_directory_verdict``）。
所有路徑先 resolve 再比對；啟動器再逐段 NOFOLLOW 開啟。

位置（``Location``）：proxy 內的 Landlock 用 proxy 的 ST（``ST/proxy/<kind>/<owner>``，在這裡建）；
執行器位置（C2-2）只給目標、執行器 ``state_root/<kind>/<owner>/{home,tmp}``（伺服器建，proxy 不建、
不 resolve）、系統路徑與唯讀 root：公開設定檔規則與專案都不給（伺服器的 START 驗證只收這些）。
封鎖埠 B 由呼叫端傳入（monitor 的 ``block_ports(location)``）；超過上限拒絕，不截斷。
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from src.config import (
    HOST_WORKSPACE_ROOT,
    PROXY_ROOT,
    is_container_runtime,
    resolve_env_file_path,
    settings,
)
from src.sandbox_kernel.spec import (
    DEFAULT_BLOCK_PORTS,
    MAX_BLOCK_PORTS,
    MAX_PORT,
    PUBLIC_CONFIG_NAMES,
    SANDBOX_ENV_UNSET,
    SPEC_VERSION,
)
from src.services.isolation.project_scan import project_scanner
from src.services.resource_manager import resource_manager
from src.utils.isolation_policy import is_safe_segment
from src.utils.touka_dirs import PROTECTED_LOCATION

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from src.models.identity import AgentIdentity
    from src.sandbox_kernel.spec import AccessName, RlimitName

Location = Literal["proxy", "executor"]
# proxy 位置的 state 種類（``ST/proxy/<kind>/<owner>``）；MCP 自建與第三方分開父目錄，段名不會撞
StateKind = Literal["agents", "evolution", "mcp_self", "mcp_third"]
# 執行器 START 的 kind（伺服器的 ``executor_session.KINDS``；kind mcp 只放自建）
ExecutorKind = Literal["agents", "evolution", "mcp"]

SYSTEM_EXEC_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
SYSTEM_READ_PATHS = ("/etc", "/proc")
DEVICE_RW_PATHS = ("/dev/null", "/dev/zero", "/dev/full")
DEVICE_READ_PATHS = ("/dev/urandom", "/dev/random")
PYTEST_ADDOPTS = "PYTEST_ADDOPTS"
SANDBOX_STATE_UNCONFIGURED = "sandbox_state_unconfigured"
BLOCK_PORTS_OVERFLOW = "block_ports_overflow"
# 執行器 START 的 kind（依 profile 名稱）
EXECUTOR_KINDS: Mapping[str, ExecutorKind] = {
    "agent_command": "agents", "evolution": "evolution", "mcp_self": "mcp",
}


class ProfileError(Exception):
    """profile 組不出來（路徑形狀不對、ST 未設定等）；呼叫端一律當隔離不可用。

    ``reason`` 是可以照實告訴使用者的原因（ST 的問題）；其他形狀問題沒有，呼叫端用籠統原因。
    """

    def __init__(self, subject: str, reason: str | None = None) -> None:
        super().__init__(subject)
        self.reason = reason


@dataclass(frozen=True)
class SandboxLayout:
    """隔離規則需要的實體路徑（全部由設定與 ResourceManager 推導）。"""

    data_root: Path
    config_dir: Path
    install_root: Path
    proxy_root: Path
    env_file: Path
    state_root: Path | None
    kernel_dir: Path | None
    executor_dir: Path | None
    mirror_roots: tuple[Path, ...]
    in_container: bool


@dataclass(frozen=True)
class PathRule:
    """一條路徑權利；``optional`` 遇 ENOENT 略過，``skip_symlink`` 遇 ELOOP 也略過。"""

    path: Path
    access: AccessName
    optional: bool = False
    skip_symlink: bool = False


@dataclass(frozen=True)
class SandboxProfile:
    """交給 runner 的完整 profile；``ungranted`` 列出要求了卻不能授權的原因。"""

    name: str
    cwd: Path
    rules: tuple[PathRule, ...]
    block_ports: tuple[int, ...]
    env_set: Mapping[str, str] = field(default_factory=dict)
    env_unset: frozenset[str] = SANDBOX_ENV_UNSET
    rlimits: Mapping[RlimitName, int] = field(default_factory=dict)
    project_root: Path | None = None
    ungranted: tuple[str, ...] = ()
    pin: bool = True  # 9p 上只有 pin 模式穩定（U1a 探測），一律預設
    owner: str | None = None  # 代理人 ID 或 skill ID（執行器 START 的 owner）


def _resolve(path: Path | str) -> Path:
    return Path(path).resolve()


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def current_layout() -> SandboxLayout:
    """依目前設定組出版面；容器內 ST／kernel 未設定時保留 None（由狀態判定原因）。"""
    in_container = is_container_runtime()
    data_root = _resolve(resource_manager.get_base_path())
    state = settings.TOUKA_SANDBOX_STATE_DIR
    kernel = settings.TOUKA_SANDBOX_KERNEL_DIR
    native_state = None if in_container else resource_manager.get_temp_path("sandbox_state")
    native_kernel = None if in_container else PROXY_ROOT / "src" / "sandbox_kernel"
    env_file = _resolve(resolve_env_file_path())
    mirrors = {env_file.parent, *(m for m in (resource_manager.get_workspace_mount(),) if m)}
    if Path(HOST_WORKSPACE_ROOT).is_absolute():
        mirrors.add(Path(HOST_WORKSPACE_ROOT))
    return SandboxLayout(
        data_root=data_root,
        config_dir=_resolve(settings.CONFIG_DIR),
        install_root=PROXY_ROOT.parent,
        proxy_root=PROXY_ROOT,
        env_file=env_file,
        state_root=_resolve(state) if state else (_resolve(native_state) if native_state else None),
        kernel_dir=_resolve(kernel) if kernel else native_kernel,
        executor_dir=_resolve(settings.TOUKA_EXECUTOR_DIR) if settings.TOUKA_EXECUTOR_DIR else None,
        mirror_roots=tuple(sorted(_resolve(m) for m in mirrors)),
        in_container=in_container,
    )


def _protected_env_dir(layout: SandboxLayout) -> Path | None:
    """E 只有在不是 D 的上層時才整個受保護（Docker 的 host_root）。"""
    env_dir = _resolve(layout.env_file.parent)
    return None if _within(_resolve(layout.data_root), env_dir) else env_dir


def fixed_protected(layout: SandboxLayout) -> tuple[Path, ...]:
    """固定受保護集合 P（設定、資料、程式、備份、kernel、執行器、ST 根、E、env 檔）。"""
    install = layout.install_root
    items = [
        layout.data_root,
        layout.config_dir,
        layout.proxy_root,
        *(install / name for name in ("ToukaMemory", "ToukaDispatch", "ToukaHeartbeat")),
        install / "backups",
        layout.env_file,
        *(p for p in (layout.kernel_dir, layout.executor_dir, layout.state_root) if p),
        *(p for p in (_protected_env_dir(layout),) if p),
    ]
    return tuple(_resolve(p) for p in items)


def grantable_roots(layout: SandboxLayout, owner: str | None) -> tuple[Path, ...]:
    """可給根目錄 G：只有代理人自己的 ``personal_drive/<id>``（共用的 docgen_output 不給）。"""
    if owner is None or not is_safe_segment(owner):
        return ()
    return (_resolve(layout.data_root) / "personal_drive" / owner,)


def _denied_by_fixed_rules(path: Path, layout: SandboxLayout) -> bool:
    mirrors = tuple(_resolve(m) for m in layout.mirror_roots)
    if path in mirrors:
        return True
    for item in fixed_protected(layout):
        if _within(path, item) or item.is_relative_to(path):
            return True
    install = _resolve(layout.install_root)
    if layout.in_container and path.is_relative_to(install):
        # 容器裡 INSTALL_ROOT 的子項目全受保護；工作區掛載根的子目錄交給 Touka 目錄偵測
        return not any(path.is_relative_to(m) and m.parent == install for m in mirrors)
    return False


async def project_root_reason(path: Path, layout: SandboxLayout) -> str | None:
    """一般使用者的專案根（登記、列檔、專案檔案工具）；可用回 None，否則回拒絕原因.

    固定受保護集合 P 與鏡像根一律檢查（解析連結後比對，P 的上層也算）。容器裡工作區掛載
    可能是 Touka 目錄的另一個路徑，另做 Touka 目錄偵測（往上、往下）。原生安裝不做：
    E 就是 INSTALL_ROOT，往上檢查會擋掉工作區根底下所有專案；原生路徑沒有掛載別名，
    解析後比對已足夠。不拒絕連結：檔案工具本身不讓路徑越出專案根。（ADR-0292）
    """
    target = _resolve(path)
    if _denied_by_fixed_rules(target, layout):
        return PROTECTED_LOCATION
    if layout.in_container:
        return await touka_directory_verdict(target, layout)
    return None


async def touka_directory_verdict(path: Path, layout: SandboxLayout) -> str | None:
    """Touka 目錄偵測與上下檢查（往上到鏡像根、往下 3 層），見 ``project_scan``。"""
    return await project_scanner.verdict(path, layout)


async def authorize_derived_path(
    path: Path, layout: SandboxLayout, owner: str | None = None
) -> str | None:
    """推導路徑（專案根等）的授權判定；回拒絕原因，可授權回 None。

    ``owner`` 是已驗證身分的 agent_id（同意帳本鍵的同一個投影），G 只有它自己的資料夾。
    路徑任何一段是符號連結或 junction（resolve 前後不同）一律拒絕：連結可以被改寫成指向別處。
    """
    target = _resolve(path)
    if os.path.normcase(Path(path).absolute()) != os.path.normcase(target):
        return PROTECTED_LOCATION
    if any(_within(target, root) for root in grantable_roots(layout, owner)):
        return None
    if _denied_by_fixed_rules(target, layout):
        return PROTECTED_LOCATION
    return await touka_directory_verdict(target, layout)


def public_config_rules(anchors: Iterable[Path], layout: SandboxLayout) -> tuple[PathRule, ...]:
    """錨點每一層上層裡、真實存在的九個公開設定檔（一般檔、非硬連結），唯讀。"""
    blocked = [_resolve(layout.data_root)]
    blocked += [p for p in (layout.state_root, _protected_env_dir(layout)) if p]
    found: dict[Path, PathRule] = {}
    for anchor in anchors:
        for parent in _resolve(anchor).parents:
            for name in sorted(PUBLIC_CONFIG_NAMES):
                candidate = parent / name
                try:
                    info = os.lstat(candidate)
                except OSError:
                    continue
                # lstat：符號連結不是一般檔；硬連結（st_nlink>1）也略過
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if any(_within(candidate, _resolve(b)) for b in blocked):
                    continue
                found[candidate] = PathRule(candidate, "ro", optional=True, skip_symlink=True)
    return tuple(found.values())


def launcher_spec_fields(profile: SandboxProfile) -> dict[str, Any]:
    """給啟動器的 spec 欄位（不含環境變數或祕密）；版本與驗證由 sandbox_kernel 定義。"""
    return {
        "version": SPEC_VERSION,
        "profile": profile.name,
        "rules": [
            {
                "path": str(rule.path),
                "access": rule.access,
                "optional": rule.optional,
                "skip_symlink": rule.skip_symlink,
            }
            for rule in profile.rules
        ],
        "block_ports": list(profile.block_ports),
        "rlimits": dict(profile.rlimits),
        "pin": profile.pin,
    }


def _url_port(url: str | None) -> int | None:
    try:
        return urlsplit(url or "").port
    except ValueError:
        return None


def _int_port(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def blocked_ports(location: Location) -> tuple[int, ...]:
    """封鎖埠集合 B；SearXNG 的埠只加在 proxy 內（執行器連不到 SearXNG）。"""
    candidates = {
        *DEFAULT_BLOCK_PORTS,
        _int_port(settings.PORT),
        _int_port(settings.DISPATCH_PORT),
        _url_port(settings.HEARTBEAT_EXTERNAL_URL),
        _url_port(settings.UI_DEFAULT_URL),
        *(_int_port(v) for v in (settings.PROXY_PORT, settings.HEARTBEAT_PORT, settings.CHAT_PORT)),
    }
    if location == "proxy" and settings.SEARXNG_BASE_URL:
        candidates.add(_url_port(settings.SEARXNG_BASE_URL))
    return tuple(sorted(p for p in candidates if isinstance(p, int) and 1 <= p <= MAX_PORT))


def checked_ports(block_ports: tuple[int, ...] | None, location: Location) -> tuple[int, ...]:
    """呼叫端傳入的 B（沒傳就是 Touka 自己的埠）；超過上限拒絕，不截斷（截斷會丟掉高號的埠）."""
    ports = blocked_ports(location) if block_ports is None else block_ports
    if len(ports) > MAX_BLOCK_PORTS:
        raise ProfileError("block_ports", BLOCK_PORTS_OVERFLOW)
    return ports


def state_dirs(
    layout: SandboxLayout, location: Location, kind: str, owner: str, *, create: bool = True
) -> tuple[Path, Path]:
    """ST 的 ``{home,tmp}``；分類可只算位置，預設建立，ST 未設定或名稱不安全就拒絕。"""
    if layout.state_root is None:
        raise ProfileError(owner, SANDBOX_STATE_UNCONFIGURED)
    if not is_safe_segment(owner):
        raise ProfileError(owner)
    base = _resolve(layout.state_root) / location / kind / owner
    dirs = (base / "home", base / "tmp")
    if not create:
        return dirs
    try:
        for directory in dirs:
            resource_manager.make_dir(directory)
    except OSError as exc:
        raise ProfileError(owner, SANDBOX_STATE_UNCONFIGURED) from exc
    return dirs


def _state(
    layout: SandboxLayout,
    location: Location,
    kind: StateKind,
    owner: str,
    executor_state: str | None,
) -> tuple[Path, Path]:
    """Proxy 位置用 ``state_dirs``；執行器位置在 READY 的 ``state_root`` 底下（伺服器建）."""
    if location == "proxy":
        return state_dirs(layout, location, kind, owner)
    if executor_state is None or not is_safe_segment(owner):
        raise ProfileError(owner)
    base = Path(executor_state) / kind / owner
    return base / "home", base / "tmp"


def _own_drive(identity: AgentIdentity, agent_dir: Path, layout: SandboxLayout) -> Path:
    agent_id = identity.agent_id
    data = _resolve(layout.data_root)
    target = _resolve(agent_dir)
    shapes = (data / "personal_drive" / agent_id, data / "custom" / agent_id / "drive")
    if not is_safe_segment(agent_id) or target not in shapes:
        raise ProfileError(agent_id)
    return target


def system_rules() -> list[PathRule]:
    """系統路徑（含 sys.prefix 與 base_prefix）唯讀＋執行，/etc、/proc 與裝置照各自的權利."""
    execs =[*SYSTEM_EXEC_PATHS, sys.prefix, sys.base_prefix]
    rules = [PathRule(_resolve(p), "rox", optional=True) for p in execs]
    rules += [PathRule(_resolve(p), "ro", optional=True) for p in SYSTEM_READ_PATHS]
    rules += [PathRule(_resolve(p), "dev_rw", optional=True) for p in DEVICE_RW_PATHS]
    rules += [PathRule(_resolve(p), "ro", optional=True) for p in DEVICE_READ_PATHS]
    return rules


def sandbox_env(
    home: Path, tmp: Path, ceilings: Iterable[Path], pytest_root: Path | None = None
) -> dict[str, str]:
    """HOME／TMP／XDG 指向 ST；git 只認自己的資料夾、不往上找到 Touka 的 `.git`。

    pytest 以 ``pytest_root``（專案根或 cwd）為 rootdir／confcutdir：上層的 pytest.ini 照樣讀，
    但不去列出或載入 Landlock 沒給的上層資料夾；指令列自己給的同名選項仍然優先。
    """
    env = {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        "GIT_CEILING_DIRECTORIES": os.pathsep.join(dict.fromkeys(str(p) for p in ceilings)),
    }
    if pytest_root is not None:
        root = shlex.quote(str(pytest_root))
        env[PYTEST_ADDOPTS] = f"--rootdir={root} --confcutdir={root}"
    return env


async def build_agent_command_profile(
    identity: AgentIdentity,
    agent_dir: Path,
    *,
    layout: SandboxLayout,
    project_root: Path | None = None,
    location: Location = "proxy",
    block_ports: tuple[int, ...] | None = None,
    executor_state: str | None = None,
) -> SandboxProfile:
    """代理人指令（終端、驗證指令）的 profile；專案根只來自 ``session_project_root``.

    執行器位置不掛專案（給了就拒絕）、不給公開設定檔；``executor_state`` 是 READY 的 state_root。
    """
    if location == "executor" and project_root is not None:
        raise ProfileError(identity.agent_id)
    ports = checked_ports(block_ports, location)
    cwd = _own_drive(identity, agent_dir, layout)
    home, tmp = _state(layout, location, "agents", identity.agent_id, executor_state)
    rules = system_rules()
    skills = _resolve(layout.config_dir) / "skills"
    rules.append(PathRule(skills, "rox", optional=True))
    rules += [PathRule(p, "rwx") for p in (cwd, home, tmp)]
    granted_project: Path | None = None
    ungranted: tuple[str, ...] = ()
    if project_root is not None:
        verdict = await authorize_derived_path(project_root, layout, identity.agent_id)
        if verdict is None:
            granted_project = _resolve(project_root)
            rules.append(PathRule(granted_project, "rwx"))
        else:
            ungranted = (verdict,)
    anchors = [cwd, *(p for p in (granted_project,) if p)]
    public = public_config_rules(anchors, layout) if location == "proxy" else ()
    rules += public
    ceilings = [p.parent for p in anchors]
    # 只有上層真的授權到公開設定檔時，pytest 才會往上把 rootdir 定在那裡：這時才固定在專案根
    # （或 cwd）；否則不注入，資料夾裡子專案自己的 testpaths 照常生效
    pytest_root = granted_project or cwd
    upper = any(pytest_root.is_relative_to(rule.path.parent) for rule in public)
    return SandboxProfile(
        name="agent_command",
        cwd=cwd,
        rules=tuple(dict.fromkeys(rules)),
        block_ports=ports,
        env_set=sandbox_env(home, tmp, ceilings, pytest_root=pytest_root if upper else None),
        project_root=granted_project,
        ungranted=ungranted,
        owner=identity.agent_id,
    )


def build_evolution_profile(
    skill_id: str,
    tool_root: Path,
    *,
    layout: SandboxLayout,
    location: Location = "proxy",
    block_ports: tuple[int, ...] | None = None,
    executor_state: str | None = None,
) -> SandboxProfile:
    """演化建置與驗證：讀寫只給 ``managed/<skill>`` 與自己的 state 資料夾；rlimit 由指令帶入."""
    ports = checked_ports(block_ports, location)
    root = _resolve(tool_root)
    managed = _resolve(layout.data_root) / "mcp_servers" / "managed"
    if not is_safe_segment(skill_id) or root != managed / skill_id:
        raise ProfileError(skill_id)
    home, tmp = _state(layout, location, "evolution", skill_id, executor_state)
    rules = system_rules()
    rules.append(PathRule(_resolve(layout.config_dir) / "skills", "rox", optional=True))
    rules += [PathRule(p, "rwx") for p in (root, home, tmp)]
    if location == "proxy":
        rules += public_config_rules([root], layout)
    return SandboxProfile(
        name="evolution",
        cwd=root,
        rules=tuple(dict.fromkeys(rules)),
        block_ports=ports,
        env_set=sandbox_env(home, tmp, [root.parent]),
        owner=skill_id,
    )

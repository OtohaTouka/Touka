"""MCP 伺服器的隔離 profile（C2-3）：自建、第三方與 scaffold.

state 段名一律由 Touka 從伺服器名算（類別標籤＋sha256 前 24 個十六進位字），自建與第三方分開
父目錄：proxy 位置 ``ST/proxy/mcp_self/<seg>``、``ST/proxy/mcp_third/<seg>``；執行器 kind ``mcp``
只放自建（``<executor_state>/mcp/<seg>``，伺服器建）。代理人給的名字（skill_id、create_mcp 的
名稱）只是雜湊的輸入，不可能等於別台或別類的段名。分類（``utils.mcp_origin``）由呼叫端先做。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from memory_server.utils.zone_paths import ALL_ANCHORS, Anchor, anchor_at, zone_of

from src.services.isolation.profiles import (
    PathRule,
    ProfileError,
    SandboxProfile,
    authorize_derived_path,
    checked_ports,
    layout_zone_roots,
    proxy_state_zone,
    sandbox_env,
    state_dirs,
    system_rules,
)
from src.utils.isolation_policy import MAX_MCP_SANDBOX_PATHS, is_safe_segment
from src.utils.mcp_origin import (
    RawMcpEntry,
    docgen_output_root,
    is_file_reference,
    managed_root,
    personal_drive_root,
    referenced_paths,
    resolve_reference_sentinel,
    scaffold_dir_for,
)
from src.utils.mcp_sentinels import APPDATA_SENTINEL
from src.utils.touka_dirs import PROTECTED_LOCATION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from src.sandbox_kernel.spec import AccessName
    from src.services.isolation.profiles import Location, SandboxLayout
    from src.utils.mcp_origin import McpClassification, McpEntry

McpStateCategory = Literal["self", "third"]
MCP_SELF_PROFILE = "mcp_self"
MCP_THIRD_PARTY_PROFILE = "mcp_third_party"
MCP_SCAFFOLD_PROFILE = "mcp_scaffold"
# proxy 位置的 state 父目錄（自建與第三方分開）；執行器 kind ``mcp`` 只放自建
MCP_PROXY_STATE_KINDS: Mapping[McpStateCategory, str] = {"self": "mcp_self", "third": "mcp_third"}
EXECUTOR_MCP_KIND = "mcp"
STATE_SEGMENT_PREFIX = "m"
STATE_SEGMENT_HEX = 24
# ungranted 的代碼（只有代碼，沒有路徑）；其他原因照永不授權回的代碼
CODE_DIR_REFUSED = "code_dir_refused"  # 引用了不只一個（或名稱不安全的）managed/<skill>
HOME_OR_ROOT = "home_or_root"  # 引用檔案的所在資料夾是家目錄或 /，要人另加更窄的資料夾
SANDBOX_PATH_INVALID = "sandbox_path_invalid"
SANDBOX_PATHS_OVERFLOW = "sandbox_paths_overflow"
SCAFFOLD_DIR_MISMATCH = "scaffold_dir_mismatch"
# 家目錄（LOW 16）：使用者的家目錄、root 的家目錄、``/home`` 底下每一個
HOME_DIR_PARENTS: tuple[Path, ...] = (Path("/home"),)
ROOT_HOME_DIRS: tuple[Path, ...] = (Path("/root"),)
# 人設的額外允許資料夾：唯讀或讀寫（kernel 的讀寫一定帶執行）
SANDBOX_PATH_ACCESS: Mapping[str, AccessName] = {"ro": "ro", "rw": "rwx"}


class SandboxPathGrant(Protocol):
    """人設的「額外允許的資料夾」一筆（設定模型由寫入那一層定義）。"""

    @property
    def path(self) -> str:
        """絕對路徑。"""
        ...

    @property
    def access(self) -> str:
        """``ro`` 或 ``rw``。"""
        ...


@dataclass(frozen=True)
class McpThirdPartyLaunch:
    """第三方／scaffold 的 profile，與 APPDATA 對映後的參數與 env（啟動時用這兩份）。"""

    profile: SandboxProfile
    args: tuple[str, ...]
    env: Mapping[str, str]
    command: str | None = None


def user_home() -> Path | None:
    """執行 Touka 的使用者的家目錄；容器使用者沒有 HOME 也沒有 passwd 項目時是 None。"""
    try:
        return Path.home()
    except RuntimeError:
        return None


def state_segment(category: McpStateCategory, name: str) -> str:
    """``m`` 加 sha256（類別、NUL、伺服器名）前 24 個十六進位字（單一安全路徑段）。"""
    raw = f"{category}\0{name}".encode("utf-8", "surrogatepass")
    return STATE_SEGMENT_PREFIX + hashlib.sha256(raw).hexdigest()[:STATE_SEGMENT_HEX]


def mcp_state_dirs(
    category: McpStateCategory,
    name: str,
    *,
    layout: SandboxLayout,
    location: Location,
    executor_state: str | None = None,
    create: bool = True,
) -> tuple[Path, Path]:
    """自己的 ``{home,tmp}``：proxy 位置在 ST 底下建立；執行器位置只算路徑（只放自建）。"""
    seg = state_segment(category, name)
    if location == "proxy":
        return state_dirs(layout, location, MCP_PROXY_STATE_KINDS[category], seg, create=create)
    if category != "self" or executor_state is None:
        raise ProfileError(seg)
    base = Path(executor_state) / EXECUTOR_MCP_KIND / seg
    return base / "home", base / "tmp"


def mcp_classification_bases(
    name: str, *, layout: SandboxLayout, base_dir: Path,
) -> tuple[Path, ...]:
    """啟動與部署凍結共用的解析基準；只算位置，不建立 state.

    版面沒設 ST 時只有 ``base_dir``；版面與 RM 的 ST 不同時往外丟 ``ProfileError``。
    """
    homes = () if proxy_state_zone(layout) is None else tuple(
        mcp_state_dirs(category, name, layout=layout, location="proxy", create=False)[0]
        for category in ("third", "self")
    )
    return (base_dir, *homes)


def build_mcp_self_profile(
    name: str,
    classification: McpClassification,
    *,
    layout: SandboxLayout,
    location: Location = "proxy",
    block_ports: tuple[int, ...] | None = None,
    executor_state: str | None = None,
) -> SandboxProfile:
    """自建：讀寫只有自己的 state；演化部署的程式資料夾唯讀＋執行；不收 ``sandbox_paths``.

    不給 ``config/skills``、代理人資料夾、專案與公開設定檔；cwd 是 home。程式資料夾是條目引用
    的那一個 ``managed/<skill>``（看路徑不看名稱）；引用不只一個時都不給（起不來，照實顯示）。
    """
    if classification.kind != "self":
        raise ProfileError(name)
    ports = checked_ports(block_ports, location)
    home, tmp = mcp_state_dirs(
        "self", name, layout=layout, location=location, executor_state=executor_state
    )
    rules = system_rules()
    rules += [PathRule(p, "rwx") for p in (home, tmp)]
    ungranted: tuple[str, ...] = ()
    skills = sorted(classification.managed_skills)
    if len(skills) == 1 and is_safe_segment(skills[0]):
        rules.append(PathRule(managed_root(layout) / skills[0], "rox"))
    elif skills:
        ungranted = (CODE_DIR_REFUSED,)
    return SandboxProfile(
        name=MCP_SELF_PROFILE,
        cwd=home,
        rules=tuple(dict.fromkeys(rules)),
        block_ports=ports,
        env_set=sandbox_env(home, tmp, [home.parent]),
        ungranted=ungranted,
        owner=state_segment("self", name),
    )


def _lexical(path: Path) -> Path:
    return Path(os.path.normpath(path))


def _same_path(value: str, target: Path) -> bool:
    try:
        path = Path(value)
        return path.is_absolute() and path.resolve() == target
    except (OSError, RuntimeError, ValueError):
        return False


def map_appdata(value: str, layout: SandboxLayout) -> str:
    """APPDATA 對映：哨兵或「等於 data 根的絕對路徑」→ personal_drive 根；OUTPUT 哨兵照解析."""
    if value == APPDATA_SENTINEL or _same_path(value, layout.data_root.resolve()):
        return str(personal_drive_root(layout))
    return resolve_reference_sentinel(value, layout)


def _home_or_root(folder: Path) -> bool:
    homes = {*ROOT_HOME_DIRS, *(home for home in (user_home(),) if home is not None)}
    return folder == Path(folder.anchor) or folder in homes or folder.parent in HOME_DIR_PARENTS


def _covered_by_system(folder: Path) -> bool:
    """系統路徑（含 ``sys.prefix``）本來就唯讀＋執行，不必再給。"""
    return any(
        r.access == "rox" and (folder == r.path or folder.is_relative_to(r.path))
        for r in system_rules()
    )


def _in_personal_drive(target: Path, layout: SandboxLayout) -> bool:
    """所有個人空間的上層本身或它底下（第三方的 APPDATA 對映到這裡）；形狀經 ``zone_of``（R3）."""
    roots = layout_zone_roots(layout)
    found = zone_of(target, roots, anchors=ALL_ANCHORS)
    if found is not None:
        return found[0] is Anchor.PERSONAL_DRIVE
    return anchor_at(target, roots, anchors=ALL_ANCHORS) is Anchor.PERSONAL_DRIVE


async def _reference_rule(
    path: Path, layout: SandboxLayout, skip: Path | None
) -> tuple[PathRule | None, str | None]:
    """引用的一條路徑：資料夾讀寫、檔案的所在資料夾唯讀＋執行；先過永不授權（owner=None）."""
    target = _lexical(path)
    is_file = is_file_reference(target)
    if not is_file and (target == docgen_output_root(layout) or _in_personal_drive(target, layout)):
        if target.resolve() != target or not target.is_dir():
            return None, PROTECTED_LOCATION
        return PathRule(target, "rwx"), None
    folder = target.parent if is_file else target
    if is_file and (folder == skip or _covered_by_system(folder)):
        return None, None
    if is_file and _home_or_root(folder):
        return None, HOME_OR_ROOT
    verdict = await authorize_derived_path(folder, layout, owner=None)
    if verdict is not None:
        return None, verdict
    return PathRule(folder, "rox" if is_file else "rwx"), None


async def _sandbox_rules(
    grants: Sequence[SandboxPathGrant], layout: SandboxLayout
) -> tuple[list[PathRule], list[str]]:
    """人設的額外允許資料夾：啟動時逐筆重驗（同存檔的規則），最多看前 16 筆。"""
    rules: list[PathRule] = []
    refused: list[str] = []
    for index, grant in enumerate(grants):
        if index >= MAX_MCP_SANDBOX_PATHS:
            refused.append(SANDBOX_PATHS_OVERFLOW)
            break
        access = SANDBOX_PATH_ACCESS.get(grant.access)
        raw = grant.path
        valid = isinstance(raw, str) and "\x00" not in raw and Path(raw).is_absolute()
        if access is None or not valid:
            refused.append(SANDBOX_PATH_INVALID)
            continue
        verdict = await authorize_derived_path(Path(raw), layout, owner=None)
        if verdict is None:
            rules.append(PathRule(_lexical(Path(raw)), access))
        else:
            refused.append(verdict)
    return rules, refused


async def build_mcp_third_party_profile(
    name: str,
    entry: McpEntry,
    classification: McpClassification,
    *,
    layout: SandboxLayout,
    sandbox_paths: Sequence[SandboxPathGrant] = (),
    block_ports: tuple[int, ...] | None = None,
) -> McpThirdPartyLaunch:
    """第三方與 scaffold（只在 proxy 位置）：state、引用的路徑、APPDATA 對映、額外允許的資料夾.

    引用的路徑（APPDATA 對映後）：personal_drive 根與它底下的資料夾、docgen 輸出夾給讀寫；其他
    資料夾讀寫、檔案的所在資料夾唯讀＋執行，都先過 ``authorize_derived_path(owner=None)``，所在
    資料夾是家目錄或 ``/`` 時不給。被拒的記進 ``ungranted``（只有代碼），伺服器照樣啟動。
    scaffold 另給自己的 ``config/mcp_servers/<name>`` 唯讀＋執行（條目的腳本確實在那裡才給）。
    """
    if classification.kind not in ("third_party", "scaffold"):
        raise ProfileError(name)
    ports = checked_ports(block_ports, "proxy")
    home, tmp = mcp_state_dirs("third", name, layout=layout, location="proxy")
    args = tuple(map_appdata(a, layout) for a in entry.args)
    env = {key: map_appdata(value, layout) for key, value in (entry.env or {}).items()}
    mapped = RawMcpEntry(entry.command, args, env, None)
    rules = system_rules()
    rules += [PathRule(p, "rwx") for p in (home, tmp)]
    refused: list[str] = []
    scaffold: Path | None = None
    if classification.kind == "scaffold":
        scaffold = scaffold_dir_for(name, mapped, layout, base_dir=home)
        if scaffold is None or scaffold != classification.scaffold_dir:
            scaffold = None
            refused.append(SCAFFOLD_DIR_MISMATCH)
        else:
            rules.append(PathRule(scaffold, "rox"))
    for ref in referenced_paths(mapped, layout, base_dir=home, infer_suffix=False):
        rule, reason = await _reference_rule(ref.path, layout, scaffold)
        rules += [rule] if rule else []
        refused += [reason] if reason else []
    extra, extra_refused = await _sandbox_rules(sandbox_paths, layout)
    is_scaffold = classification.kind == "scaffold"
    return McpThirdPartyLaunch(
        profile=SandboxProfile(
            name=MCP_SCAFFOLD_PROFILE if is_scaffold else MCP_THIRD_PARTY_PROFILE,
            cwd=home,
            rules=tuple(dict.fromkeys([*rules, *extra])),
            block_ports=ports,
            env_set=sandbox_env(home, tmp, [home.parent]),
            ungranted=tuple(dict.fromkeys([*refused, *extra_refused])),
            owner=state_segment("third", name),
        ),
        command=mapped.command,
        args=tuple(mapped.args),
        env=env,
    )

"""資源管理器 (ResourceManager) 服務.

負責 Touka 系統中所有虛擬 URI (如 touka://) 與實體路徑之間的轉譯,
並強制執行 Agent 身分主權隔離與 Docker 環境適配。
"""

from __future__ import annotations

import contextlib
import csv
import fnmatch
import json
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory_server.models.conversation_search import ConversationSearchError
from memory_server.utils.no_follow import (
    LINK_NOT_FOLLOWED,
    WalkReport,
    ZoneLinkRefusedError,
    ensure_no_links,
    is_link,
    kind_no_follow,
    make_dirs_no_follow,
    open_read_no_follow,
    read_bytes_no_follow,
    read_text_no_follow,
    remove_no_follow,
    remove_tree_no_follow,
    rename_entry_no_follow,
    replace_text_no_follow,
    walk_no_follow,
    walk_open_no_follow,
    write_bytes_no_follow,
    write_text_no_follow,
    zone_errors,
)
from memory_server.utils.zone_paths import (
    Anchor,
    ZonePath,
    ZonePathRequiredError,
    ZoneRoots,
    ZoneRootsUnavailableError,
    ZoneSegmentError,
    _register_handoff,
    _release_handoff,
    agent_root,
    current_roots,
    ensure_anchor,
    is_zone_segment,
    set_roots_provider,
)

# 使用絕對匯入避免 TID251 警告，並確保環境變數獲取一致
from src.config import HOST_WORKSPACE_ROOT, get_app_data_dir, settings
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.soul_service import soul_service
from src.utils.path_security import validate_no_symlink_escape
from src.utils.permission_repair import permission_repair, permission_repair_exempt

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import IO

    from memory_server.models.conversation_search import ConversationPage
    from memory_server.protocols import IConversationReader
    from memory_server.utils.no_follow import WalkEntry
    from memory_server.utils.zone_paths import HandoffKind

logger = logging.getLogger(__name__)

# --- Archive (skill 打包/解壓) 設定 ---
_ARCHIVE_EXCLUDE_DIRS = frozenset({"__pycache__", "node_modules"})
_ARCHIVE_EXCLUDE_GLOBS = frozenset({"*.pyc"})
_ARCHIVE_EXCLUDE_FILES = frozenset({".DS_Store"})
ARCHIVE_MAX_ENTRIES = 2_000
ARCHIVE_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB
_ARCHIVE_COPY_CHUNK = 1024 * 1024  # rewrite_archive 串流照抄時每次搬的位元組數

# list_directory／zone_list 一律隱藏的名稱（任何深度）
_LIST_HIDDEN = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"})
# 個人空間裡的輸出與上傳資料夾（/v1/outputs 服務的就是這裡）
_OUTPUTS_DIR = "outputs"
_UPLOADS_DIR = "uploads"


def _agent_zone(anchor: Anchor, folder: str) -> ZonePath | None:
    """代理人的根；資料夾名稱不能當一段時回 None（不丟例外、不通知觀察者）。"""
    return agent_root(anchor, folder) if is_zone_segment(folder) else None


def _norm_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(os.path.normcase(part) for part in parts)


def _render_tree(
    entries: list[WalkEntry], report: WalkReport, hidden: frozenset[str], max_depth: int
) -> list[str]:
    """把走訪結果排成 ``list_directory`` 的樹狀文字（同層依名稱排序；讀不到的照舊標示）."""
    children: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for entry in entries:
        if entry.parts[-1] not in hidden:
            children.setdefault(entry.parts[:-1], []).append((entry.parts[-1], entry.kind))
    seen = {entry.parts for entry in entries}
    unreadable: set[tuple[str, ...]] = set()
    for rel, _code in report.errors:
        parts = tuple(rel.split("/")) if rel else ()
        unreadable.add(parts)
        if parts and parts not in seen and parts[-1] not in hidden:
            children.setdefault(parts[:-1], []).append((parts[-1], "dir"))

    def build(parts: tuple[str, ...], depth: int, prefix: str) -> list[str]:
        if depth > max_depth:
            return [f"{prefix}... (max depth {max_depth} reached)"]
        if parts in unreadable:
            return [f"{prefix}└── [Permission Denied]"]
        items = sorted(children.get(parts, []), key=lambda item: os.path.normcase(item[0]))
        lines: list[str] = []
        for i, (name, kind) in enumerate(items):
            last = i == len(items) - 1
            connector = "└── " if last else "├── "
            if kind == "link":
                lines.append(f"{prefix}{connector}{name} {LINK_NOT_FOLLOWED}")
            elif kind == "dir":
                lines.append(f"{prefix}{connector}{name}/")
                inner = prefix + ("    " if last else "│   ")
                lines.extend(build((*parts, name), depth + 1, inner))
            else:
                lines.append(f"{prefix}{connector}{name}")
        if not items:
            lines.append(f"{prefix}└── (empty)")
        return lines

    return build((), 1, "")


def _unlock_and_retry(
    root: Path, unlocked: set[str], func: Callable[[str], object], failed: str, exc: BaseException
) -> None:
    """``remove_dir(unlock=True)`` 的 rmtree onexc：權限錯誤時補回擁有者權限再刪.

    只動要刪的樹裡、不是連結的項目（父資料夾要能寫、資料夾本身要能列）；同一項只補一次，
    補過還失敗（例如被別的行程開著）或其他錯誤照丟.
    """
    target = Path(failed)
    retried = failed in unlocked
    if not isinstance(exc, PermissionError) or not target.is_relative_to(root) or retried:
        raise exc
    unlocked.add(failed)
    link = target.is_symlink() or target.is_junction()
    if target.parent.is_relative_to(root) and not target.parent.is_symlink():
        target.parent.chmod(stat.S_IRWXU)
    if not link:
        target.chmod(stat.S_IRWXU)
    if func in (os.rmdir, os.unlink, os.remove) or link or not target.is_dir():
        func(failed)  # 刪除本身失敗：補權限後重試一次
    else:  # 資料夾讀不到（open、scandir、lstat）：補權限後整個重刪
        shutil.rmtree(target, onexc=partial(_unlock_and_retry, root, unlocked))


def bypasses_project_jail(identity: AgentIdentity) -> bool:
    """人類 admin 或 agent Debugger 可以越工作區 jail 存取專案根（ADR-0141 P6-c）.

    admin 代打（``is_human=False``）不算：代打＝管理閘權、非資源監獄旁路。
    """
    return (identity.is_human and identity.human_role == "admin") or (
        not identity.is_human and identity.agent_role == "Debugger"
    )


class ResourceManager:
    """資源路徑與主權邊界管理中心.

    負責將邏輯 URI 轉譯為實體路徑，並強制執行身分主權隔離 (SOLID - SRP)。
    實施「預設監獄」原則，確保 Agent 僅能存取其獲授權的物理路徑。
    """

    def __init__(self) -> None:
        """初始化資源管理器，偵測 Docker 環境並初始化工作區根目錄快取."""
        # 注意：不再快取「每專案根目錄」——專案 root_path 解析一律即時讀 DB
        # (見 _resolve_project_path)，杜絕改 root_path 後派發到舊目錄的陳舊問題。
        self._workspace_root_cached: Path | None = None
        self._ui_conversation_reader: IConversationReader | None = None
        self._container_mount = Path("/app/workspace")
        self._is_docker = Path("/.dockerenv").exists()

    def reset_for_test(self) -> None:
        """重置測試狀態，清空快取。"""
        self._workspace_root_cached = None
        logger.info("🛡️ ResourceManager test state reset.")

    def bind_ui_conversation_reader(self, reader: IConversationReader | None) -> None:
        """啟動時裝配；不在查詢時初始化或匯入 session store。"""
        self._ui_conversation_reader = reader

    @permission_repair_exempt(category="delegates", reason="交給已綁定的 query_ui_conversations")
    async def query_ui_conversations(
        self,
        identity: AgentIdentity,
        query: str,
        role: str | None,
        date_start: int | None,
        date_end: int | None,
        oldest_first: bool,
        limit: int,
        offset: int,
    ) -> ConversationPage:
        """只委派可信 reader；未裝配時不得當空資料。"""
        if self._ui_conversation_reader is None:
            raise ConversationSearchError("internal_error")
        return await self._ui_conversation_reader.query_ui_conversations(
            identity, query, role, date_start, date_end, oldest_first, limit, offset
        )

    async def _get_workspace_root(self) -> Path:
        """獲取目前的工作區根目錄實體路徑 (SSOT).

        Returns:
            Path: 實體工作區根目錄路徑。
        """
        if self._workspace_root_cached:
            return self._workspace_root_cached

        # 優先從資料庫獲取，否則降級至配置中心 (SSOT)
        from src.utils.async_db import get_system_setting  # noqa: LOCAL_IMPORT_BAN

        root_str = await get_system_setting("workspace_root", HOST_WORKSPACE_ROOT)
        self._workspace_root_cached = Path(root_str)
        return self._workspace_root_cached

    async def get_workspace_jail_root(self) -> Path:
        """設定的工作區根（主機路徑；Docker 裡用 ``translate_path`` 換成容器路徑）。"""
        return await self._get_workspace_root()

    def invalidate_cache(self) -> None:
        """清除工作區根目錄快取 (workspace_root 變更時呼叫)。"""
        self._workspace_root_cached = None

    @permission_repair_exempt(category="path_only", reason="只解析路徑")
    async def is_within_workspace(self, path_str: str) -> bool:
        """[A-fix] 判定路徑是否落在設定的工作區根目錄 (jail) 之內.

        解析所有路徑成分 (含 symlink) 後驗證是否仍位於 workspace_root 之下，
        阻擋 `..` 穿越與 symlink/junction 逃逸 (SSOT: path_security)。空字串視為不合法。

        Args:
            path_str: 待驗證的實體路徑字串 (專案 root_path).

        Returns:
            bool: 位於工作區內回傳 True，否則 False。
        """
        if not path_str:
            return False
        workspace = await self._get_workspace_root()
        return validate_no_symlink_escape(Path(path_str), workspace)

    def get_avatars_dir(self) -> Path:
        """取得使用者頭像目錄（ADR-0106，位於 app data 根，納入備份）。

        路徑定址 SSOT：avatar_service 經此取得目錄，不直接呼叫 get_app_data_dir
        （PATH_SSOT / TID251）。
        """
        return Path(get_app_data_dir()) / "avatars"

    @permission_repair_exempt(category="path_only", reason="只解析路徑")
    def get_vault_path(self, vault: str, identity: AgentIdentity) -> str | None:
        """取得指定代理人的 Vault 實體絕對路徑 (SSOT).

        主權隔離原則 (Sovereignty):
        - 實施「預設監獄」原則，路徑強制鎖定在 agent_id 子目錄。
        - 支援 'soul', 'custom_soul', 'legacy'。個人空間是代理人寫得到的資料夾，只以
          ``agent_drive`` 的 ``ZonePath`` 表示（ADR-0293）：這裡收到 'personal' 丟
          ``ZonePathRequiredError``。

        Args:
            vault: Vault 類型。
            identity: 經驗證的代理人身份對象。

        Returns:
            str | None: 實體絕對路徑字串，若權限不足或類型未知則回傳 None。
        """
        agent_id = identity.agent_id
        app_data_dir = Path(get_app_data_dir())

        if vault == "soul":
            # 靈魂核心資料：僅限 ToukaSoul 且實體目錄存在時存取 (Identity SSOT)
            soul_dir = app_data_dir / "soul" / agent_id
            if soul_service.is_toukasoul(identity):
                return str(soul_dir)
            return None

        if vault == "personal":
            raise ZonePathRequiredError(None, "get_vault_path")

        if vault == "custom_soul":
            # 自定義模型的「靈魂」配置空間 (Level 2 SSOT)；禁令 17：ID 不得穿越出 custom/
            # （例如 ../soul/<靈魂>，ADR-0245 修補）
            custom_root = app_data_dir / "custom"
            path = custom_root / agent_id
            if path.resolve() == custom_root.resolve() or not validate_no_symlink_escape(
                path, custom_root
            ):
                return None
            return str(path)

        if vault == "legacy":
            # 歷史歸檔庫
            legacy_dir = app_data_dir / "legacy" / agent_id
            return str(legacy_dir)

        logger.warning("Unknown vault type '%s' requested by %s", vault, agent_id)
        return None

    @permission_repair(retry=True)
    def get_vault_root(self, vault: str, identity: AgentIdentity) -> str | None:
        """取得指定 Vault 的物理根目錄 (Admin Only).

        Args:
            vault: Vault 類型（'soul'、'legacy'、'stats'、'logs'；代理人寫得到的個人空間不在這裡）.
            identity: 經驗證的身分對象 (須具備 Admin 權限).

        Returns:
            str | None: 物理根目錄路徑，若權限不足則回傳 None.
        """
        # 僅限人類管理員或特權代理人存取根目錄（ADR-0141：人類讀 human_role、agent 讀 agent_role；
        # 保留 is_human 分支＝拒絕 admin 代打混淆產物 assumed_admin，北極星性質不變）
        is_admin = identity.is_human and identity.human_role == "admin"
        is_privileged_agent = not identity.is_human and identity.agent_role in [
            "Owner",
            "Debugger",
        ]

        if not (is_admin or is_privileged_agent):
            logger.warning(
                "🚨 Vault Root Access Denied for %s (Role: %s, IsHuman: %s)",
                identity.agent_id,
                identity.display_role,
                identity.is_human,
            )
            return None

        app_data_dir = Path(get_app_data_dir())

        vault_map = {
            "soul": app_data_dir / "soul",
            "legacy": app_data_dir / "legacy",
            "stats": app_data_dir / "stats",
            "logs": app_data_dir / "logs",
        }

        if vault in vault_map:
            path = vault_map[vault]
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        return None

    def get_stats_path(self, identity: AgentIdentity) -> str:
        """取得特定代理人的統計資料目錄 (Subdivided Stats).

        Args:
            identity: 經驗證的身分對象.

        Returns:
            str: 實體統計目錄路徑。
        """
        agent_id = identity.agent_id
        path = Path(get_app_data_dir()) / "stats" / agent_id
        return str(path)

    def is_owner(self, target_identity: AgentIdentity, identity: AgentIdentity) -> bool:
        """判定指定身分是否具備目標 Agent 的主權 (Identity SSOT).

        實施層次化權限判定 (Layered Permissions):
        1. 管理員與除錯者角色具備絕對存取權 (Absolute Bypass)。
        2. 自主行為判定 (Autonomous Act)：非真人行為受 30 天自主寬限期限制。
        3. 人類駕駛員判定 (Human Pilot)：真人使用者根據擁有權清單存取。
        """
        target_id = target_identity.agent_id
        # 1. 管理員與除錯者直接放行 (Privileged Bypass)
        # ADR-0141 P6-b：人類 admin 讀 human_role（is_human 守衛拒 assumed_admin）；
        # Debugger 旁路補 `not is_human`（對齊姊妹函式 get_vault_root/list_all_agent_stats
        # 的特權 agent 判定）——不一致組合「is_human=True＋agent_role=Debugger」不再旁路。
        if (identity.is_human and identity.human_role == "admin") or (
            not identity.is_human and identity.agent_role == "Debugger"
        ):
            return True

        # 2. 自主行為判定 (Autonomous Act)：凡是非真人的自主行為，皆受 30 天寬限期限制
        if not identity.is_human and (
            identity.agent_role in ["System", "Owner"] or identity.agent_id == target_id
        ):
            from src.services.auth_service import AuthService

            auth_service = AuthService()
            users = auth_service.get_all_users()

            # #4 fix (multi-user sovereignty): a non-human "Owner"-role agent must not
            # reach across users. The grace check below keys on the *target's* owner
            # activity, so without this guard an Owner-agent owned by user A could act
            # on user B's agent whenever B was recently active. Deny ONLY when the
            # target has an explicit human owner that is NOT shared with the actor;
            # ownerless / public agents (e.g. custom models not yet assigned an owner)
            # keep the permissive fallthrough below so legitimate management does not
            # regress, and self-access (agent_id == target_id) skips this entirely.
            # ("System" — internal-only, absent from ASSIGNABLE_AGENT_ROLES — keeps its
            # documented global bypass for background scans and is intentionally not
            # constrained here; this guard scopes the externally-reachable Owner role.)
            if identity.agent_role == "Owner" and identity.agent_id != target_id:
                target_owners = [u for u in users if target_id in u.owned_agents]
                if target_owners and not any(
                    identity.agent_id in u.owned_agents for u in target_owners
                ):
                    logger.warning(
                        "🚫 Cross-owner autonomy denied: %s → %s",
                        identity.agent_id,
                        target_id,
                    )
                    return False

            # 找到該 Agent 的人類擁有者以確認最後活躍時間
            for user in users:
                if target_id in user.owned_agents:
                    if not user.last_active_at:
                        return False  # 從未登入過的代理人無權限自主

                    from src.utils.time_utils import get_current_epoch

                    # 30 天 = 2,592,000 秒
                    if int(get_current_epoch()) - user.last_active_at < 2592000:
                        return True

                    logger.warning("⏳ Autonomy expired for %s", target_id)
                    return False

            # 若無明確擁有者 (如公共代理人)，則允許 System 或自我存取
            return True

        # 3. 人類駕駛員判定 (Human Pilot)
        if identity.is_human:
            from src.services.auth_service import AuthService

            auth_service = AuthService()
            user = auth_service.get_user_by_id(identity.agent_id)

            if user:
                return target_id in user.owned_agents

        return False

    @permission_repair(retry=True)
    def list_all_agent_stats(self, identity: AgentIdentity) -> list[tuple[str, str]]:
        """列出所有代理人的統計資料目錄 (Admin Only).

        Args:
            identity: 經驗證的身分對象.

        Returns:
            list[tuple[str, str]]: (agent_id, physical_path) 列表.
        """
        is_admin = identity.is_human and identity.human_role == "admin"
        is_privileged_agent = not identity.is_human and identity.agent_role in [
            "Owner",
            "Debugger",
        ]

        if not (is_admin or is_privileged_agent):
            return []

        stats_root = Path(get_app_data_dir()) / "stats"
        if not stats_root.exists():
            return []

        results = []
        for item in stats_root.iterdir():
            if item.is_dir():
                results.append((item.name, str(item)))
        return results

    def get_logs_path(self, identity: AgentIdentity) -> str:
        """取得特定代理人的日誌目錄 (Subdivided Logs).

        Args:
            identity: 經驗證的身分對象.

        Returns:
            str: 實體日誌目錄路徑。
        """
        agent_id = identity.agent_id
        path = Path(get_app_data_dir()) / "logs" / agent_id
        return str(path)

    def get_temp_path(self, sub_path: str = "") -> str:
        """取得系統暫存目錄 (System Temp).

        Args:
            sub_path: 子路徑 (如 'ide')。

        Returns:
            str: 實體暫存目錄絕對路徑。
        """
        path = Path(get_app_data_dir()) / "temp" / sub_path
        return str(path)

    def get_base_path(self) -> str:
        """獲取 App Data 根目錄 (禁令下的合法管道)."""
        return get_app_data_dir()

    def get_container_mount(self) -> Path | None:
        """Docker 裡工作區的容器掛載點（translate_path 同一個）；不在 Docker 回 None。"""
        return self._container_mount if self._is_docker else None

    def get_workspace_mount(self) -> Path | None:
        """本行程看得到的工作區：Docker 是容器掛載點，否則是設定值；空值或相對路徑回 None。"""
        if self._is_docker:
            return self._container_mount
        value = str(self._workspace_root_cached or HOST_WORKSPACE_ROOT).strip()
        return Path(value) if value and Path(value).is_absolute() else None

    async def translate_path(self, path_str: str) -> str:
        """將主機實體路徑轉譯為容器內的可存取路徑 (Docker 適配).

        Args:
            path_str: 原始主機路徑字串。

        Returns:
            str: 轉譯後的實體路徑字串。
        """
        if not self._is_docker or not path_str:
            return path_str

        host_root = await self._get_workspace_root()
        path = Path(path_str)

        try:
            # 檢查路徑是否位於工作區根目錄內
            if path.is_relative_to(host_root):
                relative = path.relative_to(host_root)
                return str(self._container_mount / relative).replace("\\", "/")
        except ValueError:
            pass

        return path_str

    async def get_physical_path(
        self, virtual_uri: str, identity: AgentIdentity, project_id: str | None = None
    ) -> str | None:
        """將 touka:// 虛擬 URI 轉譯為實體目錄路徑 (Identity SSOT).

        Args:
            virtual_uri: 虛擬資源標識符 (如 touka://project/, touka://vault/personal/)。
            identity: 經驗證的身分對象.
            project_id: 可選的專案 ID.

        Returns:
            str | None: 轉譯後的實體絕對路徑。
        """
        if not virtual_uri.startswith("touka://"):
            logger.error("Invalid virtual URI: %s", virtual_uri)
            return None

        parts = virtual_uri.replace("touka://", "").strip("/").split("/")
        if not parts:
            return None

        res_type = parts[0]

        if res_type == "project":
            return await self._resolve_project_path(identity, project_id)

        if res_type == "vault" and len(parts) > 1:
            return self.get_vault_path(parts[1], identity)

        logger.warning("Unsupported resource type in URI: %s", virtual_uri)
        return None

    async def get_project_path(
        self, project_id: str, identity: AgentIdentity | None = None
    ) -> str | None:
        """獲獲取特定專案的物理根目錄 (SSOT)。

        Args:
            project_id: 專案 ID.
            identity: 經驗證的身分對象 (可選，供未來擴展權限檢查).

        Returns:
            str | None: 實體絕對路徑。
        """
        identity = identity or AgentIdentity(agent_id="System")
        return await self._resolve_project_path(identity, project_id)

    async def _resolve_project_path(
        self, identity: AgentIdentity, project_id: str | None
    ) -> str | None:
        """解析專案路徑 (內部方法)."""
        if not project_id:
            logger.warning(
                "Project access requested by %s but no project_id provided.",
                identity.agent_id,
            )
            return None

        from src.utils.async_db import get_project_root_path  # noqa: LOCAL_IMPORT_BAN

        try:
            root_path = await get_project_root_path(project_id)
        except Exception:
            logger.exception("Failed to resolve project path for %s", project_id)
            return None

        if not root_path:
            return None

        # A-fix: 非 admin/Debugger 呼叫者，專案根目錄必須落在工作區 jail 之內，防
        # project_tools 經任意 project_id 讀寫工作區外目錄 (與 ui_sessions.
        # _project_root_allowed 同政策)。每次呼叫即驗新鮮 root，不被快取短路——
        # 否則 admin 先快取越界路徑、root 改回界內後，非 admin 仍可取到陳舊越界路徑。
        # ADR-0141 P6-c：拆顯式雙面判定（對齊 get_vault_root/is_owner 的 is_human 分支），
        # 見 bypasses_project_jail。
        if not bypasses_project_jail(identity) and not await self.is_within_workspace(root_path):
            logger.warning(
                "Project %s root_path outside workspace; denied for %s.",
                project_id,
                identity.agent_id,
            )
            return None

        return await self.translate_path(root_path)

    # --- [I/O 代理門戶 (SSOT I/O Proxy)] ---

    @permission_repair_exempt(category="path_only", reason="政策判斷，只解析路徑")
    def is_protected_path(self, path: Path | str) -> bool:
        """檢查該路徑是否受系統核心保護 (如 legacy, soul).

        Args:
            path: 要檢查的物理路徑.

        Returns:
            bool: 若為受保護路徑則回傳 True.
        """
        p = Path(path).resolve()
        base_dir = Path(get_app_data_dir()).resolve()

        legacy_prefix = base_dir / "legacy"
        soul_prefix = base_dir / "soul"

        # 只要路徑位於 legacy 或 soul 之下即受保護。
        # DA27: is_relative_to（非 str.startswith）—— 前綴字串比對會把 sibling 目錄
        # （如 soul_archive / legacy_backup）誤判為受保護；is_relative_to 只認真正的子路徑。
        return p.is_relative_to(legacy_prefix) or p.is_relative_to(soul_prefix)

    @permission_repair(retry=True)
    def read_file_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        """安全地讀取檔案內容."""
        return Path(path).read_text(encoding=encoding)

    @permission_repair(retry=True)
    def read_file_binary(self, path: Path | str) -> bytes:
        """安全地讀取二進位檔案內容."""
        return Path(path).read_bytes()

    # --- 不跟隨連結（代理人的檔案動詞用；路徑要先解析，ADR-0292 決定 12）---
    # 失敗時例外帶絕對路徑（``no_follow`` 換好），EACCES 交給權限自癒

    @permission_repair(retry=True)
    def ensure_no_links(self, path: Path | str) -> None:
        """路上任何一段是 symlink 或 junction 就丟 ``LinkRefusedError``."""
        ensure_no_links(path)

    @permission_repair(retry=True)
    def read_file_text_no_follow(self, path: Path | str, encoding: str = "utf-8") -> str:
        """讀文字檔；路上任何一段是 symlink 或 junction 就丟 ``LinkRefusedError``."""
        return read_text_no_follow(path, encoding=encoding)

    @permission_repair(retry=True)
    def read_file_binary_no_follow(self, path: Path | str) -> bytes:
        """讀二進位檔；路上任何一段是 symlink 或 junction 就丟 ``LinkRefusedError``."""
        return read_bytes_no_follow(path)

    @permission_repair(retry=False)
    def write_file_text_no_follow(
        self, path: Path | str, content: str, encoding: str = "utf-8"
    ) -> None:
        """同 ``write_file_text``（受保護路徑拒絕、原子寫入、0600），路上有連結就拒絕."""
        if self.is_protected_path(path):
            msg = "Soul Protection Error: Automated modification of protected files is prohibited."
            raise PermissionError(msg)
        replace_text_no_follow(path, content, encoding=encoding)

    @permission_repair(retry=False)
    def append_file_text_no_follow(
        self, path: Path | str, content: str, encoding: str = "utf-8"
    ) -> None:
        """同 ``append_file_text``，路上有連結就拒絕."""
        if self.is_protected_path(path):
            msg = "Soul Protection Error: Cannot append to a protected path."
            raise PermissionError(msg)
        write_text_no_follow(path, content, encoding=encoding, append=True)

    @permission_repair(retry=False)
    def remove_file_no_follow(self, path: Path | str) -> None:
        """同 ``remove_file``（不存在時丟 FileNotFoundError），上層路上有連結就拒絕."""
        if self.is_protected_path(path):
            msg = "Soul Protection Error: Cannot delete a protected path."
            raise PermissionError(msg)
        remove_no_follow(path)

    # --- 代理人資料夾（區）：只收 ZonePath（ADR-0293 Z3）---
    # 不是 ZonePath 就丟 ZonePathRequiredError；路上有連結丟 ZoneLinkRefusedError。例外的 filename
    # 是 display()，絕對路徑在 zone_abs（權限自癒讀它）。寫入方法在錨點不存在時先建（R9）。

    @permission_repair(retry=True)  # 只有建錨點會寫（mkdir，冪等）
    def _zone_target(self, zp: ZonePath, method: str, *, create: bool = False) -> Path:
        """錨點解析後的路徑接上段落；收到的不是 ``ZonePath`` 就丟 ``ZonePathRequiredError``."""
        if not isinstance(zp, ZonePath):
            raise ZonePathRequiredError(None, method)
        roots = current_roots()
        base = ensure_anchor(roots, zp.anchor) if create else roots.resolved(zp.anchor)
        if base is None:
            raise ZoneRootsUnavailableError
        return Path(base).joinpath(*zp.parts)

    @permission_repair(retry=True)
    def zone_kind(self, zp: ZonePath) -> str:
        """最後一段是什麼：``missing``／``file``／``dir``／``link``／``other``（不跟隨）."""
        target = self._zone_target(zp, "zone_kind")
        with zone_errors(zp.display()):
            return kind_no_follow(target)

    @permission_repair(retry=True)
    def zone_read_text(self, zp: ZonePath, encoding: str = "utf-8", errors: str = "strict") -> str:
        """讀文字檔；路上或最後一段是連結就拒絕."""
        target = self._zone_target(zp, "zone_read_text")
        with zone_errors(zp.display()):
            return read_text_no_follow(target, encoding=encoding, errors=errors)

    @permission_repair(retry=True)
    def zone_read_binary(self, zp: ZonePath, size: int = -1) -> bytes:
        """讀二進位檔（``size`` 給了只讀開頭這麼多）；路上或最後一段是連結就拒絕."""
        target = self._zone_target(zp, "zone_read_binary")
        with zone_errors(zp.display()):
            return read_bytes_no_follow(target, size)

    @permission_repair(retry=True)
    def zone_open_read(self, zp: ZonePath) -> tuple[IO[bytes], os.stat_result]:
        """開一般檔案給串流讀，回（檔案, fstat）；呼叫端負責關閉。連結就拒絕."""
        target = self._zone_target(zp, "zone_open_read")
        with zone_errors(zp.display()):
            return open_read_no_follow(target)

    @permission_repair_exempt(category="delegates", reason="交給 zone_read_text")
    def zone_read_json(self, zp: ZonePath, encoding: str = "utf-8") -> Any | None:
        """讀 JSON：不存在或解析失敗回 None（壞檔記警告）；連結照樣拒絕、不塌縮成 None."""
        try:
            content = self.zone_read_text(zp, encoding=encoding)
        except FileNotFoundError:
            return None
        try:
            return json.loads(content)
        except ValueError:
            logger.warning("⚠️ Corrupt JSON detected at %s; treating as empty.", zp.display())
            return None

    @permission_repair(retry=False)
    def zone_write_text(self, zp: ZonePath, content: str, encoding: str = "utf-8") -> None:
        """原子寫入（同資料夾暫存檔、fsync 後改名，0600），需要時建上層資料夾；連結就拒絕."""
        target = self._zone_target(zp, "zone_write_text", create=True)
        with zone_errors(zp.display()):
            if kind_no_follow(target) == "link":  # 改名會蓋掉連結本身；照表拒絕
                raise ZoneLinkRefusedError(zp.display(), str(target))
            replace_text_no_follow(target, content, encoding=encoding)

    @permission_repair_exempt(category="delegates", reason="交給 zone_write_text（不重試）")
    def zone_write_json(self, zp: ZonePath, data: Any, indent: int = 4) -> None:
        """把資料寫成 JSON（同 ``write_json``）."""
        self.zone_write_text(zp, json.dumps(data, indent=indent, ensure_ascii=False))

    # retry=True 的前提同 write_file_binary：權限錯誤只在建資料夾或開檔時、截斷之前
    @permission_repair(retry=True)
    def zone_write_binary(self, zp: ZonePath, content: bytes) -> None:
        """就地寫入二進位（需要時建上層資料夾）；連結就拒絕，不截斷任何別的檔案."""
        target = self._zone_target(zp, "zone_write_binary", create=True)
        with zone_errors(zp.display()):
            write_bytes_no_follow(target, content)

    @permission_repair(retry=False)
    def zone_append_text(self, zp: ZonePath, content: str, encoding: str = "utf-8") -> None:
        """追加文字（需要時建上層資料夾）；連結就拒絕."""
        target = self._zone_target(zp, "zone_append_text", create=True)
        with zone_errors(zp.display()):
            write_text_no_follow(target, content, encoding=encoding, append=True)

    @permission_repair(retry=True)
    def zone_make_dir(self, zp: ZonePath) -> None:
        """建資料夾（含上層；已存在沒關係）；任何一段是連結就拒絕."""
        target = self._zone_target(zp, "zone_make_dir", create=True)
        with zone_errors(zp.display()):
            make_dirs_no_follow(target)

    @permission_repair(retry=False)
    def zone_remove_file(self, zp: ZonePath, missing_ok: bool = True) -> None:
        """刪檔；最後一段是連結時刪連結本身，上層路上有連結就拒絕."""
        target = self._zone_target(zp, "zone_remove_file")
        with zone_errors(zp.display()):
            try:
                if kind_no_follow(target) == "link":
                    remove_tree_no_follow(target)  # Windows 指向資料夾的連結要用 rmdir 刪
                else:
                    remove_no_follow(target)
            except FileNotFoundError:
                if not missing_ok:
                    raise

    @permission_repair(retry=False)
    def zone_remove_dir(self, zp: ZonePath, missing_ok: bool = True) -> None:
        """刪掉整棵樹（Z4）：最後一段是連結刪連結本身、不進入；樹裡的連結只刪本身."""
        target = self._zone_target(zp, "zone_remove_dir")
        with zone_errors(zp.display()):
            try:
                remove_tree_no_follow(target)
            except FileNotFoundError:
                if not missing_ok:
                    raise

    @permission_repair_exempt(category="generator", reason="走訪時才讀，讀不到的資料夾記進統計")
    def zone_walk(
        self,
        zp: ZonePath,
        report: WalkReport,
        *,
        skip_dir: Callable[[tuple[str, ...]], bool] | None = None,
        show_links: bool = False,
    ) -> Iterator[WalkEntry]:
        """走訪一棵樹（不進入連結、計數；名稱不合格的略過並計數）；根是連結就拒絕、不存在就空.

        ``show_links`` 時連結也列出（種類 ``link``，不進入）。
        """
        target = self._zone_target(zp, "zone_walk")
        with zone_errors(zp.display()):
            yield from walk_no_follow(target, report, skip_dir=skip_dir, show_links=show_links)

    @permission_repair_exempt(category="generator", reason="走訪時才開檔，開不了的記進統計")
    def zone_walk_open(
        self, zp: ZonePath, report: WalkReport
    ) -> Iterator[tuple[tuple[str, ...], IO[bytes], os.stat_result]]:
        """同 ``zone_walk`` 但只給開好的一般檔案（相對段落, 檔案, fstat）；拿下一個前關掉上一個."""
        target = self._zone_target(zp, "zone_walk_open")
        with zone_errors(zp.display()):
            yield from walk_open_no_follow(target, report)

    @permission_repair(retry=True)
    def zone_stat_files(
        self,
        zp: ZonePath,
        exclude_names: frozenset[str] = frozenset(),
        exclude_paths: frozenset[str] = frozenset(),
        exclude_globs: tuple[str, ...] = (),
        max_files: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """同 ``stat_files``：一般檔案的（相對 POSIX 路徑, 位元組數, mtime_ns）；根是連結就拒絕."""
        target = self._zone_target(zp, "zone_stat_files")
        rows: list[tuple[str, int, int]] = []

        def skip(parts: tuple[str, ...]) -> bool:
            return parts[-1] in exclude_names or "/".join(parts) in exclude_paths

        with zone_errors(zp.display()):
            kind = kind_no_follow(target)
            if kind == "link":  # 找檔與搜尋不能把「根被換成連結」當成沒有檔案
                raise ZoneLinkRefusedError(zp.display(), str(target))
            if kind != "dir":
                return rows
            walk = walk_no_follow(target, WalkReport(), skip_dir=skip)
            with contextlib.closing(walk):
                for entry in walk:
                    if entry.kind != "file" or any(
                        fnmatch.fnmatch(entry.parts[-1], pattern) for pattern in exclude_globs
                    ):
                        continue
                    rows.append(("/".join(entry.parts), entry.info.st_size, entry.info.st_mtime_ns))
                    if max_files is not None and len(rows) >= max_files:
                        break
        return rows

    @permission_repair(retry=True)
    def zone_list(
        self, zp: ZonePath, max_depth: int = 2, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """同 ``list_directory`` 的樹狀格式（連結只列名稱）；根是連結就拒絕."""
        target = self._zone_target(zp, "zone_list")
        hidden = _LIST_HIDDEN | exclude
        report = WalkReport()
        with zone_errors(zp.display()):
            kind = kind_no_follow(target)
            if kind == "link":
                raise ZoneLinkRefusedError(zp.display(), str(target))
            if kind != "dir":
                return [f"Error: Directory not found: {target}"]
            entries = (
                list(
                    walk_no_follow(
                        target,
                        report,
                        skip_dir=lambda parts: parts[-1] in hidden,
                        show_links=True,
                        list_depth=max_depth,
                    )
                )
                if max_depth >= 1
                else []
            )
        return [f"📁 {target}:", *_render_tree(entries, report, hidden, max_depth)]

    def zone_rel_from_abs(self, base: ZonePath, text: str) -> ZonePath | None:
        """檔案動詞的絕對路徑：照字面（先消掉 ``..``、不解析連結）在 ``base`` 底下才回子路徑.

        ``base`` 的字面與解析後位置都認；逐段比較（Windows 不分大小寫），不是字串前綴。
        """
        if not isinstance(base, ZonePath):
            raise ZonePathRequiredError(None, "zone_rel_from_abs")
        roots = current_roots()
        parts = Path(os.path.normpath(text)).parts
        for anchor_dir in (roots.literal(base.anchor), roots.resolved(base.anchor)):
            if anchor_dir is None:
                continue
            prefix = Path(anchor_dir).joinpath(*base.parts).parts
            if _norm_parts(parts[: len(prefix)]) == _norm_parts(prefix):
                return base.join_rel("/".join(parts[len(prefix) :]))
        return None

    @permission_repair_exempt(category="generator", reason="交出字串的期間由呼叫端決定")
    @contextlib.contextmanager
    def zone_handoff(self, zp: ZonePath, kind: HandoffKind) -> Iterator[str]:
        """把區內路徑轉成字串交給接收端（R7；只在 G1 明列處）；任何一段是連結就拒絕.

        登記期間測試絆線只放行該種類的事件，離開時取消登記。
        """
        target = self._zone_target(zp, "zone_handoff")
        with zone_errors(zp.display()):
            if kind_no_follow(target) == "link":
                raise ZoneLinkRefusedError(zp.display(), str(target))
        token = _register_handoff(str(target), kind)
        try:
            yield str(target)
        finally:
            _release_handoff(token)

    @permission_repair(retry=False)
    def rename_agent_container(self, anchor: Anchor, old_name: str, new_name: str) -> None:
        """把錨點底下代理人的整個資料夾改名（R10，只改名、不複製；G1 明列）.

        ``old_name`` 是連結就拒絕；``new_name`` 已存在就丟 ``FileExistsError``（只差大小寫的同一個
        資料夾照常改名）。自訂模型改的是 ``custom/<id>`` 整個（含設定）。
        """
        base = current_roots().resolved(anchor)
        if base is None:
            raise ZoneRootsUnavailableError
        with zone_errors(f"zone://{anchor.value}/{old_name}"):
            rename_entry_no_follow(base, old_name, new_name)

    @permission_repair(retry=False)
    def remove_custom_model_dir(self, identity: AgentIdentity) -> bool:
        """刪掉整個 ``custom/<id>``（設定、人格與個人空間；R5，G1 明列）；沒有時回 False.

        ``<id>`` 本身是連結就拒絕；整棵用不跟隨的刪樹。
        """
        base = current_roots().resolved(Anchor.CUSTOM_DRIVE)
        if base is None:
            raise ZoneRootsUnavailableError
        if not is_zone_segment(identity.agent_id):
            raise ZoneSegmentError
        target = Path(base) / identity.agent_id
        display = f"zone://{Anchor.CUSTOM_DRIVE.value}/{identity.agent_id}"
        with zone_errors(display):
            kind = kind_no_follow(target)
            if kind == "missing":
                return False
            if kind == "link":
                raise ZoneLinkRefusedError(display, str(target))
            remove_tree_no_follow(target)
        return True

    # --- 區內路徑的產生函式（Z5；不建資料夾、不解析）---

    def agent_drive(self, identity: AgentIdentity) -> ZonePath | None:
        """代理人的個人空間：靈魂是 ``PERSONAL_DRIVE/<id>``，其他是 ``CUSTOM_DRIVE/<id>/drive``.

        和以前 ``get_vault_path("personal")`` 的判斷相同；``<id>`` 不能當一段時回 None。
        """
        if soul_service.is_toukasoul(identity):
            return _agent_zone(Anchor.PERSONAL_DRIVE, identity.agent_id)
        return _agent_zone(Anchor.CUSTOM_DRIVE, identity.agent_id)

    def agent_drive_for(self, agent_id: str) -> ZonePath | None:  # noqa: IDENTITY_SSOT
        """依 agent_id 找出真實註冊身分後的個人空間（跨身分「決定論」定址，不臆測角色）.

        督導讀 DispatchAgent 看板等跨身分存取：以呼叫端臆測的角色定址，靈魂的看板會被解析到
        ``custom/`` 那一側。權限由呼叫端集中校驗，這裡只定址。
        """
        # 延遲匯入破循環：identity_service → permission_service → resource_manager。
        from src.services.identity_service import identity_service  # noqa: LOCAL_IMPORT_BAN

        return self.agent_drive(identity_service.get_identity(agent_id))

    def soul_drive(self, identity: AgentIdentity) -> ZonePath | None:
        """靈魂的個人空間 ``PERSONAL_DRIVE/<id>``（建立、刪除靈魂用，不看角色；R10）."""
        return _agent_zone(Anchor.PERSONAL_DRIVE, identity.agent_id)

    def custom_drive(self, identity: AgentIdentity) -> ZonePath | None:
        """自訂模型的個人空間 ``CUSTOM_DRIVE/<id>/drive``（刪除、改名自訂模型用；R10）."""
        return _agent_zone(Anchor.CUSTOM_DRIVE, identity.agent_id)

    def outputs_dir(self, identity: AgentIdentity) -> ZonePath | None:
        """代理人的輸出資料夾（個人空間的 ``outputs``；要建時呼叫端自己 ``zone_make_dir``）."""
        drive = self.agent_drive(identity)
        return None if drive is None else drive.child(_OUTPUTS_DIR)

    def uploads_dir(self, identity: AgentIdentity) -> ZonePath | None:
        """聊天上傳的資料夾 ``outputs/uploads``（代理人的終端與檔案動詞都能用相對路徑讀）."""
        outputs = self.outputs_dir(identity)
        return None if outputs is None else outputs.child(_UPLOADS_DIR)

    def anchor_root(self, anchor: Anchor) -> ZonePath:
        """整個錨點（R9；呼叫點列在 G1）。``CUSTOM_DRIVE`` 沒有整個錨點的區內路徑."""
        return ZonePath(anchor, ())

    @permission_repair(retry=False)
    def write_file_text(
        self,
        path: Path | str,
        content: str,
        encoding: str = "utf-8",
        make_parents: bool = True,
    ) -> None:
        """安全地寫入檔案內容，強制保護核心目錄（原子寫入：temp + os.replace）。"""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Automated modification of protected files is prohibited."
            raise PermissionError(msg)
        if make_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        # 原子寫入：先寫同目錄暫存檔，fsync 後 os.replace 原子置換，避免寫到一半當機
        # 導致目標檔損毀（autonomy_log / token_usage / mission / sessions-JSON 等）。
        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(p)
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    @permission_repair(retry=False)
    def copy_file(self, src: Path | str, dst: Path | str) -> None:
        """安全地複製檔案."""
        d = Path(dst)
        if self.is_protected_path(d):
            msg = "Soul Protection Error: Cannot copy to a protected path."
            raise PermissionError(msg)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(d))

    @permission_repair_exempt(
        category="swap_semantics", reason="swap：被占用要原樣報 in_use、原檔不動"
    )
    def move_file(self, src: Path | str, dst: Path | str) -> None:
        """安全地移動/重新命名檔案."""
        d = Path(dst)
        if self.is_protected_path(d):
            msg = "Soul Protection Error: Cannot move to a protected path."
            raise PermissionError(msg)
        d.parent.mkdir(parents=True, exist_ok=True)
        Path(src).replace(d)

    @permission_repair(retry=True)
    def exists(self, path: Path | str) -> bool:
        """檢查路徑是否存在."""
        return Path(path).exists()

    @permission_repair(retry=True)
    def make_dir(self, path: Path | str, parents: bool = True, exist_ok: bool = True) -> None:
        """建立目錄."""
        Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    @permission_repair_exempt(
        category="swap_semantics", reason="刪暫存與快照：權限錯誤要原樣往上丟"
    )
    def remove_file(self, path: Path | str, missing_ok: bool = True) -> None:
        """安全地刪除檔案."""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot delete a protected path."
            raise PermissionError(msg)
        p.unlink(missing_ok=missing_ok)

    @permission_repair(retry=False)
    def remove_dir(
        self, path: Path | str, missing_ok: bool = True, *, unlock: bool = False
    ) -> None:
        """安全地遞迴刪除目錄，強制保護核心目錄.

        Args:
            path: 要刪除的目錄路徑.
            missing_ok: 目錄不存在時是否靜默略過.
            unlock: 樹裡有唯讀或被拿掉權限的項目時補回擁有者權限再刪（不跟連結）.
        """
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot delete a protected directory."
            raise PermissionError(msg)
        if not p.exists():
            if missing_ok:
                return
            msg = f"Directory not found: {p}"
            raise FileNotFoundError(msg)
        shutil.rmtree(p, onexc=partial(_unlock_and_retry, p, set()) if unlock else None)

    # --- Archive (skill 打包/解壓) ---

    @permission_repair(retry=True)
    def iter_files(self, path: Path | str) -> list[Path]:
        """遞迴列出 path 下所有檔案的絕對 Path (無數量上限，打包用)."""
        return [p for p in Path(path).rglob("*") if p.is_file()]

    @permission_repair(retry=True)
    def stat_files(
        self,
        path: Path | str,
        exclude_names: frozenset[str] = frozenset(),
        exclude_paths: frozenset[str] = frozenset(),
        exclude_globs: tuple[str, ...] = (),
        max_files: int | None = None,
    ) -> list[tuple[str, int, int]]:
        """遞迴列出一般檔案的（相對 POSIX 路徑, 位元組數, mtime_ns），同層依名稱排序.

        不跟隨也不列出 symlink 與 junction（只收 lstat 為一般檔案者）。
        ``exclude_names`` 依目錄名在任何深度剪除；``exclude_paths`` 依相對路徑剪除；
        ``exclude_globs`` 依檔名略過；``max_files`` 為筆數上限。路徑不存在時回空清單。
        """
        root = Path(path)
        rows: list[tuple[str, int, int]] = []
        if not root.is_dir():
            return rows
        for current, dirnames, filenames in root.walk(follow_symlinks=False):
            rel_dir = current.relative_to(root)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in exclude_names
                and (rel_dir / d).as_posix() not in exclude_paths
                and not (current / d).is_junction()
            )
            for name in sorted(filenames):
                if any(fnmatch.fnmatch(name, pattern) for pattern in exclude_globs):
                    continue
                try:
                    info = (current / name).lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                rows.append(((rel_dir / name).as_posix(), info.st_size, info.st_mtime_ns))
                if max_files is not None and len(rows) >= max_files:
                    return rows
        return rows

    def _archive_should_exclude(self, rel: Path) -> bool:
        """判斷打包時是否排除此相對路徑 (建置產物/系統雜訊)."""
        if any(part in _ARCHIVE_EXCLUDE_DIRS for part in rel.parts):
            return True
        if rel.name in _ARCHIVE_EXCLUDE_FILES:
            return True
        return any(fnmatch.fnmatch(rel.name, pat) for pat in _ARCHIVE_EXCLUDE_GLOBS)

    @permission_repair(retry=False)
    def create_archive(
        self,
        src_dir: Path | str,
        output_path: Path | str,
        arc_base: Path | str | None = None,
    ) -> Path:
        """打包 src_dir 為 ZIP_DEFLATED archive，回傳 output_path.

        arcname = file.relative_to(arc_base or src_dir.parent)，預設保留
        ``<dir_name>/`` 前綴。套用排除規則 (__pycache__/node_modules/*.pyc/.DS_Store).
        """
        src = Path(src_dir)
        base = Path(arc_base) if arc_base else src.parent
        out = Path(output_path)
        if self.is_protected_path(out):
            msg = "Soul Protection Error: Cannot write archive to a protected path."
            raise PermissionError(msg)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in src.rglob("*"):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(base)
                if self._archive_should_exclude(rel):
                    continue
                zf.write(fp, rel.as_posix())
        return out

    @permission_repair(retry=False)
    def extract_archive(
        self,
        archive_path: Path | str,
        dest_dir: Path | str,
        max_entries: int = ARCHIVE_MAX_ENTRIES,
        max_total_size: int = ARCHIVE_MAX_TOTAL_BYTES,
    ) -> None:
        """安全解壓 zip 至 dest_dir.

        防護：entry 數上限 + header 宣告總量否決 + 實際累計二次確認 (防 header
        造假) + 逐 entry resolve 後須在 dest_dir 內 (path traversal).
        """
        dest_root = Path(dest_dir).resolve()
        dest_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(Path(archive_path), "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)
            if sum(i.file_size for i in infos) > max_total_size:
                raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)
            written = 0
            for info in infos:
                if info.filename.endswith("/"):
                    continue
                target = (dest_root / info.filename).resolve()
                if target != dest_root and dest_root not in target.parents:
                    raise ToukaException(ErrorCode.VALIDATION_ERROR)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(info)
                written += len(data)
                if written > max_total_size:
                    raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)
                target.write_bytes(data)

    @permission_repair_exempt(
        category="swap_semantics", reason="快照改寫，失敗前可能已寫了一部分輸出"
    )
    def rewrite_archive(
        self,
        source_path: Path | str,
        output_path: Path | str,
        *,
        drop: set[str],
        replace: dict[str, Path | bytes],
    ) -> None:
        """照原順序把 zip 重寫到 output：drop 的成員拿掉、replace 的換內容，其餘逐塊串流照抄.

        每個成員沿用原本的檔名、時間、壓縮方式與屬性（extra 不抄，避免過期的 zip64 欄位）；
        整份 zip 的 comment 照抄。驗證由呼叫端另外做。
        """
        out = Path(output_path)
        if self.is_protected_path(out):
            msg = "Soul Protection Error: Cannot write archive to a protected path."
            raise PermissionError(msg)
        with zipfile.ZipFile(Path(source_path), "r") as source, zipfile.ZipFile(out, "w") as target:
            target.comment = source.comment
            for info in source.infolist():
                if info.filename in drop:
                    continue
                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                clone.create_system = info.create_system
                clone.comment = info.comment
                content = replace.get(info.filename)
                if isinstance(content, bytes) or info.is_dir():
                    target.writestr(clone, content or b"")
                    continue
                clone.file_size = Path(content).stat().st_size if content else info.file_size
                with (
                    Path(content).open("rb") if content else source.open(info) as reader,
                    target.open(clone, "w") as writer,
                ):
                    shutil.copyfileobj(reader, writer, _ARCHIVE_COPY_CHUNK)

    @permission_repair_exempt(
        category="swap_semantics", reason="swap 換檔前複製屬性，本身就在改權限"
    )
    def copy_file_metadata(self, src: Path | str, dst: Path | str) -> None:
        """把 src 的修改時間與權限複製到 dst（換上清除後的檔之前用；dst 不得是受保護路徑）."""
        if self.is_protected_path(Path(dst)):
            msg = "Soul Protection Error: Cannot change a protected path."
            raise PermissionError(msg)
        shutil.copystat(str(src), str(dst))

    @permission_repair_exempt(
        category="swap_semantics", reason="swap 置換前把暫存檔落盤，錯誤原樣往上丟"
    )
    def fsync_file(self, path: Path | str) -> None:
        """把檔案內容確實寫到磁碟（原子置換前用；以讀寫開檔，受保護路徑不碰）."""
        if self.is_protected_path(Path(path)):
            msg = "Soul Protection Error: Cannot open a protected path for writing."
            raise PermissionError(msg)
        fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @permission_repair_exempt(
        category="swap_semantics", reason="swap 改名後把資料夾落盤，錯誤原樣往上丟"
    )
    def fsync_dir(self, path: Path | str) -> None:
        """改名後把資料夾項目寫到磁碟；Windows 不支援對資料夾 fsync，略過."""
        if os.name == "nt":
            return
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @permission_repair_exempt(
        category="swap_semantics", reason="判斷中斷暫存的格式，錯誤原樣往上丟"
    )
    def read_file_prefix(self, path: Path | str, size: int) -> bytes:
        """只讀檔案開頭的 size 個位元組（判斷格式用，不整個讀進記憶體）."""
        with Path(path).open("rb") as handle:
            return handle.read(size)

    @permission_repair_exempt(
        category="swap_semantics", reason="刪不掉時先清唯讀位元，本身就在改權限"
    )
    def make_writable(self, path: Path | str) -> None:
        """清掉唯讀位元（清除暫存檔前用；受保護路徑不動）."""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot change a protected path."
            raise PermissionError(msg)
        p.chmod(stat.S_IMODE(p.stat().st_mode) | stat.S_IWRITE)

    @permission_repair(retry=True)
    def list_directory(
        self, path: Path | str, max_depth: int = 2, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """列出目錄結構樹 (供 IResourceProxy 使用).

        Args:
            path: 起始路徑。
            max_depth: 遞迴深度限制。
            exclude: 額外要隱藏的名稱（任何深度；預設不額外隱藏）。

        Returns:
            list[str]: 格式化後的樹狀結構字串清單；symlink 與 junction 只列名稱、不進入。
        """
        p = Path(path)
        if not p.is_dir():
            return [f"Error: Directory not found: {path}"]
        hidden = _LIST_HIDDEN | exclude

        def build_tree(current: Path, depth: int, prefix: str = "") -> list[str]:
            """遞迴遍歷目錄以建構樹狀結構字串。"""
            if depth > max_depth:
                return [f"{prefix}... (max depth {max_depth} reached)"]

            lines = []
            try:
                items = sorted([item for item in current.iterdir() if item.name not in hidden])

                for i, item in enumerate(items):
                    is_last = i == len(items) - 1
                    connector = "└── " if is_last else "├── "

                    if is_link(item):
                        lines.append(f"{prefix}{connector}{item.name} {LINK_NOT_FOLLOWED}")
                    elif item.is_dir():
                        lines.append(f"{prefix}{connector}{item.name}/")
                        new_prefix = prefix + ("    " if is_last else "│   ")
                        lines.extend(build_tree(item, depth + 1, new_prefix))
                    else:
                        lines.append(f"{prefix}{connector}{item.name}")
                if not items:
                    lines.append(f"{prefix}└── (empty)")
            except PermissionError:
                lines.append(f"{prefix}└── [Permission Denied]")
            return lines

        return [f"📁 {path or '/'}:"] + build_tree(p, 1)

    @permission_repair(retry=True)
    def glob_search(
        self, path: Path | str, pattern: str, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """按檔名模式搜尋檔案 (供 IResourceProxy 使用).

        Args:
            path: 搜尋根目錄。
            pattern: glob 匹配模式。
            exclude: 額外要略過的路徑段名稱（預設不額外略過）。

        Returns:
            list[str]: 找到的相對路徑清單 (字串)。
        """
        root = Path(path)
        matches = []
        skipped = {".git", "node_modules", "__pycache__"} | exclude
        try:
            for p in root.rglob(pattern):
                if any(part in p.parts for part in skipped):
                    continue
                if p.is_file():
                    matches.append(str(p.relative_to(root).as_posix()))
                    if len(matches) >= 50:
                        break
        except Exception:
            logger.exception("Error in glob_search for %s", path)

        return matches

    @permission_repair_exempt(category="delegates", reason="交給 exists、read_file_text")
    def read_json(self, path: Path | str, encoding: str = "utf-8") -> Any | None:
        """安全地讀取並解析 JSON 檔案.

        若檔案不存在或解析失敗，將回傳 None，由業務層決定後續處理.
        """
        if not self.exists(path):
            return None
        try:
            content = self.read_file_text(path, encoding=encoding)
            return json.loads(content)
        except json.JSONDecodeError:
            # 壞檔（內容損毀）：明確告警以利偵測（有別於單純讀取失敗）
            logger.warning("⚠️ Corrupt JSON detected at %s; treating as empty.", path)
            return None
        except Exception:
            logger.exception("Failed to read JSON from %s", path)
            return None

    @permission_repair_exempt(category="delegates", reason="交給 write_file_text（不重試）")
    def write_json(
        self, path: Path | str, data: Any, encoding: str = "utf-8", indent: int = 4
    ) -> None:
        """安全地將資料寫入 JSON 檔案."""
        content = json.dumps(data, indent=indent, ensure_ascii=False)
        self.write_file_text(path, content, encoding=encoding)

    # retry=True 的前提：權限錯誤只在 mkdir（冪等）或開檔時發生，VFS 先檢查權限再截斷，
    # 開檔失敗就不會截斷；改成多步驟寫入時要重新評估這個標記。
    @permission_repair(retry=True)
    def write_file_binary(self, path: Path | str, content: bytes) -> None:
        """安全地將二進位資料寫入檔案."""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot write to a protected path."
            raise PermissionError(msg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    @permission_repair(retry=False)
    def append_file_text(self, path: Path | str, content: str, encoding: str = "utf-8") -> None:
        """安全地附加文字到檔案末尾."""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot append to a protected path."
            raise PermissionError(msg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding=encoding) as f:
            f.write(content)

    @permission_repair(retry=False)
    def append_csv_row(
        self,
        path: Path | str,
        row: list[Any],
        headers: list[str] | None = None,
        encoding: str = "utf-8-sig",
    ) -> None:
        """安全地附加單行資料至 CSV 檔案."""
        p = Path(path)
        if self.is_protected_path(p):
            msg = "Soul Protection Error: Cannot append CSV to a protected path."
            raise PermissionError(msg)
        p.parent.mkdir(parents=True, exist_ok=True)

        file_exists = p.exists()
        with p.open("a", encoding=encoding, newline="") as f:
            writer = csv.writer(f)
            if headers and not file_exists:
                writer.writerow(headers)
            writer.writerow(row)


# Global instance
resource_manager = ResourceManager()


def _zone_roots() -> ZoneRoots:
    """目前的 data 根與 sandbox state 根：判斷代理人資料夾的唯一來源（ADR-0293 R14）。"""
    state = settings.TOUKA_SANDBOX_STATE_DIR or resource_manager.get_temp_path("sandbox_state")
    return ZoneRoots(get_app_data_dir(), state)


set_roots_provider(_zone_roots)

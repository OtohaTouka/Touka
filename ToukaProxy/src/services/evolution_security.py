"""自主演化安全守衛 (Evolution Security).

負責演化過程中的沙盒隔離、路徑校驗與環境淨化。
本服務實施「預設監獄」原則，確保 Agent 產生的工具代碼無法逃逸出指定的沙盒目錄。
技能資料夾 ``mcp_servers/managed/<skill>`` 的檔案一律經 ResourceManager 的區內方法
（ADR-0293 PR 2）。
"""

import logging
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from memory_server.utils.no_follow import ZoneLinkRefusedError
from memory_server.utils.zone_paths import HandoffKind, ZoneSegmentError

from src.config import settings
from src.services.execution_broker import run_sync
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    # 只用在型別：isolation 模組（經 status）會匯入本模組，執行時匯入會循環
    from memory_server.utils.zone_paths import ZonePath

    from src.sandbox_kernel.spec import RlimitName
    from src.services.isolation.evolution_command import EvolutionLaunch

# ADR-0076: POSIX-only resource module (absent on Windows). Imported at module top so
# the sandbox can apply OS-level rlimits where available and degrade cleanly otherwise.
try:
    import resource
except ImportError:  # pragma: no cover - platform dependent (Windows)
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class EvolutionRefused(Exception):
    """演化的驗證拒絕，與隔離環境不可用分開。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@contextmanager
def _skill_changed() -> Iterator[None]:
    """技能資料夾路上有連結、或段落不合格：轉成 ``skill_changed``（ADR-0293 P10）；其他錯誤照丟."""
    try:
        yield
    except (ZoneLinkRefusedError, ZoneSegmentError) as exc:
        raise EvolutionRefused("skill_changed") from exc


class EvolutionPathGuard:
    """自主演化沙盒管理員，負責維護執行隔離邊界."""

    @staticmethod
    def is_windows() -> bool:
        """檢查是否為 Windows 系統."""
        return os.name == "nt"

    @classmethod
    def tool_zone(cls, skill_id: str) -> "ZonePath":
        """技能資料夾 ``managed/<skill>`` 的區內路徑；名稱不合格（P9）丟 ``invalid_skill_id``."""
        tool = resource_manager.tool_root(skill_id)
        if tool is None:
            raise EvolutionRefused("invalid_skill_id")
        return tool

    @classmethod
    def get_tool_root(cls, skill_id: str) -> str:
        """技能資料夾的區字串（ADR-0293 P4）：交給 profile、子行程與部署條目，不解析最後一段."""
        return resource_manager.zone_text(cls.tool_zone(skill_id))

    @classmethod
    def get_clean_env(cls) -> dict[str, str]:
        """提供「去敏感化」的環境變數字典.

        複製目前進程的環境變數，但移除所有包含密鑰、權杖與敏感配置的變數，防止子進程竊取主權。

        H4 fix: extended blacklist now also covers Python interpreter control vars
        (PYTHONHOME, PYTHONSTARTUP, PYTHONINSPECT) and dynamic library injection
        vectors (LD_PRELOAD, LD_LIBRARY_PATH, DYLD_*) that could be exploited
        for sandbox escape even without credential access.

        Returns:
            dict[str, str]: 淨化後的環境變數對映。
        """
        clean_env = cls.strip_sensitive_env(settings.get_full_env())
        # Force-override critical vars to safe sandbox values (belt-and-suspenders)
        clean_env["PYTHONPATH"] = "."
        return clean_env

    @staticmethod
    def strip_sensitive_env(env: dict[str, str]) -> dict[str, str]:
        """移除密鑰名稱與 runtime-control 變數（黑名單模式＋顯式移除清單雙保險，禁令 19）.

        get_clean_env 與呼叫端額外傳入的 env 共用此過濾，呼叫端不能把被剝除的變數加回來。
        """
        # H4 fix: expanded blacklist — credential names + runtime-control prefixes
        _BLACKLIST_PATTERNS = [
            # Original credential patterns
            "SECRET", "KEY", "TOKEN", "AUTH", "CREDENTIAL", "PASSWORD",
            # Python interpreter control (H4 fix)
            "PYTHON",     # covers PYTHONHOME, PYTHONSTARTUP, PYTHONINSPECT, etc.
            # Dynamic library injection (H4 fix)
            "LD_",        # covers LD_PRELOAD, LD_LIBRARY_PATH, etc. (Linux/glibc)
            "DYLD_",      # covers DYLD_INSERT_LIBRARIES, DYLD_LIBRARY_PATH (macOS)
            # Other runtime controls
            "NODE_",      # Node.js
            "GEM_",       # Ruby gems
            "JAVA_",      # JVM options
            "PERL",       # Perl interpreter
        ]

        clean_env = {
            k: v for k, v in env.items()
            if not any(pattern in k.upper() for pattern in _BLACKLIST_PATTERNS)
        }

        # Explicitly remove any that slipped through (e.g. exact-name matches)
        for dangerous in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
                          "LD_PRELOAD", "LD_LIBRARY_PATH",
                          "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH"):
            clean_env.pop(dangerous, None)

        return clean_env

    # NOTE (#10): the former `verify_ownership` per-skill gate was dead code — it was
    # never called and the `evolution_self_mod` capability it checked is granted
    # nowhere, so it provided a false impression of a second authorization gate.
    # Evolution is gated by the tool-dispatch RBAC/ability layer (the actual control).
    # Removed to avoid security theatre; introducing genuine fine-grained per-skill
    # ownership enforcement should be a tracked ADR, not a silently-revived method.

    # 五個檔案方法：``filename`` 只來自程式常數；路上有連結或段落不合格轉成 ``skill_changed``，
    # 其他 OSError 照丟（ADR-0293 Q3、P10）

    @classmethod
    def mkdir_tool(cls, skill_id: str) -> None:
        """建立工具沙盒目錄。"""
        tool = cls.tool_zone(skill_id)
        with _skill_changed():
            resource_manager.zone_make_dir(tool)

    @classmethod
    def write_tool_file(cls, skill_id: str, filename: str, content: str) -> None:
        """在沙盒內原子寫入檔案（需要時建資料夾）。"""
        tool = cls.tool_zone(skill_id)
        with _skill_changed():
            resource_manager.zone_write_text(tool.child(filename), content)

    @classmethod
    def read_tool_file(cls, skill_id: str, filename: str) -> str | None:
        """從沙盒內讀取檔案；不存在回 None。"""
        tool = cls.tool_zone(skill_id)
        with _skill_changed():
            try:
                return resource_manager.zone_read_text(tool.child(filename))
            except FileNotFoundError:
                return None

    @classmethod
    def delete_tool_file(cls, skill_id: str, filename: str) -> bool:
        """刪除沙盒內的檔案（是連結時只刪連結本身）；回是否刪了。"""
        tool = cls.tool_zone(skill_id)
        with _skill_changed():
            target = tool.child(filename)
            if resource_manager.zone_kind(target) == "missing":
                return False
            resource_manager.zone_remove_file(target)
            return True

    @classmethod
    def tool_file_exists(cls, skill_id: str, filename: str) -> bool:
        """檢查沙盒檔案是否存在（連結本身也算存在，和以前一樣不重建）。"""
        tool = cls.tool_zone(skill_id)
        with _skill_changed():
            return resource_manager.zone_kind(tool.child(filename)) != "missing"

    @staticmethod
    def _build_rlimit_preexec(
        memory_mb: int | None, storage_mb: int | None, cpu_sec: int | None
    ) -> Callable[[], None] | None:
        """Build a preexec_fn that applies OS-level rlimits to the sandbox child (ADR-0076).

        POSIX only — returns None on Windows (no `resource` module), where the wall-clock
        timeout + in-process JailedWriter remain the fallback. RLIMIT_FSIZE OS-enforces the
        storage quota (replacing the bypassable JailedWriter), RLIMIT_CPU caps CPU seconds,
        RLIMIT_AS caps virtual address space (≈ memory). RLIMIT_NPROC is intentionally
        omitted — on Linux it counts the real uid's processes (shared with Touka).
        """
        if resource is None:
            logger.info(
                "🪟 OS-level sandbox rlimits unavailable here (no `resource` module); "
                "relying on subprocess timeout + in-process JailedWriter."
            )
            return None

        mb = 1024 * 1024

        def _apply() -> None:  # runs in the child after fork, before exec
            if storage_mb:
                limit = storage_mb * mb
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
            if cpu_sec:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec + 1))
            if memory_mb:
                limit = memory_mb * mb
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        return _apply

    @staticmethod
    def rlimit_values(
        memory_mb: int | None, storage_mb: int | None, cpu_sec: int | None
    ) -> "dict[RlimitName, int]":
        """隔離啟動器套用的 rlimit（soft＝hard）；數值與 ``_build_rlimit_preexec`` 相同."""
        mb = 1024 * 1024
        values: dict[RlimitName, int | None] = {
            "fsize": storage_mb * mb if storage_mb else None,
            "cpu": cpu_sec or None,
            "as": memory_mb * mb if memory_mb else None,
        }
        return {name: int(value) for name, value in values.items() if value}

    @classmethod
    def run_sandbox_command(
        cls,
        skill_id: str,
        cmd_list: list[str],
        memory_mb: int | None = None,
        storage_mb: int | None = None,
        *,
        launch: "EvolutionLaunch",
        **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """在受控環境下執行指令（ADR-0076：POSIX 套用 OS 級 FSIZE/CPU/AS rlimits）.

        argv／env／pass_fds 由 spawn 前選好的隔離 runner（``launch``）改寫；直接執行時 rlimit 用
        ``preexec_fn``，隔離時由啟動器在 exec 前套用。只在 ``asyncio.to_thread`` 的 worker 呼叫
        （PDEATHSIG 綁在建立子行程的執行緒，worker 會等子行程結束）。cwd 一定是技能資料夾，spawn
        前後以「子行程的工作資料夾」交接（ADR-0293 P6）；最後一段或路上是連結就不執行。
        """
        tool = cls.tool_zone(skill_id)

        clean_env = cls.get_clean_env()
        # 呼叫端 env 也過同一道黑名單：只能補功能性變數，不能把密鑰／loader 變數加回來
        env = {**clean_env, **cls.strip_sensitive_env(kwargs.pop("env", None) or {})}

        # ADR-0076: OS-enforced resource limits on POSIX (no-op on Windows).
        cpu_sec = kwargs.get("timeout")
        preexec = cls._build_rlimit_preexec(memory_mb, storage_mb, cpu_sec)
        handoff = resource_manager.zone_handoff(tool, HandoffKind.SUBPROCESS_CWD)
        with _skill_changed(), handoff as cwd:
            plan = launch.plan(
                cmd_list,
                env,
                preexec_fn=kwargs.pop("preexec_fn", preexec),
                rlimits=cls.rlimit_values(memory_mb, storage_mb, cpu_sec),
                timeout=cpu_sec,
            )
            try:
                return run_sync(
                    list(plan.argv),
                    cwd=cwd,
                    env=plan.env,
                    preexec_fn=plan.preexec_fn,
                    pass_fds=plan.pass_fds,
                    stdin=subprocess.DEVNULL,  # 不繼承 proxy 的 stdin（TTY 時可 TIOCSTI）
                    **kwargs,
                )
            finally:
                launch.finish(plan)  # 關掉 runner 開的 fd，收下狀態 fd（回事件迴圈後 report）

"""自主演化安全守衛 (Evolution Security).

負責演化過程中的沙盒隔離、路徑校驗與環境淨化。
本服務實施「預設監獄」原則，確保 Agent 產生的工具代碼無法逃逸出指定的沙盒目錄。
"""

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import get_app_data_dir, settings
from src.services.execution_broker import run_sync

if TYPE_CHECKING:
    # 只用在型別：isolation 模組（經 status）會匯入本模組，執行時匯入會循環
    from src.sandbox_kernel.spec import RlimitName
    from src.services.isolation.evolution_command import EvolutionLaunch

# ADR-0076: POSIX-only resource module (absent on Windows). Imported at module top so
# the sandbox can apply OS-level rlimits where available and degrade cleanly otherwise.
try:
    import resource
except ImportError:  # pragma: no cover - platform dependent (Windows)
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class EvolutionPathGuard:
    """自主演化沙盒管理員，負責維護執行隔離邊界.

    Attributes:
        MANAGED_BASE_DIR (Path): 存放自主演化 MCP 伺服器的物理根目錄。
    """
    
    # SSOT: 鎖定受管理的 MCP 伺服器基礎目錄
    MANAGED_BASE_DIR = Path(get_app_data_dir()) / "mcp_servers" / "managed"

    @staticmethod
    def is_windows() -> bool:
        """檢查是否為 Windows 系統."""
        return os.name == "nt"

    @classmethod
    def get_tool_root(cls, skill_id: str) -> Path:
        """獲取該技能唯一的合法物理根目錄 (SSOT).
        
        Args:
            skill_id (str): 技能/工具的唯一識別碼。
            
        Returns:
            Path: 解析後的絕對路徑對象。
        """
        return (cls.MANAGED_BASE_DIR / skill_id).resolve()

    @classmethod
    def validate_path(cls, skill_id: str, target_path: str) -> bool:
        """驗證目標路徑是否安全地位於工具沙盒內.

        防止路徑穿透攻擊 (../../) 與絕對路徑逃逸，確保 Agent 僅能修改自己的沙盒內容。

        Args:
            skill_id (str): 技能識別碼。
            target_path (str): 欲存取的目標路徑。

        Returns:
            bool: 若路徑安全則回傳 True。
        """
        root = Path(cls.get_tool_root(skill_id)).resolve()
        try:
            target = Path(target_path).resolve()
        except Exception:
            # 若路徑無法解析（如無效字元），視為不安全
            return False
        
        # 核心安全性判定：target 必須位於 root 目錄下
        # 使用 is_relative_to (Python 3.9+) 進行結構化校驗
        try:
            is_safe = target.is_relative_to(root)
        except ValueError:
            is_safe = False
        
        if not is_safe:
            logger.warning(
                f"🛡️ [Security] Sandbox escape blocked! Skill: {skill_id}, Path: {target}"
            )
            
        return is_safe

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

    @classmethod
    def mkdir_tool(cls, skill_id: str) -> None:
        """建立工具沙盒目錄。"""
        root = cls.get_tool_root(skill_id)
        root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def write_tool_file(cls, skill_id: str, filename: str, content: str) -> None:
        """在沙盒內寫入檔案，強制執行路徑校驗。"""
        root = cls.get_tool_root(skill_id)
        target = root / filename
        if not cls.validate_path(skill_id, str(target)):
            msg = f"🛡️ Sandbox Violation: Access denied to {filename}"
            raise PermissionError(msg)
        
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    @classmethod
    def read_tool_file(cls, skill_id: str, filename: str) -> str | None:
        """從沙盒內讀取檔案。"""
        root = cls.get_tool_root(skill_id)
        target = root / filename
        if not cls.validate_path(skill_id, str(target)):
            msg = f"🛡️ Sandbox Violation: Access denied to {filename}"
            raise PermissionError(msg)
        
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    @classmethod
    def delete_tool_file(cls, skill_id: str, filename: str) -> bool:
        """刪除沙盒內的檔案。"""
        root = cls.get_tool_root(skill_id)
        target = root / filename
        if not cls.validate_path(skill_id, str(target)):
            msg = f"🛡️ Sandbox Violation: Access denied to {filename}"
            raise PermissionError(msg)
        
        if target.exists():
            target.unlink()
            return True
        return False

    @classmethod
    def tool_file_exists(cls, skill_id: str, filename: str) -> bool:
        """檢查沙盒檔案是否存在。"""
        root = cls.get_tool_root(skill_id)
        target = root / filename
        return target.exists()

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
        cwd: str | None = None,
        memory_mb: int | None = None,
        storage_mb: int | None = None,
        *,
        launch: "EvolutionLaunch",
        **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """在受控環境下執行指令（ADR-0076：POSIX 套用 OS 級 FSIZE/CPU/AS rlimits）.

        argv／env／pass_fds 由 spawn 前選好的隔離 runner（``launch``）改寫；直接執行時 rlimit 用
        ``preexec_fn``，隔離時由啟動器在 exec 前套用。只在 ``asyncio.to_thread`` 的 worker 呼叫
        （PDEATHSIG 綁在建立子行程的執行緒，worker 會等子行程結束）。
        """
        root = cls.get_tool_root(skill_id)
        if not cls.validate_path(skill_id, cwd or str(root)):
            msg = "🛡️ Sandbox Violation: Command CWD outside sandbox."
            raise PermissionError(msg)

        clean_env = cls.get_clean_env()
        # 呼叫端 env 也過同一道黑名單：只能補功能性變數，不能把密鑰／loader 變數加回來
        env = {**clean_env, **cls.strip_sensitive_env(kwargs.pop("env", None) or {})}

        # ADR-0076: OS-enforced resource limits on POSIX (no-op on Windows).
        cpu_sec = kwargs.get("timeout")
        preexec = cls._build_rlimit_preexec(memory_mb, storage_mb, cpu_sec)
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
                cwd=cwd or str(root),
                env=plan.env,
                preexec_fn=plan.preexec_fn,
                pass_fds=plan.pass_fds,
                stdin=subprocess.DEVNULL,  # 不繼承 proxy 的 stdin（TTY 時可 TIOCSTI）
                **kwargs,
            )
        finally:
            launch.finish(plan)  # 關掉 runner 開的 fd，收下狀態 fd（回事件迴圈後 report）

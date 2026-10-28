"""Hook 服務（ADR-0292）：載入設定、比對、執行、合併結果、執行紀錄與存檔.

- 設定檔 ``config/hooks.json``：每次使用前看內容有沒有變（sha256）；壞掉、讀不到或載入過之後
  被刪掉時沿用上一份好的設定、健康狀態標 ``error``（啟動時壞掉由 bootstrap 直接擋下）。從來沒有
  檔案＝關閉。
- 同一事件符合的 hook 同時執行；任一個擋下就擋下，補充說明全部保留。hook 自己失敗不擋；解讀或
  記錄一個 hook 的結果時出錯，只算那個 hook 失敗。
- command hook 每次執行前重查路徑界線；路徑界線查不了（例如專案清單讀不到）就不執行。
- 執行紀錄只在記憶體、留最新 ``RUN_LOG_SIZE`` 筆；無痕對話只記時間、事件與結果。
- 存檔：比對版本（revision）避免蓋掉別人的修改；存檔前查所有 command hook 的路徑（含停用的）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.hooks import MATCHER_EVENTS, CommandHook, HooksConfig, HttpHook
from src.services.hooks.matcher import matcher_matches
from src.services.hooks.outcome import HookVerdict, interpret
from src.services.hooks.paths import HOOKS_DIR_NAME, HookZones, command_path_problems, current_zones
from src.services.hooks.runners import RunResult, run_command, run_http
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from src.models.hooks import HookHandler
    from src.services.hooks.paths import PathProblem

logger = logging.getLogger(__name__)

RUN_LOG_SIZE: Final[int] = 50
MAX_DETAIL_CHARS: Final[int] = 500
HOOKS_FILE_NAME: Final[str] = "hooks.json"

HealthStatus = Literal["ok", "missing", "error"]
RunOutcome = Literal["blocked", "ask", "context", "none", "failed"]


class CommandRunner(Protocol):
    """執行 command hook（``runners.run_command`` 的合約）。"""

    def __call__(self, hook: CommandHook, payload: bytes, *, cwd: Path) -> Awaitable[RunResult]:
        """執行並回傳結果。"""
        ...


class HttpRunner(Protocol):
    """執行 http hook（``runners.run_http`` 的合約）。"""

    def __call__(self, hook: HttpHook, payload: bytes) -> Awaitable[RunResult]:
        """執行並回傳結果。"""
        ...


ZonesProvider = Callable[[], Awaitable[HookZones]]


@dataclass(frozen=True)
class HookHealth:
    """設定檔狀態：``revision`` 是目前檔案內容的 sha256（沒有檔案時 None）。"""

    status: HealthStatus
    revision: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class HookRunRecord:
    """一筆執行紀錄（設定頁「最近執行」）。"""

    at: float
    event: str
    hook: str
    hook_type: str
    status: str
    outcome: RunOutcome
    duration_ms: int
    exit_code: int | None = None
    http_status: int | None = None
    detail: str | None = None
    incognito: bool = False


@dataclass
class HookDecision:
    """同一事件所有 hook 合併後的結果。"""

    # 有沒有 hook 真的執行（沒有符合的 hook 時是 False）
    ran: bool = False
    blocked: bool = False
    ask: bool = False
    reasons: list[str] = field(default_factory=list)
    ask_reasons: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """給人看的擋下原因（多個 hook 時合併）。"""
        return "\n".join(self.reasons)

    @property
    def context(self) -> str:
        """給模型的補充說明（多個 hook 時合併）。"""
        return "\n\n".join(self.contexts)


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _label(hook: HookHandler) -> str:
    if hook.name:
        return hook.name
    if isinstance(hook, CommandHook):
        return Path(hook.command).name
    try:
        return urlsplit(hook.url).hostname or hook.type
    except ValueError:
        return hook.type


def _identity(hook: HookHandler) -> tuple[Any, ...]:
    if isinstance(hook, CommandHook):
        return ("command", hook.command, tuple(hook.args))
    return ("http", hook.url, tuple(sorted(hook.headers.items())))


def _outcome(verdict: HookVerdict) -> RunOutcome:
    if verdict.failed:
        return "failed"
    if verdict.blocked:
        return "blocked"
    if verdict.ask:
        return "ask"
    return "context" if verdict.context else "none"


def _detail(result: RunResult, verdict: HookVerdict) -> str | None:
    parts = [
        verdict.reason if verdict.blocked or verdict.ask else None,
        result.detail,
        result.stderr.strip().splitlines()[0] if verdict.failed and result.stderr.strip() else None,
        *verdict.notes,
        "output truncated" if result.truncated else None,
    ]
    text = "; ".join(part for part in parts if part)
    return text[:MAX_DETAIL_CHARS] or None


def _problems_text(problems: Sequence[PathProblem]) -> str:
    return ", ".join(f"{p.where}: {p.reason}" for p in problems)


class HookService:
    """hook 的單一入口；路由與 ReAct 接線只經這裡。"""

    def __init__(
        self,
        *,
        config_path: Callable[[], Path] | None = None,
        zones_provider: ZonesProvider = current_zones,
        command_runner: CommandRunner = run_command,
        http_runner: HttpRunner = run_http,
    ) -> None:
        """依賴都可替換（測試用）；預設讀 ``get_config_dir()/hooks.json``。"""
        self._config_path = config_path or (lambda: get_config_dir() / HOOKS_FILE_NAME)
        self._zones_provider = zones_provider
        self._command_runner = command_runner
        self._http_runner = http_runner
        self._save_lock = asyncio.Lock()
        self.reset_for_test()

    def reset_for_test(self) -> None:
        """清掉快取的設定與執行紀錄。"""
        self._config = HooksConfig()
        self._loaded = False  # 載入過一份好的設定之後，檔案不見也不會變成沒有 hook
        self._health = HookHealth(status="missing")
        self._runs: deque[HookRunRecord] = deque(maxlen=RUN_LOG_SIZE)

    # --- 設定 ---------------------------------------------------------------------------

    def _read_raw(self) -> bytes | None:
        path = self._config_path()
        if not resource_manager.exists(path):
            return None
        return resource_manager.read_file_binary(path)

    def _keep_last_good(self, reason: str) -> None:
        """讀不到設定檔：沿用上一份設定；revision 清掉，檔案回來時重新載入。"""
        if self._health.status != "error" or self._health.detail != reason:
            logger.warning("hooks.json could not be read; keeping the last hook config (%s)",
                           reason)
        self._health = HookHealth(status="error", detail=reason)

    def _refresh(self) -> None:
        try:
            raw = self._read_raw()
        except OSError as exc:
            self._keep_last_good(type(exc).__name__)
            return
        if raw is None:
            if self._loaded:
                self._keep_last_good("FileNotFoundError")
                return
            self._config = HooksConfig()
            self._health = HookHealth(status="missing")
            return
        revision = _revision(raw)
        if revision == self._health.revision:
            return
        try:
            config = HooksConfig.model_validate(json.loads(raw.decode("utf-8")))
        except (ValueError, RecursionError, ValidationError) as exc:
            logger.warning("hooks.json is invalid; keeping the last good hook config (%s)",
                           type(exc).__name__)
            self._health = HookHealth(status="error", revision=revision,
                                      detail=type(exc).__name__)
            return
        self._config = config
        self._loaded = True
        self._health = HookHealth(status="ok", revision=revision)

    def current(self) -> HooksConfig:
        """目前生效的設定（壞檔時是上一份好的）。"""
        self._refresh()
        return self._config

    def health(self) -> HookHealth:
        """設定檔狀態。"""
        self._refresh()
        return self._health

    def has_hooks(self, event: str) -> bool:
        """這個事件有沒有設定 hook；接線點先問這個，沒有就不組事件內容。"""
        return bool(self.current().groups_for(event))

    def runs(self) -> list[HookRunRecord]:
        """最近的執行紀錄，新的在前。"""
        return list(reversed(self._runs))

    # --- 執行 ---------------------------------------------------------------------------

    def _select(self, event: str, match: Iterable[str]) -> list[HookHandler]:
        names = list(match)
        selected: list[HookHandler] = []
        seen: set[tuple[Any, ...]] = set()
        for group in self.current().groups_for(event):
            if event in MATCHER_EVENTS and not matcher_matches(group.matcher, names):
                continue
            for hook in group.hooks:
                key = _identity(hook)
                if hook.enabled and key not in seen:
                    seen.add(key)
                    selected.append(hook)
        return selected

    def _hooks_dir(self) -> Path:
        hooks_dir = self._config_path().parent / HOOKS_DIR_NAME
        resource_manager.make_dir(hooks_dir)
        return hooks_dir

    async def _zones(self) -> HookZones | None:
        try:
            self._hooks_dir()
            return await self._zones_provider()
        except Exception:
            logger.exception("hook path check is unavailable; command hooks will not run")
            return None

    async def _run_one(
        self, hook: HookHandler, payload: bytes, zones: HookZones | None
    ) -> RunResult:
        try:
            if isinstance(hook, HttpHook):
                return await self._http_runner(hook, payload)
            if zones is None:
                return RunResult(status="rejected", duration_ms=0, detail="path check unavailable")
            if problems := command_path_problems(hook, zones):
                return RunResult(status="rejected", duration_ms=0, detail=_problems_text(problems))
            return await self._command_runner(hook, payload, cwd=zones.hooks_dir)
        except Exception as exc:
            logger.exception("hook runner failed")
            return RunResult(status="error", duration_ms=0, detail=type(exc).__name__)

    def _record(
        self, event: str, hook: HookHandler, result: RunResult, verdict: HookVerdict,
        *, incognito: bool,
    ) -> None:
        if incognito:
            record = HookRunRecord(
                at=get_current_epoch(), event=event, hook="", hook_type="", status=result.status,
                outcome=_outcome(verdict), duration_ms=result.duration_ms, incognito=True,
            )
        else:
            record = HookRunRecord(
                at=get_current_epoch(), event=event, hook=_label(hook), hook_type=hook.type,
                status=result.status, outcome=_outcome(verdict), duration_ms=result.duration_ms,
                exit_code=result.exit_code, http_status=result.http_status,
                detail=_detail(result, verdict),
            )
        self._runs.append(record)

    @staticmethod
    def _interpret(event: str, result: RunResult) -> HookVerdict:
        try:
            return interpret(event, result)
        except Exception:
            logger.exception("hook result could not be read")
            return HookVerdict(failed=True, notes=("output could not be read",))

    async def dispatch(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        match: Iterable[str] = (),
        incognito: bool = False,
    ) -> HookDecision:
        """執行這個事件符合的 hook，回傳合併結果；沒有 hook 時立即回傳空結果。"""
        decision = HookDecision()
        hooks = self._select(event, match)
        if not hooks:
            return decision
        decision.ran = True
        # 落單的 surrogate（JSON 的 ``\ud800`` 解出來的）編不成 UTF-8：換成 ``?``，hook 照樣執行
        body = json.dumps(
            {"hook_event_name": event, **payload}, ensure_ascii=False, default=str
        ).encode("utf-8", errors="replace")
        zones = await self._zones() if any(isinstance(h, CommandHook) for h in hooks) else None
        results = await asyncio.gather(*(self._run_one(h, body, zones) for h in hooks))
        for hook, result in zip(hooks, results, strict=True):
            verdict = self._interpret(event, result)
            try:
                self._record(event, hook, result, verdict, incognito=incognito)
            except Exception:
                logger.exception("hook run could not be recorded")
            if verdict.blocked:
                decision.blocked = True
                decision.reasons.append(verdict.reason or "blocked by hook")
            elif verdict.ask:
                decision.ask = True
                if verdict.reason:
                    decision.ask_reasons.append(verdict.reason)
            if verdict.context:
                decision.contexts.append(verdict.context)
        decision.ask = decision.ask and not decision.blocked
        return decision

    # --- 存檔 ---------------------------------------------------------------------------

    async def _check_paths(self, config: HooksConfig) -> None:
        commands = [
            hook
            for groups in config.hooks.values()
            for group in groups
            for hook in group.hooks
            if isinstance(hook, CommandHook)
        ]
        if not commands:
            return
        zones = await self._zones()
        if zones is None:
            raise ToukaException(ErrorCode.HOOK_PATH_REJECTED, problems=[])
        problems = [
            {"event": event, "where": problem.where, "reason": problem.reason,
             "value": problem.value}
            for event, groups in config.hooks.items()
            for group in groups
            for hook in group.hooks
            if isinstance(hook, CommandHook)
            for problem in command_path_problems(hook, zones)
        ]
        if problems:
            raise ToukaException(ErrorCode.HOOK_PATH_REJECTED, problems=problems)

    async def save(self, config: HooksConfig, expected_revision: str | None) -> str:
        """存檔並回傳新的 revision；``expected_revision`` 要等於目前檔案的 revision。"""
        async with self._save_lock:
            try:
                raw = self._read_raw()
            except OSError:
                # 讀不到目前的檔案（沒有權限、被鎖住）：比對不了 revision，不寫
                logger.exception("Reading the hook config before saving failed")
                raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED) from None
            current = _revision(raw) if raw is not None else None
            if current != expected_revision:
                raise ToukaException(ErrorCode.HOOK_CONFIG_CONFLICT, revision=current)
            await self._check_paths(config)
            try:
                resource_manager.write_json(
                    self._config_path(), config.model_dump(mode="json", exclude_none=True),
                    indent=2,
                )
            except OSError:
                # 寫不進去（磁碟滿、沒有權限）：回明確的存檔錯誤，執行中的仍是上一版
                logger.exception("Saving the hook config failed")
                raise ToukaException(ErrorCode.CONFIG_SAVE_FAILED) from None
            self._refresh()
            written = self._health.revision
            if written is None:
                raise ToukaException(ErrorCode.HOOK_CONFIG_CONFLICT, revision=None)
            return written


hook_service = HookService()

"""未隔離同意帳本（iso-c2）.

政策評估時記下「層級＋subject」；審批卡顯示 ``none`` 且經人核准時記下同意（含卡片寫的
降級去處）；工具執行前用自己的參數重算 subject 取回。只能用一次、10 分鐘過期；同一個
key 出現不同 subject 視為碰撞，兩筆都作廢（fail-closed）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from src.utils.command_safety import effective_shell
from src.utils.isolation_policy import TERMINAL_TOOLS, VERIFY_ACTION
from src.utils.time_utils import get_current_epoch

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from src.models.identity import AgentIdentity
    from src.utils.isolation_policy import IsolationLevel

CONSENT_TTL_SECONDS = 600
MAX_CONSENT_ENTRIES = 4096

Fallback = Literal["executor", "landlock", "direct"]


class ConsentKey(NamedTuple):
    """一次工具呼叫的帳本鍵：代理人（已驗證身分的字串投影）、對話、呼叫。"""

    owner: str
    session_id: str
    call_id: str

    @classmethod
    def of(cls, identity: AgentIdentity, session_id: str | None, call_id: str | None) -> ConsentKey:
        """由已驗證的身分物件建立鍵。"""
        return cls(identity.agent_id, session_id or "", call_id or "")


@dataclass(frozen=True)
class Consent:
    """人核准的降級：卡片寫的去處與它的 features（執行時的 runner 不能比它弱）。"""

    subject: str
    fallback: Fallback
    features: frozenset[str] = frozenset()


class PolicyRecord(NamedTuple):
    """政策時記下的層級、原因、專案根與這次在不在執行器範圍（審批卡據此標示隔離狀態）。"""

    level: IsolationLevel
    reason: str | None
    project_root: Path | None
    executor_eligible: bool = False


@dataclass(frozen=True)
class ExecutionGrant:
    """執行時取回的授權：政策當下的層級，以及（層級 none 時）人的同意。"""

    policy_level: IsolationLevel
    consent: Consent | None = None
    executor_eligible: bool = False  # 演化：取 grant 時（有身分）算好的執行器範圍


@dataclass
class _Entry:
    level: IsolationLevel
    subject: str
    stamp: float
    consent: Consent | None = None
    void: bool = False
    reason: str | None = None
    project_root: Path | None = None
    executor_eligible: bool = False


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(item) for item in value] if isinstance(value, list | tuple) else [str(value)]


def _payload(tool: str, args: Mapping[str, Any]) -> tuple[Any, ...]:
    """套預設值後的正規化內容；只取會改變「執行什麼」的參數。"""
    get = args.get
    if tool in TERMINAL_TOOLS:
        return ("terminal", str(get("command") or ""), effective_shell(get("env")))
    if tool == VERIFY_ACTION:
        return (VERIFY_ACTION, str(get("command") or ""))
    code_digest = _sha256(str(get("code") or ""))
    deps = sorted(_str_list(get("dependencies")))
    if tool == "evolution_implement_and_verify":
        test_args = _canonical(get("test_arguments") or {})
        return (tool, get("skill_id"), code_digest, deps, get("test_tool_name"), test_args)
    if tool == "write_mcp_implementation":
        return (tool, get("skill_id"), code_digest, deps)
    if tool == "verify_mcp_locally":
        return (tool, get("skill_id"), get("tool_name"), _canonical(get("arguments") or {}))
    if tool in {"create_mcp", "scaffold_mcp"}:
        launch = (get("name"), get("command"), _str_list(get("args")), _canonical(get("env") or {}))
        tools = _canonical(get("tools") or []) if tool == "scaffold_mcp" else None
        return (tool, *launch, tools)
    return ("other", tool, _canonical(dict(args)))


def consent_subject(tool: str, args: Mapping[str, Any] | None) -> str:
    """政策時與消耗時共用的 subject（canonical JSON 的 sha256）。"""
    return _sha256(_canonical(_payload(tool, args or {})))


class ConsentLedger:
    """以 ``ConsentKey`` 為鍵的記憶體帳本；只在事件迴圈內使用。"""

    def __init__(
        self,
        clock: Callable[[], float] = get_current_epoch,
        max_entries: int = MAX_CONSENT_ENTRIES,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[ConsentKey, _Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _expired(self, entry: _Entry) -> bool:
        return self._clock() - entry.stamp > CONSENT_TTL_SECONDS

    def record_policy(
        self,
        key: ConsentKey,
        *,
        level: IsolationLevel,
        subject: str,
        reason: str | None = None,
        project_root: Path | None = None,
        executor_eligible: bool = False,
    ) -> None:
        """政策評估時記下層級與 subject；同 key 不同 subject → 作廢；重新記錄一律清掉舊的同意。"""
        entry = self._entries.get(key)
        if entry is not None and not self._expired(entry):
            if entry.subject != subject:
                entry.void = True
            entry.level, entry.reason, entry.project_root = level, reason, project_root
            entry.executor_eligible, entry.consent = executor_eligible, None
            return
        self._entries.pop(key, None)
        self._prune()
        self._entries[key] = _Entry(
            level=level, subject=subject, stamp=self._clock(), reason=reason,
            project_root=project_root, executor_eligible=executor_eligible,
        )

    def pending(self, key: ConsentKey) -> PolicyRecord | None:
        """審批卡用：這次呼叫在政策時記下的層級（不消耗；作廢或過期回 None）。"""
        entry = self._entries.get(key)
        if entry is None or entry.void or self._expired(entry):
            return None
        return PolicyRecord(entry.level, entry.reason, entry.project_root, entry.executor_eligible)

    def grant(
        self, key: ConsentKey, *, fallback: Fallback, features: frozenset[str] = frozenset()
    ) -> bool:
        """人核准顯示 ``none`` 的審批卡：記下卡片的去處與 features；沒有紀錄或不是 none 就拒絕."""
        entry = self._entries.get(key)
        if entry is None or entry.void or self._expired(entry) or entry.level != "none":
            return False
        entry.consent = Consent(subject=entry.subject, fallback=fallback, features=features)
        entry.stamp = self._clock()
        return True

    def take(self, key: ConsentKey, *, subject: str) -> ExecutionGrant | None:
        """執行前取回（只能一次）；subject 不符、作廢或過期都回 None。"""
        entry = self._entries.pop(key, None)
        if entry is None or entry.void or self._expired(entry) or entry.subject != subject:
            return None
        return ExecutionGrant(policy_level=entry.level, consent=entry.consent)

    def clear(self) -> None:
        """清空帳本（測試隔離用）。"""
        self._entries.clear()

    def _prune(self) -> None:
        for stale in [k for k, e in self._entries.items() if self._expired(e)]:
            del self._entries[stale]
        while len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]


consent_ledger = ConsentLedger()

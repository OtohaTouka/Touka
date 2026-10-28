"""Hook 結果解讀（照 Claude Code，ADR-0292）.

- 結束碼 2：UserPromptSubmit、PreToolUse 擋下，原因取 stderr；PostToolUse（工具已經跑完）把
  stderr 當回饋給模型；Stop 只觀察。結束碼 2 時不讀 stdout 的 JSON。
- 結束碼 0：stdout 是 JSON 物件就讀欄位；不是 JSON 的文字只在 UserPromptSubmit 當補充說明。
  看起來是 JSON 卻解析失敗（含數字太長、巢狀太深）算 hook 自己失敗。
- ``continue: false``：UserPromptSubmit、PreToolUse 擋下，原因取 ``stopReason``（比其他欄位優先）；
  PostToolUse 與 Stop 不採用（工具已經跑完、回合已經結束，hook 不能讓代理人停下或繼續）。
- 其他結束碼、逾時、連不上、路徑被拒：hook 自己失敗，不擋、記下來。
- http：2xx 的回應內容當成結束碼 0 的 stdout；其他情況都是失敗。

hook 只能擋不能放行：``permissionDecision`` 的 ``allow``／``defer``、``updatedInput``、
``updatedToolOutput``、``systemMessage`` 一律不採用，只記一筆說明。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from src.services.hooks.runners import RunResult

BLOCK_EXIT_CODE: Final[int] = 2
MAX_TEXT_CHARS: Final[int] = 10_000
# 結束碼 2、decision、continue 能擋下的事件；其他事件的工具已經跑完或回合已經結束
BLOCKING_EVENTS: Final[frozenset[str]] = frozenset({"UserPromptSubmit", "PreToolUse"})

_IGNORED_TOP_LEVEL: Final[tuple[str, ...]] = ("systemMessage",)
_IGNORED_SPECIFIC: Final[tuple[str, ...]] = (
    "updatedInput",
    "updatedToolOutput",
    "updatedMCPToolOutput",
)


@dataclass(frozen=True)
class HookVerdict:
    """一個 hook 的結果代表什麼。"""

    blocked: bool = False
    ask: bool = False
    reason: str | None = None
    context: str | None = None
    failed: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text[:MAX_TEXT_CHARS] if text else None


def _text(value: Any) -> str | None:
    return _clip(value) if isinstance(value, str) else None


def _from_exit_2(event: str, stderr: str) -> HookVerdict:
    reason = _clip(stderr) or "blocked by hook"
    if event in BLOCKING_EVENTS:
        return HookVerdict(blocked=True, reason=reason)
    if event == "PostToolUse":
        return HookVerdict(context=reason)
    return HookVerdict(notes=("Stop hooks only observe; exit code 2 is ignored",))


def _from_specific(event: str, specific: Any, notes: list[str]) -> HookVerdict:
    if not isinstance(specific, dict):
        return HookVerdict()
    if specific.get("hookEventName") != event:
        notes.append("hookSpecificOutput.hookEventName does not match; ignored")
        return HookVerdict()
    notes.extend(
        f"hookSpecificOutput.{key} is not supported; ignored"
        for key in _IGNORED_SPECIFIC
        if key in specific
    )
    context = _text(specific.get("additionalContext")) if event != "Stop" else None
    if event != "PreToolUse":
        return HookVerdict(context=context)
    decision = specific.get("permissionDecision")
    reason = _text(specific.get("permissionDecisionReason"))
    if decision == "deny":
        return HookVerdict(blocked=True, reason=reason or "denied by hook", context=context)
    if decision == "ask":
        return HookVerdict(ask=True, reason=reason, context=context)
    if decision is not None:
        notes.append(f"permissionDecision {decision!r} is not supported; hooks only deny or ask")
    return HookVerdict(context=context)


def _from_json(event: str, body: dict[str, Any]) -> HookVerdict:
    notes = [f"{key} is not supported; ignored" for key in _IGNORED_TOP_LEVEL if key in body]
    specific = _from_specific(event, body.get("hookSpecificOutput"), notes)
    blocked, ask = specific.blocked, specific.ask
    reason, context = specific.reason, specific.context
    if body.get("continue") is False:
        if event in BLOCKING_EVENTS:
            stop_reason = _text(body.get("stopReason")) or "stopped by hook"
            return HookVerdict(
                blocked=True, reason=stop_reason, context=context, notes=tuple(notes)
            )
        notes.append("continue: false only blocks prompts and tool calls; ignored")
    if body.get("decision") == "block":
        top_reason = _text(body.get("reason")) or "blocked by hook"
        if event in BLOCKING_EVENTS:
            blocked, reason = True, reason or top_reason
        elif event == "PostToolUse":
            context = "\n".join(part for part in (top_reason, context) if part)
        else:
            notes.append("Stop hooks only observe; decision is ignored")
    return HookVerdict(
        blocked=blocked, ask=ask and not blocked, reason=reason, context=context, notes=tuple(notes)
    )


def _from_stdout(event: str, stdout: str) -> HookVerdict:
    text = stdout.strip()
    if not text:
        return HookVerdict()
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except (ValueError, RecursionError):  # 含數字太長、巢狀太深
            return HookVerdict(failed=True, notes=("output is not valid JSON",))
        if isinstance(body, dict):
            return _from_json(event, body)
    if event == "UserPromptSubmit":
        return HookVerdict(context=_clip(text))
    return HookVerdict()


def interpret(event: str, result: RunResult) -> HookVerdict:
    """把一次執行的結果解讀成擋下、詢問、補充說明或失敗。"""
    if result.status != "ok":
        return HookVerdict(failed=True)
    if result.http_status is not None:
        return _from_stdout(event, result.stdout)
    if result.exit_code == BLOCK_EXIT_CODE:
        return _from_exit_2(event, result.stderr)
    if result.exit_code == 0:
        return _from_stdout(event, result.stdout)
    return HookVerdict(failed=True)

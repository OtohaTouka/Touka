"""人工 live／外部模型 API 執行政策的機器可讀 SSOT（ADR-0214）。

這個模組只含 immutable metadata 與純函式；匯入時不得讀取金鑰、資料或網路。
自動化不得傳入核准旗標。旗標只表示操作者已取得「本次、明確範圍」的人類同意，
不能由先前同意、環境中存在金鑰或某個測試失敗推導而來。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


LIVE_API_APPROVAL_FLAG = "--human-approved-live-api"
PROVIDER_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GROK_API_KEY",
    "LITELLM_API_KEY",
    "OPENAI_API_KEY",
    "TAVILY_API_KEY",
    "XAI_API_KEY",
)
_HELP_FLAGS = frozenset({"-h", "--help"})
_SECRET_MARKERS = ("api_key", "authorization", "bearer ", "token=")
_APPROVAL_ISSUER = object()


class LiveApiApprovalRequired(RuntimeError):
    """本次呼叫沒有取得明確的人類 live API 核准。"""

    def __init__(self) -> None:
        super().__init__(
            "NOT RUN — HUMAN APPROVAL REQUIRED: 必須先取得本次執行的人類明確同意；"
            f"核准後才可傳入 {LIVE_API_APPROVAL_FLAG}。"
        )


class LiveApiRequestLimitExceeded(RuntimeError):
    """本次核准的 provider request 數量已耗盡。"""

    def __init__(self, max_requests: int) -> None:
        super().__init__(f"provider request limit exceeded ({max_requests})")


class LiveApiRequestBudgetRequired(RuntimeError):
    """provider transport 未收到本次執行的 request budget。"""

    def __init__(self) -> None:
        super().__init__("provider request budget is required")


class InvalidLiveApiRequestLimit(ValueError):
    """provider request 上限不是正整數。"""

    def __init__(self) -> None:
        super().__init__("max_requests must be positive")


class InvalidLiveApiTokenLimit(ValueError):
    """provider transport 未收到正整數 token 上限。"""

    def __init__(self) -> None:
        super().__init__("max_tokens must be a positive integer")


class LiveApiApprovalScopeMismatch(RuntimeError):
    """核准 capability 與即將執行的精確範圍不同。"""

    def __init__(self) -> None:
        super().__init__("live API approval scope does not match this invocation")


class UnknownLiveApiOperation(ValueError):
    """operation id 不在政策 SSOT。"""

    def __init__(self, operation_id: str) -> None:
        super().__init__(f"unknown live API operation: {operation_id}")


class IncompleteLiveApiScope(ValueError):
    """核准範圍缺少必要欄位。"""

    def __init__(self) -> None:
        super().__init__("live API approval scope must be complete")


class CredentialInLiveApiScope(ValueError):
    """核准範圍意外含憑證。"""

    def __init__(self) -> None:
        super().__init__("live API approval scope must not contain credentials")


class LiveApiPreflightExit(SystemExit):
    """最早期 CLI 閘拒絕繼續 import provider 模組。"""

    def __init__(self) -> None:
        super().__init__(
            "NOT RUN — HUMAN APPROVAL REQUIRED: 此入口可能呼叫外部／計量 API；"
            f"取得本次明確人類同意後才可加入 {LIVE_API_APPROVAL_FLAG}。"
        )


class LiveApiHelpExitFailure(RuntimeError):
    """argparse 的本機 help 未依契約終止程序。"""

    def __init__(self) -> None:
        super().__init__("argparse --help must exit")


@dataclass(frozen=True, slots=True)
class LiveApiOperation:
    """一個可能外連的人工入口及其自動化分類。"""

    operation_id: str
    entrypoint: str
    metered_api: bool
    requires_human_approval: bool = True
    automated: bool = False


@dataclass(slots=True)
class LiveApiCallBudget:
    """程序內 provider request 硬上限；在傳輸呼叫前 consume。"""

    max_requests: int
    used_requests: int = 0

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise InvalidLiveApiRequestLimit

    def consume(self) -> int:
        """核銷一次 provider request；超額時在外連前 fail-closed。"""
        if self.used_requests >= self.max_requests:
            raise LiveApiRequestLimitExceeded(self.max_requests)
        self.used_requests += 1
        return self.used_requests


@dataclass(frozen=True, slots=True)
class LiveApiScope:
    """人類在單次執行前必須看見的核准範圍。"""

    operation_id: str
    provider: str
    model: str
    limits: tuple[str, ...]
    data_scope: str
    write_scope: str


@dataclass(frozen=True, slots=True, init=False)
class LiveApiApproval:
    """只由政策函式簽發、並綁定單次完整 scope 的程序內 capability。"""

    scope: LiveApiScope
    _seal: object

    def __init__(self, scope: LiveApiScope, *, _issuer: object | None = None) -> None:
        if _issuer is not _APPROVAL_ISSUER:
            raise LiveApiApprovalRequired
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "_seal", _APPROVAL_ISSUER)


LIVE_API_OPERATIONS: tuple[LiveApiOperation, ...] = (
    LiveApiOperation(
        operation_id="manual_live_chat_probe",
        entrypoint="tests/smoke/manual/live_chat_probe.py",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="manual_memory_eval_probe",
        entrypoint="tests/smoke/manual/memory_eval_probe.py",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="manual_thinking_latency_probe",
        entrypoint="tests/smoke/manual/thinking_latency_probe.py",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="scorecard_cli",
        entrypoint="scripts/run_scorecard.py",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="scorecard_debug_endpoint",
        entrypoint="POST /v1/debug/scorecard/run",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="tool_eval_baseline_list_models",
        entrypoint="scripts/record_tool_eval_baseline.py --list-models",
        # 一般不計 token，但仍是帶憑證的外部 provider request，故同樣人工核准。
        metered_api=False,
    ),
    LiveApiOperation(
        operation_id="tool_eval_baseline_record",
        entrypoint="scripts/record_tool_eval_baseline.py --model",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="tool_eval_live_episode",
        entrypoint="scripts/run_tool_eval_episode.py --case",
        metered_api=True,
    ),
    LiveApiOperation(
        operation_id="creation_model_judge",
        entrypoint="creation eval: TOUKA_EVAL_JUDGE_ENABLED=true + eval_scoring=true",
        metered_api=True,
    ),
)

_OPERATIONS_BY_ID = {item.operation_id: item for item in LIVE_API_OPERATIONS}


def get_live_api_operation(operation_id: str) -> LiveApiOperation:
    """依穩定 id 取得人工 live operation；未知項目 fail-closed。"""
    try:
        return _OPERATIONS_BY_ID[operation_id]
    except KeyError as exc:
        raise UnknownLiveApiOperation(operation_id) from exc


def preflight_live_api_cli(
    argv: Sequence[str],
    *,
    local_only_flags: Sequence[str] = (),
) -> None:
    """在任何 provider-capable import／金鑰載入前執行的最早期閘。

    ``--help`` 與呼叫端明列的 local-only mode 不需核准；其他路徑缺旗標便立即
    ``SystemExit``，使後續模組根本不會被 import。
    """
    arguments = frozenset(argv)
    if arguments & _HELP_FLAGS:
        # 真正於任何 provider-capable import 前完成本機 help parse/exit。
        # 完整參數仍記於各腳本 docstring；此早期頁面刻意不載入產品模組。
        help_parser = argparse.ArgumentParser(
            description="人工 live API 入口；完整參數請見腳本 docstring。"
        )
        help_parser.parse_args(["--help"])
        raise LiveApiHelpExitFailure
    if arguments & frozenset(local_only_flags):
        return
    if LIVE_API_APPROVAL_FLAG not in arguments:
        raise LiveApiPreflightExit


def _validate_scope(scope: LiveApiScope) -> None:
    get_live_api_operation(scope.operation_id)
    values = (
        scope.operation_id,
        scope.provider,
        scope.model,
        scope.data_scope,
        scope.write_scope,
        *scope.limits,
    )
    if not all(value.strip() for value in values) or not scope.limits:
        raise IncompleteLiveApiScope
    normalized = "\n".join(values).casefold()
    if any(marker in normalized for marker in _SECRET_MARKERS):
        raise CredentialInLiveApiScope


def format_live_api_scope(scope: LiveApiScope) -> str:
    """輸出可稽核、無憑證的單次核准範圍。"""
    _validate_scope(scope)
    limits = ", ".join(scope.limits)
    return "\n".join(
        (
            "[HUMAN-APPROVED LIVE API SCOPE]",
            f"operation={scope.operation_id}",
            f"provider={scope.provider}",
            f"model={scope.model}",
            f"limits={limits}",
            f"data_scope={scope.data_scope}",
            f"write_scope={scope.write_scope}",
        )
    )


def require_live_api_approval(*, approved: bool, scope: LiveApiScope) -> str:
    """驗證本次人工核准，通過時回傳必須在外連前顯示的範圍。"""
    _validate_scope(scope)
    if not approved:
        raise LiveApiApprovalRequired
    return format_live_api_scope(scope)


def issue_live_api_approval(*, approved: bool, scope: LiveApiScope) -> LiveApiApproval:
    """為本次明確人類同意簽發綁定完整 scope 的程序內 capability。"""
    _validate_scope(scope)
    if not approved:
        raise LiveApiApprovalRequired
    return LiveApiApproval(scope, _issuer=_APPROVAL_ISSUER)


def validate_live_api_approval(
    approval: LiveApiApproval | None,
    *,
    expected_scope: LiveApiScope,
) -> None:
    """在執行函式內驗證 capability 真實性與本次 scope 完全一致。"""
    _validate_scope(expected_scope)
    if not isinstance(approval, LiveApiApproval) or approval._seal is not _APPROVAL_ISSUER:
        raise LiveApiApprovalRequired
    if approval.scope != expected_scope:
        raise LiveApiApprovalScopeMismatch

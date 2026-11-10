"""小幫手和 Touka 的協議要相同（ADR-0312）：兩邊各存一份，小幫手不能載入 Touka 的程式。"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from src.models.local_bridge import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_JOB_RESULT_BYTES,
    MAX_REQUESTER_CHARS,
    PROTOCOL_VERSION,
    LocalAction,
    LocalJob,
    LocalJobResult,
    LocalPollRequest,
    LocalPollResponse,
)
from src.services.local_bridge import (
    DECISION_WAIT_SECONDS,
    HELPER_DECISION_MARGIN_SECONDS,
    HELPER_EXECUTION_LIMIT_SECONDS,
    MAX_PENDING_JOBS,
)

HELPER = Path(__file__).resolve().parents[2] / "ToukaLocal" / "touka_local"


def _constants(file_name: str) -> dict[str, object]:
    """用靜態解析讀小幫手的常數，測試不需要把它加進匯入路徑。"""
    tree = ast.parse((HELPER / file_name).read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                values[node.target.id] = _evaluate(node.value, values)
            except ValueError:
                continue
    return values


def _evaluate(node: ast.expr | None, known: dict[str, object]) -> object:
    if isinstance(node, ast.Name) and node.id in known:
        return known[node.id]
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "frozenset":
        return frozenset(ast.literal_eval(node.args[0]))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _evaluate(node.left, known) * _evaluate(node.right, known)  # type: ignore[operator]
    if node is None:
        raise ValueError
    return ast.literal_eval(node)


def test_version_and_limits_match() -> None:
    helper = _constants("protocol.py")
    assert helper["PROTOCOL_VERSION"] == PROTOCOL_VERSION
    assert helper["MAX_JOB_RESULT_BYTES"] == MAX_JOB_RESULT_BYTES
    assert helper["MAX_ERROR_MESSAGE_CHARS"] == MAX_ERROR_MESSAGE_CHARS
    assert helper["MAX_REQUESTER_CHARS"] == MAX_REQUESTER_CHARS


def test_actions_match() -> None:
    assert _constants("protocol.py")["ACTIONS"] == get_args(LocalAction)


def test_fields_match() -> None:
    helper = _constants("protocol.py")
    assert helper["POLL_REQUEST_FIELDS"] == frozenset(LocalPollRequest.model_fields)
    assert helper["POLL_RESPONSE_FIELDS"] == frozenset(LocalPollResponse.model_fields)
    assert helper["JOB_FIELDS"] == frozenset(LocalJob.model_fields)
    assert helper["RESULT_FIELDS"] == frozenset(LocalJobResult.model_fields)


def test_result_statuses_match() -> None:
    status = LocalJobResult.model_fields["status"].annotation
    assert _constants("protocol.py")["RESULT_STATUSES"] == get_args(status)


def test_decision_limit_matches() -> None:
    helper = _constants("approval.py")
    assert helper["MAX_DECISION_SECONDS"] == DECISION_WAIT_SECONDS - HELPER_DECISION_MARGIN_SECONDS


def test_execution_limit_matches() -> None:
    helper = _constants("service.py")
    assert helper["HELPER_EXECUTION_LIMIT_SECONDS"] == HELPER_EXECUTION_LIMIT_SECONDS


def test_thread_cap_matches_the_queue_limit() -> None:
    assert _constants("service.py")["MAX_JOB_THREADS"] == MAX_PENDING_JOBS

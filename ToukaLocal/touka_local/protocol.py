"""和 Touka 之間的資料格式（ADR-0312）。

Touka 端的 ``ToukaProxy/src/models/local_bridge.py`` 是準；這裡另存一份是因為小幫手不能
載入 Touka 的程式。``tests/unit/test_local_protocol_parity.py`` 核對兩邊相同。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

PROTOCOL_VERSION: Final = 3
MAX_JOB_RESULT_BYTES: Final = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS: Final = 2000
MAX_CODE_CHARS: Final = 64
MAX_REQUESTER_CHARS: Final = 128

ACTIONS: Final = (
    "list",
    "read",
    "create_folder",
    "create_file",
    "propose",
    "preview",
    "request_open",
)
RESULT_STATUSES: Final = ("ok", "refused", "error")
POLL_REQUEST_FIELDS: Final = frozenset({"protocol", "helper_version"})
POLL_RESPONSE_FIELDS: Final = frozenset({"job", "cancelled", "host_mount_roots"})
JOB_FIELDS: Final = frozenset({"id", "action", "args", "requester", "decide_within", "decision"})
DECISION_FIELDS: Final = frozenset({"nonce", "digest", "shown", "decided_by", "signature"})
# 照模式的決定只有這三種；在小幫手視窗按的由小幫手自己記
DECIDED_BY_MODES: Final = ("user_card", "auto_review", "yolo")
DECIDED_BY_HELPER_WINDOW: Final = "helper_window"
# 開放請求的章（ADR-0320）：只證明是 Touka 送的，一定跳視窗，不算照模式的決定
DECIDED_BY_OPEN_REQUEST: Final = "owner_request"
RESULT_FIELDS: Final = frozenset({"status", "code", "message", "data"})

JOB_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")

# 開放名單回報給 Touka（ADR-0330）：只給配對的人在設定頁看，Touka 只放記憶體
FOLDER_REPORT_FIELDS: Final = frozenset(
    {"protocol", "instance", "revision", "exposed", "folders", "omitted"}
)
FOLDER_ENTRY_FIELDS: Final = frozenset({"name", "path", "writable", "usable"})
MAX_REPORTED_FOLDERS: Final = 64
MAX_REPORTED_PATH_CHARS: Final = 1024
MAX_REPORTED_NAME_CHARS: Final = 300
# Touka 不收的字元：控制字元與改變顯示方向的字元（parity 測試核對兩邊相同）
UNSAFE_TEXT_PATTERN: Final = r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
_UNSAFE_TEXT: Final = re.compile(UNSAFE_TEXT_PATTERN)


class ProtocolError(Exception):
    """Touka 送來的內容不是這一版協議認得的。"""


@dataclass(frozen=True)
class Decision:
    """Touka 照模式做的決定和它蓋的章。格式不對時各欄是空字串，核對一定不過。"""

    nonce: str
    digest: str
    shown: str
    decided_by: str
    signature: str


_UNUSABLE_DECISION: Final = Decision("", "", "", "", "")


@dataclass(frozen=True)
class Job:
    """一件工作。``decision`` 只有照模式決定的搬移、刪除清單才有。"""

    id: str
    action: str
    args: dict[str, Any]
    requester: str
    decide_within: float
    decision: Decision | None = None


@dataclass(frozen=True)
class PollResponse:
    """一次輪詢的回應。"""

    job: Job | None
    cancelled: tuple[str, ...]
    host_mount_roots: tuple[str, ...]


def _text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError
    return tuple(value)


def _parse_decision(value: object) -> Decision | None:
    """沒有決定是 None；有但格式不對時當作蓋錯章，小幫手會改跳視窗。"""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != DECISION_FIELDS
        or not all(isinstance(item, str) for item in value.values())
    ):
        return _UNUSABLE_DECISION
    return Decision(
        nonce=value["nonce"],
        digest=value["digest"],
        shown=value["shown"],
        decided_by=value["decided_by"],
        signature=value["signature"],
    )


def _parse_job(value: object) -> Job:
    if not isinstance(value, dict) or set(value) != JOB_FIELDS:
        raise ProtocolError
    job_id, action, args = value["id"], value["action"], value["args"]
    requester, decide_within = value["requester"], value["decide_within"]
    if (
        not isinstance(job_id, str)
        or JOB_ID.fullmatch(job_id) is None
        or action not in ACTIONS
        or not isinstance(args, dict)
        or not isinstance(requester, str)
        or isinstance(decide_within, bool)
        or not isinstance(decide_within, int | float)
        or not math.isfinite(decide_within)
        or decide_within <= 0
    ):
        raise ProtocolError
    return Job(
        job_id,
        action,
        args,
        requester[:MAX_REQUESTER_CHARS],
        float(decide_within),
        _parse_decision(value["decision"]),
    )


def parse_poll_response(payload: object) -> PollResponse:
    """解析輪詢回應，多一個或少一個欄位都拒絕。"""
    if not isinstance(payload, dict) or set(payload) != POLL_RESPONSE_FIELDS:
        raise ProtocolError
    raw_job = payload["job"]
    return PollResponse(
        job=None if raw_job is None else _parse_job(raw_job),
        cancelled=_text_list(payload["cancelled"]),
        host_mount_roots=_text_list(payload["host_mount_roots"]),
    )


def poll_request(helper_version: str) -> dict[str, Any]:
    """輪詢要帶的內容。"""
    return {"protocol": PROTOCOL_VERSION, "helper_version": helper_version}


def result_payload(
    status: str,
    *,
    code: str | None = None,
    message: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """組回報的內容，長度超過協議上限的不送出去。"""
    payload = {
        "status": status,
        "code": None if code is None else code[:MAX_CODE_CHARS],
        "message": None if message is None else message[:MAX_ERROR_MESSAGE_CHARS],
        "data": data,
    }
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_JOB_RESULT_BYTES:
        return {
            "status": "error",
            "code": "result_too_large",
            "message": "The result was too large to send back.",
            "data": None,
        }
    return payload


def _reportable(text: str, limit: int) -> bool:
    """Touka 收得下：長度在上限內、沒有不收的字元，也沒有單獨的代理字元（UTF-8 編不出來）。"""
    return (
        1 <= len(text) <= limit
        and _UNSAFE_TEXT.search(text) is None
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in text)
    )


def folder_report(
    *,
    instance: str,
    revision: int,
    exposed: bool,
    folders: Iterable[tuple[str, str, bool, bool]],
) -> dict[str, Any]:
    """開放名單的回報。Touka 不收的與超過上限的不送，只算個數，整份才不會被拒。"""
    entries: list[dict[str, Any]] = []
    omitted = 0
    for name, path, writable, usable in folders:
        if (
            len(entries) >= MAX_REPORTED_FOLDERS
            or not _reportable(name, MAX_REPORTED_NAME_CHARS)
            or not _reportable(path, MAX_REPORTED_PATH_CHARS)
        ):
            omitted += 1
            continue
        entries.append({"name": name, "path": path, "writable": writable, "usable": usable})
    return {
        "protocol": PROTOCOL_VERSION,
        "instance": instance,
        "revision": revision,
        "exposed": exposed,
        "folders": entries,
        "omitted": omitted,
    }

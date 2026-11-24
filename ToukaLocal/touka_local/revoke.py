"""Touka 設定頁縮小開放名單（ADR-0330 2b）：拿掉，或改成只能讀。

章只證明是 Touka 用配對的鑰匙送的，不跳視窗；一次性號碼由 ``service.py`` 記，只能用一次。
名稱和路徑要對上名單上的同一筆（``GrantBook.shrink`` 在名單的鎖裡找、改）。只能縮小：
沒有任何輸入能讓資料夾變成可以改，或讓名單變多。規則只放在這裡，``service.py`` 只負責接線。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from touka_local.errors import Refused
from touka_local.protocol import MAX_REPORTED_NAME_CHARS, MAX_REPORTED_PATH_CHARS, REVOKE_FIELDS
from touka_local.signing import revoke_signature_ok

if TYPE_CHECKING:
    from touka_local.grants import Grant
    from touka_local.protocol import Job

CHANGE_REMOVE: Final = "remove"
CHANGE_READ_ONLY: Final = "read_only"
# 只有這兩種；parity 測試核對和 Touka 的 RevokeChange 相同
REVOKE_CHANGES: Final = ("remove", "read_only")


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _text_within(value: object, limit: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit and not _has_lone_surrogate(value)


def verify_revoke(job: Job, key: bytes | None) -> tuple[str, str, str]:
    """核對章，回傳（名稱, 路徑, 改法）。欄位、型別、長度或章任何一項不對都是 ``bad_signature``。

    單獨的代理字元算不出指紋，在核對章之前就擋下。
    """
    args = job.args
    if set(args) != REVOKE_FIELDS:
        raise Refused("bad_signature")
    name, path, change = args["name"], args["path"], args["change"]
    if (
        not _text_within(name, MAX_REPORTED_NAME_CHARS)
        or not _text_within(path, MAX_REPORTED_PATH_CHARS)
        or not isinstance(change, str)
        or change not in REVOKE_CHANGES
    ):
        raise Refused("bad_signature")
    if key is None:
        raise Refused("needs_new_pairing")
    if job.decision is None or not revoke_signature_ok(
        key, job.id, name, path, change, job.decision
    ):
        raise Refused("bad_signature")
    return name, path, change


def revoke_data(grant: Grant, change: str, *, instance: str, revision: int) -> dict[str, Any]:
    """回給 Touka 的結果：拿掉的帶 ``removed``，改成只能讀的帶 ``writable``。

    另帶存檔後的啟動編號與修改次數，Touka 用來丟掉縮小之前就在路上的舊回報。
    """
    changed: dict[str, bool] = {"removed": True} if change == CHANGE_REMOVE else {"writable": False}
    return {"name": grant.name, **changed, "instance": instance, "revision": revision}

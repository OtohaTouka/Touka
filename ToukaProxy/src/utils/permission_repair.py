"""權限自癒的裝飾器（iso-c2 U2n）：ResourceManager 碰檔案的方法遇到 EACCES 時交給注入的掛鉤.

- ``permission_repair(retry=<字面 bool>)``：只有最外層的已裝飾呼叫處理（ContextVar 巢狀保護），
  只處理 Linux 上 ``errno == EACCES`` 而且帶 ``filename`` 的 PermissionError（區內方法的例外看
  ``zone_abs``）；掛鉤回報真的有修時，
  ``retry=True`` 再執行一次（第二次失敗照實往上丟）。其餘情況（政策拒絕的 errno 是 None、EPERM、
  沒有掛鉤、掛鉤出錯、``retry=False``）一律原樣往上丟**同一個例外物件**。
- ``permission_repair_exempt(category=…, reason=…)``：不包裝，只加標記。
- 只用標準函式庫；掛鉤（``services/isolation/repair.py``）由 ``set_permission_repair`` 注入。
  每個方法的歸類由 ``tests/lint/test_resource_manager_permission_repair.py`` 執法。
"""

from __future__ import annotations

import errno
import functools
import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal, ParamSpec, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# generator：走訪與交出路徑的產生器，例外在呼叫之後才發生，裝飾器包不到（ADR-0293）
ExemptCategory = Literal["swap_semantics", "path_only", "delegates", "generator"]
_P = ParamSpec("_P")
_R = TypeVar("_R")
_active: ContextVar[bool] = ContextVar("permission_repair_active", default=False)


class PermissionRepairHook(Protocol):
    """自癒掛鉤；白名單、受保護路徑、頻率限制與計數都在實作裡。"""

    def repair(self, method: str, paths: tuple[object, ...], *, retry: bool) -> bool:
        """修例外的 ``(filename, filename2)``；權限位元真的有改才回 True。"""
        ...

    def note_retry(self, method: str, succeeded: bool) -> None:
        """記錄修完之後那一次重試的結果。"""
        ...


_hook: PermissionRepairHook | None = None


def set_permission_repair(hook: PermissionRepairHook | None) -> None:
    """安裝或拿掉全域掛鉤（測試的 conftest 每個測試前後重設成 None）。"""
    global _hook
    _hook = hook


def _call_hook(method: str, action: Callable[[PermissionRepairHook], bool | None]) -> bool:
    hook = _hook
    if hook is None:
        return False
    try:
        return action(hook) is True
    except Exception:  # 掛鉤出錯當作沒修；呼叫端拿到的仍是原本的例外
        logger.warning("Permission repair hook failed in %s; treated as not repaired.", method)
        return False


def permission_repair(*, retry: bool) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """會碰檔案的同步方法：EACCES 時交給掛鉤修；``retry`` 標明修完能不能安全地再執行一次。"""

    def decorate(func: Callable[_P, _R]) -> Callable[_P, _R]:
        name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            if _active.get():
                return func(*args, **kwargs)
            token = _active.set(True)
            try:
                try:
                    return func(*args, **kwargs)
                except PermissionError as exc:
                    eligible = sys.platform == "linux" and exc.errno == errno.EACCES
                    # 區內方法的例外只帶顯示路徑，絕對路徑在 zone_abs（ADR-0293 R12）
                    zone_abs = getattr(exc, "zone_abs", None)
                    paths = (zone_abs, None) if zone_abs else (exc.filename, exc.filename2)
                    fixed = eligible and bool(paths[0]) and _call_hook(
                        name, lambda hook: hook.repair(name, paths, retry=retry)
                    )
                    if not (fixed and retry):
                        raise
                try:
                    result = func(*args, **kwargs)
                except BaseException:
                    _call_hook(name, lambda hook: hook.note_retry(name, succeeded=False))
                    raise
                _call_hook(name, lambda hook: hook.note_retry(name, succeeded=True))
                return result
            finally:
                _active.reset(token)

        return wrapper

    return decorate


def permission_repair_exempt(
    *, category: ExemptCategory, reason: str
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """不自癒的方法：只加標記、不包裝；``reason`` 留在原始碼給審查與結構測試。"""
    del reason

    def mark(func: Callable[_P, _R]) -> Callable[_P, _R]:
        func.__permission_repair__ = ("exempt", category)  # type: ignore[attr-defined]
        return func

    return mark

"""專用空間的範圍判定：結構層（ADR-0332）.

專用空間是 compose 的 named volume ``touka-projects``，proxy 與執行器掛在同一個位置；位置只從
部署設定 ``TOUKA_PROJECTS_DIR`` 來（``profiles.configured_projects_root``），和工作區設定脫鉤
（管理員改工作區設定不會讓錨點和實際掛載對不上）。第一層每個資料夾是一個專案，只有這些專案的指令走執行器。

``structural_scope`` 是純函式，只看路徑（不看執行器、在場與掃描），回：

- ``outside``：不在專用空間（錨點本身不算在裡面）：和現在完全一樣；
- ``ok``：專用空間第一層的一個資料夾（帶名稱與字面位置）；
- ``refused``：錨點本身、第二層以下、不安全的名稱、路上有連結，或這個部署沒有專用空間卻用了
  ``touka-projects`` 這個名稱。``refused`` 一律直接拒絕、不提供降級。

執行層（執行器、READY、掃描、在場）在 ``status.IsolationMonitor.level_for``。這個模組不匯入
``async_db``（``get_project_context`` 也用它），下載專案（PR B）共用。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.services.isolation.executor_session import PROJECT_ROOT_NAME
from src.services.isolation.profiles import has_link, literal_path
from src.utils.isolation_policy import is_safe_segment

ScopeKind = Literal["outside", "ok", "refused"]

# 結構層的拒絕原因（永久：改了位置或名稱才會好）
PROJECT_SPACE_ROOT = "project_space_root"
PROJECT_NOT_TOP_LEVEL = "project_not_top_level"
PROJECT_NAME_UNSAFE = "project_name_unsafe"
PROJECT_LINKED = "project_linked"
PROJECT_SPACE_UNAVAILABLE = "project_space_unavailable"


@dataclass(frozen=True)
class ProjectScope:
    """一個專案根的範圍；``name``、``path`` 只在 ``ok`` 時有，``reason`` 只在 ``refused`` 時有."""

    kind: ScopeKind
    name: str | None = None
    path: Path | None = None
    reason: str | None = None

    @property
    def dedicated(self) -> bool:
        """在專用空間裡（``ok`` 或 ``refused``）：不走一般專案的規則."""
        return self.kind != "outside"


OUTSIDE = ProjectScope("outside")


def _refused(reason: str) -> ProjectScope:
    return ProjectScope("refused", reason=reason)


def _names_the_space(path: Path) -> bool:
    return any(part.casefold() == PROJECT_ROOT_NAME for part in path.parts)


def structural_scope(project_root: Path | str | None, anchor: Path | None) -> ProjectScope:
    """專案根在不在專用空間（規則見模組說明）；``anchor`` 是版面的 ``projects_root``.

    字面位置與解析後的位置任一個在錨點底下就算在專用空間（``translate_path`` 不消 ``..``，
    連結進來或連結出去都要擋）；之後路上有連結一律 ``refused``。
    """
    if project_root is None:
        return OUTSIDE
    literal = literal_path(project_root)
    resolved = Path(project_root).resolve()
    if anchor is None:
        # 這個部署沒有專用空間：照名稱擋下，不讓它悄悄改用別的方式跑（README 寫明）
        named = _names_the_space(literal) or _names_the_space(resolved)
        return _refused(PROJECT_SPACE_UNAVAILABLE) if named else OUTSIDE
    inside = [path for path in (literal, resolved) if path.is_relative_to(anchor)]
    if not inside:
        return OUTSIDE
    if anchor in inside:
        return _refused(PROJECT_SPACE_ROOT)
    if has_link(project_root, resolved):  # ``..`` 也算（解析前後不同）
        return _refused(PROJECT_LINKED)
    parts = literal.relative_to(anchor).parts
    if len(parts) != 1:
        return _refused(PROJECT_NOT_TOP_LEVEL)
    name = parts[0]
    if not is_safe_segment(name):
        return _refused(PROJECT_NAME_UNSAFE)
    return ProjectScope("ok", name=name, path=anchor / name)


def is_project_folder(scope: ProjectScope) -> bool:
    """``ok`` 的專案資料夾真的在（是資料夾、不是連結）；登記前與刪除後清 HOME 用."""
    if scope.kind != "ok" or scope.path is None:
        return False
    try:
        return stat.S_ISDIR(os.lstat(scope.path).st_mode)
    except OSError:
        return False

"""``/v1/memory/health`` 的個人空間一項看的是區的錨點（ADR-0293）。

只有 ``personal_drive`` 是資料夾才算正常；不在或是一般檔案都不算。data 根換到
``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.identity import AgentIdentity
from src.routers.observability import get_memory_health

if TYPE_CHECKING:
    from pathlib import Path

_ADMIN = AgentIdentity.for_role("health-admin", "admin", is_human=True)


async def test_personal_is_true_only_when_the_folder_exists(zone_data_root: Path) -> None:
    assert (await get_memory_health(_identity=_ADMIN))["personal"] is False
    (zone_data_root / "personal_drive").mkdir()
    assert (await get_memory_health(_identity=_ADMIN))["personal"] is True


async def test_a_file_named_personal_drive_is_not_storage(zone_data_root: Path) -> None:
    (zone_data_root / "personal_drive").write_text("x", encoding="utf-8")
    assert (await get_memory_health(_identity=_ADMIN))["personal"] is False

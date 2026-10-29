"""記憶診斷的「個人空間大小」用區內走訪加總，不進入連結（ADR-0293）。

連結另一端的大檔不算進去；個人空間根本身是連結時回 None（顯示不可用）。data 根換到
``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_server.services.manager import MemoryManager
from memory_server.tools.diag import personal_drive_bytes
from src.models.identity import AgentIdentity

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

MODEL = AgentIdentity(agent_id="M1")


def _outside(tmp_path: Path) -> Path:
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "big.bin").write_bytes(b"x" * 1000)
    return fake


def test_the_size_counts_only_files_inside_the_drive(zone_data_root: Path, tmp_path: Path) -> None:
    drive = zone_data_root / "custom" / "M1" / "drive"
    (drive / "sub").mkdir(parents=True)
    (drive / "a.txt").write_bytes(b"abc")
    (drive / "sub" / "b.txt").write_bytes(b"12345")
    make_dir_link(drive / "linked", _outside(tmp_path))

    assert personal_drive_bytes(MODEL, MemoryManager()) == 8, LINK_FOLLOWED


def test_a_linked_drive_root_has_no_size(zone_data_root: Path, tmp_path: Path) -> None:
    (zone_data_root / "custom" / "M1").mkdir(parents=True)
    make_dir_link(zone_data_root / "custom" / "M1" / "drive", _outside(tmp_path))

    with expect_zone_refusal("ZoneLinkRefusedError"):
        assert personal_drive_bytes(MODEL, MemoryManager()) is None, LINK_FOLLOWED


def test_a_missing_drive_is_empty(zone_data_root: Path) -> None:
    assert personal_drive_bytes(MODEL, MemoryManager()) == 0

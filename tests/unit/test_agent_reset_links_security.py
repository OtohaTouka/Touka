"""重置不跟隨代理人個人空間裡的連結（ADR-0293 R11）。

代理人可以把自己的個人空間根、``outputs``、``missions`` 或 ``.touka`` 換成連結，指到假的設定
資料夾；以前重置解析連結後清另一端。現在：根是連結時整個重置失敗、什麼都不動（不停心跳、不停
指令、不清任何 store）；``outputs``／``missions`` 是連結時只刪連結本身；``.touka`` 是連結時目錄檔
那一項失敗、不刪另一端。

用真的重置協調器與清除原語，只把守衛換成直接放行、store 清單縮成和個人空間有關的幾個再加一個
代表其他 store 的記錄者，只用改之前就有的邊界（R15）。data 根換到 ``zone_data_root``
（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P
from src.tools import terminal

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path

ADMIN = AgentIdentity.for_role(agent_id="admin1", role="admin")
AGENT = "M1"  # 自訂模型：個人空間是 custom/M1/drive
_DRIVE_STORES = {"outputs", "mission", "catalog"}


def _snapshot(root: Path) -> list[tuple[str, str]]:
    return sorted(
        (p.relative_to(root).as_posix(), p.read_text(encoding="utf-8"))
        for p in root.rglob("*")
        if p.is_file()
    )


def _fake_config(tmp_path: Path) -> Path:
    fake = tmp_path.resolve() / "fake_config"
    for rel in ("outputs/report.txt", "missions/current_mission.json", ".touka/catalog.json"):
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        (fake / rel).write_text("keep", encoding="utf-8")
    (fake / "users.json").write_text("{}", encoding="utf-8")
    return fake


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """守衛放行、store 只留個人空間的；記下叫停心跳、叫停指令與清其他 store 的順序."""
    seen: list[str] = []

    async def _other_purge(_candidate, _guard) -> int:  # 代表 sessions、記憶等其他 store
        seen.append("other_store")
        return 0

    async def _nothing_left(_candidate, _guard) -> int:
        return 0

    async def _stop_commands(identity: AgentIdentity) -> int:
        seen.append(f"stop_commands:{identity.agent_id}")
        return 0

    def _stop_heartbeats(_target: AgentIdentity) -> list[str]:
        seen.append("stop_heartbeats")
        return []

    drive_stores = tuple(s for s in P.ALL_STORES if s.name in _DRIVE_STORES)
    other = P.StorePurger("other", _other_purge, _nothing_left)
    monkeypatch.setattr(O, "assert_agent_resettable", lambda candidate, _actor: candidate)
    monkeypatch.setattr(O, "ALL_STORES", (*drive_stores, other))
    monkeypatch.setattr(O.destructive_ops, "stop_heartbeats", _stop_heartbeats)
    monkeypatch.setattr(terminal, "stop_agent_commands", _stop_commands, raising=False)
    return seen


def _drive(zone_data_root: Path) -> Path:
    return zone_data_root / "custom" / AGENT / "drive"


async def test_a_linked_drive_fails_the_reset_and_changes_nothing(
    zone_data_root: Path, tmp_path: Path, calls: list[str]
):
    fake = _fake_config(tmp_path)
    before = _snapshot(fake)
    _drive(zone_data_root).parent.mkdir(parents=True)
    make_dir_link(_drive(zone_data_root), fake)

    try:
        await O.reset_agent_data(AGENT, ADMIN, do_backup=False)
    except ToukaException as exc:
        refused: ToukaException | None = exc
    else:
        refused = None

    assert _snapshot(fake) == before, LINK_FOLLOWED
    assert refused is not None and refused.code.name == "AGENT_DRIVE_LINKED"
    assert calls == []  # 沒叫停心跳與指令，也沒清任何 store
    assert os.path.lexists(_drive(zone_data_root))  # 連結本身也留著


async def test_linked_outputs_and_missions_are_removed_as_links(
    zone_data_root: Path, tmp_path: Path, calls: list[str]
):
    fake = _fake_config(tmp_path)
    before = _snapshot(fake)
    drive = _drive(zone_data_root)
    drive.mkdir(parents=True)
    make_dir_link(drive / "outputs", fake / "outputs")
    make_dir_link(drive / "missions", fake / "missions")

    report = await O.reset_agent_data(AGENT, ADMIN, do_backup=False)

    assert _snapshot(fake) == before, LINK_FOLLOWED
    assert not os.path.lexists(drive / "outputs")
    assert not os.path.lexists(drive / "missions")
    assert report.clean is True
    assert calls == ["stop_heartbeats", f"stop_commands:{AGENT}", "other_store"]


async def test_a_linked_catalog_folder_is_not_followed(
    zone_data_root: Path, tmp_path: Path, calls: list[str]
):
    fake = _fake_config(tmp_path)
    before = _snapshot(fake)
    drive = _drive(zone_data_root)
    drive.mkdir(parents=True)
    make_dir_link(drive / ".touka", fake / ".touka")

    with expect_zone_refusal("ZoneLinkRefusedError"):
        report = await O.reset_agent_data(AGENT, ADMIN, do_backup=False)

    assert _snapshot(fake) == before, LINK_FOLLOWED
    catalog = next(s for s in report.stores if s.name == "catalog")
    assert catalog.error
    assert report.clean is False

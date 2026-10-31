"""技能 id 只經一個函式（ADR-0293 PR 2，P9）。

技能 id 會成為執行器掛載根 ``mcp_servers/managed`` 的第一層；第一層的名稱落在 kernel 的資料根
名稱或 Touka 標記（``sandbox_kernel.spec`` 的兩份名單）時，執行器判 ``touka_root``、不服務。
``utils/isolation_policy.is_skill_segment`` 直接引用那兩份名單；演化的 ``validate_skill_id`` 與
ResourceManager 的 ``tool_root`` 都只經它。只用 ``zone_data_root``，不碰 ``data/``。
"""

from __future__ import annotations

import pytest
from src.sandbox_kernel.spec import DATA_ROOT_NAMES, TOUKA_MARKERS
from src.services.isolation import evolution_command
from src.services.isolation.evolution_command import EvolutionRefused
from src.services.resource_manager import resource_manager
from src.tools.builtin import evolution
from src.utils.isolation_policy import is_skill_segment

NINE = {
    "soul", "personal_drive", "ui_sessions.db",
    "toukaproxy", "toukamemory", "toukaheartbeat", "toukadispatch", "heartbeat-config",
    "docker-compose.ghcr.yml",
}
# 兩個含點的本來就不合 SKILL_ID_PATTERN；要新擋的是這 7 個
SEVEN = sorted(name for name in NINE if "." not in name)


def _variants(name: str) -> list[str]:
    return [name, name.upper(), name.title(), name[0].upper() + name[1:]]


def test_kernel_name_lists_are_the_nine_names():
    assert DATA_ROOT_NAMES | TOUKA_MARKERS == NINE
    assert len(SEVEN) == 7


@pytest.mark.parametrize("name", [v for n in SEVEN for v in _variants(n)])
def test_kernel_names_are_not_skill_ids(name, zone_data_root):
    assert is_skill_segment(name) is False
    with pytest.raises(EvolutionRefused, match="invalid_skill_id"):
        evolution_command.validate_skill_id(name)
    assert resource_manager.tool_root(name) is None


@pytest.mark.parametrize("name", ["ui_sessions.db", "docker-compose.ghcr.yml", "con", "COM1"])
def test_other_refused_names(name, zone_data_root):
    assert is_skill_segment(name) is False
    with pytest.raises(EvolutionRefused, match="invalid_skill_id"):
        evolution_command.validate_skill_id(name)
    assert resource_manager.tool_root(name) is None


@pytest.mark.parametrize(
    "name", ["sample", "soul2", "my-soul", "personal-drive", "toukaproxy2", "COM10"]
)
def test_similar_names_are_still_skill_ids(name, zone_data_root):
    assert is_skill_segment(name) is True
    evolution_command.validate_skill_id(name)
    assert resource_manager.tool_root(name) is not None


@pytest.mark.parametrize("name", [None, 5, b"soul", ["soul"]])
def test_non_strings_are_not_skill_ids(name):
    assert is_skill_segment(name) is False


async def test_evolution_refuses_a_kernel_name_before_writing(zone_data_root):
    result = await evolution.propose_new_capability("Soul", "N", "D", {})
    assert '"invalid_skill_id"' in result
    assert not (zone_data_root / "mcp_servers").exists()

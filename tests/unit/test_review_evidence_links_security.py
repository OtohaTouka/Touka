"""互審蒐證不跟隨連結（ADR-0293）。

代理人的指令可以把個人空間裡證據檔所在的資料夾換成連結，指到假的設定資料夾；以前用 ``is_file``
跟隨連結，另一端的檔案會被當成證據。現在這一輪以固定原因轉人（``BLOCKED_FOR_HUMAN``），證據
摘要不列另一端的檔案。

經 Proxy 的 ``mission_tools(request_review)`` 跑，用真的 ``MissionManager``、``ResourceManager``
與蒐證，只用改之前就有的邊界（R15）。data 根換到 ``zone_data_root``（``tmp_path``），
不碰 ``data/``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.tools.mission_tools import mission_tools

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")


async def test_evidence_behind_a_linked_folder_is_refused(zone_data_root: Path, tmp_path: Path):
    drive = zone_data_root / "personal_drive" / "Alice"
    drive.mkdir(parents=True)
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "report.md").write_text('{"hooks": "CANARY"}', encoding="utf-8")
    manager = MissionManager()
    await manager.initialize_mission(
        identity=ALICE,
        goal="產出報告",
        initial_tasks=[
            {
                "id": "t1",
                "description": "寫 out/report.md",
                "review_required": True,
                "artifact_refs": ["out/report.md"],
            }
        ],
    )
    await manager.update_task(identity=ALICE, task_id="t1", status=TaskStatus.DOING)
    make_dir_link(drive / "out", fake)

    out = str(
        await mission_tools(action="request_review", task_id="t1", identity=ALICE, _ctx=None)
    )
    assert "out/report.md✓" not in out, LINK_FOLLOWED
    assert "BLOCKED_FOR_HUMAN" in out
    assert "連結" in out

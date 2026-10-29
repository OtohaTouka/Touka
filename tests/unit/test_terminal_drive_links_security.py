"""終端不在被換成連結的個人空間裡執行（ADR-0293）。

代理人的指令可以把自己的個人空間換成連結，指到假的設定資料夾；以前終端解析連結後把另一端當
cwd，指令就在那裡跑。現在不執行，回固定原因的 ``permission_denied``，不帶路徑。

用真的 ``execute_command`` 與 ``ResourceManager``，只把 spawn 換成記錄 cwd 的替身，只用改之前就有
的邊界（R15）。data 根換到 ``zone_data_root``（``tmp_path``），不碰 ``data/``。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from src.models.identity import AgentIdentity
from src.tools import terminal
from src.utils.async_db import session_service

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

ALICE = AgentIdentity.for_role("Alice", "Owner", capabilities=[])


class _DoneProc:
    returncode = 0

    def __init__(self) -> None:
        self.stdout = self

    async def readline(self) -> bytes:
        return b""

    async def wait(self) -> int:
        return 0


async def test_a_command_in_a_linked_drive_is_not_run(
    zone_data_root: Path, tmp_path: Path, monkeypatch
):
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (zone_data_root / "personal_drive").mkdir()
    make_dir_link(zone_data_root / "personal_drive" / "Alice", fake)
    cwds: list[Any] = []

    async def _spawn(*_argv: Any, **kwargs: Any) -> _DoneProc:
        cwds.append(kwargs.get("cwd"))
        return _DoneProc()

    monkeypatch.setattr(terminal, "spawn_exec", _spawn)
    monkeypatch.setattr(session_service, "get_session_by_id", AsyncMock(return_value=None))
    out = await terminal.execute_command(
        "ls", ALICE, auth_level="Manual", _hitl_preapproved=True, session_id="s1",
        _call_id="c1", _ctx={"identity": ALICE},
    )
    assert cwds == [], LINK_FOLLOWED
    assert json.loads(out)["error_class"] == "permission_denied"
    assert str(fake) not in out

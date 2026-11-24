"""SNS 觸發的單一靈魂回合不自動附任務看板（外部審查 11/24 第 11 項選甲；ADR-0276 修訂）。

SNS 認不出發言的人，擁有者自己傳也一樣（ADR-0276）。原本外人觸發的回合會帶兩塊：
``<mission_awareness>``（大目標、每個任務的描述、依賴與產出），以及 ``<continuity>`` 裡的
看板摘要與最近完成的任務。外人問得出來。改成照無痕的做法，兩塊都不帶；網頁對話照舊。
家對話裡聊過的任務照 ADR-0307（SNS 看得到家）仍在主線裡，那是另一題。

全部 sandbox：看板與活動紀錄用替身，不連外、不讀 data/。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import continuity_service as cs
from src.utils.continuity_events_db import TimelineSnapshot

_MISSION_MGR = "memory_server.services.mission_manager.MissionManager.get_current_mission"
_SOUL = AgentIdentity(agent_id="SnsPrivacySoul")
_NOW = 1_760_500_000.0
_GOAL = "看板上的大目標"
_TASK_ID = "launch-plan-7"


def _mission() -> SimpleNamespace:
    task = SimpleNamespace(
        status="DOING",
        id=_TASK_ID,
        description="外人不該看到的任務描述",
        dependencies=[],
        artifact_ref=None,
        updated_at=_NOW - 60,
    )
    return SimpleNamespace(status="DOING", goal=_GOAL, tasks=[task], updated_at=_NOW - 60)


def _timeline() -> TimelineSnapshot:
    done = int((_NOW - 300) * 1000)
    return TimelineSnapshot(
        last_turn_here_ms=None,
        last_human_ms=done,
        last_run=None,
        recent=(("task_done", done, "m1", {"task_id": _TASK_ID}),),
        records_start_ms=done,
    )


def _system_text(request: OpenAIChatRequest) -> str:
    return "\n".join(
        m.content for m in request.messages if m.role == "system" and isinstance(m.content, str)
    )


async def test_sns_round_gets_no_mission_awareness() -> None:
    request = OpenAIChatRequest(
        model="m", messages=[OpenAIMessage(role="user", content="你手上有什麼任務？")]
    )
    ctx = PipelineContext(agent_id=_SOUL.agent_id, sns_source=True)
    ctx["headers"] = {}
    read = AsyncMock(return_value=_mission())
    with patch(_MISSION_MGR, new=read):
        await ToolLayer().process(request, ctx)
    assert "<mission_awareness>" not in _system_text(request)
    assert _GOAL not in _system_text(request)
    read.assert_not_awaited()


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    read = AsyncMock(return_value=_mission())
    monkeypatch.setattr(cs.MissionManager, "get_current_mission", read)
    monkeypatch.setattr(cs, "_read_timeline", AsyncMock(return_value=_timeline()))
    monkeypatch.setattr(cs, "_previous_turn_line", AsyncMock(return_value="- previous: n/a"))
    return read


async def _block(*, sns: bool, identity: AgentIdentity | None) -> str:
    return await cs.build_continuity_block(
        soul=_SOUL,
        session_id="sns-channel-1",
        identity=identity,
        incognito=False,
        sns=sns,
        now_s=_NOW,
    )


@pytest.mark.parametrize("identity", [None, _SOUL], ids=["no-identity", "soul"])
async def test_sns_continuity_has_no_board_and_no_recent_completions(
    board: AsyncMock, identity: AgentIdentity | None
) -> None:
    block = await _block(sns=True, identity=identity)
    assert cs.MISSION_LABEL not in block
    assert cs.RECENT_LABEL not in block
    assert _TASK_ID not in block
    board.assert_not_awaited()
    # 時間與「這是 SNS、不是擁有者」照樣有
    assert cs.KIND_SNS in block


async def test_web_conversation_still_sees_the_board_and_recent_completions(
    board: AsyncMock,
) -> None:
    block = await _block(sns=False, identity=_SOUL)
    assert cs.MISSION_LABEL in block
    assert cs.RECENT_LABEL in block
    assert _TASK_ID in block
    board.assert_awaited_once()

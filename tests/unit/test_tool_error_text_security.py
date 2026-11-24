"""工具出例外時，錯誤文字先遮掉金鑰與本機路徑再交出去（禁令 23；外部審查 11/24 第 2 項）。

例外原文有三個去處：畫面上的錯誤事件、這一輪給模型的結果（存下的步驟與下一輪的工具紀錄
跟著它）、小提示。三處都先消毒再截斷：機密剛好跨過截斷的位置時，也不會只留下半段沒遮。

全部 sandbox：假工具、假政策，不連外、不讀 data/。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyDecision
from src.services.permission_service import permission_service
from src.services.tool_executor import ToolExecutor
from src.utils.tool_events import TOOL_ERROR
from src.utils.tool_step_collector import ToolStepCollector

if TYPE_CHECKING:
    from pathlib import Path

_SOUL = AgentIdentity.for_role(agent_id="ErrTextProbe", role="Owner", is_human=False)
_KEY = "sk-" + "Q7rT2mX9" * 4
_TOKEN = "eyJhbGciOiJIUzI1NiJ9" + "Zk3pW8qL" * 3  # 40 字以上、只有英數：通用機密樣式
_WAIT_S = 5.0


def _raising(message: str):
    async def boom(**_kwargs: Any) -> str:
        raise RuntimeError(message)

    return boom


async def _run(monkeypatch: pytest.MonkeyPatch, message: str, *, for_model: bool) -> tuple[
    str, list[dict[str, Any]]
]:
    monkeypatch.setattr(
        permission_service,
        "evaluate_policy",
        lambda _ctx: PolicyDecision(effect="ALLOW", reason="test-allow"),
    )
    executor = ToolExecutor(
        protocol_manager=None,
        tool_registry={"ext_review_boom": _raising(message)},
        memory_tool_registry={},
        plugin_registry={},
        enforce_advertised=False,
    )
    ctx = {"identity": _SOUL, "auth_level": "YOLO", "session_id": None}
    queue: asyncio.Queue = asyncio.Queue()
    async with asyncio.timeout(_WAIT_S):
        result = await executor.execute_to_queue(
            "ext_review_boom", {"q": "x"}, "req-err", ctx, queue, "call-err", for_model=for_model
        )
    events = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)
    return result, events


def _everything_shown(result: str, events: list[dict[str, Any]]) -> list[str]:
    """模型拿到的、畫面上的每個事件、存下的步驟。"""
    collector = ToolStepCollector()
    for event in events:
        collector.feed(event)
    return [result, *(str(event) for event in events), str(collector.steps)]


@pytest.mark.parametrize("for_model", [True, False])
async def test_exception_text_is_sanitized_everywhere_it_goes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, for_model: bool
) -> None:
    # 這台機器真的本機絕對路徑（Windows 上是磁碟代號開頭，CI 的 Linux 上是 / 開頭），兩種寫法
    secret_file = tmp_path / "secret-plan" / "config.yaml"
    message = (
        f"upstream said Authorization: Bearer {_KEY} while reading {secret_file} "
        f"and {secret_file.as_posix()}"
    )
    result, events = await _run(monkeypatch, message, for_model=for_model)
    assert any(e.get("type") == TOOL_ERROR for e in events)
    for shown in _everything_shown(result, events):
        assert _KEY not in shown
        assert _KEY[:12] not in shown
        assert "secret-plan" not in shown
        assert tmp_path.name not in shown
    # 錯誤類別與可讀的原因仍然看得到
    assert "RuntimeError" in result


async def test_a_secret_crossing_the_old_cut_is_still_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """舊做法先截 200 字再遮：機密剛好跨過第 200 字時，前半段已經不像機密、遮不到。"""
    message = "word " * 37 + _TOKEN  # 機密從第 185 字開始，舊做法只留前 15 字
    assert message.index(_TOKEN) == 185
    result, events = await _run(monkeypatch, message, for_model=True)
    for shown in _everything_shown(result, events):
        assert _TOKEN[:12] not in shown

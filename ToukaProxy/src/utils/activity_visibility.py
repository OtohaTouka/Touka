"""依身分過濾工作紀錄：/stats 與 /ui/activity 共用（ADR-0300）。"""

from collections.abc import Callable


def agent_visibility(can_access: Callable[[str | None], bool]) -> Callable[[object], bool]:
    """這次請求用的判斷：每個靈魂只問一次；對不到靈魂的紀錄用 ``can_access(None)`` 的答案。"""
    visible: dict[str, bool] = {}

    def can_see(agent: object) -> bool:
        if not isinstance(agent, str) or not agent:
            return can_access(None)
        if agent not in visible:
            visible[agent] = can_access(agent)
        return visible[agent]

    return can_see


def record_agent(record: object) -> object:
    """紀錄屬於哪個靈魂；形狀不對就是沒有。"""
    return record.get("agent") if isinstance(record, dict) else None

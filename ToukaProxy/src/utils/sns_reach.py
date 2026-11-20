"""SNS 外人能叫到哪些靈魂（ADR-0276 §4、ADR-0327）.

SNS 進來的請求沒有呼叫者身分，只有機器人綁的靈魂（來源）。外人在訊息裡點到別的靈魂時，
轉給別的靈魂（閘道）與「@名字」群聊（群聊分發器）都用這一條判斷。

和 ``authorization_gateway.shares_human_owner`` 不同：這裡多了「同一個靈魂直接成立」與
「來源要恰好一位擁有者」（兩位以上擁有者時分不出是誰的機器人，只能叫它自己），不能合併。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.models.user import User


def sns_may_reach(source_id: str, target_id: str, users: Sequence[User]) -> bool:
    """機器人綁的靈魂 ``source_id`` 的外人能不能叫到 ``target_id``.

    同一個靈魂一律可以；否則 ``source_id`` 要恰好一位人類擁有者，而且這位也擁有
    ``target_id``。兩個編號都要先用 ``agent_service.identify_agent`` 正式化，這裡逐字比對；
    停權的使用者照算（和閘道原本的判斷一樣）。
    """
    if source_id == target_id:
        return True
    owners = [user for user in users if source_id in user.owned_agents]
    return len(owners) == 1 and target_id in owners[0].owned_agents

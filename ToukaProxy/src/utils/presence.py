"""在場判斷（ADR-0246 U2c-2）：這次 run 有沒有人能回答審批卡。純函式（禁令 18）。"""

from typing import Any

# 這些 run 都沒有人在場：子代理只收文字（卡會被吞掉）、派工是代理人交辦、心跳是排程。
# 判斷只看伺服器設定的情境標記，不看角色名稱。
HEADLESS_RUN_FLAGS = ("subagent_run", "dispatch_run", "heartbeat_run")


def is_human_present(ctx: Any) -> bool:
    """有人在場＝互動 UI 對話（``resumable_stream``），而且不是上面任何一種無人 run。"""
    if not ctx.get("resumable_stream"):
        return False
    return not any(ctx.get(flag) for flag in HEADLESS_RUN_FLAGS)

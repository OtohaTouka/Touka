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


# 沒人在場時，需要核准的動作交給誰（<approval_context> 與被擋建議共用，ADR-0316）
_SUBAGENT_FOLLOWUP = (
    "put the action in your report to the agent that dispatched you, so it can get approval"
)
_NO_HUMAN_FOLLOWUP = (
    "note the action in your reply or on your mission board, so a human can handle it "
    "when they return"
)


def no_human_followup(ctx: Any) -> str:
    """子代理交回派它的主代理；其他沒人在場的（心跳、派工）寫進回覆或看板。"""
    return _SUBAGENT_FOLLOWUP if ctx.get("subagent_run") else _NO_HUMAN_FOLLOWUP


def blocked_call_suggestion(ctx: Any) -> str:
    """沒人在場、動作被擋時的建議（只換 ``suggestion``，拒絕與 ``reason`` 不變）。"""
    return f"Do not retry or work around this call; {no_human_followup(ctx)}."

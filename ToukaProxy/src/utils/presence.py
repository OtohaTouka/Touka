"""在場判斷（ADR-0246 U2c-2）：這次 run 有沒有人能回答審批卡。純函式（禁令 18）。

子代理在專用空間的專案裡跑指令的窄開關（ADR-0333）也在這裡判斷：派出時算一次
（``subagent_project_commands``），結果由伺服器寫進子代理的 ctx，模型改不到。
"""

from typing import Any

from src.utils.auth_mode import AUTO_MODES, normalize_auth_mode

# 這些 run 都沒有人在場：子代理只收文字（卡會被吞掉）、派工是代理人交辦、心跳是排程。
# 判斷只看伺服器設定的情境標記，不看角色名稱。
HEADLESS_RUN_FLAGS = ("subagent_run", "dispatch_run", "heartbeat_run")

# 子代理 ctx 裡由伺服器寫入的欄位（ADR-0333；``subagent_tools._mark_subagent_run`` 寫）
SUBAGENT_PROJECT_COMMANDS = "subagent_project_commands"  # 這個子代理能在專案裡跑指令
PARENT_SESSION_ID = "parent_session_id"  # 派它的對話（擋下次數合併計算、停止）
PARENT_STREAM_ID = "parent_stream_id"  # 派它的那一輪的回覆串流（那一輪結束就不能再跑）
PARENT_IDENTITY = "parent_identity"  # 派它的靈魂（審查員的費用記在這裡）
# 這一輪自己的回覆串流（dispatcher 建好串流緩衝後寫；沒有串流的請求沒有）
TURN_STREAM_ID = "turn_stream_id"
# 派它的那一輪結束、指令被收掉或不開時給模型看的原因碼（政策與終端共用）
PARENT_TURN_ENDED = "parent_turn_ended"


def is_human_present(ctx: Any) -> bool:
    """有人在場＝互動 UI 對話（``resumable_stream``），而且不是上面任何一種無人 run。"""
    if not ctx.get("resumable_stream"):
        return False
    return not any(ctx.get(flag) for flag in HEADLESS_RUN_FLAGS)


def subagent_project_commands(
    parent_ctx: Any, *, dedicated: bool, read_only: bool, stream_id: str | None
) -> bool:
    """派出時判斷：這個子代理能不能在專案裡跑指令（ADR-0333）。

    全部成立才算：派它的那一輪有人在場、有對話（停止監看看得到）、有回覆串流（``stream_id``）、
    不是無痕、派出時的模式是 Auto 或 YOLO（Manual 要逐條按卡，子代理那邊沒有卡）；專案在專用空間
    （``dedicated``）；不是只能看。
    """
    return (
        is_human_present(parent_ctx)
        and bool(parent_ctx.get("session_id"))
        and bool(stream_id)
        and not parent_ctx.get("incognito")
        and normalize_auth_mode(parent_ctx.get("auth_level")) in AUTO_MODES
        and dedicated
        and not read_only
    )


def commands_attended(ctx: Any) -> bool:
    """專用空間的專案指令算不算「有人在場」：有人在場，或是窄開關打開的子代理（ADR-0333）。"""
    if is_human_present(ctx):
        return True
    return ctx.get("subagent_run") is True and ctx.get(SUBAGENT_PROJECT_COMMANDS) is True


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
    """動作被擋、而且沒有核准通道時的建議（只換 ``suggestion``，拒絕與 ``reason`` 不變）。

    執行器的三處只在沒人在場時走到；終端在 YOLO 串接指令時有人在場也會走到，這時說中性的話。
    """
    if is_human_present(ctx):
        return (
            "Do not retry or work around this call; tell the user what it would do so "
            "they can decide or run it themselves."
        )
    return f"Do not retry or work around this call; {no_human_followup(ctx)}."

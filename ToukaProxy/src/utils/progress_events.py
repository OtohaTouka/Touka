"""進度事件 (touka_progress) 純函式建構器 (SSOT)。

統一 Agent「複數行動」過程中各 emit 點的結構化進度事件形狀，讓前端
useProgress 能依 `kind` 分流渲染（ReAct 自主迴圈、終端自我修復、群聊
roster、創建流水線、工具內部重試）。

設計原則（禁令18：跨切面純函式抽離，可單測）：
- 全部為無副作用純函式，每次回傳全新 dict。
- 外層統一為 {"type": "touka_progress", "kind": <str>, ...}。
- 由呼叫端決定傳輸方式：handler/dispatcher 以 ``yield f"data: {json.dumps(ev)}\\n\\n"``
  推送；tool_executor 則 ``await queue.put(ev)``。
"""
from __future__ import annotations

# SSE 事件外層 type（前端 connections.ts customEventTypes 需對應註冊）。
PROGRESS_EVENT_TYPE = "touka_progress"


def _progress(kind: str, **fields: object) -> dict[str, object]:
    """組裝統一外層的進度事件。

    Args:
        kind (str): 進度種類（react_slots / repair / group / creation_stage / tool_retry）。
        **fields (object): 該種類專屬欄位。

    Returns:
        dict[str, object]: 結構化進度事件。
    """
    return {"type": PROGRESS_EVENT_TYPE, "kind": kind, **fields}


def react_slots_event(current: int, total: int) -> dict[str, object]:
    """ReAct 自主迴圈進度。

    Args:
        current (int): 已消耗的自主回合數（1-based，1 表示正進入第一個自主回合）。
        total (int): 本次會話的自主行動總額（初始 slots）。

    Returns:
        dict[str, object]: react_slots 進度事件。
    """
    return _progress("react_slots", current=current, total=total)


def repair_event(attempt: int, max_attempts: int) -> dict[str, object]:
    """終端指令自我修復進度。

    Args:
        attempt (int): 1-based 目前修復嘗試序。
        max_attempts (int): 最大修復嘗試次數。

    Returns:
        dict[str, object]: repair 進度事件。
    """
    return _progress("repair", attempt=attempt, max=max_attempts)


def group_event(index: int, total: int, agent: str) -> dict[str, object]:
    """群聊 roster 進度：正在處理第 index/total 位成員。

    Args:
        index (int): 1-based 目前成員序。
        total (int): roster 成員總數。
        agent (str): 成員顯示名稱或 ID（不寫死，由呼叫端傳入）。

    Returns:
        dict[str, object]: group 進度事件。
    """
    return _progress("group", index=index, total=total, agent=agent)


def creation_stage_event(
    stage: str, status: str, target_kind: str = ""
) -> dict[str, object]:
    """創建流水線階段進度。

    Args:
        stage (str): 階段名（VALIDATE / SMOKE / COMMIT / EVAL）。
        status (str): 階段狀態（running / ok / failed）。
        target_kind (str): 創建標的種類（如 skill / mcp），可選。

    Returns:
        dict[str, object]: creation_stage 進度事件。
    """
    return _progress("creation_stage", stage=stage, status=status, target_kind=target_kind)


def tool_retry_event(tool: str, attempt: int, max_attempts: int) -> dict[str, object]:
    """工具內部重試進度（如 research 模型 fallback）。

    Args:
        tool (str): 工具名稱。
        attempt (int): 1-based 目前嘗試序。
        max_attempts (int): 最大嘗試次數。

    Returns:
        dict[str, object]: tool_retry 進度事件。
    """
    return _progress("tool_retry", tool=tool, attempt=attempt, max=max_attempts)


def token_usage_event(
    total_tokens: int,
    cost_usd: float,
    usage_pct: float | None = None,
    context_breakdown: dict[str, object] | None = None,
    estimated: bool = False,
) -> dict[str, object]:
    """本回合 token 用量與估算成本（供活動列即時顯示資源耗費）。

    Args:
        total_tokens (int): 本回合輸入＋輸出 token 總數。
        cost_usd (float): 本回合估算成本（USD）。
        usage_pct (float | None): context window 占用百分比（input_tokens / 模型 window），
            供 ContextMeter 顯示（ADR-0092 Phase B）。None 時不附加。
        context_breakdown (dict | None): 分類別用量分解（{usage_pct, window, total_tokens,
            breakdown:{category:tokens}}），供 Claude Code /context 風格分解條。None 時不附加。
        estimated (bool): 含沒完成那一輪的估計用量（3b 裁定 6）；True 才附加 ``estimated``。

    Returns:
        dict[str, object]: token_usage 進度事件。
    """
    ev = _progress("token_usage", total=total_tokens, cost=cost_usd)
    if usage_pct is not None:
        ev["usage_pct"] = usage_pct
    if context_breakdown is not None:
        ev["context_breakdown"] = context_breakdown
    if estimated:
        ev["estimated"] = True
    return ev

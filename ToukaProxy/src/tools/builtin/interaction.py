"""互動類內建工具（HAX G10，ADR-0085）：ask_user 向使用者發問。

AI 不確定、需使用者決定時呼叫此工具：emit `ask_user_request` 事件 → 前端彈窗作答 →
經 ask_user_store 解除等待 → 回傳格式化答案供 AI 續跑。無互動介面回哨兵，不卡死；
逾時或什麼都沒選算沒回答，這一輪停下等使用者（``utils/ask_user_stop.py``）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.services.ask_user_store import get_ask_user_store
from src.services.hooks.notifications import notify_agent_question
from src.utils.ask_user_stop import is_blank_answer, mark_answered, mark_unanswered

logger = logging.getLogger(__name__)

ASK_USER_TIMEOUT = 600.0
_NO_ANSWER = (
    "使用者未在時限內回答（或目前無互動介面）。"
    "回答前不得代為決定；需要這個答案的部分先不要做。"
)


def _wait_seconds(value: Any) -> float:
    """等待秒數：模型可以縮短，不能拉長；不是數字就用預設（發問前算好，不留下沒人等的問題）。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return ASK_USER_TIMEOUT
    return seconds if 0 <= seconds <= ASK_USER_TIMEOUT else ASK_USER_TIMEOUT


async def ask_user(
    questions: list[dict[str, Any]] | None = None,
    _emit_event: Any = None,
    **kwargs: Any,
) -> str:
    """向使用者提出結構化問題（單選/複選/連續多問題），暫停生成等回答後續跑。

    Returns:
        格式化答案 JSON 字串（[{question, answer:[…]}]）；逾時/無管道回哨兵文字。
    """
    if not questions or not isinstance(questions, list):
        return "Error: ask_user 需要至少一個問題（questions 陣列）。"
    if _emit_event is None:
        # 無事件管道（非互動情境）→ 立即回哨兵，不阻塞
        return _NO_ANSWER

    timeout = _wait_seconds(kwargs.get("_timeout"))
    store = get_ask_user_store()
    # 不用工具呼叫編號：有些模型是流水號，猜得到，兩段對話同時問也會撞在一起（ADR-0300）
    token = store.generate_token()
    store.register(token)
    await _emit_event({"type": "ask_user_request", "id": token, "questions": questions})
    ctx = kwargs.get("_ctx")
    notify_agent_question(ctx or {})

    result = await store.wait_for_answer(token, timeout=timeout)
    answers = result.get("answers")
    if is_blank_answer(answers):
        logger.info("ask_user got no answer (token=%s…): %s", token[:8], result.get("status"))
        mark_unanswered(ctx, questions)
        return _NO_ANSWER

    formatted = [
        {
            "question": q.get("question", f"Q{idx + 1}"),
            "answer": answers[idx] if idx < len(answers) else [],
        }
        for idx, q in enumerate(questions)
    ]
    mark_answered(ctx, formatted)
    return json.dumps(formatted, ensure_ascii=False)

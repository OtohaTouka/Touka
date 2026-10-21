"""互動類內建工具（HAX G10，ADR-0085）：ask_user 向使用者發問。

AI 不確定、需使用者決定時呼叫此工具：emit `ask_user_request` 事件 → 前端彈窗作答 →
經 ask_user_store 解除等待 → 回傳格式化答案供 AI 續跑。逾時/無互動介面回哨兵，不卡死。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.services.ask_user_store import get_ask_user_store

logger = logging.getLogger(__name__)

ASK_USER_TIMEOUT = 600.0
_NO_ANSWER = (
    "使用者未在時限內回答（或目前無互動介面）。"
    "請在沒有此資訊的情況下，以你最合理的判斷繼續。"
)


async def ask_user(
    questions: list[dict[str, Any]] | None = None,
    _emit_event: Any = None,
    _call_id: str | None = None,
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

    store = get_ask_user_store()
    token = _call_id or store.generate_token()
    store.register(token)
    await _emit_event({"type": "ask_user_request", "id": token, "questions": questions})

    timeout = float(kwargs.get("_timeout", ASK_USER_TIMEOUT))
    result = await store.wait_for_answer(token, timeout=timeout)
    answers = result.get("answers")
    if answers is None:
        logger.info("ask_user got no answer (token=%s…): %s", token[:8], result.get("status"))
        return _NO_ANSWER

    formatted = [
        {
            "question": q.get("question", f"Q{idx + 1}"),
            "answer": answers[idx] if idx < len(answers) else [],
        }
        for idx, q in enumerate(questions)
    ]
    return json.dumps(formatted, ensure_ascii=False)

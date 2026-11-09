"""AI 問使用者問題沒等到回答時，這一輪要停下（ADR-0308）。

沒回覆不算同意：逾時或什麼都沒選時，``ask_user`` 在 ctx 記下還沒答的問題，
迴圈收完工具結果就結束這一輪，不再呼叫模型。對話裡留一段固定文字；
工具步驟不會帶到下一輪，所以問題、選項和這一輪已答的內容都要寫進去。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

UNANSWERED_KEY = "ask_user_unanswered"
ANSWERED_KEY = "ask_user_answered"

_NOTICE_HEAD = "\n\n⏸️ 這個問題沒有等到回答，這一輪先停在這裡。\n"
_NOTICE_FOOT = "\n（此問題尚未回答，回答前不得代為決定。）\n直接回覆這段對話，就會接著做。\n"
_FALLBACK_QUESTION = "（問題內容讀不到）"


def is_blank_answer(answers: Any) -> bool:
    """沒有答案，或每一題都是空的（只有全部空白才算沒回答，複選題可以合法不選）。"""
    if not isinstance(answers, list):
        return True
    return not any(
        str(choice).strip() for answer in answers if isinstance(answer, list) for choice in answer
    )


def mark_unanswered(ctx: Any, questions: list[Any]) -> None:
    """記下這一輪沒等到回答的問題；有記就會停下。"""
    if ctx is not None:
        ctx[UNANSWERED_KEY] = [*(ctx.get(UNANSWERED_KEY) or []), *questions]


def mark_answered(ctx: Any, answered: list[dict[str, Any]]) -> None:
    """記下這一輪已經回答的問題；由工具自己記，不解析結果文字（會被加時間尾綴）。"""
    if ctx is not None:
        ctx[ANSWERED_KEY] = [*(ctx.get(ANSWERED_KEY) or []), *answered]


def _question_lines(index: int, question: Any) -> list[str]:
    if not isinstance(question, Mapping):
        return [f"{index}. {question or _FALLBACK_QUESTION}"]
    lines = [f"{index}. {question.get('question') or _FALLBACK_QUESTION}"]
    options = question.get("options")
    if isinstance(options, list) and options:
        lines.append("   選項：" + "、".join(str(option) for option in options))
    return lines


def _answered_lines(answered: Iterable[Any]) -> list[str]:
    lines = []
    for item in answered:
        if isinstance(item, Mapping):
            value = item.get("answer")
            shown = "、".join(str(v) for v in value) if isinstance(value, list) else str(value)
            lines.append(f"- {item.get('question')}：{shown}")
    return lines


def _tool_names(used: Iterable[str]) -> list[str]:
    """工具名稱：拿掉 ``name.action`` 的重複項與提問工具本身，排序讓文字固定。"""
    return sorted({name for name in used if "." not in name and name != "ask_user"})


def ask_user_stop_text(ctx: Any, used: Iterable[str]) -> str | None:
    """沒有未答的問題回 None；有就回這一輪要留下的固定文字。組文字失敗也不能讓停下失敗。"""
    questions = ctx.get(UNANSWERED_KEY)
    if not questions:
        return None
    lines = [_NOTICE_HEAD, "問題："]
    for index, question in enumerate(questions, start=1):
        lines.extend(_question_lines(index, question))
    answered = _answered_lines(ctx.get(ANSWERED_KEY) or [])
    if answered:
        lines.extend(["", "這一輪已經回答的：", *answered])
    tools = _tool_names(used)
    if tools:
        lines.extend(["", "這一輪用過的工具：" + "、".join(tools)])
    return "\n".join(lines) + "\n" + _NOTICE_FOOT

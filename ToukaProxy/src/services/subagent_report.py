"""結構化子代理回報（in-process 子代理協調提案 Phase 2，需求 4）.

子代理最終輸出以「尾部 ```json 回報塊」攜帶 ``status``／``claims``／
``artifact_refs``／``confidence``，讓主 agent 拿到**可程式化裁決**的回報，
而非只有純文字摘要。

解析容錯（誠實 fallback）：弱模型不遵守格式（缺塊、壞 JSON、型別錯）時
一律回 ``status="unverified"``＋``structured=False``＋``summary=全文``——
**絕不假裝有結構**；欄位層級的型別錯（claims 非列表、confidence 非數字）
逐欄丟棄不整包作廢。傳輸層成敗（子 loop 是否跑完）與自我宣稱層
（模型說做完了）由 deliver payload 的 ``status`` vs ``report_status`` 分開承載。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

_VALID_STATUSES = frozenset({"done", "partial", "failed", "unverified"})
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)

# 子代理任務指示的回報格式要求（_subagent_user_prompt 附掛）。
REPORT_INSTRUCTION = (
    "完成任務後，請在回覆的「最後」附上一個回報塊，格式如下：\n"
    "```json\n"
    '{"status": "done|partial|failed", '
    '"claims": ["你完成或驗證了什麼（逐條、具體）"], '
    '"artifact_refs": ["產出檔案的相對路徑"], '
    '"confidence": 0.8}\n'
    "```\n"
    "status 請如實填寫（做完=done、部分=partial、失敗=failed）；"
    "confidence 為 0~1 的自評把握度。回報塊之前照常書寫你的結果全文。"
)


class SubagentReport(BaseModel):
    """子代理回報的結構化視圖（解析結果，一律可用——含 fallback 情形）."""

    model_config = ConfigDict(frozen=True)

    status: str = "unverified"
    claims: list[str] = []
    artifact_refs: list[str] = []
    confidence: float | None = None
    summary: str = ""
    structured: bool = False


def _coerce_str_list(value: Any) -> list[str]:
    """欄位級容錯：非列表→空；列表內非字串元素逐一丟棄。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _coerce_confidence(value: Any) -> float | None:
    """欄位級容錯：非數字→None；數字 clamp 到 [0, 1]。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(1.0, max(0.0, float(value)))


def parse_subagent_report(text: str) -> SubagentReport:
    """解析子代理最終輸出：取「最後一個」```json 塊為回報，其餘為敘事摘要.

    任何解析失敗（缺塊／壞 JSON／頂層非物件）→ 誠實 fallback：
    ``unverified``＋``structured=False``＋``summary=全文``。
    """
    raw = (text or "").strip()
    matches = list(_JSON_BLOCK_RE.finditer(raw))
    if not matches:
        return SubagentReport(summary=raw)

    last = matches[-1]
    try:
        data = json.loads(last.group(1))
    except (json.JSONDecodeError, ValueError):
        return SubagentReport(summary=raw)
    if not isinstance(data, dict):
        return SubagentReport(summary=raw)

    status = str(data.get("status", "")).strip().lower()
    if status not in _VALID_STATUSES:
        status = "unverified"

    summary = (raw[: last.start()] + raw[last.end() :]).strip()
    return SubagentReport(
        status=status,
        claims=_coerce_str_list(data.get("claims")),
        artifact_refs=_coerce_str_list(data.get("artifact_refs")),
        confidence=_coerce_confidence(data.get("confidence")),
        summary=summary,
        structured=True,
    )

"""工具結果給模型的上限（ADR-0323 前景-11）.

只截給模型的那一份：畫面的工具卡、成功判定、hook 與 ``/debug/execute`` 拿完整版。
超過上限時保留開頭與結尾（指令的開頭與最後的錯誤通常在這兩段），中間換成一行固定說明。只處理字串。
"""

from __future__ import annotations

# 給模型的工具結果最多幾個字（約 1 萬到 2 萬 token）
MAX_TOOL_RESULT_CHARS = 40_000
TRUNCATION_NOTE_TEMPLATE = (
    "〔工具結果過長，系統截斷：原本 {total} 字，略去中間 {omitted} 字；"
    "需要時用 view_range、背景執行或縮小範圍再查〕"
)


def cap_tool_result(text: str) -> str:
    """超過 ``MAX_TOOL_RESULT_CHARS`` 時保留頭尾各一半，中間換成截斷說明；沒超過原樣回傳。"""
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    keep = MAX_TOOL_RESULT_CHARS // 2
    note = TRUNCATION_NOTE_TEMPLATE.format(total=len(text), omitted=len(text) - 2 * keep)
    return f"{text[:keep]}\n{note}\n{text[-keep:]}"

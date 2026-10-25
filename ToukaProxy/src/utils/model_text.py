"""模型寫的文字進 prompt 前的單行與控制字元處理（ADR-0285，禁令 18）。純函式、沒有 I/O。

任務看板、前情摘要與家對話裡的文字是模型或舊紀錄寫的。區塊 tag 的中和在
``memory_server.utils.historical_data.neutralize_tags``；這裡只處理「看起來像換行」與
控制字元，必須在中和**之前**套用：中和之後才刪掉控制字元的話，``<``、NUL、``/tag>``
這種寫法會重組成真的 tag。
"""

from __future__ import annotations

import re

# 換行換成可見的符號：str.splitlines() 會切開的字元都算。
LINE_BREAK_MARK = chr(0x23CE)
_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85" + chr(0x2028) + chr(0x2029) + "]")
# 其他控制字元換成空白；保留 tab、換行、\x0b、\x0c 與 \r，讓多行的舊紀錄照樣是多行。
_CONTROL_CHAR = re.compile("[\x00-\x08\x0e-\x1f\x7f]")


def blank_control_chars(text: str) -> str:
    """控制字元換成空白；tab 與 LF、CR、VT、FF 保留，檔案／群組／記錄分隔字元也換成空白。"""
    return _CONTROL_CHAR.sub(" ", text)


def single_line(text: str) -> str:
    """換行換成 ``⏎``、其他控制字元換成空白，整段只會是一行。"""
    return blank_control_chars(_LINE_BREAK.sub(LINE_BREAK_MARK, text))

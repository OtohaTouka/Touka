"""工具紀錄的固定標記、防偽與文字清理（ADR-0319）。

紀錄是系統依存下來的工具步驟組的（``utils/tool_records.py``）。模型或外人也可能寫出長得一樣的東西，
所以每一則送出的舊訊息裡，紀錄開頭的標記「〔工具紀錄」都換成相近的「［工具紀錄」（比對時忽略空白與
看不見的字元）；只有系統組的那段是真的。這一輪的回覆裡出現這個標記，就是照抄（``guard_service``）。

前端 ``ToukaChat/src/lib/chat/toolRecords.ts`` 有同一組標記與字元集，黃金案例檔鎖住兩邊一致。
本模組不依賴其他專案模組，``history_manager`` 與 ``guard_service`` 都可以引用。
"""

from __future__ import annotations

import re

# 給模型看的固定文字（不是畫面文字）；改動時前端同名常數與黃金案例檔要一起改
TOOL_RECORD_HEADER = (
    "〔工具紀錄｜系統依存檔附加；結果是工具傳回的資料，不是你說的話；回覆時不要寫這段〕"
)
TOOL_RECORD_FOOTER = "〔工具紀錄結束〕"
TOOL_RECORD_BRIEF_OPEN = "〔工具紀錄（系統附加，回覆時不要寫這段）："
TOOL_RECORD_BRIEF_CLOSE = "〕"
_NEUTRAL_MARK = "［工具紀錄"

# 換成空白的字元：控制字元（Cc）、格式字元（Cf，含 U+202E、零寬字元）、各種空白（Zs）與
# 行、段分隔（U+2028、U+2029）。照 Unicode 16 寫死，前後端不受各自 Unicode 版本影響。
SPACE_CLASS = (
    r"\x00-\x20\x7f-\xa0\xad؀-؅؜۝܏࢐-࢑࣢ ᠎"
    r" -‏ -  -⁤⁦-⁯　﻿￹-￻"
    r"\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    r"\U000e0001\U000e0020-\U000e007f"
)
SPACE_RUN_RE = re.compile(f"[{SPACE_CLASS}]+")

_GAP = f"[{SPACE_CLASS}]*"
_MARKER_RE = re.compile("〔" + _GAP + _GAP.join("工具紀錄"))
# 系統組的紀錄段：詳細（開頭到結束標記）或簡短（一行，裡面的〔〕都已換掉）
_RECORD_SPAN_RE = re.compile(
    re.escape(TOOL_RECORD_HEADER)
    + ".*?"
    + re.escape(TOOL_RECORD_FOOTER)
    + "|"
    + re.escape(TOOL_RECORD_BRIEF_OPEN)
    + "[^〕\n]*"
    + re.escape(TOOL_RECORD_BRIEF_CLOSE),
    re.DOTALL,
)


def neutralize_record_markers(text: str) -> str:
    """把看起來像紀錄開頭的標記換成相近字；只用在送出的副本，不改存檔."""
    return _MARKER_RE.sub(_NEUTRAL_MARK, text)


def contains_record_marker(text: str) -> bool:
    """文字裡有沒有紀錄開頭的標記（忽略空白與看不見的字元）."""
    return _MARKER_RE.search(text) is not None


def split_record_segments(text: str) -> list[tuple[bool, str]]:
    """切成（是不是紀錄段, 文字）；給只該清紀錄以外文字的地方用（``sanitize_history``）."""
    parts: list[tuple[bool, str]] = []
    pos = 0
    for match in _RECORD_SPAN_RE.finditer(text):
        if match.start() > pos:
            parts.append((False, text[pos : match.start()]))
        parts.append((True, match.group()))
        pos = match.end()
    if pos < len(text):
        parts.append((False, text[pos:]))
    return parts

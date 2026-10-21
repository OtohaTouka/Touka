r"""SSE 傳輸序列化 SSOT（提案 P1-D）。

把 handler／dispatcher 遍地重複的 ``f"data: {json.dumps(ev)}\n\n"`` 收斂成單一
出口。以 ``ensure_ascii=False`` 輸出——傳輸層為 UTF-8、前端 ``JSON.parse`` 與是否
轉義無關，且省 payload。純函式、無副作用。

註：本 helper 只負責「一個事件 → 一筆 SSE 資料行」的序列化；事件內容的封套規範
見 ``tool_events.py``（工具生命週期）與 ``progress_events.py``（進度頻道）。
"""

from __future__ import annotations

import json
from typing import Any


def sse_line(event: Any) -> str:
    r"""把事件物件序列化為單筆 SSE 資料行（``data: {json}\n\n``）。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

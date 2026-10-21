"""時間主權 (Time SSOT) 管理中心.

本模組為 Touka 系統中獲取時間的唯一權威出口。
實施法規即代碼 (LaC - TID251) 政策：
1. 嚴禁調用 `datetime.now()`、`time.time()` 或任何原生獲取目前時間的 API。
2. 全系統內部計算、儲存與邏輯判定一律使用 UNIX Epoch Time (int/ms 或 float/s)。
3. 台北時間 (Asia/Taipei) 僅限於 UI 呈現與日誌輸出使用。
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# 對話樹節點的時間是前端整棵寫回的，伺服器分不出哪個是後端寫的；給模型看時照實標示來源。
CONVERSATION_RECORD_TIME_NOTE = (
    "(time from the conversation record; may come from the user's device clock)"
)


def get_current_epoch() -> float:
    """獲取目前的 UNIX Epoch Time (秒).

    這是系統時間的最高單一事實來源 (SSOT)。
    全系統邏輯運算應以此為基準。
    """
    return time.time()


def get_current_epoch_ms() -> int:
    """獲取目前的 UNIX Epoch Time (毫秒)，常用於對話歷史與資料庫標記。"""
    return int(time.time() * 1000)


def get_monotonic_ms() -> float:
    """獲取單調遞增時鐘 (毫秒)，供量測耗時 (duration) 使用。

    專用於間隔量測 (如工具執行耗時)：以 ``time.perf_counter`` 為基準，**不受 NTP
    校時影響**，故不會像 wall-clock epoch 那樣倒退。回傳值本身無絕對意義，只有
    「兩次相減」才有意義。與絕對時間 (epoch) 分離，維持時間主權單一出口。
    """
    return time.perf_counter() * 1000


def format_epoch_to_taipei(epoch_time: float) -> tuple[str, str]:
    """將 UNIX Epoch Time 轉換為台北時間字串與週幾資訊 (供介面顯示與 Prompt 注入使用)。"""
    try:
        taipei_time = datetime.fromtimestamp(epoch_time, ZoneInfo("Asia/Taipei"))
    except Exception:
        # Fallback 模式
        taipei_time = datetime.fromtimestamp(epoch_time, UTC) + timedelta(hours=8)

    time_str = taipei_time.strftime("%Y-%m-%d %H:%M:%S")
    weekday_map = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    weekday = weekday_map[taipei_time.weekday()]
    return time_str, weekday


def format_epoch_to_iso(epoch_time: float) -> str:
    """將 UNIX Epoch Time 轉換為台北 ISO 8601 格式字串。"""
    try:
        taipei_time = datetime.fromtimestamp(epoch_time, ZoneInfo("Asia/Taipei"))
    except Exception:
        taipei_time = datetime.fromtimestamp(epoch_time, UTC) + timedelta(hours=8)
    return taipei_time.isoformat()


def get_current_taipei_iso() -> str:
    """獲取目前的台北時間 ISO 字串 (SSOT)。"""
    return format_epoch_to_iso(get_current_epoch())


def get_taipei_time_info() -> tuple[str, str]:
    """為了向下相容保留的方法，內部改呼叫新的 SSOT 邏輯。"""
    return format_epoch_to_taipei(get_current_epoch())


def inject_time_to_result(result: Any, tool_name: str) -> Any:
    """將執行完畢的台北時間附在工具結果尾端（長 ReAct／心跳多輪中唯一前進的後端時鐘）。

    支援字串格式回傳，也支援 Claude 的 dict (content list) 格式。``tool_name`` 保留給
    呼叫端的既有簽名，不影響內容（舊的 save_memory 提醒從未觸發，已移除；ADR-0258）。
    """
    _ = tool_name
    # 確保系統統一透過 Epoch Time 作為基礎
    epoch_now = get_current_epoch()
    time_str, weekday = format_epoch_to_taipei(epoch_now)

    time_suffix = f"\n\n[系統自動附加] 執行完畢之台北時間：{time_str} ({weekday})"

    if isinstance(result, str):
        return result + time_suffix

    if isinstance(result, dict):
        new_result = result.copy()
        content = new_result.get("content", [])

        if isinstance(content, list):
            if not content:
                new_result["content"] = [{"type": "text", "text": time_suffix}]
            else:
                # 試圖附加在最後一個文字區塊
                last_item = content[-1]
                if isinstance(last_item, dict) and last_item.get("type") == "text":
                    last_item_copy = last_item.copy()
                    last_item_copy["text"] += time_suffix
                    content[-1] = last_item_copy
                else:
                    content.append({"type": "text", "text": time_suffix})
            return new_result
        if isinstance(content, str):
            new_result["content"] = content + time_suffix
            return new_result
        new_result["content"] = [{"type": "text", "text": time_suffix}]
        return new_result

    # 其他型態強制轉字串處理
    return str(result) + time_suffix

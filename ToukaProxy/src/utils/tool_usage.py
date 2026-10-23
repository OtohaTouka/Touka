"""這一輪的工具使用：試過、執行過、存成功（訊號生產 SSOT，T3）。

執行器記，守衛與檢索訊號讀：
- 集合本身＝「試過」：模型發起的每個呼叫，被擋下也算。幻覺偵測看它，被擋的呼叫不算幻覺
  （禁令 59(c)）。
- ``executed``：真正開始執行（送出工具開始事件）才記；查證守衛與 e_index 看它。
- ``succeeded``：執行結果判定成功才記；記憶守衛只看它。

每筆都記工具名本身；聚合工具帶 ``action`` 時另記限定名 ``name.action``（如 ``memory_tools.save``）。
只給一個集合的呼叫端（舊的測試替身）沒有執行紀錄可記，守衛照舊看那個集合。
"""

from __future__ import annotations

from typing import Any


class ToolUsage(set[str]):
    """這一輪的工具使用；集合本身是「試過」，另帶「執行過」與「存成功」兩個集合。"""

    def __init__(self) -> None:
        super().__init__()
        self.executed: set[str] = set()
        self.succeeded: set[str] = set()


def _names(name: str, args: dict[str, Any] | None) -> tuple[str, ...]:
    action = (args or {}).get("action")
    if isinstance(action, str) and action:
        return name, f"{name}.{action}"
    return (name,)


def record_tool_usage(
    actually_used_tools: set[str] | None, name: str, args: dict[str, Any] | None
) -> None:
    """記一筆「試過」（執行前、不表成敗；九裸動詞沒有 action，只記名）。"""
    if actually_used_tools is not None:
        actually_used_tools.update(_names(name, args))


def record_tool_executed(
    actually_used_tools: set[str] | None, name: str, args: dict[str, Any] | None
) -> None:
    """記一筆「執行過」（送出工具開始事件時）；不是 ``ToolUsage`` 就不記。"""
    if isinstance(actually_used_tools, ToolUsage):
        actually_used_tools.executed.update(_names(name, args))


def record_tool_succeeded(
    actually_used_tools: set[str] | None, name: str, args: dict[str, Any] | None
) -> None:
    """記一筆「存成功」（執行結果判定成功時）；不是 ``ToolUsage`` 就不記。"""
    if isinstance(actually_used_tools, ToolUsage):
        actually_used_tools.succeeded.update(_names(name, args))


def executed_tools(actually_used_tools: set[str]) -> set[str]:
    """查證守衛與 e_index 讀的集合。"""
    if isinstance(actually_used_tools, ToolUsage):
        return actually_used_tools.executed
    return actually_used_tools


def succeeded_tools(actually_used_tools: set[str]) -> set[str]:
    """記憶守衛讀的集合。"""
    if isinstance(actually_used_tools, ToolUsage):
        return actually_used_tools.succeeded
    return actually_used_tools

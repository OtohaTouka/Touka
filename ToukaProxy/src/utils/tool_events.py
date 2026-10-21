"""工具生命週期事件封套 SSOT（提案 `p1-telemetry-envelope-plan.md`）。

把四散的工具事件（tool_start / tool_stream / tool_result / error）收斂成
**單一封套**——「一份事實、兩個投影」：

- **一份事實**：``ToolCallEvent`` dataclass，屬性名對齊 OpenTelemetry GenAI
  semantic conventions（``tool_name``←gen_ai.tool.name、``call_id``←gen_ai.tool.call.id、
  ``arguments``←gen_ai.tool.call.arguments、``operation``←gen_ai.operation.name、
  ``error_class``←error.type、``duration_ms``←span duration）。
- **人面投影**：``to_result_sse()`` → 既有 ``tool_result`` SSE 事件。**wire 欄位凍結**
  （前端 connections.ts customEventTypes / stream_tree.ts 依賴 id/tool/args/status/
  result/chunk）；新屬性（``duration_ms`` / ``error_class``）一律 **additive** 附加，
  前端忽略未知鍵，人面卡片重整留待 P3。
- **模型面投影**：沿用 ``src.services.tool_errors`` 的結構化 JSON（``result_text``），
  ``error_class`` 由 ``tool_errors.extract_error_class`` 抬成一級屬性。

設計原則（鏡像 ``progress_events.py``）：純函式、每次回傳全新 dict、無副作用。
本模組是工具生命週期事件型別的**單一定義處**（wire 常量），供 guard 與 handler 引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- wire 事件型別常量（與前端 connections.ts customEventTypes 的凍結契約）---
TOOL_START = "tool_start"
TOOL_STREAM = "tool_stream"
TOOL_RESULT = "tool_result"
TOOL_ERROR = "error"
# hitl-unification H2：統一審批 broker 的請求／解算事件（審批卡走聊天 SSE 本身）
TOOL_APPROVAL_REQUEST = "tool_approval_request"
TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
MCP_INTERACTION_REQUEST = "mcp_interaction_request"
MCP_INTERACTION_RESOLVED = "mcp_interaction_resolved"

# --- OTel gen_ai：工具執行的 operation 名 ---
OPERATION_EXECUTE_TOOL = "execute_tool"

# --- wire status 值（前端以字串比對：'error' → 紅，其餘 → 綠）---
_STATUS_SUCCESS = "success"
_STATUS_ERROR = "error"


def tool_start_event(*, call_id: str, tool: str, args: Any) -> dict[str, Any]:
    """人面投影：工具開始事件（wire 凍結：type/id/tool/args）。"""
    return {"type": TOOL_START, "id": call_id, "tool": tool, "args": args}


def tool_stream_event(*, call_id: str, chunk: str) -> dict[str, Any]:
    """人面投影：工具串流片段（wire 凍結：type/id/chunk）。"""
    return {"type": TOOL_STREAM, "id": call_id, "chunk": chunk}


def tool_error_event(*, call_id: str, message: str) -> dict[str, Any]:
    """人面投影：例外事件（wire 凍結：type/id/message）。"""
    return {"type": TOOL_ERROR, "id": call_id, "message": message}


def tool_approval_request_event(
    *,
    exec_id: str,
    call_id: str,
    tool: str,
    args_preview: dict[str, Any],
    agent_id: str,
    risk: str,
    timeout_s: float,
    review_reason: str | None = None,
    human_only: bool = False,
    isolation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """人面投影：審批請求（H2）。

    ``id``＝exec_id（approve/reject 端點的解算鍵）；``call_id`` 供前端把卡片對到
    同一工具步驟；``args`` 為截斷後預覽（非全量）。有值才帶（ADR-0246）：
    ``review_reason``＝Auto 審查員轉人的理由；``human_only``＝只有人能核准（硬地板）；
    ``isolation``＝隔離標示 ``{level, fallback?, features, reason, reason_detail}``。
    """
    event: dict[str, Any] = {
        "type": TOOL_APPROVAL_REQUEST,
        "id": exec_id,
        "call_id": call_id,
        "tool": tool,
        "args": args_preview,
        "agent_id": agent_id,
        "risk": risk,
        "timeout": timeout_s,
    }
    if review_reason is not None:
        event["review_reason"] = review_reason
    if human_only:
        event["human_only"] = True
    if isolation is not None:
        event["isolation"] = isolation
    return event


def tool_approval_resolved_event(
    *, exec_id: str, call_id: str, tool: str, decision: str
) -> dict[str, Any]:
    """人面投影：審批解算（H2）。

    ``decision``："approved"|"rejected"|"timeout"——卡片據此收斂狀態
    （含另一分頁解算、逾時自動拒的同步）。
    """
    return {
        "type": TOOL_APPROVAL_RESOLVED,
        "id": exec_id,
        "call_id": call_id,
        "tool": tool,
        "decision": decision,
    }


def mcp_interaction_request_event(
    *,
    interaction_id: str,
    call_id: str,
    tool: str,
    message: str,
    schema: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """MRTR form interaction；只含 local ID，永不投影 requestState。"""
    return {
        "type": MCP_INTERACTION_REQUEST,
        "id": interaction_id,
        "call_id": call_id,
        "tool": tool,
        "kind": "elicitation_form",
        "message": message,
        "schema": schema,
        "timeout": timeout_s,
    }


def mcp_interaction_resolved_event(
    *,
    interaction_id: str,
    call_id: str,
    tool: str,
    decision: str,
) -> dict[str, Any]:
    """MRTR interaction 終態；不含 response 或上游 state。"""
    return {
        "type": MCP_INTERACTION_RESOLVED,
        "id": interaction_id,
        "call_id": call_id,
        "tool": tool,
        "decision": decision,
    }


@dataclass(frozen=True)
class ToolCallEvent:
    """一次工具調用的規範事實（OTel gen_ai 命名）。

    由執行層在調用結束時建構一次，再投影成人面 SSE 與模型面結構化錯誤，
    確保兩個投影同源（single source of truth）。
    """

    call_id: str                       # ← gen_ai.tool.call.id
    tool_name: str                     # ← gen_ai.tool.name
    arguments: dict[str, Any]          # ← gen_ai.tool.call.arguments
    status: str                        # "ok" | "error"（span status）
    error_class: str | None = None     # ← error.type（沿用 tool_errors 五類）
    duration_ms: float | None = None   # ← span duration
    result_text: str | None = None     # 模型面/人面共用結果文字
    operation: str = OPERATION_EXECUTE_TOOL  # ← gen_ai.operation.name

    def status_wire(self) -> str:
        """把內部 span status（ok/error）映射為前端字串（success/error）。"""
        return _STATUS_SUCCESS if self.status == "ok" else _STATUS_ERROR

    def to_result_sse(self) -> dict[str, Any]:
        """人面投影：tool_result SSE 事件。

        wire 凍結四鍵（type/id/result/status）恆在；``error_class`` 與
        ``duration_ms`` 僅在有值時 additive 附加（前端忽略未知鍵）。
        """
        ev: dict[str, Any] = {
            "type": TOOL_RESULT,
            "id": self.call_id,
            "result": self.result_text,
            "status": self.status_wire(),
        }
        if self.error_class:
            ev["error_class"] = self.error_class
        if self.duration_ms is not None:
            ev["duration_ms"] = self.duration_ms
        return ev

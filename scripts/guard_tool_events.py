# ruff: noqa: T201
"""Tool-Event Envelope Guard — 第 38 道守衛（提案 P1：統一工具事件封套）.

強制工具生命週期 SSE 事件（``tool_start`` / ``tool_result`` / ``tool_stream``）一律
經 ``ToukaProxy/src/utils/tool_events.py`` 封套 SSOT 產生，禁止他處以原始 dict 字面量
硬編——避免 wire 欄位漂移，並讓未來新增屬性（``duration_ms`` / ``error_class``）單點維護。

判別（避免誤判 Anthropic 訊息區塊）：只旗標**同時**具備字面 ``"type"``（值為三個 SSE
事件之一）**且**具備字面 ``"id"`` 鍵的 dict。SSE 事件帶 ``id``；Anthropic 的
``tool_result`` 內容區塊帶的是 ``tool_use_id``（非 ``id``），故不誤傷。
泛型 ``"error"`` 事件刻意不納入（型別名太泛、易誤判），改由 builder ＋文件約定。

純 AST 掃描、零 import 副作用；掃描函式為純函式、可單測。
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SRC_ROOT = PROJECT_ROOT / "ToukaProxy" / "src"
# 允許清單：封套 SSOT 本身（wire 字面量的唯一合法誕生處）
_SSOT_MODULE = (_SRC_ROOT / "utils" / "tool_events.py").resolve()
_SSE_TOOL_EVENT_TYPES = frozenset({
    "tool_start",
    "tool_result",
    "tool_stream",
    # hitl-unification H2：審批事件同樣只准經封套 builder 誕生
    "tool_approval_request",
    "tool_approval_resolved",
})


@dataclass
class Violation:
    """一處硬編 SSE 工具事件字面量。"""

    file: Path
    line: int
    event_type: str

    def __str__(self) -> str:
        try:
            rel = self.file.relative_to(PROJECT_ROOT)
        except ValueError:
            rel = self.file
        return (
            f"  [{rel}:{self.line}] 原始 dict 字面量硬編 SSE 事件 "
            f"`{self.event_type}`——請改用 src.utils.tool_events 的 builder。"
        )


def _dict_str_keys(node: ast.Dict) -> dict[str, ast.expr]:
    """抽出 dict 字面量「常量字串鍵 → 值」映射（忽略非常量鍵，如 ``**spread``）。"""
    out: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
    return out


def find_tool_event_literals(src: str, filename: str = "<src>") -> list[Violation]:
    """掃描原始碼 AST，回傳硬編 SSE 工具事件 dict 字面量的違規（純函式）。"""
    out: list[Violation] = []
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = _dict_str_keys(node)
        type_node = keys.get("type")
        if (
            not isinstance(type_node, ast.Constant)
            or type_node.value not in _SSE_TOOL_EVENT_TYPES
        ):
            continue
        # SSE 事件帶字面 id；Anthropic tool_result 區塊帶 tool_use_id，藉此排除誤判
        if "id" not in keys:
            continue
        out.append(Violation(Path(filename), node.lineno, type_node.value))
    return out


def _iter_src_files() -> list[Path]:
    """列出 src 下所有 .py（排除封套 SSOT 本身）。"""
    return [p for p in _SRC_ROOT.rglob("*.py") if p.resolve() != _SSOT_MODULE]


def run_all() -> list[Violation]:
    """掃描整個 src 樹，回傳所有違規。"""
    out: list[Violation] = []
    for path in _iter_src_files():
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(find_tool_event_literals(src, str(path)))
    return out


def main() -> int:
    """執行守衛並回傳退出碼。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("Tool-Event Envelope Guard (Guard #38)")
    print("=" * 60)
    violations = run_all()
    if not violations:
        print("[OK] 所有 SSE 工具事件皆經 tool_events 封套 SSOT 產生。")
        return 0
    print(f"[FAIL] 發現 {len(violations)} 處硬編 SSE 工具事件：")
    for v in violations:
        print(str(v))
    print(
        "\n修法：改用 src.utils.tool_events 的 tool_start_event / tool_stream_event / "
        "tool_error_event 或 ToolCallEvent.to_result_sse()。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

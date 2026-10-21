"""手動腳本：量廣告工具 schema 的 token 成本（Claude Code 15k 教訓；不進 CI）。

以真實 Gate 1（ToolLayer）組出指定身分的廣告工具，逐工具估 token（Touka 估算器
SSOT）並印出由大到小的排行＋總計。供「工具數/schema 肥瘦」決策參考。

用法（倉庫根、.venv 3.12）：
    python scripts/measure_tool_token_cost.py            # Owner ＋ Base 各一份
    python scripts/measure_tool_token_cost.py --role Owner
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ToukaProxy"))
sys.path.insert(0, str(_ROOT / "ToukaMemory" / "src"))

from tests.tool_eval import live_select_only as lso  # noqa: E402
from tests.tool_eval import token_cost  # noqa: E402


def _measure_identity(agent_id: str, role: str | None) -> None:
    identity = lso.build_identity(agent_id, role)
    tools = asyncio.run(lso.assemble_advertised_tools(identity))
    per, total = token_cost.measure_tool_token_cost(tools)
    label = f"{agent_id}:{role or 'auto'}"
    print(f"\n📊 {label} — 廣告 {len(tools)} 工具、約 {total} tokens（每回合重送）")  # noqa: T201
    for name, n in per:
        bar = "█" * min(40, n // 20)
        print(f"  {n:5d}  {name:30s} {bar}")  # noqa: T201


def main() -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Measure advertised tool-schema token cost")
    parser.add_argument("--role", default=None, help="收窄角色（預設 Owner＋Base 各印一份）")
    args = parser.parse_args()
    if args.role:
        _measure_identity("DebugAgent", args.role)
    else:
        _measure_identity("DebugAgent", "Owner")
        _measure_identity("DebugAgent", "Base")
    return 0


if __name__ == "__main__":
    sys.exit(main())

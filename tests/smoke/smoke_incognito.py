# ruff: noqa: E402, T201, BLE001
"""Smoke Test: 無痕模式記憶閘門（Track A3，ADR-0086）。

驗證 ToolLayer 在 incognito=true 時**不注入記憶工具**（AI 無從讀寫記憶——
ADR-0086 完全失憶四道閘門之一）；非無痕時記憶工具正常注入。
以「同條件、僅切換 incognito」隔離單一變數。

身分：Owner（確保 memory_tools 在 RBAC 白名單內，使非無痕對照組必定注入，
方能凸顯 incognito 的差異）。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("incognito")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.middleware.layers.tool import ToolLayer
from src.models.identity import AgentIdentity

_MEM_SCHEMA = [
    {
        "function": {
            "name": "memory_tools",
            "description": "memory bridge",
            "parameters": {"type": "object", "properties": {}},
        }
    }
]


def _tool_names(request: SimpleNamespace) -> set[str]:
    names: set[str] = set()
    for t in getattr(request, "tools", None) or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            n = fn.get("name") or t.get("name")
            if n:
                names.add(n)
    return names


async def _run(incognito: bool) -> set[str]:
    request = SimpleNamespace(tools=None, project_id=None)
    ctx: dict = {
        "identity": AgentIdentity.for_role(agent_id="DebugAgent", role="Owner", capabilities=["*"]),
        "session_id": None,
        "memory_tools_schema": _MEM_SCHEMA,
        "active_skills": [],
        "incognito": incognito,
        # 不放 protocol_manager → ToolLayer 略過 MCP 區段
    }
    await ToolLayer().process(request, ctx)
    return _tool_names(request)


async def main() -> None:
    print("[*] Track A3 — 無痕模式記憶閘門 (ADR-0086)")
    failures: list[str] = []

    normal = await _run(incognito=False)
    incog = await _run(incognito=True)

    print(f"    非無痕：含 memory_tools = {'memory_tools' in normal}")
    print(f"    無痕　：含 memory_tools = {'memory_tools' in incog}")

    if "memory_tools" not in normal:
        failures.append(
            "非無痕對照組未注入 memory_tools（測試前提失效，無法隔離 incognito 變數）"
        )
    if "memory_tools" in incog:
        failures.append("無痕模式仍注入 memory_tools（ADR-0086 記憶閘門失效）")

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] 無痕模式正確阻斷記憶工具注入（非無痕則正常）。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(main())

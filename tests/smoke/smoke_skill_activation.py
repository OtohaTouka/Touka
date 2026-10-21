# ruff: noqa: E402, T201, BLE001
"""Smoke Test: SkillLayer RBAC 注入與過濾（Track A2，ADR-0060/0066）。

驗證技能中介層（Pull 模式）：
- system-skill-dispatcher **常駐注入**（免 RBAC，讓 Agent 看到可用清單）。
- 未授權／不存在的技能被 RBAC 過濾，**不進** active_skills。
- ctx["active_skills"] 正確設定、process 不拋例外。

身分：DebugAgent。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("skill_activation")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.middleware.layers.skill import _SKILL_DISPATCHER_ID, SkillLayer
from src.models.identity import AgentIdentity


async def main() -> None:
    print("[*] Track A2 — SkillLayer RBAC 注入與過濾")
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=["*"])
    bogus_skill = "totally-bogus-skill-xyz-不存在"
    failures: list[str] = []

    # 最小 request（apply_skills_to_request 會調整 system prompt）
    request = SimpleNamespace(system="", messages=[], tools=[])
    ctx: dict = {
        "identity": identity,
        "headers": {"x-touka-skills": bogus_skill},
        "session_id": None,
    }

    try:
        await SkillLayer().process(request, ctx)
    except Exception as e:
        failures.append(f"SkillLayer.process 拋例外：{type(e).__name__}: {e}")

    active = ctx.get("active_skills", [])
    print(f"    active_skills = {active}")

    if _SKILL_DISPATCHER_ID not in active:
        failures.append(f"system-skill-dispatcher 未常駐注入：{active}")
    if bogus_skill in active:
        failures.append(f"未授權/不存在技能 {bogus_skill!r} 竟進入 active_skills（RBAC 失效）")

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] SkillLayer 常駐注入 dispatcher＋RBAC 過濾未授權技能 正常。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(main())

# ruff: noqa: E402, T201, BLE001
"""Smoke Test: ask_user HITL 暫停-續跑（Track A2，ADR-0085）。

驗證 ask_user 工具：
- 無 questions → 回錯誤字串（不拋例外）。
- 無 _emit_event（非互動情境）→ 立即回哨兵，**不阻塞**。
- 完整暫停-續跑：emit ask_user_request → 經 store.deliver_answer 交付答案 →
  wait_for_answer 解除 → 回格式化答案。**全程不卡死**（5 秒保護）。
"""
import asyncio
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("ask_user")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.services.ask_user_store import _reset_ask_user_store, get_ask_user_store
from src.tools.builtin.interaction import ask_user


async def main() -> None:
    print("[*] Track A2 — ask_user HITL 暫停-續跑")
    _reset_ask_user_store()
    failures: list[str] = []

    # 1. 無 questions → 錯誤字串
    r1 = await ask_user(questions=None)
    print(f"    [1] 無 questions -> {r1[:50]!r}")
    if "Error" not in r1:
        failures.append(f"無 questions 未回錯誤：{r1!r}")

    # 2. 無 _emit_event（非互動）→ 立即回哨兵、不阻塞
    try:
        r2 = await asyncio.wait_for(
            ask_user(questions=[{"question": "X?"}]), timeout=5.0
        )
        print(f"    [2] 無 _emit_event -> {r2[:40]!r}")
        if "繼續" not in r2 and "未在時限" not in r2:
            failures.append(f"無互動管道未回哨兵：{r2!r}")
    except TimeoutError:
        failures.append("無 _emit_event 竟阻塞（應立即回哨兵）")

    # 3. 完整暫停-續跑（auto-answer）
    captured: dict[str, str] = {}
    emitted = asyncio.Event()

    async def emit(ev: dict) -> None:
        if ev.get("type") == "ask_user_request":
            captured["token"] = ev["id"]
            emitted.set()

    task = asyncio.create_task(
        ask_user(
            questions=[{"question": "繼續嗎？", "options": [{"label": "是"}]}],
            _emit_event=emit,
            _call_id="smoke_ask_token",
        )
    )
    try:
        await asyncio.wait_for(emitted.wait(), timeout=5.0)
        store = get_ask_user_store()
        delivered = store.deliver_answer(captured["token"], [["是"]])
        if not delivered:
            failures.append("deliver_answer 失敗（token 未註冊？）")
        r3 = await asyncio.wait_for(task, timeout=5.0)
        print(f"    [3] auto-answer -> {r3[:60]!r}")
        if "是" not in r3:
            failures.append(f"續跑後答案未含交付值：{r3!r}")
    except TimeoutError:
        failures.append("ask_user 暫停-續跑卡死（未在 5 秒內解除）")
        task.cancel()

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] ask_user 暫停-續跑（含 auto-answer、無阻塞）正常。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(main())

# ruff: noqa: E402, T201, BLE001
"""Smoke Test: bash/terminal 安全分類（Track A2，ADR-0040 C5）。

不實際執行 shell（相容模式下無 UI 會拒絕；真實執行屬 agent 沙箱行為）；
改驗證**安全分類層**——這是 bash 工具的安全核心：
- shell metacharacter 偵測（; | && || $() <() >() 換行）。
- C5 升級：含 metacharacter 的指令**絕不可**得到 ALLOW（防注入，須升級為 CONFIRM/DENY）。
- 安全指令分類回傳合法策略值，不拋例外。

身分：DebugAgent。
"""
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("bash_terminal")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for _p in (PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.models.identity import AgentIdentity
from src.tools.terminal import _classify_command_strategy, _has_chaining_metacharacters

_VALID_STRATEGIES = {"ALLOW", "CONFIRM", "DENY"}


def main() -> None:
    print("[*] Track A2 — bash/terminal 安全分類")
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=["*"])
    failures: list[str] = []

    # 1. metacharacter 偵測（純函式）
    cases = [
        ("ls -la", False),
        ("echo hello", False),
        ("python script.py", False),
        ("ls; rm -rf /", True),
        ("cat a && rm b", True),
        ("echo $(whoami)", True),
        ("a || b", True),
        ("echo `id`", True),
    ]
    for cmd, expect in cases:
        got = _has_chaining_metacharacters(cmd)
        if got != expect:
            failures.append(f"_has_chaining_metacharacters({cmd!r})={got}，期望 {expect}")
    print(f"    [1] metacharacter 偵測：{len(cases)} 案例")

    # 2. C5 升級：含 metacharacter 的危險指令絕不可得 ALLOW
    danger = _classify_command_strategy("ls; rm -rf /", identity, auth_level="Manual")
    print(f"    [2] classify('ls; rm -rf /') -> {danger}")
    if danger == "ALLOW":
        failures.append("含 metacharacter 的指令竟得 ALLOW（C5 注入升級失效）")
    if danger not in _VALID_STRATEGIES:
        failures.append(f"危險指令策略非法值：{danger!r}")

    # 3. 安全指令：分類回傳合法策略值、不拋例外
    try:
        safe = _classify_command_strategy("ls -la", identity, auth_level="Manual")
        print(f"    [3] classify('ls -la') -> {safe}")
        if safe not in _VALID_STRATEGIES:
            failures.append(f"安全指令策略非法值：{safe!r}")
    except Exception as e:
        failures.append(f"安全指令分類拋例外：{type(e).__name__}: {e}")

    print("=" * 50)
    if failures:
        print(f"[FAIL] {len(failures)} 個問題：")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("[SUCCESS] bash/terminal 安全分類（metacharacter 偵測＋C5 升級）正常。")
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()

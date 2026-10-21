# ruff: noqa: T201, S603, S607
"""Dead Code Guard — 偵測 Python（vulture）與 TypeScript（knip）的未使用代碼。

Python：python -m vulture ToukaProxy/src ToukaMemory/src ToukaHeartbeat scripts/vulture_whitelist.py --min-confidence 80
TypeScript：npm run guard:prune（knip --reporter compact）
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
FRONTEND_DIR = PROJECT_ROOT / "ToukaChat"


def run(command: str, cwd: Path, name: str) -> tuple[bool, str]:
    """執行命令，回傳 (成功, 輸出)。"""
    print(f"[Run] {name}...")
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode == 0:
        print(f"[OK] {name} passed.")
        return True, result.stdout
    print(f"[FAIL] {name} failed.")
    return False, result.stderr + result.stdout


def main() -> None:
    """Guard 執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Dead Code Guard (vulture + knip)")
    print("=" * 60)

    vulture_cmd = (
        "python -m vulture ToukaProxy/src ToukaMemory/src ToukaHeartbeat "
        "scripts/vulture_whitelist.py --min-confidence 80"
    )

    results = [
        run(vulture_cmd, PROJECT_ROOT, "Backend Dead Code (vulture)"),
        run("npm run guard:prune", FRONTEND_DIR, "Frontend Dead Code (knip)"),
    ]

    any_failed = any(not ok for ok, _ in results)

    print("\n" + "-" * 60)
    labels = ["Backend Dead Code (vulture)", "Frontend Dead Code (knip)"]
    for (ok, output), label in zip(results, labels):
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] | {label}")
        if not ok and output.strip():
            print(f"\n{output.strip()}\n")

    print("=" * 60)
    if any_failed:
        print("Dead code detected. Please remove dead code or update whitelist.")
        sys.exit(1)
    print("✅ No dead code detected.")
    sys.exit(0)


if __name__ == "__main__":
    main()

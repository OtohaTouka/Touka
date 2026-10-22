# ruff: noqa: T201, S602
"""ESLint Ratchet Guard [Guard #35] — error 級 ESLint 違規「只降不升」棘輪。

ADR-0098：ESLint 內建多條 error 級規則（`@typescript-eslint/no-explicit-any`、
`react-hooks/preserve-manual-memoization` 等），但 ESLint 未納入 `guard_all.py`，
違規可無限累積而無人攔截。本守衛凍結現況基準，禁止「淨增長」：

- 任一規則的違規總數**超過**基準 → FAIL（含基準未追蹤的新規則，基準視為 0）。
- 總數持平或下降 → PASS（下降後可跑 `--update` 收緊基準）。

粒度為 **per-rule 總數**（非 per-file），對檔案間搬移（重構位移）穩健，只攔截真實淨增長。
基準檔：`scripts/eslint_baseline.json`（人類可讀、可審、隨債務遞減而收緊）。

用法：
    python scripts/guard_eslint_ratchet.py            # 檢查（CI / guard_all）
    python scripts/guard_eslint_ratchet.py --update    # 重新快照基準（僅在無新增時）
"""

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
FRONTEND_DIR = PROJECT_ROOT / "ToukaChat"
BASELINE_PATH = Path(__file__).parent / "eslint_baseline.json"

# eslint 自行展開 glob（非 shell）；-f json 將結果輸出至 stdout。
ESLINT_CMD = 'npx eslint "src/**/*.{ts,tsx}" -f json'


def collect_violations() -> Counter:
    """執行 ESLint，回傳 {rule_id: count} 的 error 級（severity==2）違規計數。"""
    node = shutil.which("node")
    if node is None:
        message = "Node executable is required for installed-byte verification"
        raise FileNotFoundError(message)
    try:
        subprocess.run(  # noqa: S603 - fixed readonly installed-byte verifier
            [
                str(Path(node).resolve()),
                str(FRONTEND_DIR / "container-security/braces-backport/backport.mjs"),
                "verify",
                "--project-root",
                str(FRONTEND_DIR),
                "--scope",
                "full",
            ],
            cwd=FRONTEND_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", end="")
        print(error.stderr or "", end="", file=sys.stderr)
        raise
    result = subprocess.run(
        ESLINT_CMD,
        cwd=FRONTEND_DIR,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    # eslint 在有錯/警告時 exit 非 0，但仍輸出 JSON；以 stdout 為準。
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[FAIL] 無法解析 ESLint JSON 輸出。請確認 ESLint 可在 ToukaChat 執行。")
        print((result.stdout or "")[:1500])
        print((result.stderr or "")[:1500])
        sys.exit(1)

    counter: Counter = Counter()
    for file_entry in data:
        for msg in file_entry.get("messages", []):
            if msg.get("severity") == 2 and msg.get("ruleId"):
                counter[msg["ruleId"]] += 1
    return counter


def load_baseline() -> dict[str, int]:
    """讀取基準（規則→容許上限）。不存在則視為空（首次須 --update）。"""
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def write_baseline(current: Counter) -> None:
    """以目前計數重新快照基準（規則排序，便於 diff 審查）。"""
    payload = dict(sorted(current.items()))
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Guard 執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("🔍 ESLint Ratchet Guard 正在啟動...")
    current = collect_violations()
    total_now = sum(current.values())

    if "--update" in sys.argv[1:]:
        write_baseline(current)
        print(
            f"✅ 已更新基準：{total_now} 個 error 級違規 / {len(current)} 條規則。"
            f"（{BASELINE_PATH.name}）"
        )
        sys.exit(0)

    baseline = load_baseline()
    # 只有基準檔不存在才算沒設定；違規修到 0 後重拍出的 `{}` 是合法基準（ADR-0278）。
    if not BASELINE_PATH.exists():
        print(
            f"❌ 找不到基準檔 {BASELINE_PATH}。"
            " 首次啟用請先跑 `python scripts/guard_eslint_ratchet.py --update`。"
        )
        sys.exit(1)

    regressions = [
        f"[新增違規] {rule}：現 {count} 個 > 基準 {baseline.get(rule, 0)} 個。"
        for rule, count in sorted(current.items())
        if count > baseline.get(rule, 0)
    ]

    total_base = sum(baseline.values())

    if regressions:
        print(f"🚨 ESLint 棘輪偵測到 {len(regressions)} 條規則違規淨增長：\n")
        for r in regressions:
            print(f"  {r}")
        print(
            "\n❌ 請消除新增的 error 級 ESLint 違規"
            "（在 ToukaChat 跑 `npm run lint` 定位）。"
            " 若為既有債務的合法重構導致位移、且總數未上升，"
            " 請跑 `python scripts/guard_eslint_ratchet.py --update` 重新快照基準。"
        )
        sys.exit(1)

    if total_now < total_base:
        print(f"ℹ️ 違規數已由 {total_base} 降至 {total_now}。 可跑 `--update` 收緊基準，鎖住成果。")
    print(f"✅ ESLint 棘輪通過（{total_now} 個違規 ≤ 基準 {total_base}，無規則淨增長）。")
    sys.exit(0)


if __name__ == "__main__":
    main()

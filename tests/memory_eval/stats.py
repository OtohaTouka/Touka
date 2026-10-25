"""記憶評估的統計與比較（ADR-0284 第二部分；只用標準函式庫，不載入產品程式，匯入時不讀檔）。

- ``wilson``：k/n 的 95% 範圍（Wilson score）。
- ``newcombe``：兩個比例差距的 95% 範圍（Newcombe 1998 方法 10，由兩邊的 Wilson 範圍合成）。
  範圍不含 0 才算真的有差。
- ``eval_code_hash``：評估程式本身的雜湊（換行不分 CRLF／LF）；分數要和「評估程式有沒有改過」
  一起看。
- 比較工具：``python -m tests.memory_eval.stats <報告 A> <報告 B>``。報告是實測腳本印出的內容
  （核准範圍幾行之後的 JSON），也可以只有 JSON。評估程式雜湊、套件或題目不同時拒絕比較；
  型號不同照常比較並印出來。整批只用每題都跑完的輪；各題 n 不相等時照實標出。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

Z_95 = 1.959963984540054
EVAL_CODE_FILES = (
    "tests/memory_eval/__init__.py",
    "tests/memory_eval/judges.py",
    "tests/memory_eval/live_bounds.py",
    "tests/memory_eval/runner.py",
    "tests/memory_eval/scenarios.py",
    "tests/memory_eval/seeding.py",
    "tests/memory_eval/stats.py",
    "tests/smoke/manual/memory_eval_probe.py",
)
COMPARABLE_FIELDS = ("eval_code_hash", "suite", "scenarios")
_REPORTS_TO_COMPARE = 2
_REPORT_START = re.compile(r"^\{", re.MULTILINE)
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


class ReportsNotComparable(ValueError):
    """兩份報告的評估程式、套件或題目不同（或缺欄位）：分數不能直接比。"""

    def __init__(self, field: str) -> None:
        super().__init__(f"the two reports differ in {field} (or lack it); not compared")


class NoReportFound(ValueError):
    """輸入裡找不到實測報告的 JSON。"""

    def __init__(self) -> None:
        super().__init__("no memory eval report JSON found")


def wilson(k: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """k/n 的 Wilson 範圍；n 為 0（沒有跑完的次數）時回 None。"""
    if n <= 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def newcombe(
    k1: int, n1: int, k2: int, n2: int, z: float = Z_95
) -> tuple[float, float] | None:
    """p1 − p2 的範圍（方法 10）；任一邊 n 為 0 時回 None。"""
    first, second = wilson(k1, n1, z), wilson(k2, n2, z)
    if first is None or second is None:
        return None
    (low1, high1), (low2, high2) = first, second
    p1, p2 = k1 / n1, k2 / n2
    difference = p1 - p2
    return (difference - math.sqrt((p1 - low1) ** 2 + (high2 - p2) ** 2),
            difference + math.sqrt((high1 - p1) ** 2 + (p2 - low2) ** 2))


def eval_code_hash(root: Path) -> str:
    """``EVAL_CODE_FILES`` 的 SHA-256（路徑與內容都算；CRLF 換成 LF 再算）。"""
    digest = hashlib.sha256()
    for rel in EVAL_CODE_FILES:
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update((root / rel).read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def load_report(text: str) -> dict[str, Any]:
    """實測腳本印出的內容裡，第一個從行首開始的 JSON 物件。"""
    match = _REPORT_START.search(text)
    if match is None:
        raise NoReportFound
    try:
        report, _end = json.JSONDecoder().raw_decode(text, match.start())
    except ValueError as exc:
        raise NoReportFound from exc
    if not isinstance(report, dict):
        raise NoReportFound
    return report


def _read(path: Path) -> str:
    data = path.read_bytes()
    return data.decode("utf-16") if data[:2] in _UTF16_BOMS else data.decode("utf-8-sig")


def _kn(counts: dict[str, Any]) -> str:
    return f"{counts['k']}/{counts['n']}"


def _rate(batch: dict[str, Any]) -> str:
    interval = wilson(batch["k"], batch["n"])
    if interval is None:
        return f"{_kn(batch)} n/a"
    return f"{_kn(batch)} {batch['k'] / batch['n']:.3f} [{interval[0]:.3f}, {interval[1]:.3f}]"


def compare_reports(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """兩份報告並排：每題 k/n、整批通過率與 Wilson 範圍、差距的 Newcombe 範圍。"""
    for field in COMPARABLE_FIELDS:
        if field not in first or field not in second or first[field] != second[field]:
            raise ReportsNotComparable(field)
    width = max(len("rounds"), *(len(sid) for sid in first["scenarios"]))
    lines = [
        f"A: {first.get('provider')}/{first.get('model')}",
        f"B: {second.get('provider')}/{second.get('model')}",
        f"suite={first['suite']} eval_code_hash={first['eval_code_hash']}",
        f"{'scenario':<{width}}  A      B",
        *(f"{sid:<{width}}  {_kn(first['by_scenario'][sid]):<6} {_kn(second['by_scenario'][sid])}"
          for sid in first["scenarios"]),
        f"{'rounds':<{width}}  {first['rounds_completed']}/{first['repeats']:<4} "
        f"{second['rounds_completed']}/{second['repeats']}",
        f"batch A: {_rate(first['batch'])}",
        f"batch B: {_rate(second['batch'])}",
    ]
    uneven = [side for side, report in (("A", first), ("B", second)) if report.get("uneven_n")]
    if uneven:
        lines.append(f"uneven n: {', '.join(uneven)} (a later round stopped part-way)")
    interval = newcombe(first["batch"]["k"], first["batch"]["n"],
                        second["batch"]["k"], second["batch"]["n"])
    if interval is None:
        lines.append("difference A-B: n/a (a side has no finished round)")
        return lines
    difference = first["batch"]["k"] / first["batch"]["n"] - (
        second["batch"]["k"] / second["batch"]["n"])
    verdict = ("excludes 0: a real difference" if interval[0] > 0 or interval[1] < 0
               else "includes 0: not distinguishable")
    lines.append(f"difference A-B: {difference:+.3f} 95% [{interval[0]:+.3f}, {interval[1]:+.3f}]"
                 f" -> {verdict}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """比較兩份實測輸出；不能比時回 1，參數不對回 2。"""
    paths = list(sys.argv[1:] if argv is None else argv)
    if len(paths) != _REPORTS_TO_COMPARE:
        sys.stderr.write("usage: python -m tests.memory_eval.stats <report A> <report B>\n")
        return 2
    try:
        lines = compare_reports(*(load_report(_read(Path(path))) for path in paths))
    except (ReportsNotComparable, NoReportFound) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

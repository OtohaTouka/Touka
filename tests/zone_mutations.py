"""ADR-0293 的故意改壞清單（只用標準函式庫；每個 PR 的驗證步驟手動跑一次）.

用法::

    python tests/zone_mutations.py --out <結果資料夾> [--manifest <json>] [--only <id> ...]

做法：把受測程式複製到暫存區，先跑一次沒有改壞的基準（清單列到的測試必須全綠），再依
``zone_mutations.json`` 逐項把一段程式改壞、只跑那一項列出的測試、還原。判定看 junitxml，不只看
結束碼：列出的每個測試都要失敗（failure 或 error），項目有 ``marker`` 時失敗訊息還要含它（連結
測試的 ``LINK_FOLLOWED``，R15；只看 failure／error 的 ``message``）。``posix_only`` 的項目在
Windows 上、``windows_only`` 的項目在 POSIX 上記成 ``skipped_platform``，各到判得出的平台跑。
子行程的 ``PYTHONPATH`` 指向暫存區的複本，所以測到的是改壞的那份。結果每項一行 JSON 寫到
``<out>/zone_mutations.jsonl``。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_ITEMS = (
    "ToukaProxy",
    "ToukaMemory",
    "ToukaDispatch",
    "ToukaHeartbeat",
    "tests",
    "scripts",
    "pytest.ini",
)
# data 與 .env 不在受測程式的資料夾裡；萬一有，也不複製（不讀真資料與金鑰）
IGNORE = shutil.ignore_patterns(
    "__pycache__", "node_modules", ".venv", "*.pyc", ".pytest_cache", "data", ".env"
)
PYTEST_FAILED = 1
TIMEOUT_SECONDS = 1800
WINDOWS = os.name == "nt"


def _copy_tree(dest: Path) -> None:
    dest.mkdir()
    for name in COPY_ITEMS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=IGNORE)
        else:
            shutil.copy2(source, dest / name)


def _other_platform(item: dict) -> bool:
    """這一項只在另一個平台判得出（``posix_only``／``windows_only``）."""
    return bool(item.get("posix_only" if WINDOWS else "windows_only"))


def _env(work: Path) -> dict[str, str]:
    proxy, memory = work / "ToukaProxy", work / "ToukaMemory"
    paths = (proxy, memory, memory / "src", work / "ToukaDispatch", work / "ToukaHeartbeat")
    env = {k: v for k, v in os.environ.items() if k != "TOUKA_Z9_FULL_RUN"}  # noqa: ENV_SSOT_VIOLATION
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in paths)
    env["TOUKA_SANDBOX_KERNEL_DIR"] = str(proxy / "src" / "sandbox_kernel")
    return env


def _run_pytest(work: Path, basetemp: Path, junit: Path, tests: list[str]) -> tuple[int, str]:
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        f"--basetemp={basetemp}",
        f"--junitxml={junit}",
        "-q",
        "-rfEs",
        "-o",
        "log_cli=false",
    ]
    done = subprocess.run(
        argv,
        cwd=work,
        env=_env(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode, "\n".join(lines[-8:])


def _node_key(node: str) -> tuple[str, str]:
    """``tests/unit/test_x.py::Cls::name`` → (``tests.unit.test_x.Cls``, ``name``)."""
    path, *rest = node.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    return ".".join([module, *rest[:-1]]), rest[-1]


def failed_cases(junit: Path) -> dict[tuple[str, str], str]:
    """junitxml 裡失敗的測試：（classname, 不含參數的名稱）→ 失敗訊息（多個參數合併）.

    只取 ``message`` 屬性，不取 traceback 本文：本文會印出測試原始碼，``LINK_FOLLOWED`` 這個名稱就在
    原始碼裡，斷言因為別的原因失敗也會被當成有標記。
    """
    found: dict[tuple[str, str], str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        bad = [child for child in case if child.tag in ("failure", "error")]
        if not bad:
            continue
        key = (case.get("classname", ""), case.get("name", "").split("[", 1)[0])
        text = "\n".join(c.get("message", "") for c in bad)
        found[key] = found.get(key, "") + text
    return found


def verdict(item: dict, failed: dict[tuple[str, str], str]) -> list[str]:
    """沒有照預期失敗的測試（沒失敗，或失敗訊息沒有 ``marker``）."""
    missing = []
    for node in item["tests"]:
        text = failed.get(_node_key(node))
        if text is None or (item.get("marker") and item["marker"] not in text):
            missing.append(node)
    return missing


def _mutate(target: Path, old: str, new: str) -> bytes | None:
    original = target.read_bytes()
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    old, new = (s.replace("\r\n", "\n").replace("\n", newline) for s in (old, new))
    if text.count(old) != 1:
        return None
    target.write_bytes(text.replace(old, new).encode("utf-8"))
    return original


def main() -> int:
    """跑基準與全部改壞；有任何一項沒照預期失敗（或基準不綠）就回 1。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_suffix(".json"))
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.only:
        manifest = [item for item in manifest if item["id"] in set(args.only)]
    scratch = Path(tempfile.mkdtemp(prefix="touka-zone-mut-"))
    work = scratch / "wt"
    _copy_tree(work)
    runnable = [item for item in manifest if not _other_platform(item)]
    every_test = sorted({test for item in runnable for test in item["tests"]})
    records = []
    code, tail = _run_pytest(work, scratch / "pt-000", scratch / "j-000.xml", every_test)
    records.append({"id": "baseline", "exit": code, "green": code == 0, "tail": tail})
    for run, item in enumerate(manifest, start=1):
        if _other_platform(item):
            records.append({"id": item["id"], "skipped_platform": True})
            continue
        target = work / item["file"]
        original = _mutate(target, item["old"], item["new"])
        if original is None:
            records.append({"id": item["id"], "error": "pattern_not_unique"})
            continue
        junit = scratch / f"j-{run:03d}.xml"
        try:
            code, tail = _run_pytest(work, scratch / f"pt-{run:03d}", junit, item["tests"])
        finally:
            target.write_bytes(original)
        record = {"id": item["id"], "exit": code, "tail": tail}
        if code != PYTEST_FAILED or not junit.is_file():
            record.update(red=False, error=f"pytest_exit_{code}")
        else:
            missing = verdict(item, failed_cases(junit))
            record.update(red=not missing, not_red=missing)
        records.append(record)
    args.out.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    (args.out / "zone_mutations.jsonl").write_text(text, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    passed = ("green", "red", "skipped_platform")
    bad = [r["id"] for r in records if not any(r.get(key) for key in passed)]
    skipped = [r["id"] for r in records if r.get("skipped_platform")]
    summary = {"mutations": len(manifest), "not_red_or_failed": bad, "skipped_platform": skipped}
    sys.stdout.write(json.dumps(summary) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

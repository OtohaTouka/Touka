"""iso-c2 突變紀錄（只用標準函式庫；在一次性容器裡跑，見 Dockerfile.isolation-test）.

用法::

    python isolation_mutations.py --out <結果資料夾> [--manifest <json>]

做法：把受測程式（唯讀掛載）複製到暫存區；先跑一次沒有突變的基準（必須全綠），再依
``isolation_mutations.json`` 逐項把一行程式改壞、只跑對應的測試、還原。每項一行 JSON 寫到
``<out>/mutations.jsonl``：``red`` 為 true 表示測試確實失敗（exit 1）；collection 錯誤等其他
exit code 記成 ``error``，不算紅。子行程的 ``PYTHONPATH`` 與 ``TOUKA_SANDBOX_KERNEL_DIR`` 指向
暫存區的複本，所以測到的是改壞的那份。

每一輪的 ``--basetemp`` 只用序號（``pt-000`` 是基準），和突變 id 無關、每輪一樣長：執行器 socket
在 basetemp 底下，路徑超過上限時執行器不服務，那些測試不管有沒有突變都會紅；長度相同時基準全綠
就證明每一項都不是因為路徑長度而紅。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COPY_ITEMS = (
    "ToukaProxy",
    "ToukaMemory",
    "ToukaDispatch",
    "ToukaHeartbeat",
    "tests",
    "scripts",
    "pytest.ini",
)
IGNORE = shutil.ignore_patterns("__pycache__", "node_modules", ".venv", "*.pyc")
PYTEST_FAILED = 1
TIMEOUT_SECONDS = 900


def _copy_tree(dest: Path) -> None:
    dest.mkdir()
    for name in COPY_ITEMS:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=IGNORE)
        else:
            shutil.copy2(source, dest / name)


def _run_pytest(work: Path, basetemp: Path, tests: list[str]) -> tuple[int, str]:
    proxy = work / "ToukaProxy"
    memory = work / "ToukaMemory"
    python_path = ":".join(str(p) for p in (proxy, memory, memory / "src", work / "ToukaDispatch"))
    argv = [
        "env",
        f"PYTHONPATH={python_path}",
        f"TOUKA_SANDBOX_KERNEL_DIR={proxy / 'src' / 'sandbox_kernel'}",
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        f"--basetemp={basetemp}",
        "-q",
        "-rfEs",
    ]
    done = subprocess.run(
        argv, cwd=work, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
    return done.returncode, "\n".join(lines[-6:])


def _basetemp(scratch: Path, run: int) -> Path:
    """第 ``run`` 輪 pytest 的 ``--basetemp``（0 是基準）；不帶突變 id，每輪一樣長."""
    return scratch / f"pt-{run:03d}"


def _mutate(target: Path, old: str, new: str) -> str | None:
    original = target.read_bytes().decode("utf-8")
    # Windows checkout 可直接掛進 Linux 容器；匹配換行但保留還原用的原始位元組。
    newline = "\r\n" if "\r\n" in original else "\n"
    old, new = (text.replace("\r\n", "\n").replace("\n", newline) for text in (old, new))
    if original.count(old) != 1:
        return None
    target.write_bytes(original.replace(old, new).encode("utf-8"))
    return original


def main() -> int:
    """跑基準與全部突變；有任何一項沒變紅（或基準不綠）就回 1。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_suffix(".json"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp(prefix="touka-iso-mutations-"))
    work = scratch / "wt"
    _copy_tree(work)
    every_test = sorted({test for item in manifest for test in item["tests"]})
    records = []
    code, tail = _run_pytest(work, _basetemp(scratch, 0), every_test)
    baseline = {"id": "baseline", "tests": every_test, "exit": code, "green": code == 0}
    records.append({**baseline, "tail": tail})
    for run, item in enumerate(manifest, start=1):
        target = work / item["file"]
        original = _mutate(target, item["old"], item["new"])
        if original is None:
            records.append({"id": item["id"], "file": item["file"], "error": "pattern_not_unique"})
            continue
        try:
            code, tail = _run_pytest(work, _basetemp(scratch, run), item["tests"])
        finally:
            target.write_bytes(original.encode("utf-8"))
        record = {"id": item["id"], "file": item["file"], "tests": item["tests"], "exit": code}
        record.update(red=code == PYTEST_FAILED, tail=tail)
        if code not in {0, PYTEST_FAILED}:
            record["error"] = f"pytest_exit_{code}"
        records.append(record)
    args.out.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    (args.out / "mutations.jsonl").write_text(text, encoding="utf-8")
    shutil.rmtree(scratch, ignore_errors=True)
    bad = [r["id"] for r in records if not (r.get("green") or r.get("red"))]
    sys.stdout.write(json.dumps({"mutations": len(manifest), "not_red_or_failed": bad}) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

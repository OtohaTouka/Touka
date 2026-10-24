"""Report theme guard diagnostics."""

import subprocess
import sys
from collections import Counter
from pathlib import Path


def list_violations_summary() -> None:
    """Run the fixed theme guard and retain its diagnostic output."""
    cwd = Path(__file__).parent.parent / "ToukaChat"
    cmd = "npx tsx scripts/guard_theme.ts"
    result = subprocess.run(  # noqa: S602 - fixed existing theme guard command
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        encoding="utf-8",
        errors="ignore",
    )

    lines = result.stderr.split("\n")
    files = []

    for line in lines:
        if line.startswith("檔案:"):
            file_path = line.replace("檔案:", "").strip().split(":")[0]
            files.append(file_path)

    sys.stdout.write(f"Total Violations: {len(files)}\n")
    sys.stdout.write("-" * 40 + "\n")

    counts = Counter(files)
    for file, count in counts.most_common():
        sys.stdout.write(f"{count:3} | {file}\n")


if __name__ == "__main__":
    list_violations_summary()

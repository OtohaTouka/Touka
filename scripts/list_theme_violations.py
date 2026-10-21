"""Report theme guard diagnostics after verifying installed development bytes."""

import shutil
import subprocess
import sys
from pathlib import Path


def list_violations() -> None:
    """Run the fixed theme guard and retain its diagnostic output."""
    cwd = Path(__file__).parent.parent / "ToukaChat"
    cmd = "npx tsx scripts/guard_theme.ts"
    node = shutil.which("node")
    if node is None:
        message = "Node executable is required for installed-byte verification"
        raise FileNotFoundError(message)
    subprocess.run(  # noqa: S603 - fixed readonly installed-byte verifier
        [
            str(Path(node).resolve()),
            str(cwd / "container-security/braces-backport/backport.mjs"),
            "verify",
            "--project-root",
            str(cwd),
            "--scope",
            "full",
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
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
    violations = []
    current_file = ""

    for line in lines:
        if line.startswith("檔案:"):
            current_file = line.replace("檔案:", "").strip()
        elif line.startswith("代碼:"):
            code = line.replace("代碼:", "").strip()
            violations.append(f"{current_file} | {code}")

    sys.stdout.write(f"Total Violations Found: {len(violations)}\n")
    for v in violations:
        sys.stdout.write(f"{v}\n")


if __name__ == "__main__":
    list_violations()

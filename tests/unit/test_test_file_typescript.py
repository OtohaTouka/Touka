"""
Structural guard: verifies that the 9 previously-erroneous test files have no TypeScript errors.

ADR-0056: Test Mock Type Alignment
TDD Red before fixes, Green after all mocks match their source type definitions.

Runs npx tsc --noEmit (main tsconfig — includes all test files) and asserts that none
of the 9 target files appear in the error output.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
FRONTEND_DIR = REPO_ROOT / "ToukaChat"

TARGET_FILES = [
    "SoulStudio.test.tsx",
    "McpTab.test.tsx",
    "SecurityTab.test.tsx",
    "useSetupStatus.test.ts",
    "connections.logout.test.ts",
    "AuthGuard.test.tsx",
    "AuthPages.test.tsx",
    "connections.security.test.ts",
    "useAgentSSE.test.ts",
]


def test_target_test_files_have_no_typescript_errors():
    """All 9 previously-erroneous test files must compile cleanly with the main tsconfig."""
    result = subprocess.run(
        "npx tsc --noEmit",
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
        timeout=120,
        shell=True,
    )
    output = result.stdout + result.stderr
    error_lines = [
        line
        for line in output.splitlines()
        if "error TS" in line and any(f in line for f in TARGET_FILES)
    ]
    assert len(error_lines) == 0, (
        "TypeScript errors remain in target test files "
        "(see ADR-0056 for fix details):\n" + "\n".join(error_lines)
    )

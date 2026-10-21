"""Unit tests for SSOTGuard LocalImportRule (LOCAL_IMPORT_BAN).

TDD: these tests define the expected behaviour before implementation.
"""
import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from lint_ssot import SSOTGuard  # noqa: E402


def _parse_noqa(content: str) -> dict[int, set[str]]:
    """Extract # noqa: RULE_ID annotations from code string."""
    ignored: dict[int, set[str]] = {}
    for i, line in enumerate(content.splitlines(), 1):
        match = re.search(r"#\s*noqa:\s*([A-Za-z0-9_,\s]+)", line)
        if match:
            ignored[i] = {r.strip() for r in match.group(1).split(",")}
    return ignored


def check_code(code: str, filename: str = "src/services/some_service.py") -> list[dict]:
    """Run SSOTGuard on a code string and return all violations."""
    ignored_lines = _parse_noqa(code)
    tree = ast.parse(code)
    visitor = SSOTGuard(filename, ignored_lines)
    visitor.visit(tree)
    return visitor.violations


# ---------------------------------------------------------------------------
# LOCAL_IMPORT_BAN tests
# ---------------------------------------------------------------------------


def test_detects_import_inside_sync_function() -> None:
    """bare `import X` inside a sync function must be flagged."""
    code = """
def do_something():
    import os
    return os.getcwd()
"""
    violations = check_code(code)
    assert any(v["rule"] == "LOCAL_IMPORT_BAN" for v in violations)


def test_detects_from_import_inside_function() -> None:
    """`from X import Y` inside a function must be flagged."""
    code = """
def do_something():
    from pathlib import Path
    return Path(".")
"""
    violations = check_code(code)
    assert any(v["rule"] == "LOCAL_IMPORT_BAN" for v in violations)


def test_ignores_module_level_imports() -> None:
    """Top-level imports must NOT be flagged."""
    code = """
import os
from pathlib import Path

def do_something():
    return os.getcwd()
"""
    violations = check_code(code)
    assert not any(v["rule"] == "LOCAL_IMPORT_BAN" for v in violations)


def test_detects_in_nested_function_once() -> None:
    """Import inside a nested function is flagged for that function only."""
    code = """
def outer():
    def inner():
        import json
        return json.dumps({})
    return inner()
"""
    violations = check_code(code)
    local_import_v = [v for v in violations if v["rule"] == "LOCAL_IMPORT_BAN"]
    # Only `inner` has the local import; `outer` does not.
    assert len(local_import_v) == 1
    assert "inner" in local_import_v[0]["msg"]


def test_noqa_exemption() -> None:
    """A line marked `# noqa: LOCAL_IMPORT_BAN` must be suppressed."""
    code = """
def do_something():
    import os  # noqa: LOCAL_IMPORT_BAN
    return os.getcwd()
"""
    violations = check_code(code)
    assert not any(v["rule"] == "LOCAL_IMPORT_BAN" for v in violations)


def test_detects_inside_async_function() -> None:
    """Local import inside an `async def` must also be flagged."""
    code = """
async def fetch_data():
    import httpx
    return await httpx.get("https://example.com")
"""
    violations = check_code(code)
    assert any(v["rule"] == "LOCAL_IMPORT_BAN" for v in violations)

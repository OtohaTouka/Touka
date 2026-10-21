"""Path security utilities (P16 fix).

Provides symlink escape detection for vault path validation.
"""

from __future__ import annotations

from pathlib import Path


def validate_no_symlink_escape(path: Path, allowed_root: Path) -> bool:
    """Return True if path is inside allowed_root with no symlink escapes.

    Resolves every path component and verifies that the resolved path
    remains inside allowed_root, blocking symlink-based traversal attacks.

    Args:
        path: The candidate path to validate.
        allowed_root: The trusted root directory that path must stay within.

    Returns:
        True if safe, False if a symlink escape is detected.
    """
    try:
        resolved = path.resolve()
        root_resolved = allowed_root.resolve()
        return resolved.is_relative_to(root_resolved)
    except (OSError, ValueError):
        return False

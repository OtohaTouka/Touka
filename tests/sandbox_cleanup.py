"""Delete the pytest session sandbox after releasing whatever still pins it.

Standard library only: conftest registers the cleanup at interpreter exit
before any product import, and injects the product-specific release steps.
"""

from __future__ import annotations

import gc
import logging
import shutil
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

ReleaseStep = tuple[str, Callable[[], object]]

RMTREE_ATTEMPTS = 3
RMTREE_RETRY_SECONDS = 0.5


def remove_session_sandbox(root: Path, release_steps: Sequence[ReleaseStep]) -> Path | None:
    """Run every release step, then delete ``root``; return it only if part of it survives.

    Repeatable: a missing ``root`` means an earlier call already finished the job.
    """
    if not root.exists():
        return None
    for name, release in release_steps:
        try:
            release()
        except Exception:
            logger.warning("Sandbox release step %s failed", name, exc_info=True)
    gc.collect()
    for attempt in range(1, RMTREE_ATTEMPTS + 1):
        try:
            shutil.rmtree(root)
        except OSError as exc:
            logger.warning("Sandbox delete attempt %d failed (%s)", attempt, exc)
            time.sleep(RMTREE_RETRY_SECONDS)
        else:
            return None
    shutil.rmtree(root, ignore_errors=True)
    if not root.exists():
        return None
    logger.warning("Sandbox not fully deleted: %s", root)
    return root

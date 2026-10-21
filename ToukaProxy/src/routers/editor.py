"""IDE 檔案瀏覽與讀取路由。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_privileged

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/editor", tags=["Editor"])

# Require Owner/Debugger/admin to browse and read workspace files.
_require_editor_role = require_privileged

# 定義排除目錄
EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".next",
    "__pycache__",
    "venv",
    ".ruff_cache",
    "out",
    "dist",
}


@router.get("/files")
async def list_files(
    path: str = Query(".", description="相對路徑"),
    _identity: AgentIdentity = Depends(_require_editor_role),  # noqa: B008
) -> Any:
    """列出指定路徑下的檔案與資料夾，用於前端 @Mentions 功能。"""
    # C4 fix: use is_relative_to() to prevent sibling-directory bypass
    base_path = Path.cwd().resolve()
    target_path_obj = (base_path / path).resolve()

    if not target_path_obj.is_relative_to(base_path):
        # 防止路徑遍歷攻擊 — 回退至根目錄
        target_path_obj = base_path

    base_dir = str(base_path)
    target_path = str(target_path_obj)

    if not target_path_obj.exists():
        return {"items": [], "error": "Path not found"}

    try:
        items = []
        rel_path = "."
        for entry in os.scandir(target_path):
            if entry.name in EXCLUDE_DIRS:
                continue

            rel_path = os.path.relpath(entry.path, base_dir).replace("\\", "/")

            items.append(
                {
                    "name": entry.name,
                    "path": rel_path,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if entry.is_file() else 0,
                }
            )

        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    except Exception as exc:
        logger.exception("Error listing files")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(exc)) from exc
    else:
        return {
            "path": path,
            "current_dir": rel_path if path != "." else ".",
            "items": items,
        }

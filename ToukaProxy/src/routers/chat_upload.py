"""聊天上傳端點（Phase 2，A1）：``POST /v1/chat/upload``。

非圖片文件（pdf/docx/xlsx/pptx/大文字）上傳到**當下 agent 自己的 vault**，回相對路徑供前端
注入「請 AI 用 personal_drive read 工具讀全文 / 用 python 終端機解析」提示。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile  # noqa: TC002

from src.config import UPLOAD_MAX_BYTES
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_authenticated
from src.services.upload_service import save_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat-upload"])

# ADR-0245：未驗證由授權閘道擋下（401）。Guest 退役後，原本被擋的代理身分（自訂模型等）
# 解析為 Base；上傳只放行人類、admin 代打與 Owner／Debugger，其餘代理身分（Base、
# Subagent、無角色）一律拒絕（避免無主寫檔；不比原本的 Guest 拒絕寬）。
_require_authenticated = require_authenticated


@router.post("/upload")
async def upload_chat_file(
    file: UploadFile = File(...),  # noqa: B008
    session_id: str = Form(""),  # noqa: B008
    identity: AgentIdentity = Depends(_require_authenticated),  # noqa: B008
) -> dict[str, Any]:
    """上傳非圖片文件到當下 agent 自己的 vault，回 ``{filename, relpath, mime, size}``。

    身分由驗證後的 token 取得（``get_verified_identity``）→ 路徑由 identity 衍生、只寫呼叫者
    agent 自己的 vault（**無 IDOR、永不碰他人資料**）。只放行人類、admin 代打與
    Owner／Debugger，其餘一律拒絕（避免無主寫檔）。
    ``save_upload`` 驗證 magic-byte 型別 + 大小、穿越防護，並經 ResourceManager 落庫。
    Bearer-in-header 驗證本身即具 CSRF 防護（跨來源無法擅設 Authorization header）。
    """
    if not (identity.is_human or identity.is_privileged()):
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)
    # 有界讀取：只取到上限+1，避免 chunked/無 Content-Length 請求繞過 10MiB 中介層後把整個
    # body 灌進記憶體（複審 DoS）。超過上限即拒、不再續讀。
    data = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise ToukaException(ErrorCode.UPLOAD_TOO_LARGE)
    return save_upload(identity, session_id, file.filename or "upload", data)

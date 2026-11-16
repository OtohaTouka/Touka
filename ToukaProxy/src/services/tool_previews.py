"""核准前的預覽（ADR-0317）：先問清楚會做什麼，再決定誰核准.

註冊表照工具名找預覽器，執行器不認得任何工具名（OCP）。目前只有本機檔案工具的搬移與刪除
清單：小幫手先掃一次，回傳和它的視窗一樣的清單文字與指紋。卡片上給人看；決定送回去時，
小幫手比對指紋，確認核准的就是要做的那一份。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from src.models.local_bridge import LocalJobResult
from src.services.authorization_gateway import has_another_owner
from src.services.local_bridge import ACTION_NEEDING_CONFIRM, ACTION_PREVIEW, get_local_bridge
from src.services.local_bridge_pairing import get_local_pairing_store
from src.services.local_bridge_results import failure_text
from src.tools.contracts import validate_native_arguments
from src.utils.local_bridge_gate import AVAILABILITY_OFFERED, local_files_availability
from src.utils.local_bridge_signing import derive_decided_by

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from src.services.approval_service import ApprovalResolution

# 卡片放得下的字數；超過就截斷，這時卡片上的核准不算「看過清單」，小幫手會再跳視窗
PREVIEW_CARD_LIMIT: Final = 20_000
# 前端真的畫出預覽區塊時，核准請求帶回卡片上的這個值
PREVIEW_ACK_FIELD: Final = "preview_ack"
LOCAL_FILES_TOOL: Final = "local_files"
_NONCE: Final = re.compile(r"[A-Za-z0-9_-]{22}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


async def _quiet() -> None:
    return None


_BAD_PREVIEW: Final = LocalJobResult(
    status="error",
    code="bad_preview",
    message="The local helper sent a list Touka could not read, so nothing was changed.",
)


@dataclass(frozen=True)
class ToolPreview:
    """小幫手掃描到的清單。``text`` 是完整文字，卡片只放得下前 ``PREVIEW_CARD_LIMIT`` 字。

    ``approver_id`` 是配對的人：只有他在卡片上按的核准才算 ``user_card``。
    ``decided_by`` 由執行器在核准之後填，工具據此決定要不要送決定。
    """

    text: str
    nonce: str
    digest: str
    approver_id: str
    helper_window: bool = False
    ack: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    decided_by: str | None = None

    @property
    def complete(self) -> bool:
        """卡片放得下整份清單。"""
        return len(self.text) <= PREVIEW_CARD_LIMIT

    def card_field(self) -> dict[str, Any]:
        """審批卡的 ``preview``：放不下時不給 ``ack``，前端就送不出「看過了」。"""
        card: dict[str, Any] = {
            "text": self.text[:PREVIEW_CARD_LIMIT],
            "complete": self.complete,
            "helper_window": self.helper_window,
        }
        if self.complete:
            card["ack"] = self.ack
        return card


class Previewer(Protocol):
    """某個工具的預覽器。``preview`` 回 None＝不預覽，回字串＝給模型看的錯誤（不執行）。"""

    def applies(self, args: Mapping[str, Any]) -> bool:
        """這次呼叫要不要預覽。"""
        ...

    async def preview(
        self,
        args: dict[str, Any],
        ctx: Mapping[str, Any],
        *,
        request_model: str | None,
        announce: Callable[[], Awaitable[None]],
    ) -> ToolPreview | str | None:
        """問一次預覽；真的要等小幫手之前呼叫 ``announce``（畫面顯示掃描中）。"""
        ...


def _parse(data: Mapping[str, Any] | None, approver_id: str) -> ToolPreview | None:
    data = data or {}
    nonce, digest, text = data.get("nonce"), data.get("digest"), data.get("text")
    reason = data.get("window_reason")
    if (
        not isinstance(nonce, str)
        or _NONCE.fullmatch(nonce) is None
        or not isinstance(digest, str)
        or _HEX64.fullmatch(digest) is None
        or not isinstance(text, str)
        or not (reason is None or isinstance(reason, str))
    ):
        return None
    return ToolPreview(
        text=text,
        nonce=nonce,
        digest=digest,
        approver_id=approver_id,
        helper_window=reason is not None,
    )


class LocalFilesPreviewer:
    """搬移與刪除清單：小幫手掃描、不動手，回清單文字與指紋。"""

    def applies(self, args: Mapping[str, Any]) -> bool:
        """只有 ``propose``。"""
        return args.get("action") == ACTION_NEEDING_CONFIRM

    async def preview(
        self,
        args: dict[str, Any],
        ctx: Mapping[str, Any],
        *,
        request_model: str | None,
        announce: Callable[[], Awaitable[None]],
    ) -> ToolPreview | str | None:
        """工具不該出現在這一輪、或參數不合法時不預覽，交給之後原本的檢查回錯。"""
        bridge = get_local_bridge()
        pairing = get_local_pairing_store().current()
        availability = local_files_availability(
            ctx,
            request_model=request_model,
            pairing=pairing,
            online=bridge.online,
            has_other_owner=has_another_owner,
        )
        if pairing is None or availability != AVAILABILITY_OFFERED:
            return None
        validated, error = validate_native_arguments(LOCAL_FILES_TOOL, args)
        if error is not None:
            return None
        await announce()
        result = await bridge.submit(
            ACTION_PREVIEW,
            {"items": validated["items"], "reason": validated["reason"]},
            str(ctx.get("agent_id") or ""),
        )
        if result.status != "ok":
            return failure_text(result)
        return _parse(result.data, pairing.human_id) or failure_text(_BAD_PREVIEW)


PREVIEWERS: Final[dict[str, Previewer]] = {LOCAL_FILES_TOOL: LocalFilesPreviewer()}


async def tool_preview(
    name: str,
    args: dict[str, Any],
    ctx: Mapping[str, Any],
    *,
    request_model: str | None,
    builtin: bool,
    announce: Callable[[], Awaitable[None]] = _quiet,
) -> ToolPreview | str | None:
    """執行器在核准前呼叫。只認內建工具：同名的外掛或 MCP 工具不預覽。"""
    previewer = PREVIEWERS.get(name)
    if previewer is None or not builtin or not previewer.applies(args):
        return None
    return await previewer.preview(args, ctx, request_model=request_model, announce=announce)


def preview_acknowledged(preview: ToolPreview, resolution: ApprovalResolution | None) -> bool:
    """卡片上整份清單都顯示了，而且核准請求帶回了這張卡的 ``ack``。"""
    if resolution is None or not preview.complete:
        return False
    payload = resolution.response_payload or {}
    return secrets.compare_digest(str(payload.get(PREVIEW_ACK_FIELD, "")), preview.ack)


def decide(
    preview: ToolPreview,
    *,
    hitl_preapproved: bool,
    resolution: ApprovalResolution | None,
    approved_by: str | None,
    auth_level: str,
) -> ToolPreview:
    """核准之後填上誰決定的；回 None 的就不送決定，小幫手改跳視窗。"""
    decided_by = derive_decided_by(
        hitl_preapproved=hitl_preapproved,
        preview_shown_complete=preview_acknowledged(preview, resolution),
        resolver_matches=resolution is not None and resolution.resolver_id == preview.approver_id,
        approved_by=approved_by,
        auth_level=auth_level,
    )
    return replace(preview, decided_by=decided_by)

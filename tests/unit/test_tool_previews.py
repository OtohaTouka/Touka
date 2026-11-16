"""核准前的預覽（ADR-0317）：誰會被預覽、預覽失敗怎麼辦、核准之後誰算決定的人。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.models.identity import AgentIdentity
from src.models.local_bridge import LocalJobResult
from src.services import local_bridge as bridge_module
from src.services import tool_previews as previews_module
from src.services.approval_service import ApprovalResolution
from src.services.tool_errors import extract_error_class, extract_field
from src.services.tool_previews import (
    PREVIEW_ACK_FIELD,
    PREVIEW_CARD_LIMIT,
    ToolPreview,
    decide,
    tool_preview,
)

SOUL = AgentIdentity.for_role(agent_id="Soul", role="Owner")
PAIRED = MagicMock(human_id="Owner", generation=1)
ITEMS = [{"op": "delete", "path": "下載/a.txt"}]
ARGS = {"action": "propose", "items": ITEMS, "reason": "清"}
GOOD = {
    "nonce": "n" * 22,
    "digest": "d" * 64,
    "text": "丟到回收桶  D:\\下載\\a.txt",
    "items": 1,
    "entries": 1,
    "marks": [],
    "window_reason": None,
}


def _ctx(**overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "identity": SOUL,
        "agent_id": "Soul",
        "is_toukasoul": True,
        "resumable_stream": True,
        "human_principal_id": "Owner",
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    bridge = MagicMock()
    bridge.online = True
    bridge.submit = AsyncMock(return_value=LocalJobResult(status="ok", data=dict(GOOD)))
    store = MagicMock()
    store.current.return_value = PAIRED
    monkeypatch.setattr(previews_module, "get_local_bridge", lambda: bridge)
    monkeypatch.setattr(previews_module, "get_local_pairing_store", lambda: store)
    monkeypatch.setattr(previews_module, "has_another_owner", lambda *_args: False)
    return bridge


async def _preview(
    args: dict[str, Any] = ARGS, *, name: str = "local_files", builtin: bool = True, **ctx: Any
) -> ToolPreview | str | None:
    return await tool_preview(name, args, _ctx(**ctx), request_model="Soul", builtin=builtin)


@pytest.mark.asyncio
async def test_a_list_is_previewed_before_anyone_approves(bridge: MagicMock) -> None:
    preview = await _preview()
    assert isinstance(preview, ToolPreview)
    assert (preview.nonce, preview.digest, preview.text) == (
        GOOD["nonce"],
        GOOD["digest"],
        GOOD["text"],
    )
    assert preview.approver_id == "Owner"
    assert preview.decided_by is None
    assert preview.helper_window is False
    bridge.submit.assert_awaited_once_with(
        bridge_module.ACTION_PREVIEW, {"items": ITEMS, "reason": "清"}, "Soul"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "name", "builtin"),
    [
        ({"action": "list"}, "local_files", True),
        ({"action": "create_file", "path": "a", "content": "x"}, "local_files", True),
        (ARGS, "other_tool", True),
        # 同名的外掛或 MCP 工具不是小幫手
        (ARGS, "local_files", False),
        # 參數不合法：不預覽，之後由原本的驗證回錯
        (
            {"action": "propose", "items": [{"op": "chmod", "path": "a"}], "reason": "r"},
            "local_files",
            True,
        ),
    ],
)
async def test_only_the_helper_list_is_previewed(
    bridge: MagicMock, args: dict[str, Any], name: str, *, builtin: bool
) -> None:
    assert await _preview(args, name=name, builtin=builtin) is None
    bridge.submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ctx", [{"human_principal_id": "Someone"}, {"subagent_run": True}, {"resumable_stream": False}]
)
async def test_no_preview_when_the_tool_is_not_offered(
    bridge: MagicMock, ctx: dict[str, Any]
) -> None:
    assert await _preview(**ctx) is None
    bridge.submit.assert_not_called()


@pytest.mark.asyncio
async def test_an_offline_helper_stops_the_call(bridge: MagicMock) -> None:
    bridge.submit.return_value = LocalJobResult(status="error", code=bridge_module.CODE_OFFLINE)
    preview = await _preview()
    assert isinstance(preview, str)
    assert extract_error_class(preview) == "upstream_error"


@pytest.mark.asyncio
async def test_a_refused_preview_stops_the_call(bridge: MagicMock) -> None:
    bridge.submit.return_value = LocalJobResult(
        status="refused", code="protected", message="That folder is protected."
    )
    preview = await _preview()
    assert isinstance(preview, str)
    assert extract_error_class(preview) == "permission_denied"
    assert extract_field(preview, "reason") == "protected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"nonce": None},
        {"nonce": "short"},
        {"digest": "D" * 64},
        {"text": 7},
        {"window_reason": 1},
    ],
)
async def test_a_preview_touka_cannot_read_stops_the_call(
    bridge: MagicMock, change: dict[str, Any]
) -> None:
    bridge.submit.return_value = LocalJobResult(status="ok", data={**GOOD, **change})
    preview = await _preview()
    assert isinstance(preview, str)
    assert extract_field(preview, "reason") == "bad_preview"


@pytest.mark.asyncio
async def test_the_card_says_when_the_helper_will_ask_again(bridge: MagicMock) -> None:
    bridge.submit.return_value = LocalJobResult(
        status="ok", data={**GOOD, "window_reason": "marked"}
    )
    preview = await _preview()
    assert isinstance(preview, ToolPreview)
    assert preview.card_field()["helper_window"] is True


def _sample(text: str = "清單") -> ToolPreview:
    return ToolPreview(text=text, nonce="n" * 22, digest="d" * 64, approver_id="Owner")


def test_a_list_that_fits_carries_an_ack() -> None:
    card = _sample().card_field()
    assert card == {"text": "清單", "complete": True, "helper_window": False, "ack": card["ack"]}
    assert len(card["ack"]) >= 16
    assert _sample().ack != _sample().ack


def test_a_list_too_long_for_the_card_is_cut_and_has_no_ack() -> None:
    preview = _sample("x" * (PREVIEW_CARD_LIMIT + 1))
    card = preview.card_field()
    assert card["complete"] is False
    assert len(card["text"]) == PREVIEW_CARD_LIMIT
    assert "ack" not in card


def _approved(
    preview: ToolPreview, *, resolver: str = "Owner", ack: object = None
) -> ApprovalResolution:
    payload = {PREVIEW_ACK_FIELD: preview.ack if ack is None else ack}
    return ApprovalResolution(decision="accepted", response_payload=payload, resolver_id=resolver)


@pytest.mark.parametrize(
    ("hitl", "card", "approved_by", "level", "expected"),
    [
        # 配對的人在完整顯示的卡片上按了核准
        (True, "seen", None, "Manual", "user_card"),
        # 卡片太長、舊前端沒送 ack、送錯 ack、別人（管理員）代按：都讓小幫手跳視窗
        (True, "too_long", None, "Manual", None),
        (True, "no_ack", None, "Manual", None),
        (True, "wrong_ack", None, "Manual", None),
        (True, "someone_else", None, "Manual", None),
        # YOLO 下 hook 要人核准、卡片沒看完：不能退回 yolo
        (True, "no_ack", None, "YOLO", None),
        (False, None, "auto_reviewer", "Auto", "auto_review"),
        (False, None, "something_else", "Auto", None),
        (False, None, None, "YOLO", "yolo"),
        (False, None, None, "Manual", None),
        (False, None, None, "Auto", None),
    ],
)
def test_who_decided(
    hitl: bool, card: str | None, approved_by: str | None, level: str, expected: str | None
) -> None:
    preview = _sample("x" * (PREVIEW_CARD_LIMIT + 1)) if card == "too_long" else _sample()
    resolution = {
        None: None,
        "seen": _approved(preview),
        "too_long": _approved(preview),
        "no_ack": ApprovalResolution(decision="accepted", resolver_id="Owner"),
        "wrong_ack": _approved(preview, ack="not-the-ack"),
        "someone_else": _approved(preview, resolver="Admin"),
    }[card]
    decided = decide(
        preview,
        hitl_preapproved=hitl,
        resolution=resolution,
        approved_by=approved_by,
        auth_level=level,
    )
    assert decided.decided_by == expected
    assert (decided.nonce, decided.digest, decided.text) == (
        preview.nonce,
        preview.digest,
        preview.text,
    )

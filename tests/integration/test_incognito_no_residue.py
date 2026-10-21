"""無痕模式零殘留守衛（HAX G17，ADR-0086）。

端到端證明：incognito 對話請求跑完，payload 日誌**未寫入**（旗標貫穿＋log 閘門生效）；
對照組（非無痕）正常寫入。log_payload 在 process_proxy_request 對每請求無條件呼叫，
是「旗標是否傳達到後端」最乾淨的判別訊號。
"""

from unittest.mock import MagicMock

import pytest
from src.services import proxy_service

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)

_PAYLOAD_BASE = {
    "model": "claude-3-5-sonnet",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "hi"}],
    "stream": True,
}


@pytest.mark.asyncio
async def test_incognito_request_skips_payload_log(
    auth_client, mock_anthropic, mock_text_response, monkeypatch
):
    log_spy = MagicMock()
    monkeypatch.setattr(proxy_service, "log_payload", log_spy)
    mock_anthropic.messages.create.side_effect = [mock_text_response("Hi.")]
    auth_client.app.state.context["anthropic_client"] = mock_anthropic

    resp = auth_client.post(
        "/v1/messages", json={**_PAYLOAD_BASE, "session_id": "incog-1", "incognito": True}
    )
    assert resp.status_code == 200
    log_spy.assert_not_called()
    assert mock_anthropic.messages.create.call_count == 1  # 3b-U1：確實走到模型


@pytest.mark.asyncio
async def test_normal_request_writes_payload_log(
    auth_client, mock_anthropic, mock_text_response, monkeypatch
):
    log_spy = MagicMock()
    monkeypatch.setattr(proxy_service, "log_payload", log_spy)
    mock_anthropic.messages.create.side_effect = [mock_text_response("Hi.")]
    auth_client.app.state.context["anthropic_client"] = mock_anthropic

    resp = auth_client.post(
        "/v1/messages", json={**_PAYLOAD_BASE, "session_id": "normal-1"}
    )
    assert resp.status_code == 200
    log_spy.assert_called()
    assert mock_anthropic.messages.create.call_count == 1  # 3b-U1：確實走到模型

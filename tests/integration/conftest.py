"""Integration test for the ReAct loop."""

from dataclasses import asdict, dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


@dataclass
class ContentBlock:
    type: str
    text: str = None
    id: str = None
    name: str = None
    input: dict = None
    thinking: str = None

    def model_dump(self, **kwargs):
        return {k: v for k, v in asdict(self).items() if v is not None}

@pytest.fixture
def mock_anthropic():
    """Mock Anthropic client for integration tests."""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock()
    # 忠實模擬真實 anthropic client：with_options() 回傳「同一 client、套新選項後的檢視」
    # （共用連線池、不重建）。ADR-0118（root-cause-B）的 factory 會在有即時金鑰時呼叫
    # anthropic_client.with_options(api_key=…)；mock 若不回自身，被換掉的副本收到 create 呼叫、
    # 而測試檢查的原 mock 之 call_args 為 None——只有開發者 .env 有真實 ANTHROPIC_API_KEY 時才顯現。
    mock_client.with_options.return_value = mock_client
    return mock_client

@pytest.fixture
def mock_tool_response():
    """Create a mock Anthropic response with tool usage."""
    def _create(tool_name: str, tool_input: dict, call_id: str = "call_123"):
        mock_resp = MagicMock()
        mock_resp.content = [
            ContentBlock(type="text", text=f"I will use {tool_name}."),
            ContentBlock(type="tool_use", id=call_id, name=tool_name, input=tool_input)
        ]
        mock_resp.usage = MagicMock()
        mock_resp.usage.model_dump.return_value = {"input_tokens": 10, "output_tokens": 20}
        
        # 使其可以被 model_dump (在 proxy_service.py 中紀錄 payload 時會用到)
        mock_resp.model_dump.return_value = {
            "content": [b.model_dump() for b in mock_resp.content],
            "usage": mock_resp.usage.model_dump()
        }
        return mock_resp
    return _create

@pytest.fixture
def mock_text_response():
    """Create a mock Anthropic response with plain text."""
    def _create(text: str):
        mock_resp = MagicMock()
        mock_resp.content = [
            ContentBlock(type="text", text=text)
        ]
        mock_resp.usage = MagicMock()
        mock_resp.usage.model_dump.return_value = {"input_tokens": 5, "output_tokens": 15}
        mock_resp.model_dump.return_value = {
            "content": [b.model_dump() for b in mock_resp.content],
            "usage": mock_resp.usage.model_dump()
        }
        return mock_resp
    return _create

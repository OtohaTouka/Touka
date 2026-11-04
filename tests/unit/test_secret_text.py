"""憑證遮罩（N7/N9）：``key=`` 的門檻是 8 個字元；JSON 的引號寫法與網址參數也要遮。"""

from __future__ import annotations

import pytest
from src.services.models.manager import _sanitize_connection_error
from src.utils.secret_text import redact_credentials


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("401 Bearer abc123", "abc123"),
        ("Authorization xyz", "xyz"),
        ("called sk-live_123", "sk-live_123"),
        ("GET /v1?key=abcdefgh", "abcdefgh"),
        ("api_key: hunter22", "hunter22"),
        ("api-key=hunter22", "hunter22"),
        ("Password=hunter22", "hunter22"),
        ("https://user:pass@example.test/x", "user:pass"),
        ('{"password": "hunter 22"}', "hunter 22"),
        ('{"api_key":"abcd1234"}', "abcd1234"),
        ("{'password': 'hunter 22'}", "hunter 22"),
        ('{\\"password\\": \\"hunter22\\"}', "hunter22"),
        ("GET /v1?access_token=abc123&x=1", "abc123"),
        ("GET /v1?token=abc", "abc"),
        ("client_secret=abc123", "abc123"),
        ("secret: abc123", "abc123"),
        ("refresh_token: abc123", "abc123"),
        ("pwd=abc123", "abc123"),
        ("passwd: abc123", "abc123"),
    ],
)
def test_credentials_are_replaced(raw, secret):
    result = redact_credentials(raw)
    assert secret not in result
    assert "[REDACTED]" in result


@pytest.mark.parametrize(
    "raw", ["connection refused", "GET /v1?key=short", "keys are fine", "max_tokens: 100"]
)
def test_ordinary_text_is_left_alone(raw):
    assert redact_credentials(raw) == raw


def test_connection_errors_use_the_same_redaction():
    raw = "401 Bearer abc123 key=abcdefgh"
    assert _sanitize_connection_error(raw) == redact_credentials(raw)

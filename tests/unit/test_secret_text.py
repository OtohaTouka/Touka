"""憑證遮罩：搬到共用函式之後行為不變（N7/N9），``key=`` 的門檻是 8 個字元。"""

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
    ],
)
def test_credentials_are_replaced(raw, secret):
    result = redact_credentials(raw)
    assert secret not in result
    assert "[REDACTED]" in result


@pytest.mark.parametrize("raw", ["connection refused", "GET /v1?key=short", "keys are fine"])
def test_ordinary_text_is_left_alone(raw):
    assert redact_credentials(raw) == raw


def test_connection_errors_use_the_same_redaction():
    raw = "401 Bearer abc123 key=abcdefgh"
    assert _sanitize_connection_error(raw) == redact_credentials(raw)

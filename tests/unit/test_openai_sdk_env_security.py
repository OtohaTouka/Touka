"""3b-U4a：openai SDK 讀的環境變數不得改寫 Touka 的請求（ADR-0248 D4；DEV-A 附註 8）。

openai 3.24 建構 client 時讀 ``OPENAI_ORG_ID``／``OPENAI_PROJECT_ID``（加標頭）、
``OPENAI_CUSTOM_HEADERS``（任意標頭，含 Authorization）、``OPENAI_ADMIN_KEY``、
``OPENAI_WEBHOOK_SECRET``；``import openai`` 時讀 ``OPENAI_LOG``。``src.config`` 載入時與每個
建構點都清掉這批；``OPENAI_API_KEY``／``OPENAI_BASE_URL`` 是 Touka 自己的設定名，保留，建構點一律
顯式傳值。openai 與 httpx2 的 logger 在 logging 設定與清除時都釘在 WARNING。實際送出的請求在
test_responses_request.py 用 httpx2 MockTransport 驗。
"""

from __future__ import annotations

import logging

import pytest
from src import config
from src.core import logging_config

from tests.support.zone_roots import point_zone_roots

_ENV = {"OPENAI_API_KEY": "sk-env-not-used", "OPENAI_BASE_URL": "https://env.invalid/v1",
        "OPENAI_ORG_ID": "org-env", "OPENAI_PROJECT_ID": "proj-env",
        "OPENAI_CUSTOM_HEADERS": "X-From-Env: 1"}


@pytest.fixture
def sdk_loggers():
    loggers = [logging.getLogger(name) for name in ("openai", "httpx2")]
    original = [lg.level for lg in loggers]
    yield loggers
    for lg, level in zip(loggers, original, strict=True):
        lg.setLevel(level)


def test_strip_removes_only_sdk_override_names_and_pins_loggers(sdk_loggers):
    environ = {**_ENV, "OPENAI_ADMIN_KEY": "a", "OPENAI_WEBHOOK_SECRET": "w", "OPENAI_LOG": "debug"}
    for lg in sdk_loggers:
        lg.setLevel(logging.DEBUG)  # 等同 import openai 時依 OPENAI_LOG 調高
    assert config.strip_openai_sdk_env_overrides(environ) == (
        "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ADMIN_KEY", "OPENAI_CUSTOM_HEADERS",
        "OPENAI_WEBHOOK_SECRET", "OPENAI_LOG")
    assert environ == {"OPENAI_API_KEY": "sk-env-not-used",
                       "OPENAI_BASE_URL": "https://env.invalid/v1"}  # Touka 自己的設定名保留
    assert [lg.level for lg in sdk_loggers] == [logging.WARNING, logging.WARNING]


def test_logging_setup_pins_sdk_loggers(monkeypatch, tmp_path, sdk_loggers):
    point_zone_roots(monkeypatch, tmp_path, tmp_path / "zone_state")
    monkeypatch.setattr(logging_config.logging, "basicConfig", lambda **_kw: None)
    for lg in sdk_loggers:
        lg.setLevel(logging.DEBUG)
    try:
        logging_config.setup_logging()
    finally:
        logging_config.close_sandbox_log_handlers(str(tmp_path))
    assert [lg.level for lg in sdk_loggers] == [logging.WARNING, logging.WARNING]

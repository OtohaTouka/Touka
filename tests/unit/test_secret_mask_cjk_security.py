"""金鑰遮蔽的缺口（ADR-0324 PR 2；ADR-0319 決定 4 記下的已知限制）.

``redact_secrets_from_content`` 除了紀錄，也用在 MCP 資源與提示送給 AI 前的遮蔽、備份錯誤輸出與稽核。
以前後端的 ``\\b`` 把中文當字母，金鑰前後直接接中文時遮不到；改成 re.ASCII 後空白寫成明確字元集，
全形與不換行空白後面的 Bearer 權杖照樣遮。另外補上幾種常見金鑰與標頭；`Authorization` 鍵名與字串裡
``"鍵名": "值"`` 的值只在紀錄裡另外遮，設定頁不受影響。
"""

from __future__ import annotations

import pytest
from src.utils.secrets import (
    MASK,
    is_record_secret_key,
    is_secret_key,
    mask_record_key_values,
    mask_value,
    redact_secrets_from_content,
)
from src.utils.tool_records import RECORD_DETAILED, render_tool_record

_SK = "sk-abcdefghijklmnop1234"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
_AIZA = "AIza" + "B" * 34 + "-"


def _redacted(text: str) -> str:
    return redact_secrets_from_content(text)[0]


# --------------------------------------------------------------------------- #
# 1. 金鑰前後直接接中文
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        f"金鑰{_SK}",
        f"{_SK}是金鑰",
        "金鑰Bearer abcdefghijklmnop",
        f"權杖{_JWT}結束",
        "金鑰ghp_abcdefghijklmnopqrst",
    ],
)
def test_secrets_glued_to_chinese_text_are_redacted(text: str):
    out = _redacted(text)
    assert "<redacted>" in out
    assert "abcdefghijklmnop" not in out
    assert "dozjgNryP4J3jVmNHl0w5N" not in out


# --------------------------------------------------------------------------- #
# 2. 只認英數之後，Bearer 後面的各種空白照樣遮（MCP 遮蔽拿的是原文）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("space", [" ", "\t", "\xa0", "\u3000", "\u2003", "\u202f", "\ufeff", "\x85"])
def test_bearer_after_any_space_is_still_redacted(space: str):
    assert _redacted(f"Bearer{space}abcdefghijklmnop") == "<redacted>"


# --------------------------------------------------------------------------- #
# 3. 新加的共用樣式
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "secret",
    [
        _AIZA,
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Authorization:\u3000Bearer short1",
        "authorization: bearer abc",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghij",
        "sk_live_abcdefghijklmnop",
        "rk_live_abcdefghijklmnop",
    ],
)
def test_more_known_secret_shapes_are_redacted(secret: str):
    assert _redacted(f"value {secret} end") == "value <redacted> end"


@pytest.mark.parametrize(
    "text",
    [
        "Basic configuration is fine",
        "authorization_url=https://example.com/oauth",
        "AIza-too-short",
        "Bearer short",
        "the sky-is-blue",
    ],
)
def test_ordinary_text_is_not_redacted(text: str):
    assert _redacted(text) == text


# --------------------------------------------------------------------------- #
# 4. 鍵名：共用的多認 apiKey 類；Authorization 只給紀錄；怪字元大小寫和前端一致
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["apiKey", "x-api-key", "OPENAI_APIKEY", "api_key", "MY_SECRET"])
def test_shared_key_names(key: str):
    assert is_secret_key(key)


def test_authorization_is_masked_only_in_records():
    assert not is_secret_key("Authorization")
    assert is_record_secret_key("Authorization")
    # 設定頁看得到不是金鑰的設定
    assert mask_value("OAUTH_AUTHORIZATION_URL", "https://example.com") == "https://example.com"
    assert mask_value("OPENAI_APIKEY", "real") == MASK


@pytest.mark.parametrize("key", ["ſecret", "toKen", "apiıkey"])
def test_odd_case_folding_letters_are_not_keys(key: str):
    """ſ、K（克耳文）、ı 不當英文字母（和前端不帶 u 旗標的 /i 一致）."""
    assert not is_secret_key(key)
    assert not is_record_secret_key(key)


def test_key_value_pairs_inside_strings_are_masked_in_records():
    text = '{"password": "hunter2", "user": "amy", "Authorization":"Basic xyz", "token_count": "5"}'
    out = mask_record_key_values(text)
    assert "hunter2" not in out
    assert '"user": "amy"' in out
    assert "xyz" not in out
    assert f'"password": "{MASK}"' in out


def test_records_mask_authorization_keys_at_any_depth():
    steps = [
        {
            "id": "c1",
            "tool": "http",
            "args": {
                "Authorization": "Basic xyz",
                "headers": {"Authorization": "Basic nested-secret"},
                # 值是物件：字串裡的「"鍵名": "值"」遮不到，要靠巢狀鍵名
                "h": {"Authorization": {"v": "objsecret1"}},
            },
            "status": "success",
            "result": '{"password": "hunter2"}',
        }
    ]
    out = render_tool_record(steps, RECORD_DETAILED)
    assert "xyz" not in out
    assert "nested-secret" not in out
    assert "objsecret1" not in out
    assert "hunter2" not in out
    assert MASK in out

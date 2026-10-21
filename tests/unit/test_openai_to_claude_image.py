"""A1 修復：OpenAI ``image_url`` 多模態 block → Anthropic image block 轉換。

背景：`convert_openai_to_anthropic` 過去對 ``image_url`` 直接 ``pass``（靜默丟棄圖片，
見稽核 A1）。本測試先行（TDD-Red）鎖定行為契約：
- data URI(base64) → ``{"type":"image","source":{"type":"base64",...}}``
- http(s) URL → ``{"type":"image","source":{"type":"url","url":...}}``
- 無法解析/不支援 → ``None``（best-effort 略過、不拋例外、不破壞其餘 block）
- 端到端：含圖片的 user 訊息經 convert 後，圖片 block **不再被丟棄**。

純函式測試（不觸碰任何 agent 資料/磁碟）。
"""

import pytest
from src.adapters.openai_to_claude import (
    _openai_image_block_to_anthropic,
    convert_openai_to_anthropic,
)
from src.adapters.schemas import OpenAIChatRequest

# 1×1 PNG 的 base64（內容不重要，僅驗證原樣搬運；不含逗號）。
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_data_uri_base64_to_base64_source():
    block = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"}}
    assert _openai_image_block_to_anthropic(block) == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    }


def test_data_uri_default_media_type_when_absent():
    block = {"type": "image_url", "image_url": {"url": f"data:;base64,{_PNG_B64}"}}
    out = _openai_image_block_to_anthropic(block)
    assert out["source"]["type"] == "base64"
    assert out["source"]["media_type"] == "image/jpeg"  # 缺 media_type → 預設
    assert out["source"]["data"] == _PNG_B64


def test_data_uri_tolerates_extra_params():
    block = {"type": "image_url", "image_url": {"url": f"data:image/webp;name=x;base64,{_PNG_B64}"}}
    out = _openai_image_block_to_anthropic(block)
    assert out["source"]["media_type"] == "image/webp"
    assert out["source"]["data"] == _PNG_B64


@pytest.mark.parametrize("url", ["https://example.com/cat.png", "http://example.com/cat.jpg"])
def test_http_url_to_url_source(url):
    block = {"type": "image_url", "image_url": {"url": url}}
    assert _openai_image_block_to_anthropic(block) == {
        "type": "image",
        "source": {"type": "url", "url": url},
    }


def test_bare_string_image_url_tolerated():
    # 防禦：image_url 直接給字串（非 {"url": ...} 物件）。
    block = {"type": "image_url", "image_url": "https://example.com/cat.png"}
    out = _openai_image_block_to_anthropic(block)
    assert out["source"] == {"type": "url", "url": "https://example.com/cat.png"}


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image_url"},  # 缺 image_url
        {"type": "image_url", "image_url": {}},  # 缺 url
        {"type": "image_url", "image_url": {"url": ""}},  # 空 url
        {"type": "image_url", "image_url": {"url": "ftp://x/y.png"}},  # 非 http/data
        {"type": "image_url", "image_url": {"url": "data:image/svg+xml,<svg/>"}},  # data 非 base64
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},  # 空 base64 資料
        {"type": "image_url", "image_url": 123},  # 型別錯誤
    ],
)
def test_unparseable_returns_none(block):
    assert _openai_image_block_to_anthropic(block) is None


def test_convert_preserves_image_end_to_end():
    """A1 回歸：含圖片的 user 訊息經 convert 後，文字與圖片 block 皆保留（圖片不再被丟棄）。"""
    req = OpenAIChatRequest(
        model="claude-4-6",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "這是什麼？"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"},
                    },
                ],
            }
        ],
    )
    out = convert_openai_to_anthropic(req)
    user_msgs = [m for m in out.messages if m.role == "user"]
    assert user_msgs, "應有 user 訊息"
    blocks = user_msgs[-1].content
    types = [b.get("type") for b in blocks]
    assert "text" in types
    assert "image" in types  # 修復前此處會失敗（圖片被 pass 丟棄）
    img = next(b for b in blocks if b.get("type") == "image")
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"] == _PNG_B64


def test_convert_drops_unconvertible_image_gracefully():
    """無法轉換的圖片 block 不致使轉換崩潰；文字仍保留、且不混入壞 image block。"""
    req = OpenAIChatRequest(
        model="claude-4-6",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": "data:image/svg+xml,<svg/>"}},
                ],
            }
        ],
    )
    out = convert_openai_to_anthropic(req)
    blocks = [m for m in out.messages if m.role == "user"][-1].content
    types = [b.get("type") for b in blocks]
    assert "text" in types
    assert "image" not in types  # 壞圖片被略過、不混入

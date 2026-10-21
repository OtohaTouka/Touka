"""``AnthropicThinking`` 只定義一次，OpenAPI 只有一個元件（ADR-0250 C6）。

原本 ``adapters/schemas.py`` 定義兩次（欄位相同），OpenAI 形狀與 Claude 形狀的請求各指向
不同的類別，OpenAPI 因此產生 ``…AnthropicThinking__1``／``__2`` 兩個元件。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from src.adapters import schemas

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ToukaProxy" / "src" / "adapters" / "schemas.py"


def test_schemas_defines_anthropic_thinking_once():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    defs = [n.lineno for n in tree.body if isinstance(n, ast.ClassDef)
            and n.name == "AnthropicThinking"]
    assert len(defs) == 1, defs


def test_both_request_shapes_share_the_same_thinking_model():
    openai_field = schemas.OpenAIChatRequest.model_fields["thinking"].annotation
    claude_field = schemas.AnthropicRequest.model_fields["thinking"].annotation
    assert openai_field == claude_field == (schemas.AnthropicThinking | None)


def test_openapi_has_one_thinking_component():
    components = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))["components"]
    names = [name for name in components["schemas"] if "AnthropicThinking" in name]
    assert names == ["AnthropicThinking"]

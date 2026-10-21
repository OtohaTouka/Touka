"""P3d 總量上限閘（opt-in 累計成本上限）— autonomy 預算判定 ＋ handler 續跑閘。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.claude_handler import handle_claude_request
from src.models.autonomy import AutonomyConfigModel
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.providers import pricing
from src.services.rhythm_service import rhythm_service
from src.services.telemetry import accumulate_token_usage

from tests.helpers.claude_stream import delegate_stream_to_create


def test_autonomy_model_carries_quota_and_cap_roundtrip():
    """ADR-0094：quota/cap 為一級欄位——admin GET/POST 的 model_dump 不再丟棄它們。

    修復前 AutonomyConfigModel 無這兩欄，extra='ignore' 會在 model_validate→model_dump
    時把使用者送入的 cap/quota 整個丟掉（成本上限永遠存不進設定、永遠失效）。
    """
    m = AutonomyConfigModel()
    assert m.quota.daily_max == 50
    assert m.cap.enabled is False
    assert m.cap.max_total_cost_usd is None

    raw = {"cap": {"enabled": True, "max_total_cost_usd": 12.5}, "quota": {"daily_max": 99}}
    dumped = AutonomyConfigModel.model_validate(raw).model_dump()
    assert dumped["cap"] == {"enabled": True, "max_total_cost_usd": 12.5}
    assert dumped["quota"]["daily_max"] == 99


def test_is_over_budget_honors_model_validated_config(monkeypatch):
    """經 admin 路徑（model_dump）產生的設定能真正驅動 is_over_budget（端到端修復驗證）。"""
    identity = AgentIdentity.for_role(agent_id="BudgetReal", role="Owner")
    cfg = AutonomyConfigModel.model_validate(
        {"cap": {"enabled": True, "max_total_cost_usd": 10.0}}
    ).model_dump()
    monkeypatch.setattr(autonomy_service, "config", cfg)
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda i: 11.0)
    assert autonomy_service.is_over_budget(identity) is True


def test_get_total_cost_usd_sums_providers(monkeypatch):
    monkeypatch.setattr(
        "src.services.autonomy.resource_manager.read_json",
        lambda p: {"Anthropic": {"total_cost_usd": 1.5}, "Gemini": {"total_cost_usd": 0.5}},
    )
    cost = autonomy_service.get_total_cost_usd(AgentIdentity.for_role(
        agent_id="BudgetX", role="Owner")
    )
    assert cost == 2.0


def test_is_over_budget_respects_cap_config(monkeypatch):
    identity = AgentIdentity.for_role(agent_id="BudgetTester", role="Owner")
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda i: 12.0)

    # 未啟用 → 一律 False（不讀成本）
    monkeypatch.setitem(autonomy_service.config, "cap", {"enabled": False, "max_total_cost_usd": 10.0})
    assert autonomy_service.is_over_budget(identity) is False

    # 啟用且超過 → True
    monkeypatch.setitem(autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": 10.0})
    assert autonomy_service.is_over_budget(identity) is True

    # 啟用但未超過 → False
    monkeypatch.setitem(autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": 20.0})
    assert autonomy_service.is_over_budget(identity) is False

    # 啟用但未設上限 → False
    monkeypatch.setitem(autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": None})
    assert autonomy_service.is_over_budget(identity) is False


def test_is_over_budget_boundary_cost_equals_cap(monkeypatch):
    """成本『正好等於』上限即應停（>= 邊界）。

    現有 respects_cap_config 只測 over(12>10)/under(12<20)，從未測相等情形，
    故 `>=`→`>` 變異存活（成本剛好打平時不擋，錢會繼續花）。本測試釘死該邊界。
    """
    identity = AgentIdentity.for_role(agent_id="BudgetEdge", role="Owner")
    monkeypatch.setitem(
        autonomy_service.config, "cap", {"enabled": True, "max_total_cost_usd": 10.0}
    )
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda i: 10.0)
    assert autonomy_service.is_over_budget(identity) is True



@pytest.mark.parametrize(("model", "agent", "cost", "cap", "over"), [
    # 1M 輸入＋1M 快取讀：10＋0.25；泛用 claude 價只記 3.3，落到 Fable 5 會記 11.0
    ("claude-fable-5-1", "PricingGateFable51", 10.25, 10.0, True),
    ("claude-opus-4-8", "PricingGateOpus48", 5.5, 10.0, False),  # claude-opus-4 價會記 16.5
    ("claude-haiku-5-5", "PricingGateHaiku55", 0.55, 1.0, False),  # 泛用價會記 3.3
])
def test_cost_cap_charges_new_claude_models_at_their_own_price(monkeypatch, model, agent, cost,
                                                              cap, over):
    """R-3a（P24）：入帳照新價格，成本上限閘據此判斷。"""
    monkeypatch.setattr(pricing.config_utils, "read_config_json", lambda _name: None)
    identity = AgentIdentity.for_role(agent_id=agent, role="Owner")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
             "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
    accumulate_token_usage(identity, "Anthropic", usage, model=model)
    monkeypatch.setitem(autonomy_service.config, "cap",
                        {"enabled": True, "max_total_cost_usd": cap})
    assert autonomy_service.get_total_cost_usd(identity) == cost
    assert autonomy_service.is_over_budget(identity) is over


def _text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 3, "output_tokens": 2}
    resp.model_dump.return_value = {
        "id": "m",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return resp


def _tool_response(name: str = "vault_tools") -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.id = "toolu_budget_1"
    block.input = {"action": "list"}
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 3, "output_tokens": 2}
    resp.model_dump.return_value = {
        "id": "m",
        "model": "claude-3-5-sonnet",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_budget_1", "name": name, "input": {}}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    return resp


@pytest.mark.asyncio
async def test_claude_handler_stops_autonomous_loop_when_over_budget():
    """超過總量上限 → 工具輪零執行（fail-closed）＋警告＋最後一次無工具收尾。

    ADR-0146：成本閘移到工具分支每輪檢查（舊制只在純文字分支查＝狂調工具的
    路徑是盲區）。模型吐工具調用但已超額 → 不執行、注入收尾提示、以
    tool_choice=none 再呼叫一次取得總結（LLM 共兩次呼叫、工具零執行）。
    """
    request_id = "test-budget-req-1"
    rhythm_service.clear_session(request_id)
    client = MagicMock()
    tool_mock = AsyncMock(return_value="should-not-run")
    client.messages.create = AsyncMock(
        side_effect=[_tool_response(), _text_response("上限到了，總結收尾。")]
    )
    context = {
        "anthropic_client": delegate_stream_to_create(client),  # 3b-U1：主迴圈走 messages.stream()
        "protocol_manager": MagicMock(),
        "tool_registry": {"vault_tools": tool_mock},
        "memory_tool_registry": {},
        "plugin_registry": {},
        "request_id": request_id,
        "t0": 0.0,
        "agent_id": "Charlie",
        "identity": AgentIdentity.for_role(agent_id="Charlie", role="Owner", capabilities=["*"]),
        "is_toukasoul": True,
        "session_id": "sess-budget-1",
        "model": "claude-3-5-sonnet",
    }
    request = MagicMock()
    request.model = "claude-3-5-sonnet"
    request.max_tokens = None  # 真實請求的預設（3a 起轉換器會讀）
    request.pulse_slots = None
    request.model_dump.return_value = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [],
    }
    request.messages = [MagicMock(content="hi", role="user")]

    with patch(
        "src.handlers.react_core.autonomy_service.is_over_budget", return_value=True
    ):
        chunks: list[str] = []
        async for c in handle_claude_request(request, context):
            chunks.append(c)
            if len(chunks) > 80:
                break

    # 超預算 → 工具零執行；LLM 兩次呼叫（原輪＋無工具收尾輪）
    assert tool_mock.await_count == 0
    assert client.messages.create.call_count == 2
    # 收尾輪必須禁用工具
    assert client.messages.create.call_args_list[-1].kwargs.get("tool_choice") == {"type": "none"}
    # 有送出上限警告（解碼 SSE content）
    texts = []
    for c in chunks:
        payload = c[len("data: "):].strip() if c.startswith("data: ") else ""
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
        if delta:
            texts.append(delta)
    assert "已達總量上限" in "".join(texts)
    rhythm_service.clear_session(request_id)

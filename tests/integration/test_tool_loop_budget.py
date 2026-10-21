"""#4-Q1（ADR-0146）——工具輪預算取代 nudge 自主續圈（實作前＝紅）。

owner 拍板的語意翻轉：
1. ③ nudge 續圈（模型回純文字被硬推繼續）**直接移除**——純文字＝回合結束，
   與主流 harness（Claude Code/Agent SDK）一致；中文停止詞逐字匹配（grok-4.5
   永遠燒滿 5 slots 的病根）隨之退役。
2. 「ReAct 上限」自此指 ② 工具迴圈（``while True`` 工具調用續圈——修前無任何
   上限、裸奔）：slots 語意從「系統逼幾輪」翻轉為「工具輪保險絲」。
3. 分頻鐵則：**互動（有人在場）預設不限**（0＝不限；煞車＝人類中斷＋HITL＋
   成本閘）；**無人 run（heartbeat）一律有限**——排程值＞全域保險絲＞保底 50，
   全域設 0 也強制退保底（HEADLESS_FALLBACK_ROUNDS）。
4. 額度耗盡＝優雅著陸：本輪工具**不執行**（fail-closed），改走「最後一次
   無工具收尾呼叫」（tool_choice=none）讓模型消化既有結果後總結。
5. 每日配額只計無人 pulse（舊制反了：互動聊天扣配額、心跳反而不扣，且
   check_quota 從無執法點）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from src.services.autonomy import AutonomyService, autonomy_service
from src.services.identity_router import get_optional_identity, get_verified_identity
from src.services.permission_service import permission_service
from src.services.rhythm_service import (
    HEADLESS_FALLBACK_ROUNDS,
    rhythm_service,
)
from src.utils.permission_grants import NameGrant

from tests.helpers.claude_stream import delegate_stream_to_create


@pytest.fixture
def mock_anthropic(mock_anthropic):
    """主迴圈走 messages.stream()：本地包裝委派給 create（3b-U1；conftest 不改）。"""
    return delegate_stream_to_create(mock_anthropic)

# ── get_effective_rounds：分頻解析矩陣 ───────────────────────────────────────────


def _cfg(rounds: int) -> dict:
    return {"react_loop": {"max_tool_rounds": rounds}}


def test_interactive_default_is_unlimited():
    """互動＋全域 0 → 0（不限）。"""
    assert rhythm_service.get_effective_rounds(_cfg(0), None, headless=False) == 0


def test_headless_fallback_is_iron_rule():
    """無人＋全域 0 → 強制保底 50——無人 run 絕不允許無限。"""
    assert (
        rhythm_service.get_effective_rounds(_cfg(0), None, headless=True)
        == HEADLESS_FALLBACK_ROUNDS
    )
    assert HEADLESS_FALLBACK_ROUNDS == 50


def test_global_fuse_applies_to_both_modes():
    """全域保險絲 >0 時兩種模式都吃它。"""
    assert rhythm_service.get_effective_rounds(_cfg(30), None, headless=False) == 30
    assert rhythm_service.get_effective_rounds(_cfg(30), None, headless=True) == 30


def test_schedule_rounds_take_priority():
    """排程明示值（pulse 深度）優先於全域。"""
    assert rhythm_service.get_effective_rounds(_cfg(30), 20, headless=True) == 20
    assert rhythm_service.get_effective_rounds(_cfg(0), 20, headless=True) == 20


def test_invalid_config_degrades_safely():
    """設定缺失/非法 → 互動不限、無人保底（fail-safe 兩個方向都對）。"""
    for bad in (
        {},
        {"react_loop": {}},
        {"react_loop": {"max_tool_rounds": -3}},
        {"react_loop": {"max_tool_rounds": "abc"}},
    ):
        assert rhythm_service.get_effective_rounds(bad, None, headless=False) == 0
        assert (
            rhythm_service.get_effective_rounds(bad, None, headless=True)
            == HEADLESS_FALLBACK_ROUNDS
        )


# ── consume_round：預算消耗語意 ─────────────────────────────────────────────────


def test_consume_round_unlimited_never_exhausts():
    rid = "budget-unlimited"
    rhythm_service.initialize_session(rid, 0)  # 0 = 不限
    assert all(rhythm_service.consume_round(rid) for _ in range(200))
    rhythm_service.clear_session(rid)


def test_consume_round_bounded_then_denies():
    rid = "budget-bounded"
    rhythm_service.initialize_session(rid, 2)
    assert rhythm_service.consume_round(rid) is True
    assert rhythm_service.consume_round(rid) is True
    assert rhythm_service.consume_round(rid) is False  # 耗盡
    assert rhythm_service.consume_round(rid) is False  # 拒絕是黏著的
    rhythm_service.clear_session(rid)


def test_consume_round_uninitialized_is_unlimited():
    """未初始化（防禦性）＝不限——絕不能把正常請求誤判為耗盡。"""
    assert rhythm_service.consume_round("budget-never-initialized") is True


def test_progress_accounting_for_bounded_runs():
    """有限 run 的 used/total 供進度事件（wire 形狀沿用 react_slots_event）。"""
    rid = "budget-progress"
    rhythm_service.initialize_session(rid, 3)
    rhythm_service.consume_round(rid)
    assert rhythm_service.get_total_rounds(rid) == 3
    assert rhythm_service.get_used_rounds(rid) == 1
    rhythm_service.clear_session(rid)


# ── 舊機制退役（防退化錨）───────────────────────────────────────────────────────


def test_nudge_machinery_is_gone():
    """nudge 提示與停止詞匹配自 rhythm_service 移除（死碼不留）。"""
    assert not hasattr(rhythm_service, "get_nudge_prompt")
    assert not hasattr(rhythm_service, "should_stop_react")
    assert not hasattr(rhythm_service, "decrement_slots")


# ── 設定遷移（決策 3：改鍵名＋遷移）────────────────────────────────────────────


def test_legacy_config_normalized_in_memory(tmp_path):
    """舊制 react_loop（default_max_slots/stop_keywords/nudge_prompt）＋root
    遺留鍵 → 載入時正規化為新形（max_tool_rounds=0）；**不搬舊數值**——舊 5 是
    「nudge 輪數」，搬過來會把互動工具迴圈砍到 5 輪（語意翻轉不可搬值）。
    bootstrap 停用（測試沙箱）時檔案不回寫。"""
    legacy = {
        "react_loop": {
            "default_max_slots": 5,
            "stop_keywords": ["任務已完成"],
            "nudge_prompt": "請繼續（{slots}）",
        },
        "max_react_slots": 5,
        "max_repair_attempts": 5,
        "quota": {"daily_max": 50},
    }
    cfg_file = tmp_path / "autonomy.json"
    cfg_file.write_text(json.dumps(legacy), encoding="utf-8")

    svc = AutonomyService.__new__(AutonomyService)
    svc.config = {}
    svc._interrupted_runs = []
    # 服務以模組常數尋檔——載入器接受注入路徑（測試專用）
    svc._load_config(config_file=cfg_file)

    react = svc.config.get("react_loop", {})
    assert react.get("max_tool_rounds") == 0, "語意翻轉：不搬舊 nudge 輪數"
    for dead in ("default_max_slots", "stop_keywords", "nudge_prompt"):
        assert dead not in react
    assert "max_react_slots" not in svc.config
    assert "max_repair_attempts" not in svc.config
    # 沙箱（bootstrap 停用）不回寫檔案
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "default_max_slots" in on_disk["react_loop"]


def test_new_shape_config_loads_verbatim(tmp_path):
    cfg_file = tmp_path / "autonomy.json"
    cfg_file.write_text(
        json.dumps({"react_loop": {"max_tool_rounds": 30}, "quota": {"daily_max": 50}}),
        encoding="utf-8",
    )
    svc = AutonomyService.__new__(AutonomyService)
    svc.config = {}
    svc._interrupted_runs = []
    svc._load_config(config_file=cfg_file)
    assert svc.config["react_loop"]["max_tool_rounds"] == 30


# ── 每日配額：只計無人 pulse（決策 2）──────────────────────────────────────────


def test_consume_quota_counts_and_check_quota_gates(mock_identity):
    """consume_quota +1／check_quota 在 daily_max 時擋下（首次真正有執法語意）。"""
    identity = mock_identity(agent_id="QuotaPulseAgent")
    before = autonomy_service.get_daily_quota(identity)["daily_count"]
    autonomy_service.consume_quota(identity, "pulse-req-1", rounds=50)
    after = autonomy_service.get_daily_quota(identity)["daily_count"]
    assert after == before + 1

    # 灌到上限 → check_quota False
    daily_max = autonomy_service.config.get("quota", {}).get("daily_max", 50)
    for i in range(daily_max):
        autonomy_service.consume_quota(identity, f"pulse-req-fill-{i}")
    assert autonomy_service.check_quota(identity) is False


# ── handler 層（claude 路徑，沿 test_lifecycle_atomicity 治具風格）──────────────


def _wire_claude(auth_client, mock_anthropic, tool_result="Mocked Result"):
    reg = auth_client.app.state.context["tool_registry"]
    reg["vault_tools"] = AsyncMock(return_value=tool_result)
    auth_client.app.state.context["anthropic_client"] = mock_anthropic
    auth_client.app.state.context["is_toukasoul"] = True
    return reg


_PAYLOAD = {
    "model": "claude-3-5-sonnet",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "做點事"}],
    "stream": False,
}

# 舊名 vault_tools 要它的後繼檔案動詞全部列給模型才放行（h2）：授權時一併給後繼工具
_VAULT_TOOLS_WITH_SUCCESSORS = [
    "vault_tools", "read", "list", "find", "grep", "write", "edit", "delete",
]


@pytest.mark.asyncio
async def test_text_reply_ends_turn_no_nudge(auth_client, mock_anthropic, mock_text_response):
    """純文字回覆＝回合結束：**恰好一次**模型呼叫（不帶任何停止詞）。

    現況紅：nudge 會注入「自主自由時間」假 user 訊息硬續到 slots 燒完。
    """
    _wire_claude(auth_client, mock_anthropic)
    mock_anthropic.messages.create.side_effect = [
        mock_text_response("好的，我看完了，沒有需要處理的事。")  # 無停止詞
    ]

    r = auth_client.post("/v1/messages", json=_PAYLOAD)

    assert r.status_code == 200
    assert mock_anthropic.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_interactive_multi_tool_rounds_unlimited_by_default(
    auth_client, mock_anthropic, mock_tool_response, mock_text_response, monkeypatch
):
    """互動預設不限：連續多輪工具照跑、不觸發收尾（多步能力零損失）。"""
    reg = _wire_claude(auth_client, mock_anthropic)
    grants = permission_service.resolve_grants(None)
    monkeypatch.setattr(
        permission_service,
        "resolve_grants",
        lambda _role: replace(grants, tools=NameGrant.from_lists(_VAULT_TOOLS_WITH_SUCCESSORS)),
    )
    args = {"vault": "personal", "action": "list"}
    mock_anthropic.messages.create.side_effect = [
        mock_tool_response("vault_tools", args, call_id="c1"),
        mock_tool_response("vault_tools", args, call_id="c2"),
        mock_tool_response("vault_tools", args, call_id="c3"),
        mock_text_response("整理完成。"),
    ]

    r = auth_client.post("/v1/messages", json=_PAYLOAD)

    assert r.status_code == 200
    assert mock_anthropic.messages.create.call_count == 4
    assert reg["vault_tools"].await_count == 3


@pytest.mark.asyncio
async def test_bounded_run_wraps_up_gracefully(
    auth_client, mock_anthropic, mock_tool_response, mock_text_response, monkeypatch
):
    """有限 run（模擬無人 pulse，額度 1）：第 2 輪工具**不執行**（fail-closed），
    改走最後一次無工具收尾呼叫（tool_choice=none）取得總結。"""
    reg = _wire_claude(auth_client, mock_anthropic)
    grants = permission_service.resolve_grants(None)
    monkeypatch.setattr(
        permission_service,
        "resolve_grants",
        lambda _role: replace(grants, tools=NameGrant.from_lists(_VAULT_TOOLS_WITH_SUCCESSORS)),
    )
    auth_client.app.state.context["enable_autonomy"] = True
    auth_client.app.state.context["pulse_rounds"] = 1
    args = {"vault": "personal", "action": "list"}
    mock_anthropic.messages.create.side_effect = [
        mock_tool_response("vault_tools", args, call_id="c1"),  # 第 1 輪：授權執行
        mock_tool_response("vault_tools", args, call_id="c2"),  # 第 2 輪：額度已盡
        mock_text_response("額度用完，總結：已列出清單。"),  # 收尾呼叫
    ]

    r = auth_client.post("/v1/messages", json=_PAYLOAD)

    assert r.status_code == 200
    assert mock_anthropic.messages.create.call_count == 3
    assert reg["vault_tools"].await_count == 1, "耗盡後的工具輪必須零執行"
    # 收尾呼叫必須禁用工具（tool_choice=none）
    final_kwargs = mock_anthropic.messages.create.call_args_list[-1].kwargs
    assert final_kwargs.get("tool_choice") == {"type": "none"}


@pytest.mark.asyncio
async def test_interactive_does_not_consume_daily_quota(
    auth_client, mock_anthropic, mock_text_response, mock_identity
):
    """決策 2：互動聊天不再扣每日配額（舊制每次靈魂對話都 +1）。"""
    identity = mock_identity(agent_id="QuotaChatAgent")
    auth_client.app.dependency_overrides[get_verified_identity] = lambda: identity
    auth_client.app.dependency_overrides[get_optional_identity] = lambda: identity

    _wire_claude(auth_client, mock_anthropic)
    mock_anthropic.messages.create.side_effect = [mock_text_response("嗨。")]
    before = autonomy_service.get_daily_quota(identity)["daily_count"]

    r = auth_client.post("/v1/messages", json=_PAYLOAD)

    assert r.status_code == 200
    after = autonomy_service.get_daily_quota(identity)["daily_count"]
    assert after == before, "互動聊天不得扣自主配額"

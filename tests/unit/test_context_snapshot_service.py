"""Unit tests for context_snapshot_service (TDD — 真實組裝重寫 ADR-0133).

快照改為「跑真實 pipeline 組裝 → 用 ALL_BLOCK_TAGS SSOT 有序解析」，去硬編碼。
純函式（parse_system_blocks／build_snapshot／render_snapshot_markdown）在此獨立測試，
不需 pipeline；async harness get_context_snapshot 另於整合/冒煙測。
"""

from __future__ import annotations

from src.models.identity import AgentIdentity
from src.services import context_snapshot_service as css
from src.services.prompt_blocks import ALL_BLOCK_TAGS

IDENTITY = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
_MODEL = "claude-opus-4-8"  # window 200_000

# 生產組裝順序（soul 路徑）：persona → summary(波動) → tool_availability(穩定) → mission(波動)。
_TRAPPED_SYSTEM = (
    "<persona>You are Touka.</persona>\n\n"
    "<conversation_summary>earlier chat</conversation_summary>\n"
    "<tool_availability>foo denied</tool_availability>\n"
    "<validation_protocol>validate after edits</validation_protocol>\n"
    "<mission_awareness>progress 2/5</mission_awareness>\n"
)


# ── parse_system_blocks（tag SSOT、有序、非硬編碼）──────────────────────────────


def test_parse_system_blocks_ordered_by_appearance():
    blocks = css.parse_system_blocks(_TRAPPED_SYSTEM)
    tags = [b["tag"] for b in blocks]
    # 依「實際出現順序」而非 registry 宣告順序
    assert tags == [
        "persona",
        "conversation_summary",
        "tool_availability",
        "validation_protocol",
        "mission_awareness",
    ]
    # 內容逐字保留
    persona = next(b for b in blocks if b["tag"] == "persona")
    assert persona["content"] == "You are Touka."


def test_parse_system_blocks_empty_and_no_known_tags():
    assert css.parse_system_blocks("") == []
    assert css.parse_system_blocks("just some prose, no xml blocks") == []


def test_parse_is_driven_by_tag_ssot_not_hardcoded():
    # 只要 tag 在 ALL_BLOCK_TAGS 就會被解析——不依賴硬編碼的 8 塊清單
    for tag in ALL_BLOCK_TAGS:
        got = css.parse_system_blocks(f"<{tag}>x</{tag}>")
        assert [b["tag"] for b in got] == [tag]


def test_block_meta_covers_all_tags():
    """防漂移（單元級）：每個 ALL_BLOCK_TAGS 都要有 metadata，新 tag 逼你補。"""
    assert set(ALL_BLOCK_TAGS) <= set(css.BLOCK_META)


# ── build_snapshot（token 成本／快取斷點／真實 vs mock／tools＋home）────────────


def _snapshot(system_text=_TRAPPED_SYSTEM, tools=None, home=None):
    return css.build_snapshot(
        agent_id="DebugAgent",
        model=_MODEL,
        system_text=system_text,
        tools=tools or [],
        home_messages=home or [],
    )


def test_build_snapshot_orders_system_then_tools_then_home():
    tools = [{"function": {"name": "write"}}, {"function": {"name": "read"}}]
    home = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
    snap = _snapshot(tools=tools, home=home)
    names = [b["block_name"] for b in snap["blocks"]]
    # 系統字串區塊在前（依序），tools 與 home 續接於後
    assert names[0].startswith("Persona")
    assert "Tool Schemas" in names
    assert "Home Messages" in names
    assert names.index("Tool Schemas") < names.index("Home Messages")
    # position 連續遞增
    assert [b["position"] for b in snap["blocks"]] == list(range(1, len(snap["blocks"]) + 1))


def test_build_snapshot_tools_and_home_blocks_reflect_counts():
    tools = [{"function": {"name": "write"}}, {"function": {"name": "read"}}]
    home = [{"role": "user", "content": "hi"}]
    snap = _snapshot(tools=tools, home=home)
    tool_block = next(b for b in snap["blocks"] if b["block_name"] == "Tool Schemas")
    home_block = next(b for b in snap["blocks"] if b["block_name"] == "Home Messages")
    assert tool_block["content_length"] == 2  # 2 tools
    assert tool_block["tokens"] > 0
    assert home_block["content_length"] == 1  # 1 home message


def test_build_snapshot_token_totals_and_window():
    snap = _snapshot()
    assert snap["window"] == 200_000
    assert snap["total_tokens"] > 0
    assert 0 <= snap["usage_pct"] <= 100
    # 每塊都有 token 估算
    for b in snap["blocks"]:
        assert b["tokens"] >= 0
    # 完整組裝的 system 逐字附上（供人類完整檢視／複製）
    assert snap["system_text"] == _TRAPPED_SYSTEM


def test_build_snapshot_cache_tiers_reuse_real_p4_functions():
    """#4-2：套真實 reorder+split，穩定/波動分層＋困住旗標（ADR-0130 可視化）。"""
    tools = [{"function": {"name": "write"}}]
    snap = _snapshot(tools=tools)
    tier = {b["tag"]: b["cache_tier"] for b in snap["blocks"] if b["tag"]}
    # persona 與被困的 tool_availability/validation_protocol 重排後進穩定前綴
    assert tier["persona"] == "stable"
    assert tier["tool_availability"] == "stable"
    assert tier["validation_protocol"] == "stable"
    # summary/mission 為波動後綴
    assert tier["conversation_summary"] == "volatile"
    assert tier["mission_awareness"] == "volatile"
    # tools 有自己的跨回合斷點
    tool_block = next(b for b in snap["blocks"] if b["block_name"] == "Tool Schemas")
    assert tool_block["cache_tier"] == "stable"
    # 本例穩定塊原本被困在波動後 → 重排有作用
    assert snap["cache_reordered"] is True


def test_build_snapshot_no_reorder_when_already_optimal():
    # 短對話（無 summary），mission 已在尾端 → 不需重排
    system = (
        "<persona>P</persona>\n"
        "<tool_availability>x</tool_availability>\n"
        "<mission_awareness>1/3</mission_awareness>\n"
    )
    snap = _snapshot(system_text=system)
    assert snap["cache_reordered"] is False


def test_build_snapshot_kind_from_registry():
    snap = _snapshot(
        system_text="<persona>P</persona>\n<ide_context>f.py:1</ide_context>\n"
        "<project_context>proj</project_context>"
    )
    kind = {b["tag"]: b["kind"] for b in snap["blocks"] if b["tag"]}
    assert kind["persona"] == "real"
    assert kind["ide_context"] == "per_request"
    assert kind["project_context"] == "mock"


# ── render_snapshot_markdown（更新結構）────────────────────────────────────────


def test_render_markdown_new_structure():
    snap = _snapshot(tools=[{"function": {"name": "write"}}])
    md = css.render_snapshot_markdown(snap)
    assert "# Context Snapshot: DebugAgent" in md
    assert "Persona" in md
    assert "Tool Schemas" in md
    # token 與快取分層資訊出現
    assert "token" in md.lower()
    assert "stable" in md.lower() or "volatile" in md.lower()

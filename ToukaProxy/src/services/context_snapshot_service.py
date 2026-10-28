"""Context Snapshot Service（真實組裝重寫，ADR-0133）。

供人類**預先檢視**「組裝完成的系統提示」。做法（Option B，避免硬編碼）：以合成的
DebugAgent 請求跑**真實 ProxyPipeline**（`protocol_manager` 沿用 app.state.context、
**永不打模型**、純讀），再用 `prompt_blocks.ALL_BLOCK_TAGS`（tag SSOT）**有序解析**
組裝後的 `request.system`——區塊清單與順序全由真實組裝產生，之後中介層改動也如實反映。

各區塊標註：注入層／來源／`real|mock|per_request`／token 成本／快取分層（套用真實
P4 快取函式：`reorder_system_stable_first`＋`to_cached_system`，可視化四層斷點與
「穩定塊是否被困在波動後」＝ADR-0130 健康檢查）。Tool Schemas 與 Home Messages 為
`request.tools`／`request.messages` 欄位、另計。純函式（parse／build／render）與 async
harness 分離，前者免 pipeline 即可測。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.adapters.tool_schema import reorder_system_stable_first, to_cached_system
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.models.context_window_registry import get_context_window
from src.services.prompt_blocks import (
    ALL_BLOCK_TAGS,
    CAPABILITIES,
    CONTINUITY,
    CONVERSATION_SUMMARY,
    HARNESS_CONTEXT,
    HOOK_OUTPUT,
    IDE_CONTEXT,
    MCP_UNAVAILABLE,
    MISSION_AWARENESS,
    PERSONA,
    PROJECT_CONTEXT,
    TOOL_AVAILABILITY,
    VALIDATION_PROTOCOL,
    VOLATILE_SYSTEM_TAGS,
)
from src.utils.token_estimator import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

_PREVIEW_LENGTH = 200
_SNAPSHOT_PROBE = "[context snapshot probe]"

# 區塊 metadata（tag → 顯示資訊）。**唯一的 per-block 知識**；防漂移守衛驗證
# keys ⊇ ALL_BLOCK_TAGS，新增 block tag 即逼你在此補一筆（杜絕靜默漂移）。
# kind：real＝可由此 agent 真實取得；mock＝需情境（如選定專案）才有、附格式示例；
# per_request＝本質隨單次請求（如 IDE header），只能示範格式。
BLOCK_META: dict[str, dict[str, str]] = {
    PERSONA: {
        "name": "Persona (Soul)",
        "layer": "SoulLayer",
        "source": "soul/<agent>/soul.md",
        "kind": "real",
        "mock": "<persona>…人格／soul 本體…</persona>",
    },
    CONVERSATION_SUMMARY: {
        "name": "Conversation Summary",
        "layer": "SoulLayer",
        "source": "session.context_summary (DB, ADR-0092 Phase D)",
        "kind": "real",
        "mock": "（被剪舊對話的摘要；僅剪過的長對話有）",
    },
    PROJECT_CONTEXT: {
        "name": "Project Context",
        "layer": "ContextLayer",
        "source": "get_project_context(project_id) (DB)",
        "kind": "mock",
        "mock": "<project_context>…選定專案時注入的專案背景…</project_context>",
    },
    IDE_CONTEXT: {
        "name": "IDE Context",
        "layer": "ContextLayer（注入最後一則 user 訊息，非 system）",
        "source": "x-touka-ide-file / x-touka-ide-line headers",
        "kind": "per_request",
        "mock": "📄 example.py | 📍 Line 42",
    },
    MISSION_AWARENESS: {
        "name": "Mission Awareness",
        "layer": "ToolLayer",
        "source": "MissionManager.get_current_mission (DB)",
        "kind": "real",
        "mock": "### 目前任務狀態 … ✅/⏳/⬜ 各任務",
    },
    CAPABILITIES: {
        "name": "Technical Capabilities",
        "layer": "CustomModelLayer（僅 L2 自定義模型路徑）",
        "source": "instruction_templates.json",
        "kind": "real",
        "mock": "<capabilities>TECHNICAL_SPECIFICATIONS…</capabilities>",
    },
    TOOL_AVAILABILITY: {
        "name": "Tool Availability",
        "layer": "ToolLayer",
        "source": "Gate-1 政策過濾（被拒工具的可讀說明）",
        "kind": "real",
        "mock": "（有工具被政策過濾時注入；防模型幻覺被過濾的工具）",
    },
    MCP_UNAVAILABLE: {
        "name": "MCP Unavailable",
        "layer": "ToolLayer",
        "source": "ProtocolManager.unavailable_shared_servers()（ADR-0281 T1）",
        "kind": "real",
        "mock": "（有設定好但沒連上的共用 MCP 時注入；每輪會變）",
    },
    VALIDATION_PROTOCOL: {
        "name": "Validation Protocol",
        "layer": "ToolLayer",
        "source": "build_validation_protocol(auth_level)（改動類開發工具在場才注入）",
        "kind": "real",
        "mock": "After you change files … VALIDATE the outcome before reporting success.",
    },
    HARNESS_CONTEXT: {
        "name": "Harness Context",
        "layer": "ToolLayer",
        "source": "build_harness_context(model, role, advertised)（toukasoul 面；ADR-0139）",
        "kind": "real",
        "mock": "Touka OS runtime facts: harness / model / role / mechanics（英文、零風格）",
    },
    CONTINUITY: {
        "name": "Continuity",
        "layer": "SoulLayer／心跳（附在最後一則 user 訊息尾端，非 system）",
        "source": "continuity_service（系統時鐘、可信活動事件、MissionManager 唯讀）",
        "kind": "per_request",
        "mock": "<continuity>現在時間（台北）／距上一輪多久／任務看板更新時間</continuity>",
    },
    HOOK_OUTPUT: {
        "name": "Hook Output",
        "layer": "HookLayer（附在最後一則 user 訊息尾端，非 system）",
        "source": "管理員設定的 UserPromptSubmit hook（ADR-0292；資料，不是指令）",
        "kind": "per_request",
        "mock": "（有 hook 回補充說明時才注入；預覽不執行 hook）",
    },
}

_UNKNOWN_META = {"name": "Unknown Block", "layer": "?", "source": "?", "kind": "real", "mock": ""}


# ── 純解析：tag SSOT、有序、非硬編碼 ──────────────────────────────────────────


def parse_system_blocks(system_text: str) -> list[dict[str, Any]]:
    """把組裝後的 system 字串依 ``ALL_BLOCK_TAGS`` 切成**有序**區塊（依實際出現位置）。

    非硬編碼：迭代 tag SSOT、找出每個 ``<tag>…</tag>``、依起始位置排序。回傳
    ``[{tag, content, start}]``；空字串或無已知 tag → ``[]``。
    """
    if not system_text:
        return []
    found: list[dict[str, Any]] = []
    for tag in ALL_BLOCK_TAGS:
        for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", system_text, re.DOTALL):
            found.append({"tag": tag, "content": m.group(1).strip(), "start": m.start()})
    found.sort(key=lambda b: b["start"])
    return found


def _classify_cache(system_text: str) -> tuple[bool, str]:
    """套**真實 P4 函式**判斷快取分層。回 ``(cache_reordered, stable_prefix_text)``。

    穩定前綴文字內含 ``<tag>`` 的區塊＝跨回合快取（stable）；其餘＝波動後綴（volatile）。
    """
    reordered = reorder_system_stable_first(system_text, volatile_tags=VOLATILE_SYSTEM_TAGS)
    cached = to_cached_system(reordered, volatile_tags=VOLATILE_SYSTEM_TAGS)
    stable_text = ""
    if isinstance(cached, list) and len(cached) >= 2:
        stable_text = cached[0].get("text", "") if isinstance(cached[0], dict) else ""
    elif isinstance(cached, list) and len(cached) == 1:
        # 單一區塊：無波動 tag → 整段皆穩定（跨回合）；波動 tag 在最前 → 整段皆波動。
        has_volatile = any(f"<{t}>" in system_text for t in VOLATILE_SYSTEM_TAGS)
        stable_text = "" if has_volatile else (
            cached[0].get("text", "") if isinstance(cached[0], dict) else ""
        )
    return (reordered != system_text), stable_text


def _tier_for(tag: str, stable_text: str) -> str:
    return "stable" if f"<{tag}>" in stable_text else "volatile"


def _block(
    position: int,
    *,
    tag: str | None,
    name: str,
    layer: str,
    source: str,
    kind: str,
    present: bool,
    content_length: int | None,
    tokens: int,
    preview: str | None,
    cache_tier: str | None,
) -> dict[str, Any]:
    return {
        "position": position,
        "tag": tag,
        "block_name": name,
        "layer": layer,
        "source": source,
        "kind": kind,
        "present": present,
        "content_length": content_length,
        "tokens": tokens,
        "preview": preview,
        "cache_tier": cache_tier,
    }


def build_snapshot(
    *,
    agent_id: str,
    model: str,
    system_text: str,
    tools: list[dict[str, Any]],
    home_messages: list[dict[str, Any]],
    last_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由真實組裝的 system／tools／home 產出完整快照（純函式）。

    區塊順序：①system 字串內**實際出現**的區塊（真實順序）→ ②未出現的已知 tag
    （mock 格式佔位、present=false）→ ③Tool Schemas（request.tools）→ ④Home Messages。
    各塊附 token 成本與快取分層；頂層附 total_tokens／window／usage_pct／cache_reordered。
    """
    cache_reordered, stable_text = _classify_cache(system_text)
    parsed = parse_system_blocks(system_text)
    present_tags = {b["tag"] for b in parsed}

    blocks: list[dict[str, Any]] = []
    pos = 1

    # ① 實際出現的系統區塊（真實順序、真實內容）
    for pb in parsed:
        meta = BLOCK_META.get(pb["tag"], _UNKNOWN_META)
        content = pb["content"]
        blocks.append(_block(
            pos, tag=pb["tag"], name=meta["name"], layer=meta["layer"],
            source=meta["source"], kind=meta["kind"], present=True,
            content_length=len(content), tokens=estimate_tokens(content, model),
            preview=content[:_PREVIEW_LENGTH],
            cache_tier=_tier_for(pb["tag"], stable_text),
        ))
        pos += 1

    # ② 未出現的已知區塊 → mock 格式佔位（誠實：present=false、tokens=0）
    for tag in ALL_BLOCK_TAGS:
        if tag in present_tags:
            continue
        meta = BLOCK_META[tag]
        blocks.append(_block(
            pos, tag=tag, name=meta["name"], layer=meta["layer"], source=meta["source"],
            kind=meta["kind"], present=False, content_length=None, tokens=0,
            preview=meta.get("mock") or None, cache_tier=None,
        ))
        pos += 1

    # ③ Tool Schemas（request.tools 欄位；跨回合快取斷點）
    tool_names = [
        (t.get("function") or {}).get("name") or t.get("name")
        for t in tools if isinstance(t, dict)
    ]
    tool_names = [n for n in tool_names if n]
    blocks.append(_block(
        pos, tag=None, name="Tool Schemas", layer="ToolLayer",
        source="builtin + memory + MCP（request.tools）", kind="real" if tools else "mock",
        present=bool(tools), content_length=len(tools),
        tokens=estimate_tokens(json.dumps(tools, ensure_ascii=False), model) if tools else 0,
        preview=", ".join(tool_names[:20]) or None,
        cache_tier="stable" if tools else None,
    ))
    pos += 1

    # ④ Home Messages（get_home_messages，前置於 request.messages）
    blocks.append(_block(
        pos, tag=None, name="Home Messages", layer="SoulLayer",
        source="get_home_messages（DB，前置於 messages）", kind="real" if home_messages else "mock",
        present=bool(home_messages), content_length=len(home_messages),
        tokens=estimate_messages_tokens(home_messages, model) if home_messages else 0,
        preview=_home_preview(home_messages), cache_tier=None,
    ))
    pos += 1

    total = sum(b["tokens"] for b in blocks)
    window = get_context_window(model)
    usage_pct = round(min(100.0, total / window * 100), 1) if window > 0 else 0.0
    return {
        "agent_id": agent_id,
        "model": model,
        "window": window,
        "total_tokens": total,
        "usage_pct": usage_pct,
        "cache_reordered": cache_reordered,
        "system_text": system_text,  # 完整組裝的 system（供人類完整檢視／複製）
        "blocks": blocks,
        "last_payload": last_payload,
    }


def _home_preview(home_messages: list[dict[str, Any]]) -> str | None:
    if not home_messages:
        return None
    first = home_messages[0]
    role = first.get("role", "?") if isinstance(first, dict) else "?"
    content = first.get("content", "") if isinstance(first, dict) else str(first)
    if not isinstance(content, str):
        content = str(content)
    return f"[{len(home_messages)} msg(s)] {role}: {content[:_PREVIEW_LENGTH]}"


# ── async harness：跑真實 pipeline、停在模型前 ────────────────────────────────


def _system_to_text(system: Any) -> str:
    """把 request.system（str / block list / None）攤平成文字。"""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in system
        ]
        return "".join(parts)
    return str(system)


async def get_context_snapshot(
    identity: AgentIdentity,
    base_ctx: dict[str, Any],
    *,
    seed_mock: bool = True,
    last_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑真實 pipeline 組裝（停在模型前）→ 回結構化快照。

    以合成的 DebugAgent 請求驅動 ``ProxyPipeline``（沿用 ``base_ctx`` 的
    protocol_manager／工具 schema；**不呼叫模型**、純讀），讀出組裝後的
    ``request.system``／``tools``／``messages`` 再交 ``build_snapshot``。無 session_id
    （避免碰任何 session 狀態）。``seed_mock`` → 塞入 IDE header 等佔位以示範格式。
    """
    from src.adapters.schemas import AnthropicRequest  # noqa: LOCAL_IMPORT_BAN
    from src.models.context import PipelineContext  # noqa: LOCAL_IMPORT_BAN
    from src.services.proxy_service import pipeline  # noqa: LOCAL_IMPORT_BAN

    request = AnthropicRequest(
        model=identity.agent_id,
        max_tokens=1024,
        messages=[{"role": "user", "content": _SNAPSHOT_PROBE}],
    )
    if seed_mock:
        request.ide_context = {"fileName": "example.py", "line": 42}

    ctx = PipelineContext(**base_ctx)
    ctx.identity = identity
    ctx.agent_id = identity.agent_id
    ctx.auth_level = base_ctx.get("auth_level") or "Manual"
    ctx.request_id = "context-snapshot"
    ctx.run_hooks = False  # 預覽不執行 hook（ADR-0292）
    # 刻意不設 session_id：避免 clear_interrupt / session 連動（純唯讀快照）。

    model = identity.agent_id
    try:
        await pipeline.execute(request, ctx)
        model = getattr(request, "model", identity.agent_id)
        system_text = _system_to_text(getattr(request, "system", ""))
        tools = getattr(request, "tools", None) or []
        messages = getattr(request, "messages", None) or []
        home = messages[:-1] if len(messages) > 1 else []
        home = [m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in home]
    except Exception:
        logger.exception("context snapshot pipeline assembly failed")
        system_text, tools, home = "", [], []

    return build_snapshot(
        agent_id=identity.agent_id,
        model=model,
        system_text=system_text,
        tools=tools,
        home_messages=home,
        last_payload=last_payload,
    )


# ── Markdown 輸出（供匯出檔） ──────────────────────────────────────────────────


def render_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    """把 ``build_snapshot`` 的輸出轉為人類可讀 Markdown（供 export 端點寫檔）。"""
    agent_id = snapshot.get("agent_id", "unknown")
    model = snapshot.get("model", "?")
    blocks: list[dict[str, Any]] = snapshot.get("blocks", [])
    total = snapshot.get("total_tokens", 0)
    window = snapshot.get("window", 0)
    usage = snapshot.get("usage_pct", 0.0)
    reordered = snapshot.get("cache_reordered", False)

    lines: list[str] = [
        f"# Context Snapshot: {agent_id}",
        "",
        f"- **model**: `{model}`",
        f"- **tokens**: {total} / {window}（{usage}% of window）",
        f"- **cache reordered (ADR-0130)**: {reordered}",
        "",
        "依真實 ProxyPipeline 組裝順序解析（tag SSOT）。`present=false` 者為本次請求未出現、"
        "附格式示例。快取分層：`stable`＝跨回合、`volatile`＝迴圈內。",
        "",
    ]
    for b in blocks:
        present = b.get("present")
        flag = "" if present else " ⚪ (not in this request)"
        tier = b.get("cache_tier")
        tier_txt = f" · cache: `{tier}`" if tier else ""
        lines.append(f"## {b['position']}. {b['block_name']}{flag}")
        lines.append("")
        lines.append(f"- **layer**: `{b['layer']}`")
        lines.append(f"- **source**: `{b['source']}`")
        lines.append(f"- **kind**: `{b['kind']}` · **tokens**: {b['tokens']}{tier_txt}")
        preview = b.get("preview")
        if preview:
            lines.append("")
            lines.append("```")
            lines.append(preview)
            lines.append("```")
        lines.append("")

    return "\n".join(lines)

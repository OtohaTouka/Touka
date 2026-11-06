"""工具 schema 跨格式轉換 — 單一事實來源 (SSOT)。

各供應商的工具（function calling）schema 形狀不同：
  - OpenAI：``{"type": "function", "function": {name, description, parameters}}``（canonical）。
  - Anthropic：``{name, description, input_schema}``。

過去 OpenAI→Anthropic 的轉換**重複寫在兩處**（ToolLayer 與 openai_to_claude），且 dispatch
的 Anthropic→OpenAI 那塊缺一份。本模組把這些轉換收斂為單一來源；新增供應商或修轉換
只動此處。純函式、防禦式，**永不拋例外**。

（Gemini 工具轉換因含 schema 大小寫正規化、且僅單源存在於 ``openai_to_gemini``，暫不移入。）
"""
from __future__ import annotations

import re
from typing import Any

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
# Anthropic 的 input_schema 根層不收這三個組合子（整包 400）；內層可以。
_ROOT_COMBINATORS = ("oneOf", "anyOf", "allOf")


def _valid_name(name: object) -> bool:
    """工具名須為非空字串（Anthropic/OpenAI 皆要求 name 為必填非空）。"""
    return isinstance(name, str) and bool(name.strip())


def _anthropic_input_schema(schema: object) -> dict[str, Any]:
    """另建一份 Anthropic 收得下的 input_schema；不改輸入（模組層 schema 以參考進請求）。

    剝掉根層 oneOf／anyOf／allOf，其他根層鍵（含 ``$defs``）照留、內層不動。各分支的
    properties 先併進根層 properties（同名以根層為準），欄位才不會跟著消失；分支之間同名
    欄位的定義依出現順序收集、相同的只留一份，只有一種照用，多種合成 ``{"anyOf": [...]}``
    （例如只寫在分支裡的判別欄位，每個值都還選得到）。required 只留根層原有的。根層 type
    一律 object。非 dict → 空 object schema。條件必填因此只剩說明文字；執行前仍照正本檢查。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = {k: v for k, v in schema.items() if k not in _ROOT_COMBINATORS}
    variants: dict[str, list[Any]] = {}
    for key in _ROOT_COMBINATORS:
        branches = schema.get(key)
        for branch in branches if isinstance(branches, list) else []:
            props = branch.get("properties") if isinstance(branch, dict) else None
            for name, prop in (props if isinstance(props, dict) else {}).items():
                seen = variants.setdefault(name, [])
                if prop not in seen:
                    seen.append(prop)
    if variants:
        branch_props = {
            name: seen[0] if len(seen) == 1 else {"anyOf": list(seen)}
            for name, seen in variants.items()
        }
        root_props = schema.get("properties")
        out["properties"] = {**branch_props, **(root_props if isinstance(root_props, dict) else {})}
    out["type"] = "object"
    return out


def openai_to_anthropic_tools(openai_tools: list[dict] | None) -> list[dict] | None:
    """OpenAI 工具 schema → Anthropic 工具 schema。空／None／無有效項 → ``None``。

    **冪等**：已是 Anthropic 形狀（有 input_schema，或有 name 且無 function）→ 複製後直通，
    讓出場端（claude_handler）可對「OpenAI 或已是 Anthropic」一律安全呼叫。
    兩種形狀的 input_schema 都經 ``_anthropic_input_schema`` 剝根層組合子；一律回新 dict，
    之後在輸出寫 ``cache_control`` 不會改到呼叫端的工具。
    **防禦**：name 非有效字串者一律略過（避免送出 name=null 違反 Anthropic API）；
    ``function`` 僅在為 dict 時採用（容忍 ``function: null`` 等畸形項）。
    """
    if not isinstance(openai_tools, list):
        return None
    out: list[dict] = []
    for t in openai_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":  # OpenAI 形狀
            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
            if not _valid_name(fn.get("name")):
                continue
            out.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": _anthropic_input_schema(fn.get("parameters")),
            })
        elif ("input_schema" in t or "name" in t) and not isinstance(t.get("function"), dict):
            if _valid_name(t.get("name")):  # 已是 Anthropic → 複製後直通（須有效 name）
                tool = dict(t)
                if "input_schema" in t:  # 伺服器工具（沒有 input_schema）不補欄位
                    tool["input_schema"] = _anthropic_input_schema(t["input_schema"])
                out.append(tool)
    return out or None


def apply_tool_cache_breakpoint(tools: list[dict] | None) -> list[dict] | None:
    """在最後一個工具加 Anthropic ephemeral 快取斷點（P4：prompt 快取前綴優化）。

    Anthropic prompt caching 順序 tools→system→messages；在最後一個工具設斷點即
    快取整組**穩定**工具 schema（P0 量到 Owner 27 工具 ≈ 6346 token）。ReAct 迴圈內
    同一組工具重複命中→快取讀取省 ~90% token 成本／加速首 token。

    **冪等**：先清掉各工具既有 ``cache_control``、只在最後一個標記（避免超過斷點上限）。
    純函式：複製每個工具、不改輸入。空／None → 原樣傳回。
    """
    if not tools:
        return tools
    out = [{k: v for k, v in t.items() if k != "cache_control"} for t in tools]
    out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


def _text_block(text: str) -> dict[str, Any]:
    """帶快取斷點的 Anthropic text block。"""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _earliest_volatile_index(system: str, volatile_tags: tuple[str, ...]) -> int | None:
    """回傳 system 中最早出現的波動區塊開標 ``<tag>`` 的索引；找不到則 None。"""
    found = [i for t in volatile_tags if (i := system.find(f"<{t}>")) >= 0]
    return min(found) if found else None


def to_cached_system(system: Any, *, volatile_tags: tuple[str, ...] = ()) -> Any:
    """把 Anthropic system prompt 轉成帶快取斷點的 block list（P4b／P4c：system 前綴快取）。

    ``str``：
      - 提供 ``volatile_tags`` 且找到 → **穩定前綴細分**（P4c）：於最早的波動區塊
        ``<tag>`` 處切成 ``[穩定前綴(斷點), 波動後綴(斷點)]``——穩定前綴（persona 等）
        **跨回合命中**、波動後綴（mission/summary 等）**迴圈內命中**（兩斷點疊加、
        Anthropic 取最長匹配前綴）。
      - 穩定前綴為空白／無波動 tag／未提供 volatile_tags → **單一區塊**（整段一斷點，
        迴圈內命中，向後相容 ADR-0128）。
    已是 ``list`` → 對最後一個 text block 加斷點（冪等：先清各 block 既有 cache_control）。
    空字串／None → 原樣傳回。純函式、不改輸入。

    註：``services.context_breakdown._system_to_text`` 已能攤平多 block list（忽略
    cache_control），故遙測／context breakdown 無需改動。
    """
    if not system:
        return system
    if isinstance(system, str):
        idx = _earliest_volatile_index(system, volatile_tags)
        if idx is None or not system[:idx].strip():
            return [_text_block(system)]
        return [_text_block(system[:idx]), _text_block(system[idx:])]
    if isinstance(system, list):
        out = [{k: v for k, v in b.items() if k != "cache_control"}
               if isinstance(b, dict) else b for b in system]
        for b in reversed(out):
            if isinstance(b, dict) and b.get("type") == "text":
                b["cache_control"] = {"type": "ephemeral"}
                break
        return out
    return system


def reorder_system_stable_first(
    system: Any, *, volatile_tags: tuple[str, ...] = ()
) -> Any:
    """把波動區塊搬到 system 尾端，讓穩定區塊連成單一前綴（P4d：中介層順序重排）。

    中介層跨兩層注入波動區塊（``conversation_summary`` 於 SoulLayer、
    ``mission_awareness`` 於 ToolLayer），使長對話中 ``conversation_summary`` 夾在
    persona 與穩定工具區塊之間；``to_cached_system`` 於此切開、把穩定的
    ``tool_availability``/``validation_protocol`` 困在波動後綴（不跨回合快取）。本函式
    在**送出前**把所有波動區塊搬到尾端（各區塊位元逐字不變、保持同組出現順序），使
    ``to_cached_system`` 的切點落在「所有穩定區塊之後」——穩定前綴因而涵蓋工具區塊、
    跨回合命中。

    不動中介層 pipeline 順序（SoulLayer 須早於 ToolLayer 以備妥身份／腦／desired_tools），
    只在組裝完成的單一出場點正規化區塊順序。**冪等**：波動區塊已在尾端 → 原樣傳回
    （no-op，避免無謂位元變動）。非 ``str``／空／None／未給 ``volatile_tags`` → 原樣傳回。
    純函式、不改輸入。假設各波動區塊為頂層 XML 邊界、不巢狀（與 ``context_breakdown``
    的解析假設一致）。

    Args:
        system: 已組裝的 Anthropic system prompt（本優化僅處理 ``str`` 形態）。
        volatile_tags: 每回合會變動的區塊 tag（見 ``prompt_blocks.VOLATILE_SYSTEM_TAGS``）。

    Returns:
        波動區塊移至尾端後的 system 字串；無需重排（或非 str）時回原輸入。
    """
    if not system or not isinstance(system, str) or not volatile_tags:
        return system
    matches: list[re.Match[str]] = []
    for tag in volatile_tags:
        matches.extend(re.finditer(rf"<{tag}>.*?</{tag}>", system, re.DOTALL))
    if not matches:
        return system
    matches.sort(key=lambda m: m.start())
    volatile_blocks = [m.group(0) for m in matches]
    # 已在尾端判定：第一個波動區塊之後若僅剩波動區塊與空白 → 原樣不動（冪等 no-op）。
    tail = system[matches[0].start():]
    for block in volatile_blocks:
        tail = tail.replace(block, "", 1)
    if not tail.strip():
        return system
    # 否則搬移：移除各波動區塊、依出現順序接到尾端（穩定區塊因而連成前綴）。
    stable = system
    for block in volatile_blocks:
        stable = stable.replace(block, "", 1)
    return stable.rstrip() + "\n" + "".join(volatile_blocks)


def anthropic_to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Anthropic 工具 schema → OpenAI 工具 schema；已是 OpenAI 形狀（function 為 dict）則直通。

    空／None／非 list 回 ``None``（維持「無工具」語意，向後相容 ``tools=None``）。
    name 非有效字串者略過（防畸形項往下游傳遞）。
    """
    if not isinstance(tools, list):
        return None
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("function"), dict):  # 已是 OpenAI 形狀 → 直通
            out.append(t)
        elif _valid_name(t.get("name")):  # Anthropic 形狀 {name, description, input_schema}
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", _EMPTY_SCHEMA),
                },
            })
    return out or None

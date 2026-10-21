"""真實 runner：以 DebugAgent 在程序內驅動對話 pipeline，量測成本＋擷取結構訊號。

⚠️ 真實執行會花費 API 費用（手動觸發）。正確性以**手動實跑**驗證；單元測試一律用假
runner（注入），絕不在 CI 打真實 API。SSE 事件解析採防禦式（容忍形狀差異）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.adapters.schemas import AnthropicRequest
from src.models.context import PipelineContext
from src.services.providers import pricing
from src.services.proxy_service import process_proxy_request
from src.services.scorecard import cost_meter
from src.services.scorecard.artifact_verify import extract_artifact_path, verify_artifact
from src.services.scorecard.models import RunOutcome
from src.services.skill_service import skill_service
from src.utils.secrets import MASK, is_secret_key
from src.utils.token_estimator import estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from src.models.identity import AgentIdentity
    from src.services.scorecard.models import Scenario

logger = logging.getLogger(__name__)

_ERR_MARKERS = ("Traceback (most recent call last)", "INTERNAL_ERROR", "'NoneType' object")
# handler 在連線/認證/4xx 失敗時，把錯誤訊息當「文字 delta」吐出（非 error 事件），故需從
# 文字偵測——否則失敗的呼叫（如 404）會因「有文字、無 error」被誤判為通過。這類屬環境/
# 供應商不可用，engine 會歸為 not-testable（略過、非品質失敗）。
_PROVIDER_ERR_MARKERS = ("連線錯誤", "API Error (", "Access Denied", "Missing `", "API Key")
_EARLY_STOP_MARKER = "已達總量上限"  # 預算/總量上限提早停（openai_handler emit 的文字標記）
_TRACE_LIMIT = 500  # 工具軌跡 args/result 截斷上限（避免大段記憶/搜尋內容進可下載 JSON）


@dataclass
class _ParsedEvents:
    """``_parse_events`` 的結構化結果（取代易碎的長 tuple）。"""

    text: str = ""
    tools: tuple[str, ...] = ()
    error: str = ""
    tokens: int = 0
    cost: float = 0.0
    tool_trace: tuple[dict, ...] = ()
    react_turns: int = 0
    finish_reason: str = ""
    early_stop: bool = False


def _mask_args(args: object) -> object:
    """遮罩工具參數中 secret 類鍵的字串值（金鑰永不進軌跡）。"""
    if not isinstance(args, dict):
        return args
    return {
        k: (MASK if isinstance(v, str) and is_secret_key(str(k)) else v)
        for k, v in args.items()
    }


def _summarize(value: object, limit: int = _TRACE_LIMIT) -> str:
    """把工具 args/result 轉成截斷字串（供前端顯示「搜了/讀到/存了什麼」）。"""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…(truncated)"


def _new_trace_entry(name: str = "") -> dict:
    """空白工具軌跡項（名稱/參數/結果/狀態）。"""
    return {"name": name, "args": "", "result": "", "status": ""}


def _missing_skill_mcp(base_ctx: dict, skills: tuple[str, ...]) -> list[str]:
    """情境技能所需、但未在 protocol registry 註冊的 MCP server 名單（供 not-testable 略過）。

    防禦式：無可用的 registry.get_server → 回空（不略過、照常跑），只有「**確定** server
    未註冊」才回報。與 ToolLayer 連線用的同一個 server 名比對，故不會誤判。
    （查表為安全操作；若真拋例外，外層 run_scorecard 會接住並記為該情境錯誤。）
    """
    registry = getattr(base_ctx.get("protocol_manager"), "registry", None)
    get_server = getattr(registry, "get_server", None)
    if not callable(get_server):
        return []
    missing: list[str] = []
    for sid in skills:
        skill = skill_service.get_skill(sid) or {}
        for srv in skill.get("mcp_servers") or []:
            if not get_server(srv):
                missing.append(srv)
    return missing


def _parse_events(raw_chunks: list[str]) -> _ParsedEvents:
    """從 SSE 原始 chunk 擷取文字/工具/錯誤/用量 ＋ telemetry（工具軌跡、ReAct 迴圈）。

    tokens／cost 取自 `touka_progress`/`token_usage` 事件（per-model 費率、逐回合加總，
    權威來源）。工具軌跡由 `tool_start`(args)＋`tool_result`(result) 依 id 配對、已遮罩截斷。
    ReAct 回合數由 `touka_progress`/`react_slots` 事件數推得；`finish_reason` 取串流最後值；
    `early_stop` 偵測預算上限標記。防禦式解析，容忍形狀差異。
    """
    text_parts: list[str] = []
    tools: list[str] = []
    error = ""
    tokens = 0
    cost = 0.0
    react_turns = 0
    finish_reason = ""
    trace_by_id: dict[str, dict] = {}
    trace_order: list[str] = []
    for chunk in raw_chunks:
        for raw_line in chunk.split("\n"):
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type", ""))
            if "error" in etype.lower():
                error = error or json.dumps(ev, ensure_ascii=False)[:200]
            # token 用量／成本＋ReAct 回合數（皆為 touka_progress 進度事件）。
            if etype == "touka_progress":
                kind = ev.get("kind")
                if kind == "token_usage":
                    try:
                        tokens += int(ev.get("total", 0) or 0)
                        cost += float(ev.get("cost", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                elif kind == "react_slots":
                    react_turns += 1
            tname = ev.get("tool") or ev.get("name")
            if etype in ("tool_start", "tool_call", "tool_result") and tname:
                tools.append(str(tname))
            # 工具軌跡：tool_start/tool_call 帶 args、tool_result 帶 result，依 id 配對。
            if etype in ("tool_start", "tool_call"):
                cid = str(ev.get("id") or f"t{len(trace_order)}")
                if cid not in trace_by_id:
                    trace_by_id[cid] = _new_trace_entry()
                    trace_order.append(cid)
                if tname:
                    trace_by_id[cid]["name"] = str(tname)
                if ev.get("args") is not None:
                    trace_by_id[cid]["args"] = _summarize(_mask_args(ev.get("args")))
            elif etype == "tool_result":
                fallback = trace_order[-1] if trace_order else "t0"
                cid = str(ev.get("id") or fallback)
                if cid not in trace_by_id:
                    trace_by_id[cid] = _new_trace_entry(str(tname or ""))
                    trace_order.append(cid)
                trace_by_id[cid]["result"] = _summarize(ev.get("result"))
                trace_by_id[cid]["status"] = str(ev.get("status") or "")
            for choice in ev.get("choices", []) or []:
                if isinstance(choice, dict):
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        text_parts.append(str(content))
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = str(fr)
    text = "".join(text_parts)
    # 命中標記時帶出該處起算的文字片段（含上游真正錯誤訊息，如「API Error (400): Model Not
    # Exist」），讓 detail 能說清楚為何不可測，而非只回標記本身。
    if not error:
        for marker in _ERR_MARKERS:
            idx = text.find(marker)
            if idx != -1:
                error = "internal error: " + text[idx:idx + 160].replace("\n", " ").strip()
                break
    if not error:
        for marker in _PROVIDER_ERR_MARKERS:
            idx = text.find(marker)
            if idx != -1:
                error = "provider/env error: " + text[idx:idx + 160].replace("\n", " ").strip()
                break
    return _ParsedEvents(
        text=text,
        tools=tuple(dict.fromkeys(tools)),  # 去重保序
        error=error,
        tokens=tokens,
        cost=round(cost, 6),
        tool_trace=tuple(trace_by_id[cid] for cid in trace_order),
        react_turns=react_turns,
        finish_reason=finish_reason,
        early_stop=_EARLY_STOP_MARKER in text,
    )


def _build_scorecard_context(
    base_ctx: dict,
    identity: AgentIdentity,
    scenario: Scenario,
    t0: float,
    clean_slate: bool = False,
) -> PipelineContext:
    """以生產的 base context 組裝計分卡情境（與聊天／心跳同源）。

    base_ctx 來自 app.state.context（含 memory_tools_schema／protocol_manager／builtin／
    plugins／降級旗標）→ ToolLayer 才注入得了記憶與 MCP 工具。身分用 SSOT 解析、資料仍
    歸 DebugAgent。**刻意不設 resumable_stream**：自主執行無真人可回答 ask_user（與心跳
    一致，否則模型呼叫 ask_user 會卡到逾時）。

    clean_slate=True → 開無痕模式（ADR-0086）：不注入記憶、零殘留——供「白紙狀態」基線
    與「正常 vs 累積」對照（記憶情境此模式下會略過＝正常）。
    """
    ctx = PipelineContext(**base_ctx)
    ctx.identity = identity
    ctx.agent_id = identity.agent_id
    ctx.is_toukasoul = True
    ctx.auth_level = base_ctx.get("auth_level") or "Manual"
    ctx.request_id = f"scorecard-{scenario.id}"
    ctx.t0 = t0
    ctx.incognito = clean_slate
    if scenario.skills:
        # 預先啟用情境所需技能（如 docgen 的 document-generator）→ SkillLayer 會連其 MCP，
        # 工具才在第一回合就出現（計分卡無 session，無法靠 activate_skill 跨回合持久化）。
        headers = dict(ctx.get("headers") or {})
        headers["x-touka-skills"] = ",".join(scenario.skills)
        ctx["headers"] = headers
    return ctx


def _scenario_request(model: str, scenario: Scenario) -> AnthropicRequest:
    """情境請求：Claude 用情境的 max_tokens，其他家不帶上限（和 O20 之前一樣，ADR-0254）。

    ``convert_to_openai`` 只轉送「呼叫端明確給的」max_tokens（O20，給 IDE 的 Anthropic 入口）。
    情境上限只給 Claude：值照設，但不記成明確給的欄位，轉成其他家時就不帶。模型可能是靈魂、
    要到 SoulLayer 才換成主要模型，所以不在這裡依模型名判斷會送到哪一家。
    """
    validated = AnthropicRequest(
        model=model,
        max_tokens=scenario.max_tokens,
        messages=[{"role": "user", "content": scenario.prompt}],
        stream=True,
    )
    return AnthropicRequest.model_construct(
        _fields_set=validated.model_fields_set - {"max_tokens"}, **dict(validated)
    )


async def _drive_scenario(
    model: str,
    scenario: Scenario,
    base_ctx: dict,
    identity: AgentIdentity,
    clean_slate: bool = False,
) -> RunOutcome:
    """驅動一個情境並回傳正規化結果。成本＝該身分 token_usage.json 前後差額。"""
    loop = asyncio.get_event_loop()
    # 前置：情境技能所需的 MCP 未安裝 → 乾淨略過（not-testable），避免「工具不在 → 模型亂試
    # → 紅 fail」的困惑（如 docgen 需先在 設定→MCP Hub 安裝 mcp-docgen）。
    missing_mcp = _missing_skill_mcp(base_ctx, scenario.skills)
    if missing_mcp:
        srv = ", ".join(missing_mcp)
        return RunOutcome(
            skipped=True,
            skip_reason=f"required MCP not installed: {srv}（請至 設定→MCP Hub 安裝）",
        )
    request = _scenario_request(model, scenario)
    ctx = _build_scorecard_context(base_ctx, identity, scenario, loop.time(), clean_slate)
    cost_before, tok_before = cost_meter.measure(identity)
    t_start = loop.time()
    raw_chunks: list[str] = []
    try:
        response = await process_proxy_request(request, ctx, is_anthropic=True)
        body_iter = getattr(response, "body_iterator", None)
        if body_iter is not None:
            async for chunk in body_iter:
                raw_chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        else:
            body = getattr(response, "body", b"")
            raw_chunks.append(body.decode() if isinstance(body, bytes) else str(body))
    except Exception as exc:  # 回傳為錯誤結果，不中斷整輪
        return RunOutcome(error=str(exc), latency_ms=int((loop.time() - t_start) * 1000))

    latency_ms = int((loop.time() - t_start) * 1000)
    cost_after, tok_after = cost_meter.measure(identity)
    pe = _parse_events(raw_chunks)
    text, tools, error, ev_tokens, ev_cost = pe.text, pe.tools, pe.error, pe.tokens, pe.cost
    # 優先採串流內 token_usage 事件（權威、per-model 計價、逐回合加總）；
    # 缺事件時才退回 token_usage.json 前後差額。
    delta_tokens = max(0, tok_after - tok_before)
    delta_cost = round(max(0.0, cost_after - cost_before), 6)
    tokens = ev_tokens if ev_tokens > 0 else delta_tokens
    cost = ev_cost if ev_cost > 0 else delta_cost
    estimated = False
    if tokens == 0 and not error and text.strip():
        # 上游未回報 usage（部分代理／聚合／自架端點會省略 usage 欄）→ 從 prompt＋回應文字
        # 保守估算 token，並以該模型費率估算成本，明確標記為估算值（不偽裝成實測）。
        prompt_est = estimate_tokens(scenario.prompt, model)
        completion_est = estimate_tokens(text, model)
        tokens = prompt_est + completion_est
        cost = pricing.get_pricing(model).cost_usd(prompt_est, completion_est)
        estimated = tokens > 0
    # Batch 5：產檔情境→從軌跡取產出路徑、開檔驗「格式合法＋含內容」（零相依、不阻斷整輪）。
    artifact_path = ""
    artifact_valid: bool | None = None
    artifact_detail = ""
    if scenario.artifact_kind:
        artifact_path = extract_artifact_path(pe.tool_trace, scenario.artifact_kind)
        artifact_valid, artifact_detail = verify_artifact(
            artifact_path, scenario.artifact_kind, scenario.artifact_must_contain
        )
    return RunOutcome(
        text=text,
        tools_called=tools,
        tokens=tokens,
        cost_usd=round(cost, 6),
        latency_ms=latency_ms,
        error=error,
        estimated=estimated,
        tool_trace=pe.tool_trace,
        react_turns=pe.react_turns,
        finish_reason=pe.finish_reason,
        early_stop=pe.early_stop,
        artifact_path=artifact_path,
        artifact_valid=artifact_valid,
        artifact_detail=artifact_detail,
    )


def make_real_runner(
    base_ctx: dict, identity: AgentIdentity, clean_slate: bool = False
) -> Callable[[str, Scenario], Awaitable[RunOutcome]]:
    """以生產的 base context（app.state.context）＋ SSOT 身分建立真實 runner。

    端點 /scorecard/run 用此工廠：計分卡與聊天／心跳走同一套情境＋身分來源，模型才會
    真的被發到記憶／MCP 工具（軌B 生產保真）。clean_slate=True → 該輪走無痕（白紙基線）。
    """
    async def _runner(model: str, scenario: Scenario) -> RunOutcome:
        return await _drive_scenario(model, scenario, base_ctx, identity, clean_slate)

    return _runner

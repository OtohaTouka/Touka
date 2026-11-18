import copy
import functools
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from src.adapters.content_normalize import append_to_last_user
from src.adapters.schemas import AnthropicMessage, OpenAIChatRequest, OpenAIMessage
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.context import PipelineContext  # noqa: TC001
from src.models.identity import AgentIdentity
from src.services.authorization_gateway import can_drive_soul
from src.services.client_system import CLIENT_SYSTEM_KEY
from src.services.dispatchers.factory import get_stream_generator
from src.services.dispatchers.utils import strip_redundant_labels
from src.services.group_router import make_alias_resolver, parse_group_roster
from src.services.mainline_service import inject_mainline
from src.services.model_manager import model_manager
from src.services.prompt_blocks import IDE_CONTEXT, wrap_block
from src.services.resource_manager import resource_manager
from src.services.soul_service import soul_service
from src.utils.ask_user_stop import UNANSWERED_KEY
from src.utils.brain_persistence import get_active_brain, record_working_brain
from src.utils.group_member_view import (
    MEMBER_SEPARATOR,
    LabelGuard,
    SpokenTurn,
    awareness_note,
    member_label,
    member_messages,
    neutralize_system_markers,
)
from src.utils.mcp_request_identity import GROUP_MEMBER_FLAG
from src.utils.progress_events import group_event
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)


def _member_brains(roster: list[dict[str, str]]) -> tuple[dict[str, str], str | None]:
    """每位成員送出用的模型名（ADR-0250 A3／A5）；有一位沒設或名字模糊就回點名的錯誤訊息。"""
    brains: dict[str, str] = {}
    for entry in roster:
        brain = get_active_brain(AgentIdentity(agent_id=entry["id"]))
        code = ErrorCode.AGENT_BRAIN_NOT_SET
        if brain:
            try:
                brains[entry["id"]] = model_manager.request_model_name(brain)
                continue
            except ToukaException as exc:
                code = exc.code
        logger.warning("Group member '%s' not dispatched: %s", entry["id"], code.name)
        reason = (
            "還沒設定主要模型"
            if code is ErrorCode.AGENT_BRAIN_NOT_SET
            else "的主要模型名稱只差大小寫、無法判斷"
        )
        return {}, f"『@{entry['name']}』{reason}，請先到靈魂設定選擇主要模型。"
    return brains, None


async def dispatch_group_stream(
    request: Any,
    request_id: str,
    start_time: float,
    context: PipelineContext,
) -> AsyncGenerator[str]:
    """Multi-agent group chat dispatcher (members are souls only, ADR-0245)."""
    registry_path = get_config_dir() / "agents_registry.json"
    data = resource_manager.read_json(str(registry_path))
    registry = data if data else {}  # fallback: empty registry (no agents configured)

    roster = parse_group_roster(request.messages, registry)
    # ADR-0245 修補：每位成員都要呼叫者有權驅動，有一位不行整輪不派發；
    # 沒有身分只會是 SNS 程式內部路徑，照舊。
    caller = context.get("identity")
    if isinstance(roster, list) and caller is not None:
        denied = next(
            (e for e in roster if not can_drive_soul(caller, AgentIdentity(agent_id=e["id"]))),
            None,
        )
        if denied:
            roster = {
                "error": f"沒有權限和『@{denied['name']}』對話，只限靈魂本身、擁有者或管理員。"
            }
    brains: dict[str, str] = {}
    if isinstance(roster, list):  # ADR-0250 A3：有一位沒有可用的主要模型，整輪不派發
        brains, brain_error = _member_brains(roster)
        if brain_error:
            roster = {"error": brain_error}

    if isinstance(roster, dict) and "error" in roster:
        error_msg = roster["error"]
        is_openai_req = isinstance(request, OpenAIChatRequest)
        if context.get("is_native", False) and not is_openai_req:
            payload = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f"❌ **系統提示**：{error_msg}\n\n"},
            }
            yield f"data: {json.dumps(payload)}\n\n"
        else:
            payload = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(get_current_epoch()),
                "model": "ToukaGroup",
                "choices": [
                    {"index": 0, "delta": {"content": f"❌ **系統提示**：{error_msg}\n\n"}}
                ],
                "system_fingerprint": None,
            }
            yield f"data: {json.dumps(payload)}\n\n"
        return

    if not roster:
        error_msg = "群組對話中未指定任何代理人，請使用 @名稱 標記要對話的對象。"
        is_openai_req = isinstance(request, OpenAIChatRequest)
        if context.get("is_native", False) and not is_openai_req:
            payload = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f"❌ **系統提示**：{error_msg}\n\n"},
            }
            yield f"data: {json.dumps(payload)}\n\n"
        else:
            payload = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(get_current_epoch()),
                "model": "ToukaGroup",
                "choices": [
                    {"index": 0, "delta": {"content": f"❌ **系統提示**：{error_msg}\n\n"}}
                ],
                "system_fingerprint": None,
            }
            yield f"data: {json.dumps(payload)}\n\n"
        return

    is_openai_req = isinstance(request, OpenAIChatRequest)
    msg_cls = OpenAIMessage if is_openai_req else AnthropicMessage
    # 標籤與分隔照舊依 is_native；成員文字照上游的格式（OpenAI 請求就是 OpenAI 片段）
    label_native = bool(context.get("is_native", False)) and not is_openai_req
    text_native = not is_openai_req
    resolve_alias = _alias_resolver(roster, registry)
    aliases = [entry["name"] for entry in roster]
    carried = any(entry.get("carried") for entry in roster)
    spoken: list[SpokenTurn] = []

    for idx, entry in enumerate(roster):
        alias_name = entry["name"]
        target_agent = entry["id"]

        # P1 結構化進度：群聊正在處理第 (idx+1)/len(roster) 位成員（@alias 顯示名）
        yield (
            f"data: {json.dumps(group_event(index=idx + 1, total=len(roster), agent=alias_name))}\n\n"
        )

        # 這位成員看到的訊息：自己說的算 assistant，別人說的包成群聊成員發言放在 user 那側；
        # 只對請求原本的訊息與這一輪已說過的人做，在套靈魂與主線之前（ADR-0322）
        current_request = copy.deepcopy(request)
        current_request.messages = member_messages(
            current_request.messages,
            self_id=target_agent,
            self_alias=alias_name,
            resolve=resolve_alias,
            spoken=spoken,
            make=functools.partial(_message, msg_cls),
        )

        # 成員一律是靈魂：parse_group_roster 只回 agents_registry 的鍵（ADR-0245）
        target_identity = AgentIdentity(agent_id=target_agent)
        brain_model = brains[target_agent]
        current_request.model = brain_model
        soul_service.apply_soul_to_request(
            current_request, target_identity, client_system=context.get(CLIENT_SYSTEM_KEY)
        )
        await inject_mainline(  # 前情＋家原文（ADR-0305）；無痕都不帶
            current_request,
            target_identity,
            mode="incognito" if context.get("incognito") else "group",
            sns=bool(context.get("sns_source")),
        )
        neutralize_system_markers(current_request)  # 前情裡也假造不了群聊成員發言

        ide_ctx = getattr(request, "ide_context", None)
        ide_note = ""
        if ide_ctx:
            fn = ide_ctx.get("fileName", "Unknown")
            ln = ide_ctx.get("line", "?")
            ide_note = "\n" + wrap_block(IDE_CONTEXT, f"📄 {fn} | 📍 Line {ln}")

        append_to_last_user(
            current_request,
            awareness_note(
                brain=brain_model,
                aliases=aliases,
                index=idx,
                carried=carried,
                sns=bool(context.get("sns_source")),
                ide_note=ide_note,
            ),
        )

        # UI Label Prefix
        yield _text_chunk(member_label(alias_name), native=label_native)

        member = _MemberText(alias_name)

        # Nested context for individual agent call
        nested_ctx = context.model_copy()
        nested_ctx["is_toukasoul"] = True
        # 成員的 ctx 看起來和直接跟靈魂聊天一樣：標成群聊，執行器才不讓它用使用者各自授權的 MCP
        nested_ctx[GROUP_MEMBER_FLAG] = True
        # 失敗句不混進成員的話：由這裡補給外面，下一位只看到「這一輪出錯」（ADR-0322）
        nested_ctx["caller_sends_failure_text"] = True

        stream_gen = await get_stream_generator(
            brain_model,
            current_request,
            target_identity,
            request_id,
            start_time,
            nested_ctx,
        )

        async for chunk in stream_gen:
            if chunk.strip() == "data: [DONE]":
                tail = member.finish(_failure_text(nested_ctx))
                if tail:
                    yield _text_chunk(tail, native=text_native)
                yield chunk
                continue

            if chunk.startswith("data: "):
                try:
                    text_delta, is_tool_call = _delta_of(json.loads(chunk[6:]))

                    if not text_delta and not is_tool_call:
                        yield chunk
                        continue

                    if is_tool_call:
                        released = member.before_tool_call()
                        if released:
                            yield _text_chunk(released, native=text_native)
                        member.full += text_delta
                        yield chunk
                        continue

                    released = member.text(text_delta)
                    if released == text_delta:
                        yield chunk  # 沒有留字也沒有改字：原樣轉送（保留片段的其他欄位）
                    elif released:
                        yield _text_chunk(released, native=text_native)
                    continue
                except Exception:
                    if member.started:
                        yield chunk
                    continue

            if member.started or not chunk.startswith("data: "):
                yield chunk

        tail = member.finish(_failure_text(nested_ctx))
        if tail:
            yield _text_chunk(tail, native=text_native)

        # 這位成員問的問題沒等到回答：整輪停下，下一位成員不再呼叫模型（ADR-0308）
        if nested_ctx.get(UNANSWERED_KEY):
            record_working_brain(target_identity, brain_model)  # 這位的模型呼叫是成功的
            break

        if idx < len(roster) - 1:
            yield _text_chunk(MEMBER_SEPARATOR, native=label_native)

        spoken.append(
            SpokenTurn(
                alias=alias_name,
                soul_id=target_agent,
                text=strip_redundant_labels(member.full, alias_name),
                failed=bool(nested_ctx.get("model_failed")),
            )
        )
        record_working_brain(target_identity, brain_model)


@dataclass
class _MemberText:
    """一位成員串流出來的文字：先拿掉開頭自己加的標籤，再經守門換掉偽造的成員標籤。"""

    alias: str
    guard: LabelGuard = field(default_factory=LabelGuard)
    buffer: str = ""
    full: str = ""
    started: bool = False
    finished: bool = False

    def _strip_own_label(self, text: str) -> str:
        pattern = (
            rf"^\s*(\*\*|__)?\s*[【\[\(]?\s*{re.escape(self.alias)}"
            rf"\s*[】\]\)]?\s*(\*\*|__)?\s*[:：\s-]*\n*"
        )
        return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)

    def text(self, delta: str) -> str:
        """收一段文字，回傳現在可以送出的部分（開頭 50 字內或第一個換行前先留著找標籤）。"""
        self.full += delta
        if self.started:
            return self.guard.feed(delta)
        self.buffer += delta
        if len(self.buffer) > 50 or "\n" in self.buffer:
            self.started = True
            return self.guard.feed(self._strip_own_label(self.buffer))
        return ""

    def before_tool_call(self) -> str:
        """工具呼叫前：開頭還留著的文字先處理掉。"""
        if self.started:
            return ""
        self.started = True
        return self.guard.feed(self._strip_own_label(self.buffer)) if self.buffer else ""

    def finish(self, failure: str | None) -> str:
        """收尾（只做一次）：開頭還留著的、守門留著的尾巴，失敗時再接失敗句。"""
        if self.finished:
            return ""
        self.finished = True
        out = self.before_tool_call() + self.guard.flush()
        if failure:
            out += ("\n\n" if self.full.strip() else "") + failure
        return out


def _delta_of(data: dict[str, Any]) -> tuple[str, bool]:
    """一個串流片段裡的文字，以及它是不是工具呼叫（OpenAI 與 Anthropic 兩種格式）。"""
    if "choices" in data and len(data["choices"]) > 0:
        delta = data["choices"][0].get("delta", {})
        return delta.get("content", "") or "", bool(delta.get("tool_calls"))
    if data.get("type") == "content_block_delta":
        return data["delta"].get("text", "") or "", False
    if data.get("type") in ["content_block_start", "message_start"]:
        return "", data.get("content_block", {}).get("type") != "text"
    return "", False


def _failure_text(nested_ctx: PipelineContext) -> str | None:
    """成員模型失敗時，``react_core`` 記下的失敗句（已遮罩；SNS 是固定文字）。"""
    return nested_ctx.get("model_failure_text") if nested_ctx.get("model_failed") else None


def _message(msg_cls: type[Any], role: str, content: Any) -> Any:
    return msg_cls(role=role, content=content)


def _text_chunk(text: str, *, native: bool) -> str:
    """一段文字的串流片段（Anthropic 原生或 OpenAI 格式）。"""
    if native:
        payload: dict[str, Any] = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    else:
        payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return f"data: {json.dumps(payload)}\n\n"


def _alias_resolver(roster: list[dict[str, Any]], registry: dict) -> Callable[[str], str | None]:
    """舊回合標籤的別名 → 靈魂 ID：先看這一輪的名單，再看白名單（用到才讀，ADR-0322）。"""
    this_round = {entry["name"].lower(): entry["id"] for entry in roster}
    whitelist: list[Callable[[str], str | None]] = []

    def resolve(alias: str) -> str | None:
        found = this_round.get(alias.lower())
        if found is not None:
            return found
        if not whitelist:
            whitelist.append(make_alias_resolver(registry))
        return whitelist[0](alias)

    return resolve

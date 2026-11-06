import copy
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from src.adapters.schemas import AnthropicMessage, OpenAIChatRequest, OpenAIMessage
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.context import PipelineContext  # noqa: TC001
from src.models.identity import AgentIdentity
from src.services.authorization_gateway import can_drive_soul
from src.services.dispatchers.factory import get_stream_generator
from src.services.dispatchers.utils import strip_redundant_labels
from src.services.group_router import parse_group_roster
from src.services.model_manager import model_manager
from src.services.prompt_blocks import IDE_CONTEXT, wrap_block
from src.services.resource_manager import resource_manager
from src.services.soul_service import soul_service
from src.utils.brain_persistence import get_active_brain, record_working_brain
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

    cumulative_request = copy.deepcopy(request)
    is_openai_req = isinstance(request, OpenAIChatRequest)

    for idx, entry in enumerate(roster):
        alias_name = entry["name"]
        target_agent = entry["id"]

        # P1 結構化進度：群聊正在處理第 (idx+1)/len(roster) 位成員（@alias 顯示名）
        yield (
            f"data: {json.dumps(group_event(index=idx + 1, total=len(roster), agent=alias_name))}\n\n"
        )

        # Ensure API role alternation
        if cumulative_request.messages and cumulative_request.messages[-1].role == "assistant":
            dummy_content = f"（系統：請 @{alias_name} 接續發言）"
            msg_cls = OpenAIMessage if is_openai_req else AnthropicMessage
            cumulative_request.messages.append(msg_cls(role="user", content=dummy_content))

        current_request = copy.deepcopy(cumulative_request)

        # 成員一律是靈魂：parse_group_roster 只回 agents_registry 的鍵（ADR-0245）
        target_identity = AgentIdentity(agent_id=target_agent)
        brain_model = brains[target_agent]
        current_request.model = brain_model
        soul_service.apply_soul_to_request(current_request, target_identity)
        await soul_service.inject_home_messages(current_request, target_identity)

        ide_ctx = getattr(request, "ide_context", None)
        ide_note = ""
        if ide_ctx:
            fn = ide_ctx.get("fileName", "Unknown")
            ln = ide_ctx.get("line", "?")
            ide_note = "\n" + wrap_block(IDE_CONTEXT, f"📄 {fn} | 📍 Line {ln}")

        awareness_note = (
            f"\n\n(你目前使用的底層模型為 '{brain_model}'。使用者稱呼你為 '@{alias_name}'。"
            f"\n你正在一個群聊系統中。{ide_note}"
            "\n請「直接」開始你的回答內容。"
            "\n**嚴禁**在開頭加上任何標章或前綴。不要加上自己的名稱或 @提及。"
            "\n**嚴禁**模仿或重複前一位發言者的標籤（例如 **【...】**）。"
            "\n請以此身份直接從對話內容開始回覆即可。)"
        )

        if current_request.messages:
            last_msg = current_request.messages[-1]
            if hasattr(last_msg, "content") and isinstance(last_msg.content, str):
                last_msg.content += awareness_note
            elif isinstance(last_msg, dict) and "content" in last_msg:
                last_msg["content"] += awareness_note

        # UI Label Prefix
        agent_prefix = f"**【{alias_name}】**：\n\n"
        if context.get("is_native", False) and not is_openai_req:
            payload = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": agent_prefix},
            }
            yield f"data: {json.dumps(payload)}\n\n"
        else:
            payload = {"choices": [{"delta": {"content": agent_prefix}, "index": 0}]}
            yield f"data: {json.dumps(payload)}\n\n"

        agent_full_response = ""
        alias_pattern = (
            rf"^\s*(\*\*|__)?\s*[【\[\(]?\s*{re.escape(alias_name)}"
            rf"\s*[】\]\)]?\s*(\*\*|__)?\s*[:：\s-]*\n*"
        )
        buffer = ""
        stripped_first_label = False

        # Nested context for individual agent call
        nested_ctx = context.model_copy()
        nested_ctx["is_toukasoul"] = True
        # 成員的 ctx 看起來和直接跟靈魂聊天一樣：標成群聊，執行器才不讓它用使用者各自授權的 MCP
        nested_ctx[GROUP_MEMBER_FLAG] = True

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
                if not stripped_first_label and buffer:
                    cleaned_buffer = re.sub(alias_pattern, "", buffer, count=1, flags=re.IGNORECASE)
                    if is_openai_req:
                        payload = {"choices": [{"delta": {"content": cleaned_buffer}, "index": 0}]}
                        yield f"data: {json.dumps(payload)}\n\n"
                    else:
                        payload = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": cleaned_buffer},
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                    stripped_first_label = True
                yield chunk
                continue

            if chunk.startswith("data: "):
                try:
                    data = json.loads(chunk[6:])
                    text_delta = ""
                    is_tool_call = False

                    if "choices" in data and len(data["choices"]) > 0:
                        delta = data["choices"][0].get("delta", {})
                        text_delta = delta.get("content", "")
                        if delta.get("tool_calls"):
                            is_tool_call = True
                    elif data.get("type") == "content_block_delta":
                        text_delta = data["delta"].get("text", "")
                    elif data.get("type") in ["content_block_start", "message_start"]:
                        if data.get("content_block", {}).get("type") != "text":
                            is_tool_call = True

                    if not text_delta and not is_tool_call:
                        yield chunk
                        continue

                    if is_tool_call and not stripped_first_label:
                        if buffer:
                            cleaned_buffer = re.sub(
                                alias_pattern, "", buffer, count=1, flags=re.IGNORECASE
                            )
                            if is_openai_req:
                                payload = {
                                    "choices": [
                                        {"delta": {"content": cleaned_buffer}, "index": 0}
                                    ]
                                }
                                yield f"data: {json.dumps(payload)}\n\n"
                            else:
                                payload = {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": cleaned_buffer,
                                    },
                                }
                                yield f"data: {json.dumps(payload)}\n\n"
                        stripped_first_label = True
                        yield chunk
                        continue

                    agent_full_response += text_delta

                    if not stripped_first_label:
                        buffer += text_delta
                        if len(buffer) > 50 or "\n" in buffer:
                            cleaned_buffer = re.sub(
                                alias_pattern, "", buffer, count=1, flags=re.IGNORECASE
                            )
                            if is_openai_req:
                                payload = {
                                    "choices": [
                                        {"delta": {"content": cleaned_buffer}, "index": 0}
                                    ]
                                }
                                yield f"data: {json.dumps(payload)}\n\n"
                            else:
                                payload = {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "text_delta",
                                        "text": cleaned_buffer,
                                    },
                                }
                                yield f"data: {json.dumps(payload)}\n\n"
                            stripped_first_label = True
                        continue
                except Exception:
                    if stripped_first_label:
                        yield chunk
                    continue

            if stripped_first_label or not chunk.startswith("data: "):
                yield chunk

        if not stripped_first_label and buffer:
            cleaned_buffer = re.sub(alias_pattern, "", buffer, count=1, flags=re.IGNORECASE)
            if is_openai_req:
                payload = {"choices": [{"delta": {"content": cleaned_buffer}, "index": 0}]}
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                payload = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": cleaned_buffer},
                }
                yield f"data: {json.dumps(payload)}\n\n"
            stripped_first_label = True

        if idx < len(roster) - 1:
            sep = "\n\n&nbsp;\n\n---\n\n&nbsp;\n\n"
            if context.get("is_native", False) and not is_openai_req:
                payload = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": sep},
                }
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                payload = {"choices": [{"delta": {"content": sep}, "index": 0}]}
                yield f"data: {json.dumps(payload)}\n\n"

        final_content = strip_redundant_labels(agent_full_response, alias_name)
        msg_cls = OpenAIMessage if is_openai_req else AnthropicMessage
        cumulative_request.messages.append(msg_cls(role="assistant", content=final_content))
        record_working_brain(target_identity, brain_model)

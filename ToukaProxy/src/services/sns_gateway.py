"""SNS 閘道器服務模組."""
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.core.exceptions import ErrorCode, ToukaException
from src.models.context import PipelineContext
from src.models.sns import SNSIncomingEvent, SNSOutgoingMessage  # noqa: TC001
from src.protocols.sns_base import BaseSNSAdapter  # noqa: TC001
from src.services.agent_detector import detect_target_agent
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.proxy_service import process_proxy_request
from src.utils.async_db import session_service
from src.utils.user_intent import SNS_MESSAGE_PREFIX

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


# M3 fix: maximum allowed content length before regex processing.
# Prevents excessively long strings from being fed to the alias regex.
MAX_SNS_CONTENT_LENGTH: int = 10_000


class SNSGatewayService:
    """大一統 SNS 通訊閘道器.

    負責標準化不同平台的通訊邏輯，並對接代理人執行層 (SOLID - SRP)。
    支援 @alias 語法自動切換至 ToukaGroup 群聊分發器（成員只限靈魂，ADR-0245）。
    """

    # M3 fix: bounded quantifier {1,50} replaces the bare + to prevent ReDoS.
    # Aliases longer than 50 chars are not captured as a single match.
    # Also shared with group_router.py as SSOT for @alias syntax.
    _ALIAS_PATTERN: re.Pattern[str] = re.compile(
        r"@([a-zA-Z0-9_\-一-龥]{1,50})"
    )

    def __init__(self) -> None:
        """初始化閘道器."""
        self.last_session_id: str | None = None
        self.last_target_agent: str | None = None
        self.context: dict[str, Any] = {}

    @classmethod
    def _has_group_mentions(cls, content: str) -> bool:
        """判斷訊息是否含有 @alias 群聊觸發語法。

        偵測到 @alias 時，on_event 會將請求轉向 ToukaGroup 分發器，
        由 ToukaGroup 解析 group_whitelist.json，且只分配給靈魂（ADR-0245）。
        """
        return bool(cls._ALIAS_PATTERN.search(content))

    def _guard_content_length(self, content: str) -> str:
        """Truncate content that exceeds MAX_SNS_CONTENT_LENGTH (M3 fix).

        Prevents excessively long strings from being fed into the alias regex,
        defending against ReDoS via crafted inputs.

        Args:
            content: Raw SNS message content.

        Returns:
            Content truncated to MAX_SNS_CONTENT_LENGTH if necessary.
        """
        if len(content) > MAX_SNS_CONTENT_LENGTH:
            logger.warning(
                "⚠️ [SNSGateway] Content truncated from %d to %d chars (ReDoS guard).",
                len(content),
                MAX_SNS_CONTENT_LENGTH,
            )
            return content[:MAX_SNS_CONTENT_LENGTH]
        return content

    @staticmethod
    async def _save_user_message(
        session_id: str | None, content: str, identity: "AgentIdentity"
    ) -> None:
        """有對話時把外人訊息寫進去；寫不進去回 ``SESSION_SAVE_FAILED``."""
        if session_id and not await session_service.append_message_to_session(
            session_id, "user", content, identity=identity
        ):
            raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

    def _warn_if_unverified(self, event: SNSIncomingEvent) -> None:
        """Log a warning when processing an SNS event that has not been signature-verified.

        C7 fix: every incoming SNS event should be authenticated by the platform
        SDK before being processed. Platform adapters MUST set event.is_verified=True
        only after successful HMAC/signature validation.
        """
        if not event.is_verified:
            logger.warning(
                "⚠️  [C7] Processing unverified SNS event from platform=%s channel=%s. "
                "Platform adapter should validate webhook signature and set is_verified=True.",
                event.platform,
                event.channel_id,
            )

    async def on_event(
        self,
        adapter: BaseSNSAdapter,
        event: SNSIncomingEvent,
        session_mode: str = "independent",
    ) -> str:
        """處理來自任何 SNS 適配器的統一事件.

        Args:
            adapter: 發送事件的適配器實例。
            event: 標準化的輸入事件。
            session_mode: 對話模式 ('independent' 或 'home')。

        Returns:
            str: AI 生成的最終回覆內容。
        """
        # C7 fix: warn if event has not been signature-verified by the platform adapter
        self.last_session_id = self.last_target_agent = None
        self._warn_if_unverified(event)

        # M3 fix: guard content length before any regex processing
        event = event.model_copy(
            update={"content": self._guard_content_length(event.content)}
        )

        # 1. 偵測目標 Agent (SSOT)
        target_agent_id = adapter.agent_id
        is_group = self._has_group_mentions(event.content)
        if not is_group:
            # 無 @alias → 嘗試單 Agent 偵測（保留現有行為）
            detected = detect_target_agent([{"role": "user", "content": event.content}], "")
            if detected:
                target_agent_id = detected

        try:
            source = agent_service.identify_agent(adapter.agent_id)
            target = agent_service.identify_agent(target_agent_id)
            retarget = source and target and source["agent_id"] != target["agent_id"]
            owners = (
                [user for user in AuthService().get_all_users()
                 if source["agent_id"] in user.owned_agents]
                if retarget else []
            )
        except Exception:
            raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH) from None
        if (
            not source or not target or not source["is_toukasoul"] or not target["is_toukasoul"]
            or (retarget and (len(owners) != 1 or target["agent_id"] not in owners[0].owned_agents))
        ):
            raise ToukaException(ErrorCode.SESSION_SCOPE_MISMATCH)
        target_agent_id = target["agent_id"]
        identity = identity_service.get_identity(target_agent_id)
        self.last_target_agent = "ToukaGroup" if is_group else target_agent_id
        session_id = None
        if not is_group:
            home = (
                await session_service.read_home_snapshot(identity)
                if session_mode == "home" else None
            )
            session_id = home.session["id"] if home else await session_service.open_channel_session(
                "sns", identity, event.platform, event.channel_id,
                title=f"{event.platform.capitalize()}: {event.channel_id}",
            )
        self.last_session_id = session_id

        # 3. 外人訊息加固定開頭（存檔與送出一致），Auto 審查員的使用者原意不會把它當成人類原話
        #    （R-2c ① #3）
        content = f"{SNS_MESSAGE_PREFIX}\n{event.content}"

        # 4. 呼叫 Proxy 執行層
        # 外人訊息不帶靈魂身分進 pipeline：技能與特權維持零，只固定目標靈魂與對話。
        ctx = PipelineContext(**{
            **self.context,
            "is_native": False,
            "session_id": session_id,
            "agent_id": identity.agent_id,
        })

        # 4a. 決定模型：@alias → ToukaGroup 群聊分發器，否則單 Agent
        model_name = (
            "ToukaGroup"
            if is_group
            else target_agent_id
        )

        request = OpenAIChatRequest(
            model=model_name,
            messages=[OpenAIMessage(role="user", content=content)],
            stream=True,
            session_id=session_id,
        )

        # 顯示正在輸入中
        await adapter.show_typing(event.channel_id)

        # 4b. 紀錄 User 訊息：pipeline 跑完才存，UserPromptSubmit hook 擋下的不寫進對話、
        #     也不回覆外人（擋下原因是設定的一部分，不給外人看；ADR-0292），只記一筆 log；
        #     其他錯誤照舊先存再往外丟。回覆是串流，存好之前不會讀，存不進去就不呼叫模型。
        try:
            response = await process_proxy_request(request, ctx, is_anthropic=False)
        except ToukaException as exc:
            if exc.code == ErrorCode.HOOK_PROMPT_BLOCKED:
                logger.info(
                    "SNS message from platform=%s channel=%s was blocked by a hook",
                    event.platform,
                    event.channel_id,
                )
                return ""
            await self._save_user_message(session_id, content, identity)
            raise
        except Exception:
            await self._save_user_message(session_id, content, identity)
            raise
        await self._save_user_message(session_id, content, identity)

        # 5. 聚合回應內容 (Aggregator)
        full_text = ""
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")

            lines = chunk.split("\n\n")
            for line in lines:
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta:
                        full_text += delta["content"]
                except Exception:
                    logger.exception("Failed to parse SSE chunk")
                    continue

        # 6. 紀錄 Assistant 回覆並發送
        if full_text:
            if session_id and not await session_service.append_message_to_session(
                session_id,
                "assistant",
                full_text,
                name=target_agent_id,
                identity=identity,
            ):
                raise ToukaException(ErrorCode.SESSION_SAVE_FAILED)

            outgoing = SNSOutgoingMessage(text=full_text)
            await adapter.send_message(event.channel_id, outgoing)

        return full_text


# Singleton 實例
sns_gateway = SNSGatewayService()

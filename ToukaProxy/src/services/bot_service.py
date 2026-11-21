"""機器人管理服務模組."""
import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

from src.adapters.discord_bot import DiscordAdapter
from src.adapters.telegram_bot import TelegramAdapter
from src.config import get_config_dir
from src.services.resource_manager import resource_manager
from src.utils.agent_spellings import legacy_spellings

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.protocols.sns_base import BaseSNSAdapter

logger = logging.getLogger(__name__)

_PLATFORMS = ("discord", "telegram")
# 停一個機器人最多等多久（停用與整批重載都平行停，各自計時；ADR-0326）
BOT_STOP_SECONDS = 10.0
# bots.json 的讀改寫（禁令 21；ADR-0326）：設定頁的讀取遷移、存檔與停用共用。讀與寫都是同步程式、
# 中間沒有 await；Proxy 只跑單一程序（compose 不給 --workers），所以和 users.json 一樣用程序內鎖。
_config_lock = threading.Lock()


def flatten_agent_id(value: Any) -> str:
    """接受字串或舊版 AgentIdentity-dict，回傳 agent id 字串（B3）.

    歷史上 `agent_id` 經 AgentIdentity 驗證器序列化成完整物件落盤；adapter 與前端
    都只要字串 id，比對與 API 邊界一律攤平為字串（執行期 identity 於 dispatch 時解析）。
    """
    if isinstance(value, dict):
        return str(value.get("agent_id", ""))
    return str(value) if value else ""


def disable_bound(
    config: dict[str, Any], spellings: tuple[str, str]
) -> tuple[dict[str, Any], int]:
    """把綁著這些寫法、而且開著的機器人關掉；回新設定與關掉幾個（不改傳入的物件）.

    只改 ``enabled``：token、``id``、``agent_id`` 與其他鍵都不動；不是 dict、沒有 ``agent_id``、
    綁別的名字、已經關掉的條目原樣保留。比對逐字（只差大小寫的是另一個代理人，ADR-0326）。
    """
    result = dict(config)
    count = 0
    for platform in _PLATFORMS:
        items = []
        for entry in config.get(platform) or []:
            bound = (
                isinstance(entry, dict)
                and entry.get("enabled")
                and flatten_agent_id(entry.get("agent_id")) in spellings
            )
            items.append({**entry, "enabled": False} if bound else entry)
            count += 1 if bound else 0
        if platform in config:
            result[platform] = items
    return result, count


class BotsDisabled(NamedTuple):
    """停用的結果：設定有沒有寫成，以及已經標停用、移出執行清單、還要停的 adapter."""

    config_ok: bool
    adapters: list["BaseSNSAdapter"]


class BotService:
    """機器人管理服務 (SOLID - SRP).

    負責從設定檔載入、啟動、停止以及管理所有 SNS 機器人適配器。
    """

    def __init__(self) -> None:
        """初始化機器人服務."""
        self.config_path = get_config_dir() / "bots.json"
        self._running_bots: dict[str, dict[str, Any]] = {"discord": {}, "telegram": {}}
        self._tasks: list[asyncio.Task[None]] = []

    def locked(self) -> threading.Lock:
        """``bots.json`` 讀改寫的鎖：讀、改、寫整段放在 ``with bot_service.locked():`` 裡."""
        return _config_lock

    def load_config(self) -> dict[str, list[dict[str, Any]]]:
        """從設定檔載入機器人配置.

        Returns:
            dict: 包含 'discord' 與 'telegram' 配置列表的字典。
        """
        data = resource_manager.read_json(self.config_path)
        return data if data else {"discord": [], "telegram": []}

    def save_config(self, config: dict[str, list[dict[str, Any]]]) -> None:
        """將機器人配置儲存至設定檔.

        Args:
            config: 要儲存的配置字典。
        """
        resource_manager.make_dir(self.config_path.parent)
        resource_manager.write_json(self.config_path, config)

    def _read_for_update(self) -> dict[str, Any] | None:
        """停用用的嚴格讀取：沒有檔案回空設定；讀不出來或形狀不對回 None（不能當成沒有機器人）."""
        if not resource_manager.exists(self.config_path):
            return {"discord": [], "telegram": []}
        data = resource_manager.read_json(self.config_path)
        if not isinstance(data, dict):
            return None
        if any(not isinstance(data.get(p, []), list) for p in _PLATFORMS):
            return None
        return data

    def disable_bots_for(self, identity: "AgentIdentity") -> BotsDisabled:
        """關掉綁著這個名字（兩種寫法）的機器人，正在跑的標停用、移出執行清單（不丟例外）.

        設定寫不成（讀不出來、寫不進去、意料外的例外）時 ``config_ok`` 為 False，但正在跑的照樣
        標停用：閘道從此拒絕它們轉來的訊息。這裡沒有 await；回傳的 adapter 交給 ``stop_adapters``。
        其他機器人不停、不重啟。
        """
        spellings = legacy_spellings(identity.agent_id)
        config_ok = True
        try:
            with _config_lock:
                config = self._read_for_update()
                if config is None:
                    logger.error("bots.json is unreadable; bots of %s not disabled", spellings[0])
                    config_ok = False
                else:
                    updated, count = disable_bound(config, spellings)
                    if count:
                        self.save_config(updated)
                        logger.info("🔌 Disabled %d bot(s) bound to %s", count, spellings[0])
        except Exception:
            logger.exception("❌ Bots bound to %s were not disabled in bots.json", spellings[0])
            config_ok = False
        retired: list[BaseSNSAdapter] = []
        for platform, bots in self._running_bots.items():
            for key, bot in list(bots.items()):
                if flatten_agent_id(bot.agent_id) in spellings:
                    bot.retired = True
                    del self._running_bots[platform][key]
                    retired.append(bot)
        return BotsDisabled(config_ok, retired)

    async def stop_adapters(self, adapters: list["BaseSNSAdapter"]) -> None:
        """平行停這些 adapter，每個最多 ``BOT_STOP_SECONDS``；停不下來只記警告（不丟例外）."""

        async def _stop(adapter: "BaseSNSAdapter") -> None:
            try:
                async with asyncio.timeout(BOT_STOP_SECONDS):
                    await adapter.stop()
            except Exception:
                # 只寫綁的名字：執行清單的鍵含 token 開頭
                logger.warning(
                    "A bot bound to %s did not stop cleanly",
                    flatten_agent_id(adapter.agent_id),
                    exc_info=True,
                )

        await asyncio.gather(*(_stop(adapter) for adapter in adapters))

    # handle_bot_request 已由 SNSGatewayService 接管

    async def start_bots(self) -> None:
        """根據配置啟動所有啟用的機器人."""
        config = self.load_config()

        # 啟動 Discord
        for bot_conf in config.get("discord", []):
            if bot_conf.get("enabled", False) and bot_conf.get("token"):
                token = bot_conf.get("token")
                key = f"discord_{token[:5]}"
                if key not in self._running_bots["discord"]:
                    bot = DiscordAdapter(
                        token=token,
                        agent_id=bot_conf.get("agent_id"),
                        is_multi_agent=bot_conf.get("is_multi_agent", False),
                        session_mode=bot_conf.get("session_mode", "independent"),
                    )
                    task = asyncio.create_task(bot.start())
                    self._tasks.append(task)
                    self._running_bots["discord"][key] = bot

        # 啟動 Telegram
        for bot_conf in config.get("telegram", []):
            if bot_conf.get("enabled", False) and bot_conf.get("token"):
                token = bot_conf.get("token")
                key = f"telegram_{token[:5]}"
                if key not in self._running_bots["telegram"]:
                    bot = TelegramAdapter(
                        token=token,
                        agent_id=bot_conf.get("agent_id"),
                        is_multi_agent=bot_conf.get("is_multi_agent", False),
                        session_mode=bot_conf.get("session_mode", "independent"),
                    )
                    task = asyncio.create_task(bot.start())
                    self._tasks.append(task)
                    self._running_bots["telegram"][key] = bot

    async def stop_bots(self) -> None:
        """停止所有正在運行的機器人.

        先換成新的空清單、舊的全標停用，再平行停：停用可能在停的期間改執行清單，不能一邊迭代
        一邊 await（ADR-0326）。
        """
        old = [bot for bots in self._running_bots.values() for bot in bots.values()]
        tasks = self._tasks
        self._running_bots = {"discord": {}, "telegram": {}}
        self._tasks = []
        for bot in old:
            bot.retired = True
        await self.stop_adapters(old)
        for task in tasks:
            task.cancel()

    async def reload_bots(self) -> None:
        """重新載入並啟動所有機器人 (熱重載支援)."""
        logger.info("Reloading Bots...")
        await self.stop_bots()
        await self.start_bots()

    async def send_external_message(
        self, platform: str, channel_id: str, content: str
    ) -> bool:
        """從外部（如 Tool 或 Heartbeat）發送訊息至指定 SNS 頻道.

        Args:
            platform: 平台類型 ('discord' 或 'telegram')。
            channel_id: 頻道/對話 ID。
            content: 訊息內容。

        Returns:
            bool: 是否發送成功。
        """
        try:
            if platform == "discord":
                # 尋找第一個可用的 Discord Bot（迭代快照：停用可能在 await 期間改清單）
                for bot in list(self._running_bots["discord"].values()):
                    if not bot.client:
                        continue
                    channel = bot.client.get_channel(int(channel_id))
                    if not channel:
                        # 嘗試 fetch
                        try:
                            channel = await bot.client.fetch_channel(int(channel_id))
                        except Exception:
                            logger.debug("Failed to fetch Discord channel in external send")
                            continue
                    if channel:
                        await channel.send(content)
                        return True
            elif platform == "telegram":
                for bot in list(self._running_bots["telegram"].values()):
                    try:
                        await bot.application.bot.send_message(
                            chat_id=channel_id, text=content
                        )
                    except Exception:
                        logger.debug("Telegram external send failed in loop")
                        continue
                    else:
                        return True
        except Exception:
            logger.exception(f"Failed to send external message to {platform}:{channel_id}")
        return False


bot_service = BotService()

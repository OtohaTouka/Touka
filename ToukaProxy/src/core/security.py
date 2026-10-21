"""JWT 密鑰管理 (AuthSecretManager).

安全原則：
- 不使用任何公開已知的預設值作為 JWT 簽署密鑰。
- 若 TOUKA_AUTH_SECRET 未設定，啟動時自動生成隨機密鑰（不可持久，重啟後 Token 失效）。
- 建議生產環境務必透過環境變數設定強密鑰（>=32 字元）。
"""

import logging
import secrets
import threading

from src.config import TOUKA_AUTH_SECRET, persist_generated_secret, usable_secret  # noqa: TC001

logger = logging.getLogger(__name__)


class AuthSecretManager:
    """密鑰管理 (SSOT)：負責讀取與維護驗證金鑰 (SOLID - SRP)。

    這是驗證 Token 的唯一事實來源。
    """

    _secret: str | None = None
    _lock = threading.Lock()

    @classmethod
    def get_secret(cls) -> str:
        """獲取驗證金鑰。

        優先使用 TOUKA_AUTH_SECRET 環境變數（歷來公開過的值當成沒設定，見 ``usable_secret``）。
        若未設定，啟動時自動生成 64-char 隨機 hex 密鑰；bootstrap 啟用時**持久化至 .env**
        （比照 HEARTBEAT_TOKEN，重啟後 Token 仍有效），持久化失敗才退回臨時密鑰並警告。
        延遲生成以 double-checked locking 保護，避免並行首呼各生成不同密鑰。
        """
        if cls._secret is None:
            with cls._lock:
                if cls._secret is None:  # 鎖內複查：先到者已生成則直接沿用
                    configured = usable_secret("TOUKA_AUTH_SECRET", TOUKA_AUTH_SECRET)
                    if configured:
                        cls._secret = configured
                    else:
                        cls._secret = secrets.token_hex(32)
                        # 禁令 15：持久密鑰首次隨機生成即持久化（免精靈、免重啟登出）。
                        if not persist_generated_secret("TOUKA_AUTH_SECRET", cls._secret):
                            logger.warning(
                                "⚠️  TOUKA_AUTH_SECRET 未設定且未能持久化——已生成臨時密鑰"
                                "（重啟後所有 Token 失效）。生產環境請設定 TOUKA_AUTH_SECRET。"
                            )
        return cls._secret

    @classmethod
    def reset_for_test(cls) -> None:
        """重置快取密鑰（僅供測試使用）。"""
        cls._secret = None

    @classmethod
    def verify_secret_strength(cls) -> bool:
        """判定密鑰強度是否足以對抗暴力破解（≥32 字元）。"""
        return len(cls.get_secret()) >= 32

"""自主權服務 (Autonomy Service).

負責管理 Agent 的每日配額 (Quota)、預算記錄 (Logs) 以及自主權配置 (Config)。
本服務專注於「主權與預算」，而具體的行動節奏由 RhythmService 負責。
"""

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import get_config_dir, is_bootstrap_enabled
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.utils.time_utils import get_current_taipei_iso

logger = logging.getLogger(__name__)

# P18 fix: serialize quota reads/writes to prevent concurrent file corruption
_autonomy_lock = threading.Lock()

# 配置與日誌路徑 (SSOT)
CONFIG_FILE = get_config_dir() / "autonomy.json"


@dataclass(frozen=True)
class AutonomyConfigHealth:
    """autonomy.json 讀取健康快照。"""

    degraded: bool = False
    reason: str = ""


def _read_config_file(target: Path) -> Any | None:
    """讀設定檔：不存在或壞檔回 None（同 read_json）；存在卻讀不到時拋 OSError。"""
    if not resource_manager.exists(target):
        return None
    try:
        data = json.loads(resource_manager.read_file_text(target))
    except ValueError:
        logger.warning("⚠️ Corrupt JSON detected at %s; treating as empty.", target)
        return None
    return data


class AutonomyService:
    """管理代理人自主預算與配置的核心服務.

    Attributes:
        config (dict[str, Any]): 自主權全域配置。
    """

    _config_health: AutonomyConfigHealth = AutonomyConfigHealth()

    def __init__(self) -> None:
        """初始化自主權服務並載入配置."""
        self.config: dict[str, Any] = {}
        # P5c：啟動掃描偵測到的「上次未完成背景任務」（供 /stats 顯示，記憶體內）
        self._interrupted_runs: list[dict[str, Any]] = []
        self._load_config()

    @property
    def config_health(self) -> AutonomyConfigHealth:
        """autonomy.json 讀取健康快照：讀不到時 degraded，自主執行停止。"""
        return self._config_health

    def _load_config(self, config_file: Path | None = None) -> None:
        """從物理檔案載入自主權配置並正規化為新制形狀 (SSOT).

        ADR-0146 遷移（決策 3：改鍵名＋遷移）：舊制 ``react_loop.default_max_slots``
        （nudge 輪數）／``stop_keywords``／``nudge_prompt`` 與 root 遺留鍵
        ``max_react_slots``／``max_repair_attempts`` 一律移除，改為
        ``react_loop.max_tool_rounds``（工具輪保險絲，0＝不限）。**刻意不搬舊
        數值**——舊 5 是「nudge 硬續輪數」，搬到工具輪會把互動多步能力砍到 5 輪。
        遷移結果僅在 bootstrap 啟用時回寫檔案（測試沙箱嚴禁自動寫系統檔）。

        Args:
            config_file: 測試注入用的設定檔路徑；預設走模組常數 CONFIG_FILE。
        """
        target = config_file or CONFIG_FILE
        try:
            data = _read_config_file(target)
        except OSError:
            # 檔案在卻讀不到：不知道檔案寫了什麼，不退回預設（不限輪數、總量上限關閉），
            # 每一項取最嚴的值。成本上限 0＝每一輪工具都算超額，自主執行停止。
            self.config = {
                "quota": {"daily_max": 0, "reset_hour": 0},
                "react_loop": {"max_tool_rounds": 1},
                "cap": {"enabled": True, "max_total_cost_usd": 0.0},
                "self_repair": {"enabled": False},
            }
            self._config_health = AutonomyConfigHealth(
                degraded=True, reason="autonomy_config_unreadable"
            )
            logger.warning(
                "AUDIT: autonomy config at %s exists but cannot be read; autonomous "
                "execution is paused (every tool round counts as over the cost cap, "
                "heartbeat pulses are skipped). To recover, restore read permission "
                "(compose: docker compose run --rm touka-init), then restart touka-proxy.",
                target,
                exc_info=True,
            )
            return

        self._config_health = AutonomyConfigHealth()
        if data is None:
            # 預設保底配置（新制形狀）
            self.config = {
                "quota": {"daily_max": 50, "reset_hour": 0},
                "react_loop": {"max_tool_rounds": 0},
                # 總量上限閘（opt-in，預設關閉）：累計成本超過上限即停自主行動。
                "cap": {"enabled": False, "max_total_cost_usd": None},
            }
            return

        migrated = self._normalize_config(data)
        self.config = data
        if migrated and config_file is None and is_bootstrap_enabled():
            try:
                resource_manager.write_json(CONFIG_FILE, self.config)
                logger.info("⚙️ [Autonomy] Legacy config migrated to max_tool_rounds shape.")
            except Exception:
                logger.exception("Failed to persist migrated autonomy config")

    @staticmethod
    def _normalize_config(data: dict[str, Any]) -> bool:
        """就地正規化設定為新制形狀；回傳是否有遷移動作。"""
        migrated = False
        react = data.get("react_loop")
        if not isinstance(react, dict):
            react = {}
            data["react_loop"] = react
        for dead in ("default_max_slots", "stop_keywords", "nudge_prompt"):
            if react.pop(dead, None) is not None:
                migrated = True
        if "max_tool_rounds" not in react:
            react["max_tool_rounds"] = 0
            migrated = True
        for dead_root in ("max_react_slots", "max_repair_attempts"):
            if data.pop(dead_root, None) is not None:
                migrated = True
        return migrated

    def _load_logs(self, identity: AgentIdentity) -> dict[str, Any]:
        """載入特定代理人的配額使用日誌 (Subdivided & Smart Loading)."""
        log_file = Path(resource_manager.get_stats_path(identity)) / "autonomy_log.json"
        
        data = resource_manager.read_json(log_file)
        if data:
            # 智慧相容：如果資料結構中包含 agent_id 作為 Key，則自動解包 (Legacy Pattern)
            if identity.agent_id in data:
                return data[identity.agent_id]
            return data
        return {}

    def _save_logs(self, identity: AgentIdentity, logs: dict[str, Any]) -> None:
        """持久化特定代理人的配額使用日誌 (Subdivided & Flattened)."""
        log_file = Path(resource_manager.get_stats_path(identity)) / "autonomy_log.json"
        
        try:
            # 僅儲存該代理人的資料字典，不再包裝一層 agent_id (Identity SSOT)
            resource_manager.write_json(log_file, logs)
        except Exception:
            logger.exception("Failed to save autonomy logs")

    def check_quota(self, identity: AgentIdentity) -> bool:
        """檢查特定代理人今日是否仍有剩餘配額 (Identity SSOT).

        Args:
            identity (AgentIdentity): 經驗證的身分對象.

        Returns:
            bool: 若有剩餘配額回傳 True。
        """
        with _autonomy_lock:  # P18 fix: serialize reads
            return self._check_quota_unlocked(identity)

    def _check_quota_unlocked(self, identity: AgentIdentity) -> bool:
        agent_logs = self._load_logs(identity)
        if not agent_logs:
            return True
            
        # 換日檢查
        now_iso = get_current_taipei_iso()
        last_used = agent_logs.get("last_used_time", "")
        
        if last_used and last_used[:10] != now_iso[:10]:
            # 已過零點，重置計數
            agent_logs["daily_count"] = 0
            
        daily_count = agent_logs.get("daily_count", 0)
        daily_max = self.config.get("quota", {}).get("daily_max", 50)

        return daily_count < daily_max

    def get_daily_quota(self, identity: AgentIdentity) -> dict[str, int]:
        """獲取代理人今日配額狀態（供 UI 顯示，Identity SSOT）。

        套用換日重置邏輯（與 _check_quota_unlocked 一致），回傳目前計數、上限與剩餘。

        Args:
            identity (AgentIdentity): 經驗證的身分對象。

        Returns:
            dict[str, int]: {"daily_count", "daily_max", "remaining"}。
        """
        with _autonomy_lock:
            agent_logs = self._load_logs(identity) or {}
            now_iso = get_current_taipei_iso()
            last_used = agent_logs.get("last_used_time", "")
            # 換日後計數視為 0
            if last_used and last_used[:10] != now_iso[:10]:
                daily_count = 0
            else:
                daily_count = agent_logs.get("daily_count", 0)
            daily_max = self.config.get("quota", {}).get("daily_max", 50)
            return {
                "daily_count": daily_count,
                "daily_max": daily_max,
                "remaining": max(0, daily_max - daily_count),
            }

    def get_total_cost_usd(self, identity: AgentIdentity) -> float:
        """讀取該身分累計成本（token_usage.json 各 provider total_cost_usd 加總）。

        Args:
            identity (AgentIdentity): 經驗證的身分對象。

        Returns:
            float: 累計成本（USD）；讀不到回 0.0。
        """
        try:
            token_file = Path(resource_manager.get_stats_path(identity)) / "token_usage.json"
            data = resource_manager.read_json(token_file)
            if not isinstance(data, dict):
                return 0.0
            return round(
                sum(
                    p.get("total_cost_usd", 0.0)
                    for p in data.values()
                    if isinstance(p, dict)
                ),
                6,
            )
        except Exception:
            return 0.0

    def _cap_limit(self) -> float | None:
        """總量上限（USD）；上限閘沒開或沒設上限回 None。"""
        cap = self.config.get("cap", {})
        if not cap.get("enabled", False):
            return None
        return cap.get("max_total_cost_usd")

    def is_over_budget(self, identity: AgentIdentity) -> bool:
        """總量上限閘（opt-in，預設關閉）：累計成本超過設定上限則回 True。

        Args:
            identity (AgentIdentity): 經驗證的身分對象。

        Returns:
            bool: 啟用且累計成本 >= max_total_cost_usd 回 True；否則 False。
        """
        limit = self._cap_limit()
        return limit is not None and self.get_total_cost_usd(identity) >= limit

    def would_exceed(self, identity: AgentIdentity, worst_usd: float) -> bool:
        """送前估算（r55）：已花＋這次呼叫的最壞花費超過上限就回 True（剛好等於不算）。

        上限閘沒開或沒設上限一律 False；``worst_usd`` 由 ``cost_guard.worst_case_call_usd`` 估。
        """
        limit = self._cap_limit()
        return limit is not None and self.get_total_cost_usd(identity) + worst_usd > limit

    def consume_quota(
        self,
        identity: AgentIdentity,
        request_id: str,
        rounds: int | None = None,
    ) -> None:
        """扣一次每日自主配額並記錄本次 run (Identity SSOT).

        ADR-0146（決策 2）：配額只計「無人 pulse」（heartbeat 每個 pulse 扣一次）
        ——舊制在每次互動靈魂對話扣配額（且 check_quota 從無執法點）已翻正。
        工具輪解析與追蹤移交 RhythmService（本服務只管主權與預算）。

        Args:
            identity: 經驗證的身分對象。
            request_id: 請求識別碼。
            rounds: 本次 run 解析後的工具輪上限（僅供日誌顯示）。
        """
        with _autonomy_lock:  # P18 fix: serialize reads and writes
            now_iso = get_current_taipei_iso()
            agent_logs = self._load_logs(identity)

            # 1. 換日重置邏輯
            last_used = agent_logs.get("last_used_time", "")
            if last_used and last_used[:10] != now_iso[:10]:
                agent_logs["daily_count"] = 0

            # 2. 累加次數並記錄本次 run
            agent_logs["daily_count"] = agent_logs.get("daily_count", 0) + 1
            agent_logs.update(
                {
                    "last_used_time": now_iso,
                    "active_request_id": request_id,
                }
            )
            if rounds is not None:
                agent_logs["active_slots"] = rounds
            self._save_logs(identity, agent_logs)

            logger.info(
                "📊 [Autonomy] Pulse quota consumed for %s. Daily: %s",
                identity.agent_id,
                agent_logs["daily_count"],
            )

    # --- [P5c 當機復原可見化：背景迴圈 in-flight 標記] ---

    def mark_heartbeat_in_flight(
        self, identity: AgentIdentity, info: dict[str, Any]
    ) -> None:
        """背景多脈衝迴圈起點：落盤 in-flight 標記 (Identity SSOT).

        當機/被強制關閉時，此標記會殘留在 autonomy_log.json，供下次啟動掃描偵測
        「上次有未跑完的背景任務」。正常結束時由 clear_heartbeat_in_flight 清除。
        以 _autonomy_lock 串行化、不覆蓋既有配額欄位。

        Args:
            identity (AgentIdentity): 經驗證的身分對象。
            info (dict[str, Any]): 標記內容（如 session_id/agent/total/started_at）。
        """
        with _autonomy_lock:
            agent_logs = self._load_logs(identity)
            agent_logs["heartbeat_in_flight"] = info
            self._save_logs(identity, agent_logs)

    def clear_heartbeat_in_flight(self, identity: AgentIdentity) -> None:
        """背景迴圈結束（finally）：清除 in-flight 標記 (Identity SSOT).

        只移除 heartbeat_in_flight 一個鍵，其餘欄位（daily_count 等）保留。

        Args:
            identity (AgentIdentity): 經驗證的身分對象。
        """
        with _autonomy_lock:
            agent_logs = self._load_logs(identity)
            if agent_logs.pop("heartbeat_in_flight", None) is not None:
                self._save_logs(identity, agent_logs)

    def scan_and_clear_interrupted(self) -> list[dict[str, Any]]:
        """啟動掃描：找出所有 agent 殘留的 in-flight 標記並清除 (Crash Recovery 可見化).

        殘留標記代表上次程序當機/被關時有背景心跳迴圈未跑完。本方法收集這些標記、
        清除 stale 標記（避免下次啟動重複報），並將結果存入服務供 /stats 顯示。
        範圍＝偵測＋顯示＋清 stale（非自動續跑）。

        Returns:
            list[dict[str, Any]]: 偵測到的未完成背景任務清單。
        """
        interrupted: list[dict[str, Any]] = []
        stats_root = Path(resource_manager.get_base_path()) / "stats"
        if not stats_root.exists():
            self._interrupted_runs = []
            return interrupted

        with _autonomy_lock:
            for agent_dir in stats_root.iterdir():
                if not agent_dir.is_dir():
                    continue
                log_file = agent_dir / "autonomy_log.json"
                data = resource_manager.read_json(log_file)
                if not isinstance(data, dict):
                    continue
                marker = data.get("heartbeat_in_flight")
                if not marker:
                    continue
                entry = dict(marker) if isinstance(marker, dict) else {"raw": marker}
                entry.setdefault("agent", agent_dir.name)
                interrupted.append(entry)
                # 清除 stale 標記（其餘欄位保留），失敗不影響其他 agent 的掃描
                data.pop("heartbeat_in_flight", None)
                try:
                    resource_manager.write_json(log_file, data)
                except Exception:
                    logger.exception(
                        "[Autonomy] 清除 stale 標記失敗：%s", log_file
                    )

        self._interrupted_runs = interrupted
        if interrupted:
            logger.warning(
                "⚠️ [Autonomy] 偵測到 %d 個上次未完成的背景任務（已清除 stale 標記）。",
                len(interrupted),
            )
        return interrupted

    def get_interrupted_runs(self) -> list[dict[str, Any]]:
        """回傳啟動掃描偵測到的『上次未完成背景任務』(供 /stats 顯示)。"""
        return list(self._interrupted_runs)

    def clear_interrupted_run(self, session_id: str) -> None:
        """從『上次未完成背景任務』清單移除指定會話 (P5c bonus：一鍵重新觸發後不再顯示)。

        Args:
            session_id (str): 要移除的會話識別碼。
        """
        self._interrupted_runs = [
            r for r in self._interrupted_runs if r.get("session_id") != session_id
        ]

    def reset_quota(self, identity: AgentIdentity) -> None:
        """手動強制重置特定代理人的每日配額 (Identity SSOT).

        Args:
            identity (AgentIdentity): 經驗證的身分對象.
        """
        with _autonomy_lock:  # P18: 與 consume/check 共用同一鎖，防止重置被並行寫覆蓋
            agent_logs = self._load_logs(identity)
            if agent_logs:
                agent_logs["daily_count"] = 0
                self._save_logs(identity, agent_logs)
                logger.info(f"🔄 [Autonomy] Quota reset for {identity.agent_id}")

    # --- [Self-Repair 邏輯] ---
    _repair_attempts: dict[str, int] = {}

    def get_repair_instruction(self, session_id: str, _exit_code: int) -> str | None:
        """獲取終端機自動修復指令 (SSOT).
        
        Args:
            session_id (str): 會話識別碼.
            _exit_code (int): 錯誤退出碼.
            
        Returns:
            str | None: 修復提示詞，若超出預算則回傳 None.
        """
        config = self.config.get("self_repair", {})
        if not config.get("enabled", True):
            return None
            
        attempts = self._repair_attempts.get(session_id, 0) + 1
        max_attempts = config.get("max_attempts", 3)
        
        if attempts > max_attempts:
            logger.warning(f"⚠️ [Autonomy] Repair budget exhausted for session {session_id}")
            return None
            
        self._repair_attempts[session_id] = attempts
        template = config.get("repair_prompt", "\n[REPAIR] 指令執行失敗 (退出碼: {exit_code})。這是第 {attempts}/{max_attempts} 次嘗試。請修正錯誤後重試。")
        return template.format(exit_code=_exit_code, attempts=attempts, max_attempts=max_attempts)

    def reset_repair_attempts(self, session_id: str) -> None:
        """重置特定會話的修復計數."""
        if session_id in self._repair_attempts:
            del self._repair_attempts[session_id]
            logger.debug(f"🔄 [Autonomy] Repair attempts reset for {session_id}")

    def get_repair_state(self, session_id: str) -> tuple[int, int]:
        """獲取會話目前的修復進度 (attempt, max_attempts)，供進度顯示 (SSOT).

        Args:
            session_id (str): 會話識別碼.

        Returns:
            tuple[int, int]: (目前已嘗試次數, 最大允許次數)。
        """
        config = self.config.get("self_repair", {})
        attempt = self._repair_attempts.get(session_id, 0)
        max_attempts = config.get("max_attempts", 3)
        return attempt, max_attempts


    def update_config(self, new_config: dict[str, Any]) -> bool:
        """動態更新自主權配置並持久化.

        Args:
            new_config (dict[str, Any]): 全新的配置字典。

        Returns:
            bool: 若成功儲存則回傳 True。
        """
        self.config = new_config
        try:
            resource_manager.write_json(CONFIG_FILE, self.config)
        except Exception:
            logger.exception("Failed to save updated config")
            return False
        else:
            self._config_health = AutonomyConfigHealth()
            logger.info("⚙️ [Autonomy] Configuration updated and saved.")
            return True

    def reload_all(self) -> None:
        """重置自主權服務的所有內部狀態與配置 (SSOT: 供測試環境使用)。"""
        self._load_config()
        self._repair_attempts.clear()
        self._interrupted_runs = []
        logger.debug("🔄 [Autonomy] All autonomy states reloaded.")


# 全系統單例
autonomy_service = AutonomyService()
